"""Deterministic, auditable tool planning for explanation-frontier evidence."""
from __future__ import annotations

import random
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from agent.episode_state import EvidenceStatus
from agent.explanation import (EvidenceNeed, EvidenceTargetKind,
                               ExplanationGraph, stable_id)
from agent.permissions import Role, allowed_tools
from agent.state_machine import READ_TOOLS, Phase

if TYPE_CHECKING:
    from agent.toolbox import Toolbox


@dataclass(frozen=True)
class ToolPlanningConfig:
    min_tools: int = 1
    max_tools: int = 3
    exploration_ratio: float = 0.10
    random_seed: int = 7319
    required_bonus: float = 2.0
    p0_bonus: float = 3.0
    coverage_weight: float = 0.6
    discriminator_weight: float = 1.0
    l2_cap: float = 0.75
    l4_cap: float = 0.75
    use_learned: bool = True
    use_l2: bool = True
    use_l4: bool = True


DEFAULT_TOOL_PLANNING = ToolPlanningConfig()


@dataclass(frozen=True)
class ToolAvailability:
    tool: str
    available: bool
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ToolScore:
    tool: str
    frontier_discrimination: float = 0.0
    unresolved_path_coverage: float = 0.0
    required_evidence_bonus: float = 0.0
    p0_obligation_bonus: float = 0.0
    l2_conditional_policy: float = 0.0
    l4_information_gain: float = 0.0
    latency_resource_cost: float = 0.0
    unknown_error_probability: float = 0.0
    repeated_evidence_penalty: float = 0.0
    total: float = 0.0
    sample_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlannedEvidenceTask:
    task_id: str
    explanation_id: str
    explanation_revision: int
    need_ids: list[str]
    path_ids: list[str]
    target_kind: str
    target_ids: list[str]
    evidence_types: list[str]
    selected_tools: list[str]
    score_components: dict[str, dict]
    local_subgraph: dict
    incident_window: dict = field(default_factory=dict)
    target_context: dict = field(default_factory=dict)
    learning_context: dict[str, dict] = field(default_factory=dict)
    unavailable_reason: str = ""

    @property
    def candidate_tools(self) -> list[str]:
        return self.selected_tools

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ToolPlan:
    explanation_id: str
    explanation_revision: int
    tasks: list[PlannedEvidenceTask] = field(default_factory=list)
    skipped_fresh_need_ids: list[str] = field(default_factory=list)
    unavailable_needs: dict[str, str] = field(default_factory=dict)
    availability: dict[str, ToolAvailability] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "explanation_id": self.explanation_id,
            "explanation_revision": self.explanation_revision,
            "tasks": [task.to_dict() for task in self.tasks],
            "skipped_fresh_need_ids": list(self.skipped_fresh_need_ids),
            "unavailable_needs": dict(self.unavailable_needs),
            "availability": {key: value.to_dict() for key, value in
                             self.availability.items()},
        }


_EXTENSIONS = {
    "simulate_index": "hypopg",
    "get_physical_bloat": "pgstattuple",
}
_TARGET_REQUIREMENTS = {
    "explain_query": ("hot_query",),
    "get_indexes": ("table",),
    "get_table_stats": ("table",),
    "get_physical_bloat": ("table",),
    "simulate_index": ("hot_query", "table"),
    "fetch_raw": ("raw_ref",),
}
_COST = {
    "explain_query": 0.9,
    "simulate_index": 1.0,
    "get_physical_bloat": 0.8,
    "get_top_queries": 0.35,
    "get_database_stats": 0.3,
    "get_vacuum_horizon": 0.3,
    "get_active_sessions": 0.2,
    "get_blocking_chain": 0.2,
    "get_connection_stats": 0.15,
    "get_table_stats": 0.2,
    "get_indexes": 0.1,
}


def infer_target_context(hot_query: str = "", **values: Any) -> dict:
    """Build explicit target context using the SQL AST when possible."""
    context = {key: value for key, value in values.items()
               if value not in (None, "", [], {})}
    if hot_query:
        context["hot_query"] = hot_query
        try:
            from pglast import parse_sql
            from pglast.visitors import Visitor

            class _Tables(Visitor):
                def __init__(self):
                    super().__init__()
                    self.names: list[str] = []

                def visit_RangeVar(self, _ancestors, node):
                    if node.relname and node.relname not in self.names:
                        self.names.append(node.relname)

            visitor = _Tables()
            parseable = re.sub(
                r"%\([A-Za-z_][A-Za-z0-9_]*\)s", "NULL", hot_query)
            visitor(parse_sql(parseable))
            if visitor.names:
                context.setdefault("tables", visitor.names)
                context.setdefault("table", visitor.names[0])
                context.setdefault("target_resolution", "ast_first_relation")
        except Exception:
            # Invalid or redacted SQL leaves the target deliberately unknown.
            pass
    return context


