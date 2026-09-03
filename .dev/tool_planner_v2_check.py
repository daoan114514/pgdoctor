"""Offline acceptance checks for v2 dynamic tool planning and subagents."""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState
from agent.explanation import (CausalStatus, EvidenceBinding, EvidenceNeed,
                               EvidenceReport, PredicateResult)
from agent.hooks import make_phase_hook
from agent.investigator import EvidenceTaskResult, task_tool_names
from agent.orchestrator import merge_evidence_task_results
from agent.permissions import Role, allowed_tools
from agent.state_machine import Phase, PhaseViolation, StateMachine
from agent.toolbox import Toolbox
from agent.tool_planner import (PlannedEvidenceTask, ToolPlanningConfig,
                                environment_availability,
                                plan_evidence_tasks,
                                task_environment_tools)
from knowledge.causal_graph import graph as G
from sandbox.traces import TRACE_DIR, TraceStore


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<58} {detail}")


class FakeObserver:
    def __init__(self, extensions: dict[str, bool] | None = None):
        self.extensions = extensions or {"hypopg": True, "pgstattuple": True}
        self.trace = None

    def extension_available(self, name: str) -> bool:
        return self.extensions.get(name, False)

    def __getattr__(self, name: str):
        if name.startswith("get_") or name in {
                "explain_query", "simulate_index", "fetch_raw"}:
            return lambda *args, **kwargs: None
        raise AttributeError(name)


class BareObserver:
    trace = None

    def extension_available(self, _name: str) -> bool:
        return False


