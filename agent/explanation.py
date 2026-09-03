"""Serializable contracts for episode-level causal explanations.

The static graph keeps its existing node kinds.  The types in this module
describe path-local roles and episode-local evidence state only.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
TRACE_ROOT = ROOT / "traces"
_RAW_REF = re.compile(r"^trace://([^/]+)/step_(\d+)$")
_EVIDENCE_STATUSES = frozenset({"OBSERVED", "UNKNOWN", "ERROR"})


class CausalStatus(str, Enum):
    UNTESTED = "UNTESTED"
    SUPPORTED = "SUPPORTED"
    REFUTED = "REFUTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class DynamicRole(str, Enum):
    OBSERVED_SYMPTOM = "OBSERVED_SYMPTOM"
    MECHANISM = "MECHANISM"
    ROOT_CAUSE = "ROOT_CAUSE"


class ObligationStatus(str, Enum):
    OPEN = "OPEN"
    SUPPORTED = "SUPPORTED"
    REFUTED = "REFUTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    UNAVAILABLE = "UNAVAILABLE"


class InterventionKind(str, Enum):
    CORRECTIVE = "CORRECTIVE"
    MITIGATION = "MITIGATION"
    CONTAINMENT = "CONTAINMENT"
    MANUAL = "MANUAL"


class ExplanationScope(str, Enum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"


class PredicateResult(str, Enum):
    SUPPORTS = "SUPPORTS"
    REFUTES = "REFUTES"
    NEUTRAL = "NEUTRAL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvidenceTargetKind(str, Enum):
    NODE = "NODE"
    EDGE = "EDGE"
    BRANCH = "BRANCH"
    P0 = "P0"
    INTERVENTION = "INTERVENTION"


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def json_ready(value: Any) -> Any:
    """Convert nested contracts to JSON data without losing enum values."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {f.name: json_ready(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(v) for v in value]
    if isinstance(value, set):
        return [json_ready(v) for v in sorted(value, key=str)]
    if isinstance(value, Path):
        return str(value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(json_ready(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def value_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _unique(values: list[str] | tuple[str, ...] | None,
            *, sort: bool = False) -> list[str]:
    result = list(dict.fromkeys(str(v) for v in (values or [])))
    return sorted(result) if sort else result


@dataclass
class EvidenceBinding:
    binding_id: str = ""
    episode_id: str = ""
    raw_ref: str = ""
    evidence_type: str = ""
    status: str = "OBSERVED"
    observed_at: float = 0.0
    window_start: float | None = None
    window_end: float | None = None
    source_epoch: str = ""
    target_node_ids: list[str] = field(default_factory=list)
    target_edge_ids: list[str] = field(default_factory=list)
    predicate_id: str = ""
    predicate_result: str = PredicateResult.NOT_APPLICABLE.value
    summary: str = ""
    value_digest: str = ""
    fresh_until: float | None = None

    def __post_init__(self) -> None:
        self.status = str(_enum_value(self.status))
        self.predicate_result = str(_enum_value(self.predicate_result))
        if self.status not in _EVIDENCE_STATUSES:
            raise ValueError(f"invalid evidence status: {self.status}")
        if self.predicate_result not in {r.value for r in PredicateResult}:
            raise ValueError(f"invalid predicate result: {self.predicate_result}")
        if (self.status != "OBSERVED" and
                self.predicate_result in {PredicateResult.SUPPORTS.value,
                                          PredicateResult.REFUTES.value}):
            raise ValueError("UNKNOWN/ERROR evidence cannot support or refute")
        self.target_node_ids = _unique(self.target_node_ids, sort=True)
        self.target_edge_ids = _unique(self.target_edge_ids, sort=True)
        expected = self.expected_binding_id()
        if self.binding_id and self.binding_id != expected:
            raise ValueError("binding_id does not match raw_ref/predicate/target")
        self.binding_id = expected

    @classmethod
    def create(cls, *, episode_id: str, raw_ref: str, evidence_type: str,
               status: Any, observed_at: float, predicate_id: str,
               predicate_result: Any, structured_value: Any,
               target_node_ids: list[str] | None = None,
               target_edge_ids: list[str] | None = None,
               summary: str = "", window_start: float | None = None,
               window_end: float | None = None, source_epoch: str = "",
               fresh_until: float | None = None) -> "EvidenceBinding":
        return cls(
            episode_id=episode_id,
            raw_ref=raw_ref,
            evidence_type=evidence_type,
            status=_enum_value(status),
            observed_at=observed_at,
            window_start=window_start,
            window_end=window_end,
            source_epoch=source_epoch,
            target_node_ids=target_node_ids or [],
            target_edge_ids=target_edge_ids or [],
            predicate_id=predicate_id,
            predicate_result=_enum_value(predicate_result),
            summary=summary,
            value_digest=value_digest(structured_value),
            fresh_until=fresh_until,
        )

    def expected_binding_id(self) -> str:
        return stable_id("binding", {
            "raw_ref": self.raw_ref,
            "predicate_id": self.predicate_id,
            "target_node_ids": self.target_node_ids,
            "target_edge_ids": self.target_edge_ids,
        })

    def validate_raw_ref(self, trace_root: Path | None = None) -> bool:
        payload = self._trace_payload(trace_root)
        return payload is not None and payload.get("ref") == self.raw_ref

    def _trace_payload(self, trace_root: Path | None = None) -> dict | None:
        match = _RAW_REF.fullmatch(self.raw_ref)
        if not match or match.group(1) != self.episode_id:
            return None
        root = trace_root or TRACE_ROOT
        trace_file = root / self.episode_id / f"step_{int(match.group(2)):03d}.json"
        try:
            return json.loads(trace_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None

    def validate_value_digest(self, trace_root: Path | None = None) -> bool:
        payload = self._trace_payload(trace_root)
        return bool(payload is not None and self.value_digest and
                    value_digest(payload.get("digest")) == self.value_digest)

    def structured_value(self, trace_root: Path | None = None) -> Any:
        """Return the trace-backed value, never the mutable human summary."""
        payload = self._trace_payload(trace_root)
        if (payload is None or not self.value_digest or
                value_digest(payload.get("digest")) != self.value_digest):
            return None
        return payload.get("digest")

    def is_fresh(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return self.fresh_until is None or self.fresh_until >= current

    def is_trusted(self, trace_root: Path | None = None,
                   now: float | None = None) -> bool:
        return (self.status == "OBSERVED" and
                self.validate_raw_ref(trace_root) and
                self.validate_value_digest(trace_root) and
                self.is_fresh(now))

    def to_dict(self) -> dict:
        return json_ready(self)

    @classmethod
    def from_dict(cls, value: dict) -> "EvidenceBinding":
        return cls(**{f.name: value[f.name] for f in fields(cls)
                      if f.name in value})


@dataclass
class CausalPath:
    path_id: str
    node_ids: list[str]
    edge_ids: list[str]
    observed_symptom_id: str
    root_node_id: str
    node_roles: dict[str, str] = field(default_factory=dict)
    score_components: dict[str, float] = field(default_factory=dict)
    source: list[str] = field(default_factory=lambda: ["graph"])
    status: str = CausalStatus.UNTESTED.value
    required_evidence_types: list[str] = field(default_factory=list)
    evidence_binding_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.node_ids) < 2 or not self.edge_ids:
            raise ValueError("a causal path must contain at least one CAUSES edge")
        if len(self.edge_ids) != len(self.node_ids) - 1:
            raise ValueError("edge_ids must connect each adjacent node exactly once")
        if len(set(self.node_ids)) != len(self.node_ids):
            raise ValueError("causal paths cannot repeat nodes")
        if len(set(self.edge_ids)) != len(self.edge_ids):
            raise ValueError("causal paths cannot repeat edges")
        if self.root_node_id != self.node_ids[0]:
            raise ValueError("root_node_id must be the upstream path endpoint")
        if self.observed_symptom_id != self.node_ids[-1]:
            raise ValueError("observed_symptom_id must be the downstream endpoint")
        expected_roles = self.roles_for(self.node_ids)
        supplied_roles = {k: str(_enum_value(v)) for k, v in self.node_roles.items()}
        if supplied_roles and supplied_roles != expected_roles:
            raise ValueError("node_roles must be derived from the path position")
        self.node_roles = expected_roles
        self.status = str(_enum_value(self.status))
        if self.status not in {s.value for s in CausalStatus}:
            raise ValueError(f"invalid path status: {self.status}")
        if isinstance(self.source, str):
            self.source = [self.source]
        self.source = _unique(self.source)
        self.required_evidence_types = _unique(self.required_evidence_types)
        self.evidence_binding_ids = _unique(self.evidence_binding_ids)

    @staticmethod
    def roles_for(node_ids: list[str]) -> dict[str, str]:
        roles = {node_id: DynamicRole.MECHANISM.value for node_id in node_ids}
        roles[node_ids[0]] = DynamicRole.ROOT_CAUSE.value
        roles[node_ids[-1]] = DynamicRole.OBSERVED_SYMPTOM.value
        return roles

    @classmethod
    def create(cls, *, graph_version: str, node_ids: list[str],
               edge_ids: list[str], observed_symptom_id: str | None = None,
               score_components: dict[str, float] | None = None,
               source: str | list[str] = "graph",
               status: Any = CausalStatus.UNTESTED,
               required_evidence_types: list[str] | None = None,
               evidence_binding_ids: list[str] | None = None) -> "CausalPath":
        path_id = stable_id("path", {
            "graph_version": graph_version,
            "node_ids": node_ids,
            "edge_ids": edge_ids,
        })
        return cls(
            path_id=path_id,
            node_ids=list(node_ids),
            edge_ids=list(edge_ids),
            observed_symptom_id=observed_symptom_id or node_ids[-1],
            root_node_id=node_ids[0],
            score_components=score_components or {},
            source=[source] if isinstance(source, str) else source,
            status=_enum_value(status),
            required_evidence_types=required_evidence_types or [],
            evidence_binding_ids=evidence_binding_ids or [],
        )

    def merge_from(self, other: "CausalPath") -> None:
        if self.path_id != other.path_id:
            raise ValueError("only identical structural paths can be merged")
        self.source = _unique(self.source + other.source)
        self.required_evidence_types = _unique(
            self.required_evidence_types + other.required_evidence_types)
        self.evidence_binding_ids = _unique(
            self.evidence_binding_ids + other.evidence_binding_ids)
        for name, score in other.score_components.items():
            if name not in self.score_components:
                self.score_components[name] = score
            elif isinstance(score, (int, float)):
                self.score_components[name] = max(self.score_components[name], score)
        if self.status == CausalStatus.UNTESTED.value:
            self.status = other.status
        elif (other.status != CausalStatus.UNTESTED.value and
              other.status != self.status):
            self.status = CausalStatus.INCONCLUSIVE.value

    def to_dict(self) -> dict:
        return json_ready(self)

    @classmethod
    def from_dict(cls, value: dict) -> "CausalPath":
        return cls(**{f.name: value[f.name] for f in fields(cls)
                      if f.name in value})


@dataclass
class P0Obligation:
    cause_id: str
    reachable_path_ids: list[str] = field(default_factory=list)
    status: str = ObligationStatus.OPEN.value
    required_evidence_types: list[str] = field(default_factory=list)
    evidence_binding_ids: list[str] = field(default_factory=list)
    resolution_reason: str = ""
    truncated: bool = False

    def __post_init__(self) -> None:
        self.status = str(_enum_value(self.status))
        if self.status not in {s.value for s in ObligationStatus}:
            raise ValueError(f"invalid P0 obligation status: {self.status}")
        self.reachable_path_ids = _unique(self.reachable_path_ids)
        self.required_evidence_types = _unique(self.required_evidence_types)
        self.evidence_binding_ids = _unique(self.evidence_binding_ids)

    @property
    def resolved(self) -> bool:
        return (not self.truncated and
                self.status in {ObligationStatus.SUPPORTED.value,
                                ObligationStatus.REFUTED.value})

    def to_dict(self) -> dict:
        return json_ready(self)

    @classmethod
    def from_dict(cls, value: dict) -> "P0Obligation":
        return cls(**{f.name: value[f.name] for f in fields(cls)
                      if f.name in value})


@dataclass
class ExplanationGraph:
    explanation_id: str
    graph_version: str
    episode_id: str
    schema_version: int = 2
    revision: int = 0
    observed_symptoms: list[str] = field(default_factory=list)
    candidate_paths: list[CausalPath] = field(default_factory=list)
    node_status: dict[str, str] = field(default_factory=dict)
    edge_status: dict[str, str] = field(default_factory=dict)
    evidence_bindings: dict[str, EvidenceBinding] = field(default_factory=dict)
    selected_path_ids: list[str] = field(default_factory=list)
    selected_root_causes: list[str] = field(default_factory=list)
    unexplained_symptoms: list[str] = field(default_factory=list)
    p0_obligations: dict[str, P0Obligation] = field(default_factory=dict)
    scope: str = ExplanationScope.FULL.value
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("ExplanationGraph.schema_version must be 2")
        self.scope = str(_enum_value(self.scope))
        if self.scope not in {s.value for s in ExplanationScope}:
            raise ValueError(f"invalid explanation scope: {self.scope}")
        self.observed_symptoms = _unique(self.observed_symptoms)
        self.unexplained_symptoms = _unique(self.unexplained_symptoms)
        self.candidate_paths = [
            p if isinstance(p, CausalPath) else CausalPath.from_dict(p)
            for p in self.candidate_paths
        ]
        self._merge_duplicate_paths()
        self.evidence_bindings = {
            key: (binding if isinstance(binding, EvidenceBinding)
                  else EvidenceBinding.from_dict(binding))
            for key, binding in self.evidence_bindings.items()
        }
        self.p0_obligations = {
            key: (obligation if isinstance(obligation, P0Obligation)
                  else P0Obligation.from_dict(obligation))
            for key, obligation in self.p0_obligations.items()
        }
        self.node_status = self._normalise_status_map(self.node_status)
        self.edge_status = self._normalise_status_map(self.edge_status)
        self.selected_path_ids = _unique(self.selected_path_ids)
        unknown = set(self.selected_path_ids) - set(self.path_map())
        if unknown:
            raise ValueError(f"selected paths are not candidates: {sorted(unknown)}")
        self.selected_root_causes = self.derive_selected_root_causes()

    @classmethod
    def create(cls, *, graph_version: str, episode_id: str,
               observed_symptoms: list[str], candidate_paths: list[CausalPath],
               p0_obligations: dict[str, P0Obligation] | None = None,
               created_at: float | None = None) -> "ExplanationGraph":
        paths: dict[str, CausalPath] = {}
        for path in candidate_paths:
            if path.path_id in paths:
                paths[path.path_id].merge_from(path)
            else:
                paths[path.path_id] = CausalPath.from_dict(path.to_dict())
        explanation_id = stable_id("explanation", {
            "schema_version": 2,
            "graph_version": graph_version,
            "episode_id": episode_id,
            "observed_symptoms": sorted(set(observed_symptoms)),
            "candidate_path_ids": sorted(paths),
        })
        now = time.time() if created_at is None else created_at
        covered = {path.observed_symptom_id for path in paths.values()}
        return cls(
            explanation_id=explanation_id,
            graph_version=graph_version,
            episode_id=episode_id,
            observed_symptoms=observed_symptoms,
            candidate_paths=list(paths.values()),
            unexplained_symptoms=[s for s in observed_symptoms if s not in covered],
            p0_obligations=p0_obligations or {},
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _normalise_status_map(values: dict[str, Any]) -> dict[str, str]:
        result = {key: str(_enum_value(value)) for key, value in values.items()}
        invalid = set(result.values()) - {s.value for s in CausalStatus}
        if invalid:
            raise ValueError(f"invalid causal status values: {sorted(invalid)}")
        return result

    def _merge_duplicate_paths(self) -> None:
        merged: dict[str, CausalPath] = {}
        for path in self.candidate_paths:
            if path.path_id in merged:
                merged[path.path_id].merge_from(path)
            else:
                merged[path.path_id] = path
        self.candidate_paths = list(merged.values())

    def path_map(self) -> dict[str, CausalPath]:
        return {path.path_id: path for path in self.candidate_paths}

    def derive_selected_root_causes(self) -> list[str]:
        paths = self.path_map()
        return _unique([paths[path_id].root_node_id
                        for path_id in self.selected_path_ids])

    def _changed(self) -> None:
        self.revision += 1
        self.updated_at = time.time()

    def set_node_status(self, node_id: str, status: Any) -> bool:
        value = str(_enum_value(status))
        self._normalise_status_map({node_id: value})
        if self.node_status.get(node_id, CausalStatus.UNTESTED.value) == value:
            return False
        self.node_status[node_id] = value
        self._changed()
        return True

    def set_edge_status(self, edge_id: str, status: Any) -> bool:
        value = str(_enum_value(status))
        self._normalise_status_map({edge_id: value})
        if self.edge_status.get(edge_id, CausalStatus.UNTESTED.value) == value:
            return False
        self.edge_status[edge_id] = value
        self._changed()
        return True

    def set_path_status(self, path_id: str, status: Any) -> bool:
        value = str(_enum_value(status))
        self._normalise_status_map({path_id: value})
        path = self.path_map().get(path_id)
        if path is None:
            raise KeyError(path_id)
        if path.status == value:
            return False
        path.status = value
        self._changed()
        return True

    def add_evidence_binding(self, binding: EvidenceBinding, *,
                             trace_root: Path | None = None,
                             require_trace: bool = True) -> bool:
        if binding.episode_id != self.episode_id:
            raise ValueError("evidence belongs to another episode")
        if require_trace and not binding.validate_raw_ref(trace_root):
            raise ValueError("raw_ref is not verifiable in the current episode")
        current = self.evidence_bindings.get(binding.binding_id)
        if current:
            if current.to_dict() != binding.to_dict():
                raise ValueError("binding_id collision with different evidence")
            return False
        self.evidence_bindings[binding.binding_id] = binding
        target_nodes = set(binding.target_node_ids)
        target_edges = set(binding.target_edge_ids)
        for path in self.candidate_paths:
            if target_nodes.intersection(path.node_ids) or target_edges.intersection(path.edge_ids):
                path.evidence_binding_ids = _unique(
                    path.evidence_binding_ids + [binding.binding_id])
        self._changed()
        return True

    def select_paths(self, path_ids: list[str], *,
                     unexplained_symptoms: list[str] | None = None,
                     scope: Any | None = None) -> bool:
        selected = _unique(path_ids)
        unknown = set(selected) - set(self.path_map())
        if unknown:
            raise ValueError(f"cannot select unknown paths: {sorted(unknown)}")
        new_scope = self.scope if scope is None else str(_enum_value(scope))
        if new_scope not in {s.value for s in ExplanationScope}:
            raise ValueError(f"invalid explanation scope: {new_scope}")
        new_unexplained = (self.unexplained_symptoms if unexplained_symptoms is None
                           else _unique(unexplained_symptoms))
        changed = (selected != self.selected_path_ids or
                   new_unexplained != self.unexplained_symptoms or
                   new_scope != self.scope)
        if not changed:
            return False
        self.selected_path_ids = selected
        self.selected_root_causes = self.derive_selected_root_causes()
        self.unexplained_symptoms = new_unexplained
        self.scope = new_scope
        self._changed()
        return True

    def resolve_p0(self, cause_id: str, status: Any, *, reason: str,
                   binding_ids: list[str] | None = None) -> bool:
        if cause_id not in self.p0_obligations:
            raise KeyError(cause_id)
        obligation = self.p0_obligations[cause_id]
        new_status = str(_enum_value(status))
        if new_status not in {s.value for s in ObligationStatus}:
            raise ValueError(f"invalid P0 obligation status: {new_status}")
        new_bindings = _unique(binding_ids or obligation.evidence_binding_ids)
        changed = (obligation.status != new_status or
                   obligation.resolution_reason != reason or
                   obligation.evidence_binding_ids != new_bindings)
        if not changed:
            return False
        obligation.status = new_status
        obligation.resolution_reason = reason
        obligation.evidence_binding_ids = new_bindings
        self._changed()
        return True

    def unresolved_p0_paths(self) -> list[str]:
        path_ids: list[str] = []
        for obligation in self.p0_obligations.values():
            if not obligation.resolved:
                path_ids.extend(obligation.reachable_path_ids)
        return _unique(path_ids)

    def to_dict(self) -> dict:
        return json_ready(self)

    @classmethod
    def from_dict(cls, value: dict) -> "ExplanationGraph":
        payload = {f.name: value[f.name] for f in fields(cls) if f.name in value}
        payload.pop("selected_root_causes", None)
        graph = cls(**payload)
        # A supplied projection is never trusted; roots are always path-derived.
        graph.selected_root_causes = graph.derive_selected_root_causes()
        return graph


@dataclass
class EvidenceNeed:
    need_id: str
    path_ids: list[str]
    target_kind: str
    target_ids: list[str]
    evidence_type: str
    predicate_id: str
    required: bool
    freshness_seconds: int
    candidate_tools: list[str] = field(default_factory=list)
    reason: str = ""

    def __post_init__(self) -> None:
        self.target_kind = str(_enum_value(self.target_kind))
        if self.target_kind not in {k.value for k in EvidenceTargetKind}:
            raise ValueError(f"invalid evidence target kind: {self.target_kind}")
        self.path_ids = _unique(self.path_ids, sort=True)
        self.target_ids = _unique(self.target_ids, sort=True)
        self.candidate_tools = _unique(self.candidate_tools)
        expected = self.expected_need_id()
        if self.need_id and self.need_id != expected:
            raise ValueError("need_id does not match its causal target")
        self.need_id = expected

    @classmethod
    def create(cls, *, path_ids: list[str], target_kind: Any,
               target_ids: list[str], evidence_type: str, predicate_id: str,
               required: bool, freshness_seconds: int,
               candidate_tools: list[str] | None = None,
               reason: str = "") -> "EvidenceNeed":
        return cls(
            need_id="",
            path_ids=path_ids,
            target_kind=_enum_value(target_kind),
            target_ids=target_ids,
            evidence_type=evidence_type,
            predicate_id=predicate_id,
            required=required,
            freshness_seconds=freshness_seconds,
            candidate_tools=candidate_tools or [],
            reason=reason,
        )

    def expected_need_id(self) -> str:
        return stable_id("need", {
            "path_ids": self.path_ids,
            "target_kind": self.target_kind,
            "target_ids": self.target_ids,
            "evidence_type": self.evidence_type,
            "predicate_id": self.predicate_id,
        })

    def to_dict(self) -> dict:
        return json_ready(self)

    @classmethod
    def from_dict(cls, value: dict) -> "EvidenceNeed":
        return cls(**{f.name: value[f.name] for f in fields(cls)
                      if f.name in value})


@dataclass
class EvidenceReport:
    need_id: str
    tool: str
    raw_refs: list[str]
    observations: list[dict]
    collection_status: str
    limitations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.collection_status = str(_enum_value(self.collection_status))
        if self.collection_status not in _EVIDENCE_STATUSES:
            raise ValueError(f"invalid collection status: {self.collection_status}")
        self.raw_refs = _unique(self.raw_refs)

    def to_dict(self) -> dict:
        return json_ready(self)

    @classmethod
    def from_dict(cls, value: dict) -> "EvidenceReport":
        forbidden = {"verdict", "confirmed", "refuted", "predicate_result"}
        if forbidden.intersection(value):
            raise ValueError("subagent evidence reports cannot contain a verdict")
        return cls(**{f.name: value[f.name] for f in fields(cls)
                      if f.name in value})


@dataclass
class InterventionPlan:
    plan_id: str
    explanation_id: str
    explanation_revision: int
    selected_path_id: str
    intervention_target: str
    fix_id: str
    intervention_kind: str
    action_type: str
    sql: str
    rollback: str
    execution: str = "gated"
    manual: bool = False
    preconditions: list[dict] = field(default_factory=list)
    precondition_results: list[dict] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    expected_effect_nodes: list[str] = field(default_factory=list)
    expected_effects: list[dict] = field(default_factory=list)
    rationale: str = ""

    def __post_init__(self) -> None:
        self.intervention_kind = str(_enum_value(self.intervention_kind))
        if self.intervention_kind not in {k.value for k in InterventionKind}:
            raise ValueError(f"invalid intervention kind: {self.intervention_kind}")
        self.manual = bool(self.manual or
                           self.intervention_kind == InterventionKind.MANUAL.value or
                           self.execution == "escalate_only")
        if self.manual and self.sql.strip():
            raise ValueError("manual/escalate-only plans cannot contain SQL")
        if not self.manual and not self.sql.strip():
            raise ValueError("executable intervention plans require SQL")
        self.expected_effect_nodes = _unique(self.expected_effect_nodes)
        self.evidence_refs = _unique(self.evidence_refs)
        required_effect_fields = {
            "metric", "direction", "minimum_change", "window_seconds",
        }
        for effect in self.expected_effects:
            if not isinstance(effect, dict):
                raise ValueError("expected effects must be structured objects")
            missing = required_effect_fields - set(effect)
            if missing:
                raise ValueError(
                    f"expected effect is missing fields: {sorted(missing)}")
            if not str(effect.get("metric") or "").strip():
                raise ValueError("expected effect metric cannot be empty")
            if str(effect.get("direction") or "") not in {
                    "increase", "decrease", "stable"}:
                raise ValueError("expected effect direction is invalid")
            try:
                float(effect["minimum_change"])
                window_seconds = int(effect["window_seconds"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "expected effect threshold/window must be numeric") from exc
            if window_seconds <= 0:
                raise ValueError("expected effect window must be positive")
        if not self.manual and (not self.expected_effect_nodes or
                                not self.expected_effects):
            raise ValueError(
                "executable plans require downstream structured expected effects")
        denied_claims = (
            "root cause eliminated", "root cause resolved", "root cause removed",
            "eliminates the root cause", "fixes the root cause",
            "根因已消除", "根因已解决", "根因已修复", "根因已根治",
            "消除根因", "解决根因", "修复根因", "根治根因",
        )
        if (self.intervention_kind in {
                InterventionKind.MITIGATION.value,
                InterventionKind.CONTAINMENT.value} and
                any(claim in self.rationale.lower() for claim in denied_claims)):
            raise ValueError(
                f"{self.intervention_kind} rationale cannot claim root-cause elimination")
        expected = self.expected_plan_id()
        if self.plan_id and self.plan_id != expected:
            raise ValueError("plan_id does not match its explanation and intervention")
        self.plan_id = expected

    @classmethod
    def create(cls, *, explanation_id: str, explanation_revision: int,
               selected_path_id: str, intervention_target: str, fix_id: str,
               intervention_kind: Any, action_type: str, sql: str,
               rollback: str, expected_effect_nodes: list[str],
               expected_effects: list[dict], rationale: str,
               execution: str = "gated", manual: bool = False,
               preconditions: list[dict] | None = None,
               precondition_results: list[dict] | None = None,
               evidence_refs: list[str] | None = None) -> "InterventionPlan":
        return cls(
            plan_id="",
            explanation_id=explanation_id,
            explanation_revision=explanation_revision,
            selected_path_id=selected_path_id,
            intervention_target=intervention_target,
            fix_id=fix_id,
            intervention_kind=_enum_value(intervention_kind),
            action_type=action_type,
            sql=sql,
            rollback=rollback,
            execution=execution,
            manual=manual,
            preconditions=preconditions or [],
            precondition_results=precondition_results or [],
            evidence_refs=evidence_refs or [],
            expected_effect_nodes=expected_effect_nodes,
            expected_effects=expected_effects,
            rationale=rationale,
        )

    def expected_plan_id(self) -> str:
        return stable_id("plan", {
            "explanation_id": self.explanation_id,
            "explanation_revision": self.explanation_revision,
            "selected_path_id": self.selected_path_id,
            "intervention_target": self.intervention_target,
            "fix_id": self.fix_id,
            "action_type": self.action_type,
            "sql": self.sql,
        })

    def to_dict(self) -> dict:
        return json_ready(self)

    @classmethod
    def from_dict(cls, value: dict) -> "InterventionPlan":
        return cls(**{f.name: value[f.name] for f in fields(cls)
                      if f.name in value})


@dataclass
class CausalGateContext:
    explanation_id: str
    explanation_revision: int
    selected_path_ids: list[str]
    intervention_target: str
    fix_id: str
    intervention_kind: str
    expected_effect_nodes: list[str]
    esc_report_id: str
    evidence_refs: list[str]
    unresolved_p0_paths: list[str]
    expected_effects: list[dict] = field(default_factory=list)

    TRUSTED_FIELDS = frozenset({
        "explanation_id", "explanation_revision", "selected_path_ids",
        "intervention_target", "fix_id", "intervention_kind",
        "expected_effect_nodes", "expected_effects", "esc_report_id", "evidence_refs",
        "unresolved_p0_paths",
    })

    def __post_init__(self) -> None:
        self.intervention_kind = str(_enum_value(self.intervention_kind))
        if self.intervention_kind not in {k.value for k in InterventionKind}:
            raise ValueError(f"invalid intervention kind: {self.intervention_kind}")
        self.selected_path_ids = _unique(self.selected_path_ids)
        self.expected_effect_nodes = _unique(self.expected_effect_nodes)
        self.evidence_refs = _unique(self.evidence_refs)
        self.unresolved_p0_paths = _unique(self.unresolved_p0_paths)

    @classmethod
    def build(cls, explanation: ExplanationGraph, plan: InterventionPlan,
              esc_report_id: str, *, model_payload: dict | None = None,
              trace_root: Path | None = None,
              now: float | None = None) -> "CausalGateContext":
        if (plan.explanation_id != explanation.explanation_id or
                plan.explanation_revision != explanation.revision):
            raise ValueError("intervention plan is stale for this explanation")
        if plan.selected_path_id not in explanation.selected_path_ids:
            raise ValueError("intervention plan is not bound to a selected path")
        path = explanation.path_map()[plan.selected_path_id]
        if plan.intervention_target not in path.node_ids:
            raise ValueError("intervention target is outside the selected path")
        target_index = path.node_ids.index(plan.intervention_target)
        downstream = set(path.node_ids[target_index + 1:])
        if not set(plan.expected_effect_nodes).issubset(downstream):
            raise ValueError("expected effects must be downstream on the selected path")

        target_index = path.node_ids.index(plan.intervention_target)
        adjacent_edges = set(path.edge_ids[max(0, target_index - 1):target_index + 1])
        refs = []
        for binding in explanation.evidence_bindings.values():
            target_bound = bool(
                {plan.intervention_target, plan.fix_id}.intersection(
                    binding.target_node_ids) or
                adjacent_edges.intersection(binding.target_edge_ids))
            if target_bound and binding.is_trusted(trace_root, now):
                refs.append(binding.raw_ref)
        trusted = {
            "explanation_id": explanation.explanation_id,
            "explanation_revision": explanation.revision,
            "selected_path_ids": explanation.selected_path_ids,
            "intervention_target": plan.intervention_target,
            "fix_id": plan.fix_id,
            "intervention_kind": plan.intervention_kind,
            "expected_effect_nodes": plan.expected_effect_nodes,
            "expected_effects": plan.expected_effects,
            "esc_report_id": esc_report_id,
            "evidence_refs": _unique(refs),
            "unresolved_p0_paths": explanation.unresolved_p0_paths(),
        }
        for key in cls.TRUSTED_FIELDS.intersection(model_payload or {}):
            if json_ready(model_payload[key]) != json_ready(trusted[key]):
                raise ValueError(f"model payload conflicts with trusted {key}")
        aliases = {
            "selected_path_id": plan.selected_path_id,
            "root_cause": plan.intervention_target,
            "esc_verdict": "SUFFICIENT",
            "partial_explanation": (
                explanation.scope == ExplanationScope.PARTIAL.value),
            "action_type": plan.action_type,
            "sql": plan.sql,
            "rollback": plan.rollback,
            "rationale": plan.rationale,
        }
        for key in set(aliases).intersection(model_payload or {}):
            if json_ready(model_payload[key]) != json_ready(aliases[key]):
                raise ValueError(f"model payload conflicts with trusted {key}")
        return cls(**trusted)

    def to_dict(self) -> dict:
        return json_ready(self)

    @classmethod
    def from_dict(cls, value: dict) -> "CausalGateContext":
        return cls(**{f.name: value[f.name] for f in fields(cls)
                      if f.name in value})


def legacy_readonly_projection(*, episode_id: str, symptoms: list[str],
                               ledger: dict[str, Any]) -> ExplanationGraph:
    """Project v1 state without inventing path or edge validation."""
    statuses: dict[str, str] = {}
    for node_id, entry in ledger.items():
        verdict = entry.verdict if hasattr(entry, "verdict") else entry.get("verdict", "")
        if verdict == "CONFIRMED":
            statuses[node_id] = CausalStatus.SUPPORTED.value
        elif verdict in {"REFUTED", "REFUTED_BY_REMEDIATION"}:
            statuses[node_id] = CausalStatus.REFUTED.value
        elif verdict == "INCONCLUSIVE":
            statuses[node_id] = CausalStatus.INCONCLUSIVE.value
        else:
            statuses[node_id] = CausalStatus.UNTESTED.value
    return ExplanationGraph(
        explanation_id=stable_id("v1_projection", {"episode_id": episode_id}),
        graph_version="v1-unknown",
        episode_id=episode_id,
        observed_symptoms=list(symptoms),
        candidate_paths=[],
        node_status=statuses,
        edge_status={},
        selected_path_ids=[],
        unexplained_symptoms=list(symptoms),
        scope=ExplanationScope.PARTIAL.value,
        created_at=0.0,
        updated_at=0.0,
    )