def environment_availability(
        toolbox: "Toolbox", *, phase: Phase = Phase.INVESTIGATE,
        role: Role = Role.INVESTIGATOR,
        target_context: dict | None = None) -> dict[str, ToolAvailability]:
    """Probe non-mutating runtime capabilities for every investigator tool."""
    context = target_context or {}
    observer = getattr(toolbox, "o", None)
    out: dict[str, ToolAvailability] = {}
    for tool in sorted(set(READ_TOOLS) | {"report_evidence"}):
        checks: dict[str, bool] = {}
        reasons: list[str] = []
        local = tool == "report_evidence"
        checks["method_exists"] = local or (
            hasattr(toolbox, tool) and observer is not None and
            hasattr(observer, tool))
        checks["readonly_role"] = local or (
            role is Role.INVESTIGATOR and tool in READ_TOOLS)
        checks["phase"] = phase is Phase.INVESTIGATE
        required_targets = _TARGET_REQUIREMENTS.get(tool, ())
        missing = [name for name in required_targets if not context.get(name)]
        checks["target_resolved"] = not missing
        if missing:
            reasons.append("missing target: " + ", ".join(missing))

        extension = _EXTENSIONS.get(tool)
        extension_ok = True
        if extension:
            probe = getattr(observer, "extension_available", None)
            if not callable(probe):
                extension_ok = False
                reasons.append(f"cannot verify extension {extension}")
            else:
                try:
                    extension_ok = bool(probe(extension))
                except Exception as exc:
                    extension_ok = False
                    reasons.append(
                        f"extension probe failed: {type(exc).__name__}")
                if not extension_ok and not reasons:
                    reasons.append(f"extension {extension} is not installed")
        checks["extension_available"] = extension_ok
        for name, ok in checks.items():
            if not ok and name not in {"target_resolved", "extension_available"}:
                reasons.append(name.replace("_", " ") + " check failed")
        out[tool] = ToolAvailability(
            tool=tool, available=all(checks.values()), checks=checks,
            reasons=list(dict.fromkeys(reasons)))
    return out


def _need_has_fresh_evidence(explanation: ExplanationGraph,
                             need: EvidenceNeed, now: float | None) -> bool:
    targets = set(need.target_ids)
    for binding in explanation.evidence_bindings.values():
        binding_targets = set(binding.target_node_ids + binding.target_edge_ids)
        if (binding.evidence_type == need.evidence_type and
                binding.predicate_id == need.predicate_id and
                targets.intersection(binding_targets) and
                binding.status == EvidenceStatus.OBSERVED.value and
                binding.is_trusted(now=now)):
            return True
    return False


def _roots(explanation: ExplanationGraph, need: EvidenceNeed) -> list[str]:
    paths = explanation.path_map()
    return list(dict.fromkeys(
        paths[path_id].root_node_id for path_id in need.path_ids
        if path_id in paths))


def _learning_context(explanation: ExplanationGraph, need: EvidenceNeed,
                      environment_tools: set[str], toolbox: "Toolbox") -> dict:
    try:
        from knowledge.evolution import v2_context_signatures
        incident = getattr(getattr(toolbox, "st", None),
                           "incident_window", {}) or {}
        return v2_context_signatures(
            explanation, need, environment_tools,
            scenario_revision=int(incident.get("scenario_revision", 1)))
    except Exception:
        return {
            "frontier_signature": stable_id("frontier", need.path_ids),
            "evidence_state_signature": stable_id(
                "evidence_state", {"revision": explanation.revision}),
            "p0_signature": stable_id("p0_state", {}),
            "capability_signature": stable_id(
                "capabilities", sorted(environment_tools)),
            "evidence_need_signature": stable_id(
                "evidence_need", need.to_dict()),
            "graph_version": explanation.graph_version,
            "scenario_revision": 1,
            "tool_schema_version": 2,
        }