episode_id = f"tool_planner_v2_{uuid.uuid4().hex}"
trace_dir = TRACE_DIR / episode_id
try:
    st = EpisodeState(episode_id, "fixture", phase=Phase.INVESTIGATE.value)
    explanation = G.recall_explanation(
        ["disk_growing"], episode_id=episode_id, use_learned=False)
    st.explanation_graph = explanation
    sm = StateMachine(st)
    tb = Toolbox(FakeObserver(), st, sm)
    needs = G.evidence_needs(explanation)
    context = {
        "hot_query": "SELECT * FROM orders WHERE user_id = 4242",
        "table": "orders",
    }

    print("[1] Effective permissions are the exact four-way intersection")
    fixture_need = EvidenceNeed.create(
        path_ids=[explanation.candidate_paths[0].path_id],
        target_kind="BRANCH", target_ids=["fixture_edge"],
        evidence_type="lock_blocking_chain",
        predicate_id="lock_blocking_chain_v2", required=True,
        freshness_seconds=60,
        candidate_tools=["get_blocking_chain", "explain_query"])
    environment = {"get_blocking_chain", "report_evidence", "report_verdict"}
    effective = allowed_tools(
        Phase.INVESTIGATE, Role.INVESTIGATOR,
        evidence_need=fixture_need, environment_tools=environment)
    check("phase ∩ role ∩ need ∩ environment is exact",
          effective == {"get_blocking_chain", "report_evidence"}, effective)
    empty_need = EvidenceNeed.create(
        path_ids=[explanation.candidate_paths[0].path_id],
        target_kind="NODE", target_ids=["missing_target"],
        evidence_type="fixture", predicate_id="fixture_v2", required=True,
        freshness_seconds=60, candidate_tools=[])
    empty = allowed_tools(
        Phase.INVESTIGATE, Role.INVESTIGATOR,
        evidence_need=empty_need, environment_tools=environment)
    check("empty v2 graph derivation never expands permissions",
          empty == {"report_evidence"}, empty)
    check("v2 investigator has no verdict/root/proposal tools",
          not effective.intersection({"report_verdict", "set_hypothesis",
                                      "declare_root_cause", "submit_proposal"}))

    print("\n[2] Environment checks cover methods, extensions, phase, and targets")
    available = environment_availability(
        tb, target_context=context)
    check("a present read-only method with a concrete target is available",
          available["get_table_stats"].available)
    missing_target = environment_availability(tb, target_context={})
    check("table tools require an explicit target object",
          not missing_target["get_table_stats"].available and
          "missing target" in " ".join(
              missing_target["get_table_stats"].reasons))
    no_extensions = Toolbox(
        FakeObserver({"hypopg": False, "pgstattuple": False}), st, sm)
    extension_availability = environment_availability(
        no_extensions, target_context=context)
    check("missing extensions remove dependent tools",
          not extension_availability["simulate_index"].available and
          not extension_availability["get_physical_bloat"].available)
    missing_method = Toolbox(BareObserver(), st, sm)
    method_availability = environment_availability(
        missing_method, target_context=context)
    check("missing observer methods remove advertised tools",
          not method_availability["get_table_stats"].available)
    wrong_phase = environment_availability(
        tb, phase=Phase.PLAN, target_context=context)
    check("the current phase is part of environment availability",
          not wrong_phase["get_table_stats"].available)

    print("\n[3] Hook, schema exposure, and Toolbox runtime share authority")
    task = PlannedEvidenceTask(
        task_id="task_permission_fixture",
        explanation_id=explanation.explanation_id,
        explanation_revision=explanation.revision,
        need_ids=[fixture_need.need_id], path_ids=fixture_need.path_ids,
        target_kind="BRANCH", target_ids=fixture_need.target_ids,
        evidence_types=[fixture_need.evidence_type],
        selected_tools=["get_blocking_chain"], score_components={},
        local_subgraph={}, target_context=context)
    schema_names = task_tool_names(task)
    check("schema exposure contains only one evidence tool plus report channel",
          schema_names == {"get_blocking_chain", "report_evidence"},
          schema_names)
    hook = make_phase_hook(
        Phase.INVESTIGATE, role=Role.INVESTIGATOR,
        task_context=task, environment_tools=task_environment_tools(task))
    guard = hook["PreToolUse"][0].hooks[0]
    hook_allows = asyncio.run(guard(
        {"tool_name": "mcp__pgdoctor__get_blocking_chain"}, "ok", None)) == {}
    hook_denies = asyncio.run(guard(
        {"tool_name": "mcp__pgdoctor__explain_query"}, "no", None)) != {}
    check("hook allows exactly the schema collection tool",
          hook_allows and hook_denies)
    scoped = tb.scoped(
        role=Role.INVESTIGATOR, task_context=task,
        environment_tools=task_environment_tools(task))
    scoped._enter("get_blocking_chain")
    runtime_denied = False
    try:
        scoped._enter("explain_query")
    except PhaseViolation:
        runtime_denied = True
    check("Toolbox runtime enforces the identical set", runtime_denied)
    target_task = PlannedEvidenceTask(
        task_id="task_target_fixture",
        explanation_id=explanation.explanation_id,
        explanation_revision=explanation.revision,
        need_ids=[fixture_need.need_id], path_ids=fixture_need.path_ids,
        target_kind="NODE", target_ids=["fixture"],
        evidence_types=["fixture"], selected_tools=["get_table_stats"],
        score_components={}, local_subgraph={}, target_context=context)
    target_scoped = tb.scoped(
        role=Role.INVESTIGATOR, task_context=target_task,
        environment_tools=task_environment_tools(target_task))
    wrong_target_denied = False
    try:
        target_scoped._enter("get_table_stats", {"table": "users"})
    except PhaseViolation:
        wrong_target_denied = True
    check("runtime also binds object-taking tools to the planned target",
          wrong_target_denied)

    print("\n[4] Scoring keeps mandatory work and merges shared tool calls")
    plan = plan_evidence_tasks(
        explanation, needs, tb, target_context=context,
        incident_window={"window_start": 1, "window_end": 2})
    planned_need_ids = {need_id for item in plan.tasks
                        for need_id in item.need_ids}
    mandatory_available = {
        need.need_id for need in needs
        if (need.required or need.target_kind == "P0") and
        need.need_id not in plan.unavailable_needs
    }
    check("all available required and P0 needs survive ranking",
          mandatory_available <= planned_need_ids,
          mandatory_available - planned_need_ids)
    check("every subagent receives between one and three tools",
          all(1 <= len(item.selected_tools) <= 3 for item in plan.tasks))
    vacuum_tasks = [item for item in plan.tasks
                    if item.selected_tools == ["get_vacuum_horizon"]]
    check("one tool serving multiple needs is planned as one call",
          len(vacuum_tasks) == 1 and len(vacuum_tasks[0].need_ids) > 1,
          [(item.selected_tools, len(item.need_ids)) for item in plan.tasks])
    required_score_keys = {
        "frontier_discrimination", "unresolved_path_coverage",
        "required_evidence_bonus", "p0_obligation_bonus",
        "l2_conditional_policy", "l4_information_gain",
        "latency_resource_cost", "unknown_error_probability",
        "repeated_evidence_penalty", "total",
    }
    check("tool scores retain every auditable component",
          all(required_score_keys <= set(score)
              for item in plan.tasks
              for score in item.score_components.values()))

    optional = EvidenceNeed.create(
        path_ids=[explanation.candidate_paths[0].path_id],
        target_kind="NODE", target_ids=["disk_pressure"],
        evidence_type="exploration_fixture", predicate_id="fixture_v2",
        required=False, freshness_seconds=30,
        candidate_tools=["get_database_stats", "get_vacuum_horizon"])
    explore_cfg = ToolPlanningConfig(exploration_ratio=1.0, random_seed=99)
    first = plan_evidence_tasks(
        explanation, [optional], tb, target_context=context,
        config=explore_cfg)
    second = plan_evidence_tasks(
        explanation, [optional], tb, target_context=context,
        config=explore_cfg)
    check("fixed-seed legal exploration is replayable",
          [item.selected_tools for item in first.tasks] ==
          [item.selected_tools for item in second.tasks])
    exploit_cfg = ToolPlanningConfig(exploration_ratio=0.0, random_seed=99)
    before_failure = plan_evidence_tasks(
        explanation, [optional], tb, target_context=context,
        config=exploit_cfg)
    failed_tool = before_failure.tasks[0].selected_tools[0]
    st.note(
        "fixture", optional.evidence_type, "collection failed",
        status="ERROR", collection_tool=failed_tool)
    after_failure = plan_evidence_tasks(
        explanation, [optional], tb, target_context=context,
        config=exploit_cfg)
    check("tool-specific UNKNOWN/ERROR history changes the next choice",
          after_failure.tasks[0].selected_tools[0] != failed_tool,
          (failed_tool, after_failure.tasks[0].selected_tools[0]))

    print("\n[5] Fresh evidence is reused and expired evidence is replanned")
    disk_need = next(need for need in needs
                     if need.target_kind == "P0" and
                     need.evidence_type == "disk_usage")
    store = TraceStore(episode_id)
    value = {"used_pct": 91.0}
    fresh_ref = store.record(
        "fixture", {"need_id": disk_need.need_id}, json.dumps(value), value)
    binding = EvidenceBinding.create(
        episode_id=episode_id, raw_ref=fresh_ref,
        evidence_type=disk_need.evidence_type,
        status="OBSERVED", observed_at=time.time(),
        predicate_id=disk_need.predicate_id,
        predicate_result=PredicateResult.SUPPORTS,
        structured_value=value, target_node_ids=disk_need.target_ids,
        fresh_until=time.time() + 600)
    explanation.add_evidence_binding(binding)
    fresh_plan = plan_evidence_tasks(
        explanation, [disk_need], tb, target_context=context)
    check("fresh trusted evidence suppresses repeat collection",
          disk_need.need_id in fresh_plan.skipped_fresh_need_ids and
          not fresh_plan.tasks)
    binding.fresh_until = 0.0
    expired_plan = plan_evidence_tasks(
        explanation, [disk_need], tb, target_context=context)
    check("expired evidence creates a new task", bool(expired_plan.tasks))

    print("\n[6] Report and binding merge is idempotent and revision-safe")
    late_task = expired_plan.tasks[0]
    late_value = {"used_pct": 92.0}
    late_ref = store.record(
        "fixture", {"task": late_task.task_id},
        json.dumps(late_value), late_value)
    st.note(
        "investigator", disk_need.evidence_type, "fixture observation",
        raw_ref=late_ref, bears_on=disk_need.target_ids,
        structured_value=late_value, predicate_id=disk_need.predicate_id,
        target_kind=disk_need.target_kind, target_ids=disk_need.target_ids,
        explanation_id=late_task.explanation_id,
        explanation_revision=late_task.explanation_revision,
        evidence_task_id=late_task.task_id,
        evidence_need_ids=late_task.need_ids)
    late_report = EvidenceReport(
        need_id=disk_need.need_id, tool=late_task.selected_tools[0],
        raw_refs=[late_ref], observations=[late_value],
        collection_status="OBSERVED")
    late_result = EvidenceTaskResult(
        need_id=disk_need.need_id, task_id=late_task.task_id,
        need_ids=late_task.need_ids, reports=[late_report],
        explanation_id=late_task.explanation_id,
        explanation_revision=late_task.explanation_revision)
    before_bindings = len(explanation.evidence_bindings)
    explanation.set_node_status("disk_pressure", CausalStatus.INCONCLUSIVE)
    late_merge = merge_evidence_task_results(st, expired_plan, [late_result])
    check("late reports are persisted but cannot overwrite causal state",
          bool(late_merge.late_report_ids) and
          len(explanation.evidence_bindings) == before_bindings)

    current_plan = plan_evidence_tasks(
        explanation, [disk_need], tb, target_context=context)
    current_task = current_plan.tasks[0]
    current_value = {"used_pct": 93.0}
    current_ref = store.record(
        "fixture", {"task": current_task.task_id},
        json.dumps(current_value), current_value)
    st.note(
        "investigator", disk_need.evidence_type, "fixture observation",
        raw_ref=current_ref, bears_on=disk_need.target_ids,
        structured_value=current_value, predicate_id=disk_need.predicate_id,
        target_kind=disk_need.target_kind, target_ids=disk_need.target_ids,
        explanation_id=current_task.explanation_id,
        explanation_revision=current_task.explanation_revision,
        evidence_task_id=current_task.task_id,
        evidence_need_ids=current_task.need_ids)
    current_report = EvidenceReport(
        need_id=disk_need.need_id, tool=current_task.selected_tools[0],
        raw_refs=[current_ref], observations=[current_value],
        collection_status="OBSERVED")
    current_result = EvidenceTaskResult(
        need_id=disk_need.need_id, task_id=current_task.task_id,
        need_ids=current_task.need_ids, reports=[current_report],
        explanation_id=current_task.explanation_id,
        explanation_revision=current_task.explanation_revision)
    merged = merge_evidence_task_results(st, current_plan, [current_result])
    repeated = merge_evidence_task_results(st, current_plan, [current_result])
    check("on-time report produces deterministic EvidenceBinding",
          bool(merged.binding_ids))
    check("need/report and binding merges are idempotent",
          bool(repeated.duplicate_report_ids) and not repeated.binding_ids)

    print("\n[7] Different frontier fragments may receive different tools")
    paths_by_root = {}
    for path in explanation.candidate_paths:
        paths_by_root.setdefault(path.root_node_id, []).append(path)
    two_paths = next(group[:2] for group in paths_by_root.values()
                     if len(group) >= 2)
    branch_a = EvidenceNeed.create(
        path_ids=[two_paths[0].path_id], target_kind="EDGE",
        target_ids=[two_paths[0].edge_ids[0]], evidence_type="branch_a",
        predicate_id="fixture_a", required=False, freshness_seconds=30,
        candidate_tools=["get_table_stats"])
    branch_b = EvidenceNeed.create(
        path_ids=[two_paths[1].path_id], target_kind="EDGE",
        target_ids=[two_paths[1].edge_ids[0]], evidence_type="branch_b",
        predicate_id="fixture_b", required=False, freshness_seconds=30,
        candidate_tools=["get_vacuum_horizon"])
    branch_plan = plan_evidence_tasks(
        explanation, [branch_a, branch_b], tb, target_context=context)
    tools_by_need = {
        need_id: item.selected_tools[0] for item in branch_plan.tasks
        for need_id in item.need_ids
    }
    check("path-local frontier context changes the assigned tool",
          tools_by_need.get(branch_a.need_id) == "get_table_stats" and
          tools_by_need.get(branch_b.need_id) == "get_vacuum_horizon" and
          two_paths[0].root_node_id == two_paths[1].root_node_id)

finally:
    shutil.rmtree(trace_dir, ignore_errors=True)

print("\n" + "=" * 76)
print("TOOL PLANNER V2:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