def _learned_components(context: dict, tool: str,
                        config: ToolPlanningConfig
                        ) -> tuple[float, float, int]:
    if not config.use_learned:
        return 0.0, 0.0, 0
    try:
        from knowledge.evolution import v2_tool_learning_components
        l2, l4, samples = v2_tool_learning_components(
            context, tool, l2_cap=config.l2_cap, l4_cap=config.l4_cap,
            use_learned=True)
        return (l2 if config.use_l2 else 0.0,
                l4 if config.use_l4 else 0.0,
                samples)
    except Exception:
        return 0.0, 0.0, 0


def _score_tool(explanation: ExplanationGraph, need: EvidenceNeed, tool: str,
                toolbox: "Toolbox", frontier: list[dict],
                config: ToolPlanningConfig,
                learning_context: dict) -> ToolScore:
    target_keys = {(item["target_kind"], item["target_id"]): item
                   for item in frontier}
    frontier_score = max([
        float(target_keys[(need.target_kind, target_id)].get(
            "discrimination_score", 0.0))
        for target_id in need.target_ids
        if (need.target_kind, target_id) in target_keys
    ] or [0.0])
    unresolved_paths = len(set(need.path_ids))
    l2, l4, samples = _learned_components(
        learning_context, tool, config)

    history = list(getattr(getattr(toolbox, "st", None), "scratchpad", []))
    relevant = [
        entry for entry in history
        if entry.get("evidence_type") == need.evidence_type and
        (not entry.get("collection_tool") or
         entry.get("collection_tool") == tool)
    ]
    failed = [entry for entry in relevant
              if entry.get("status") in {EvidenceStatus.UNKNOWN.value,
                                         EvidenceStatus.ERROR.value}]
    failure_probability = len(failed) / len(relevant) if relevant else 0.0
    repeated = min(1.0, len(relevant) * 0.2)
    required_bonus = config.required_bonus if need.required else 0.0
    p0_bonus = (config.p0_bonus
                if need.target_kind == EvidenceTargetKind.P0.value else 0.0)
    coverage = unresolved_paths * config.coverage_weight
    discrimination = frontier_score * config.discriminator_weight
    cost = _COST.get(tool, 0.25)
    total = (discrimination + coverage + required_bonus + p0_bonus + l2 + l4 -
             cost - failure_probability - repeated)
    return ToolScore(
        tool=tool,
        frontier_discrimination=round(discrimination, 6),
        unresolved_path_coverage=round(coverage, 6),
        required_evidence_bonus=required_bonus,
        p0_obligation_bonus=p0_bonus,
        l2_conditional_policy=round(l2, 6),
        l4_information_gain=round(l4, 6),
        latency_resource_cost=cost,
        unknown_error_probability=round(failure_probability, 6),
        repeated_evidence_penalty=round(repeated, 6),
        total=round(total, 6),
        sample_count=samples,
    )


def _local_subgraph(explanation: ExplanationGraph,
                    needs: list[EvidenceNeed]) -> dict:
    path_ids = list(dict.fromkeys(path_id for need in needs
                                  for path_id in need.path_ids))
    paths = explanation.path_map()
    selected = [paths[path_id] for path_id in path_ids if path_id in paths]
    return {
        "paths": [{
            "path_id": path.path_id,
            "status": path.status,
            "node_ids": list(path.node_ids),
            "edge_ids": list(path.edge_ids),
            "node_roles": dict(path.node_roles),
            "node_status": {node_id: explanation.node_status.get(
                node_id, "UNTESTED") for node_id in path.node_ids},
            "edge_status": {edge_id: explanation.edge_status.get(
                edge_id, "UNTESTED") for edge_id in path.edge_ids},
        } for path in selected],
        "target_ids": list(dict.fromkeys(target_id for need in needs
                                          for target_id in need.target_ids)),
    }


def plan_evidence_tasks(
        explanation: ExplanationGraph, needs: list[EvidenceNeed],
        toolbox: "Toolbox", *, target_context: dict | None = None,
        incident_window: dict | None = None,
        config: ToolPlanningConfig = DEFAULT_TOOL_PLANNING,
        now: float | None = None) -> ToolPlan:
    """Choose legal tools and merge all needs served by the same call."""
    if not 0.0 <= config.exploration_ratio <= 1.0:
        raise ValueError("exploration_ratio must be between zero and one")
    if config.min_tools < 1 or config.max_tools < config.min_tools:
        raise ValueError("invalid per-task tool bounds")

    availability = environment_availability(
        toolbox, target_context=target_context)
    environment_tools = {tool for tool, item in availability.items()
                         if item.available}
    plan = ToolPlan(
        explanation_id=explanation.explanation_id,
        explanation_revision=explanation.revision,
        availability=availability,
    )
    frontier = __import__(
        "knowledge.causal_graph.graph", fromlist=["path_frontier"]
    ).path_frontier(explanation)
    rng = random.Random(f"{config.random_seed}:{explanation.explanation_id}:"
                        f"{explanation.revision}")

    chosen_by_tool: dict[str, list[EvidenceNeed]] = {}
    score_cache: dict[tuple[str, str], ToolScore] = {}
    learning_contexts: dict[str, dict] = {}
    for need in needs:
        if _need_has_fresh_evidence(explanation, need, now):
            plan.skipped_fresh_need_ids.append(need.need_id)
            continue
        effective = allowed_tools(
            Phase.INVESTIGATE, Role.INVESTIGATOR,
            evidence_need=need, environment_tools=environment_tools)
        legal = sorted((effective - {"report_evidence"}) &
                       set(need.candidate_tools))
        if not legal:
            reasons = [reason for tool in need.candidate_tools
                       for reason in availability.get(
                           tool, ToolAvailability(tool, False)).reasons]
            plan.unavailable_needs[need.need_id] = (
                "; ".join(dict.fromkeys(reasons)) or
                "no tool survives phase/role/need/environment intersection")
            continue
        learning_contexts[need.need_id] = _learning_context(
            explanation, need, environment_tools, toolbox)
        scores = []
        for tool in legal:
            score = _score_tool(explanation, need, tool, toolbox,
                                frontier, config,
                                learning_contexts[need.need_id])
            score_cache[(need.need_id, tool)] = score
            scores.append(score)
        scores.sort(key=lambda item: (-item.total, item.tool))
        selected = scores[0]
        # Exploration never displaces a required or P0 tool.  For an ordinary
        # need it may choose one legal low-sample alternative, deterministically
        # under the configured seed.
        if (not need.required and
                need.target_kind != EvidenceTargetKind.P0.value and
                len(scores) > 1 and rng.random() < config.exploration_ratio):
            selected = min(scores[1:], key=lambda item: (
                item.sample_count, -item.total, item.tool))
        chosen_by_tool.setdefault(selected.tool, []).append(need)

    for tool, covered in sorted(chosen_by_tool.items()):
        # One task owns this tool, so it is called once even when it covers
        # several needs.  Separate EvidenceReports/bindings retain each target.
        need_ids = sorted(need.need_id for need in covered)
        task_id = stable_id("evidence_task", {
            "explanation_id": explanation.explanation_id,
            "explanation_revision": explanation.revision,
            "need_ids": need_ids,
            "tools": [tool],
        })
        kinds = sorted(set(need.target_kind for need in covered))
        task = PlannedEvidenceTask(
            task_id=task_id,
            explanation_id=explanation.explanation_id,
            explanation_revision=explanation.revision,
            need_ids=need_ids,
            path_ids=sorted(set(path_id for need in covered
                                for path_id in need.path_ids)),
            target_kind=kinds[0] if len(kinds) == 1 else "BRANCH",
            target_ids=sorted(set(target_id for need in covered
                                  for target_id in need.target_ids)),
            evidence_types=sorted(set(need.evidence_type
                                      for need in covered)),
            selected_tools=[tool],
            score_components={
                need.need_id: score_cache[(need.need_id, tool)].to_dict()
                for need in covered
            },
            local_subgraph=_local_subgraph(explanation, covered),
            incident_window=dict(incident_window or {}),
            target_context=dict(target_context or {}),
            learning_context={
                need.need_id: dict(learning_contexts[need.need_id])
                for need in covered
            },
        )
        if not (config.min_tools <= len(task.selected_tools) <=
                config.max_tools):
            raise AssertionError("planned task violates per-agent tool bounds")
        plan.tasks.append(task)
    return plan


def task_environment_tools(task: PlannedEvidenceTask) -> set[str]:
    return set(task.selected_tools) | {"report_evidence"}
