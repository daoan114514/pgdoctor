"""Deterministic intervention verification and scoped failure attribution."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from typing import Any

from agent.episode_state import (EpisodeState, EvidenceStatus,
                                 InterventionAttempt)
from agent.explanation import (CausalStatus, EvidenceBinding,
                               ExplanationScope, PredicateResult)


_KPI_ALIASES = {
    "latency_p99_ms": "p99_ms",
    "cpu_usage_pct": "cpu_pct",
    "throughput_qps": "qps",
    "error_count": "errors",
}
_ABSOLUTE_CHANGE_METRICS = {
    "blocked_session_count",
    "oldest_backend_xmin_age",
}
_SOURCE_FOR_METRIC = {
    "row_estimate_error_ratio": "explain_query",
    "dead_tuple_ratio": "get_table_stats",
    "autovacuum_backlog_ratio": "get_table_stats",
    "autovacuum_enabled": "get_table_stats",
    "blocked_session_count": "get_blocking_chain",
    "oldest_backend_xmin_age": "get_vacuum_horizon",
    "xid_age_ratio": "get_vacuum_horizon",
    "connection_usage_ratio": "get_connection_stats",
    "temp_bytes_delta": "get_database_stats",
    "requested_checkpoint_ratio": "get_database_stats",
    "disk_usage_ratio": "get_database_stats",
}
_METRIC_NODE = {
    "latency_p99_ms": "latency_p99_up",
    "cpu_usage_pct": "cpu_saturated",
    "throughput_qps": "throughput_down",
    "row_estimate_error_ratio": "stale_statistics",
    "dead_tuple_ratio": "table_bloat",
    "autovacuum_enabled": "autovacuum_starvation",
    "blocked_session_count": "queries_blocked",
    "oldest_backend_xmin_age": "long_idle_transaction",
    "xid_age_ratio": "xid_wraparound_risk",
    "connection_usage_ratio": "conn_near_limit",
    "temp_bytes_delta": "work_mem_spill",
    "requested_checkpoint_ratio": "checkpoint_pressure",
    "disk_usage_ratio": "disk_growing",
}


def observation_window(plan) -> int:
    return max([int(effect.get("window_seconds", 0))
                for effect in (plan.expected_effects if plan else [])] or [0])


def start_attempt(st: EpisodeState, plan) -> InterventionAttempt:
    attempt = InterventionAttempt.create(
        episode_id=st.episode_id, plan=plan,
        ordinal=len(st.intervention_attempts) + 1)
    st.record_intervention_attempt(attempt)
    return attempt


def mark_execution(attempt: InterventionAttempt, result) -> None:
    attempt.execution_status = "SUCCEEDED" if result.executed else "FAILED"
    attempt.execution_error = str(result.error or "")
    attempt.execution_undo_id = str(result.undo_id or "")
    attempt.execution_duration_s = float(result.duration_s or 0.0)
    attempt.outcome = "EXECUTED" if result.executed else "EXECUTION_FAILED"
    attempt.failure_scope = "NONE" if result.executed else "EXECUTION"
    attempt.learnable = False
    attempt.updated_at = time.time()


def ready_for_causal_verification(attempt: InterventionAttempt | None) -> bool:
    return bool(attempt and attempt.execution_status == "SUCCEEDED")


def _mapping(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    return {}


def _trace(observer):
    return getattr(observer, "trace", None)


def _record(trace, tool: str, args: dict, digest: dict) -> str:
    if trace is None or not hasattr(trace, "record"):
        return ""
    return trace.record(tool, args, json.dumps(digest, ensure_ascii=False,
                                               default=str), digest)


def _plan_table(plan) -> str:
    try:
        from agent.explanation_runtime import _sql_facts
        return str(_sql_facts(plan.sql).get("table") or "")
    except Exception:
        return ""


def _source_raw_ref(observer, tool: str, value: dict) -> str:
    ref = str(value.get("raw_ref") or "")
    if not ref and hasattr(observer, "raw_ref_for"):
        ref = str(observer.raw_ref_for(tool) or "")
    return ref


def _collect_sources(observer, metrics: list[str], *, plan, hot_query: str,
                     kpi: dict, phase: str) -> dict[str, dict]:
    sources: dict[str, dict] = {}
    trace = _trace(observer)
    kpi_digest = dict(kpi)
    sources["kpi"] = {
        "status": EvidenceStatus.OBSERVED.value,
        "value": kpi_digest,
        "raw_ref": _record(trace, "verify_kpi_snapshot", {"phase": phase},
                           kpi_digest),
    }
    required = set(_SOURCE_FOR_METRIC.get(metric, "") for metric in metrics)
    required.discard("")
    table = _plan_table(plan)
    for tool in sorted(required):
        try:
            if tool == "explain_query":
                if not hot_query:
                    raise ValueError("hot query is not bound")
                raw = observer.explain_query(hot_query)
            elif tool == "get_table_stats":
                if not table:
                    raise ValueError("target table is not bound")
                raw = observer.get_table_stats(table)
            elif tool == "get_blocking_chain":
                raw = observer.get_blocking_chain()
            else:
                raw = getattr(observer, tool)()
            value = _mapping(raw)
            if tool == "get_blocking_chain" and not value:
                value = {"chains": list(raw or [])}
            sources[tool] = {
                "status": EvidenceStatus.OBSERVED.value,
                "value": value,
                "raw_ref": _source_raw_ref(observer, tool, value),
            }
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            sources[tool] = {
                "status": EvidenceStatus.UNKNOWN.value,
                "value": {}, "raw_ref": "",
                "reason": f"{type(exc).__name__}: {exc}"[:200],
            }
        except Exception as exc:
            sources[tool] = {
                "status": EvidenceStatus.ERROR.value,
                "value": {}, "raw_ref": "",
                "reason": f"{type(exc).__name__}: {exc}"[:200],
            }
    return sources


def _latest_binding_baseline(st: EpisodeState, metric: str) -> float | None:
    explanation = st.explanation_graph
    if explanation is None:
        return None
    wanted = {
        "temp_bytes_delta": "temp_file_volume",
        "requested_checkpoint_ratio": "checkpoint_stats",
    }.get(metric)
    if not wanted:
        return None
    bindings = sorted(explanation.evidence_bindings.values(),
                      key=lambda item: item.observed_at, reverse=True)
    for binding in bindings:
        if binding.evidence_type != wanted or not binding.is_trusted():
            continue
        value = binding.structured_value()
        if not isinstance(value, dict):
            continue
        if metric == "temp_bytes_delta" and isinstance(value.get("temp_bytes"),
                                                         (int, float)):
            return float(value["temp_bytes"])
        timed, requested = value.get("ckpt_timed"), value.get("ckpt_requested")
        if isinstance(timed, (int, float)) and isinstance(requested, (int, float)):
            return float(requested) / max(float(timed) + float(requested), 1.0)
    return None


def _metric_value(metric: str, sources: dict[str, dict], *,
                  before_sources: dict[str, dict] | None = None
                  ) -> tuple[float | None, str, str, str]:
    tool = _SOURCE_FOR_METRIC.get(metric, "kpi")
    source = sources.get(tool, {})
    status = str(source.get("status") or EvidenceStatus.UNKNOWN.value)
    ref = str(source.get("raw_ref") or "")
    reason = str(source.get("reason") or "")
    if status != EvidenceStatus.OBSERVED.value:
        return None, status, ref, reason
    value = source.get("value") or {}
    try:
        if metric in _KPI_ALIASES:
            result = value.get(_KPI_ALIASES[metric])
        elif metric == "row_estimate_error_ratio":
            ratios = value.get("rows_est_vs_actual", [])
            result = max((max(float(est) / max(float(actual), 1e-9),
                              float(actual) / max(float(est), 1e-9))
                          for est, actual in ratios
                          if float(est) > 0 and float(actual) > 0), default=None)
        elif metric == "dead_tuple_ratio":
            result = value.get("dead_ratio")
        elif metric == "autovacuum_backlog_ratio":
            result = float(value["n_dead_tup"]) / max(
                float(value["autovacuum_trigger"]), 1.0)
        elif metric == "autovacuum_enabled":
            result = 1.0 if value.get("autovacuum_enabled") is True else 0.0
        elif metric == "blocked_session_count":
            chains = value.get("chains", [])
            result = len({row.get("blocked_pid") for row in chains
                          if row.get("blocked_pid") is not None})
        elif metric == "oldest_backend_xmin_age":
            result = value.get("oldest_backend_xmin_age")
        elif metric == "xid_age_ratio":
            result = float(value["db_xid_age"]) / max(
                float(value["freeze_max_age"]), 1.0)
        elif metric == "connection_usage_ratio":
            result = float(value["used"]) / max(
                float(value["max_connections"]), 1.0)
        elif metric in {"temp_bytes_delta", "requested_checkpoint_ratio"}:
            previous = (before_sources or {}).get(tool, {}).get("value") or {}
            if not previous:
                return None, EvidenceStatus.UNKNOWN.value, ref, \
                    "pre-intervention cumulative snapshot is unavailable"
            if metric == "temp_bytes_delta":
                if (previous.get("db_stats_reset") != value.get("db_stats_reset") or
                        "temp_bytes" not in previous or "temp_bytes" not in value):
                    return None, EvidenceStatus.UNKNOWN.value, ref, \
                        "database statistics source epoch changed"
                result = float(value["temp_bytes"]) - float(previous["temp_bytes"])
            else:
                if (previous.get("ckpt_stats_reset") != value.get("ckpt_stats_reset") or
                        any(key not in previous or key not in value for key in
                            ("ckpt_timed", "ckpt_requested"))):
                    return None, EvidenceStatus.UNKNOWN.value, ref, \
                        "checkpoint statistics source epoch changed"
                timed = float(value["ckpt_timed"]) - float(previous["ckpt_timed"])
                requested = (float(value["ckpt_requested"]) -
                             float(previous["ckpt_requested"]))
                if timed < 0 or requested < 0:
                    return None, EvidenceStatus.UNKNOWN.value, ref, \
                        "checkpoint cumulative counters moved backwards"
                result = requested / max(timed + requested, 1.0)
        elif metric == "disk_usage_ratio":
            result = float(value["disk_usage"]["used_pct"]) / 100.0
        else:
            result = value.get(metric)
        if not isinstance(result, (int, float)):
            return None, EvidenceStatus.UNKNOWN.value, ref, \
                f"metric {metric} is unavailable from {tool}"
        return float(result), status, ref, reason
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        return None, EvidenceStatus.UNKNOWN.value, ref, \
            f"{type(exc).__name__}: {exc}"[:200]


def capture_pre_intervention(st: EpisodeState, observer, plan, *,
                             kpi: dict, hot_query: str = "") -> dict:
    metrics = [str(effect.get("metric") or "")
               for effect in plan.expected_effects]
    sources = _collect_sources(observer, metrics, plan=plan,
                               hot_query=hot_query, kpi=kpi, phase="before")
    values = {}
    for metric in metrics:
        value, status, raw_ref, reason = _metric_value(metric, sources)
        baseline = _latest_binding_baseline(st, metric)
        if baseline is not None:
            value, status = baseline, EvidenceStatus.OBSERVED.value
        values[metric] = {"value": value, "status": status,
                          "raw_ref": raw_ref, "reason": reason}
    snapshot = {"captured_at": time.time(), "values": values,
                "sources": sources}
    st.pre_intervention_effects = snapshot
    return snapshot


def _target_node(plan, effect: dict) -> str:
    explicit = str(effect.get("target_node_id") or "")
    if explicit:
        return explicit
    mapped = _METRIC_NODE.get(str(effect.get("metric") or ""), "")
    allowed = {plan.intervention_target, *plan.expected_effect_nodes}
    if mapped in allowed:
        return mapped
    return plan.expected_effect_nodes[-1] if plan.expected_effect_nodes else ""


def _change(metric: str, direction: str, before: float,
            actual: float) -> float:
    if direction == "stable":
        return abs(actual - before)
    delta = before - actual if direction == "decrease" else actual - before
    if metric in _ABSOLUTE_CHANGE_METRICS:
        return delta
    return delta / max(abs(before), 1e-9)


def evaluate_expected_effects(st: EpisodeState, observer, plan, *,
                              kpi: dict, hot_query: str = "") -> dict:
    before_snapshot = st.pre_intervention_effects or {}
    metrics = [str(effect.get("metric") or "")
               for effect in plan.expected_effects]
    sources = _collect_sources(observer, metrics, plan=plan,
                               hot_query=hot_query, kpi=kpi, phase="after")
    effects = []
    trace = _trace(observer)
    for index, expected in enumerate(plan.expected_effects):
        metric = str(expected.get("metric") or "")
        before_row = (before_snapshot.get("values") or {}).get(metric, {})
        before = before_row.get("value")
        actual, status, source_ref, reason = _metric_value(
            metric, sources,
            before_sources=before_snapshot.get("sources") or {})
        observed_change = None
        met = None
        if (status == EvidenceStatus.OBSERVED.value and
                isinstance(before, (int, float)) and
                isinstance(actual, (int, float))):
            observed_change = _change(
                metric, str(expected.get("direction") or ""),
                float(before), float(actual))
            threshold = float(expected.get("minimum_change", 0.0))
            met = (observed_change <= threshold
                   if expected.get("direction") == "stable" else
                   observed_change >= threshold)
        elif status == EvidenceStatus.OBSERVED.value:
            status = EvidenceStatus.UNKNOWN.value
            reason = reason or "pre-intervention metric is unavailable"
        result = ("SUPPORTED" if met is True else
                  "REFUTED" if met is False else "INCONCLUSIVE")
        row = {
            "effect_id": f"{plan.plan_id}:effect:{index}",
            "target_node_id": _target_node(plan, expected),
            "metric": metric,
            "expected": {
                "direction": expected.get("direction"),
                "minimum_change": expected.get("minimum_change"),
                "window_seconds": expected.get("window_seconds"),
            },
            "before": before,
            "actual": actual,
            "observed_change": observed_change,
            "met": met,
            "collection_status": status,
            "result": result,
            "source_raw_ref": source_ref,
            "before_raw_ref": before_row.get("raw_ref", ""),
            "reason": reason,
        }
        row["raw_ref"] = _record(
            trace, "verify_expected_effect",
            {"plan_id": plan.plan_id, "effect_index": index}, row)
        effects.append(row)
    outcome = ("INCONCLUSIVE" if any(item["met"] is None for item in effects)
               else "REFUTED" if any(item["met"] is False for item in effects)
               else "SUPPORTED" if effects else "INCONCLUSIVE")
    return {
        "plan_id": plan.plan_id,
        "expected_effect_nodes": list(plan.expected_effect_nodes),
        "configured_window_seconds": observation_window(plan),
        "effects": effects,
        "effects_outcome": outcome,
    }


def verification_passed(*, recovered: bool, effects_outcome: str,
                        regression_passed: bool) -> bool:
    return bool(recovered and effects_outcome == "SUPPORTED" and
                regression_passed)


def classify_failure_scope(st: EpisodeState, attempt: InterventionAttempt,
                           verification: dict) -> str:
    if attempt.execution_status != "SUCCEEDED":
        return "EXECUTION"
    effects = verification.get("effects", [])
    if verification.get("effects_outcome") == "INCONCLUSIVE":
        return "EVIDENCE"
    failed = [effect for effect in effects if effect.get("met") is False]
    if failed:
        target_changed = any(
            effect.get("target_node_id") == attempt.intervention_target and
            effect.get("met") is True for effect in effects)
        downstream_failed = any(
            effect.get("target_node_id") != attempt.intervention_target
            for effect in failed)
        if target_changed and downstream_failed:
            return "PATH_SEGMENT"
        return "INTERVENTION"
    if not verification.get("regression_passed", False):
        return "REGRESSION"
    if not verification.get("recovered", False):
        explanation = st.explanation_graph
        if (explanation is None or
                explanation.scope == ExplanationScope.PARTIAL.value or
                explanation.unexplained_symptoms or
                len(explanation.derive_selected_root_causes()) > 1):
            return "CONTEXT"
        return "CONTEXT"
    return "NONE"


def affected_edges_on_path(st: EpisodeState, attempt: InterventionAttempt,
                           verification: dict) -> list[str]:
    explanation = st.explanation_graph
    if explanation is None:
        return []
    path = explanation.path_map().get(attempt.selected_path_id)
    if path is None or attempt.intervention_target not in path.node_ids:
        return []
    start = path.node_ids.index(attempt.intervention_target)
    affected = []
    for effect in verification.get("effects", []):
        if effect.get("met") is not False:
            continue
        node_id = str(effect.get("target_node_id") or "")
        end = path.node_ids.index(node_id) if node_id in path.node_ids else len(
            path.node_ids) - 1
        if end > start:
            affected.extend(path.edge_ids[start:end])
    return list(dict.fromkeys(affected))


def apply_failure_knowledge(st: EpisodeState, attempt: InterventionAttempt,
                            verification: dict) -> None:
    explanation = st.explanation_graph
    if explanation is None or attempt.failure_scope != "PATH_SEGMENT":
        return
    affected = set(attempt.affected_edge_ids)
    path = explanation.path_map().get(attempt.selected_path_id)
    if path is None:
        return
    for effect in verification.get("effects", []):
        if effect.get("met") is not False or not effect.get("raw_ref"):
            continue
        node_id = str(effect.get("target_node_id") or "")
        end = path.node_ids.index(node_id) if node_id in path.node_ids else len(
            path.node_ids) - 1
        start = path.node_ids.index(attempt.intervention_target)
        edge_ids = [edge_id for edge_id in path.edge_ids[start:end]
                    if edge_id in affected]
        if not edge_ids:
            continue
        structured = {key: value for key, value in effect.items()
                      if key != "raw_ref"}
        binding = EvidenceBinding.create(
            episode_id=st.episode_id,
            raw_ref=effect["raw_ref"],
            evidence_type="intervention_expected_effect",
            status=EvidenceStatus.OBSERVED,
            observed_at=time.time(),
            predicate_id="intervention_expected_effect_v2",
            predicate_result=PredicateResult.REFUTES,
            structured_value=structured,
            target_node_ids=[node_id] if node_id else [],
            target_edge_ids=edge_ids,
            summary=(f"intervention {attempt.plan_id} did not meet the "
                     f"{effect.get('metric')} prediction"),
            window_start=st.pre_intervention_effects.get("captured_at"),
            window_end=time.time(),
            source_epoch=st.episode_id,
        )
        explanation.add_evidence_binding(binding)
    for edge_id in attempt.affected_edge_ids:
        explanation.set_edge_status(edge_id, CausalStatus.REFUTED)
    explanation.set_path_status(attempt.selected_path_id,
                                CausalStatus.REFUTED)


def retry_phase_for_failure(failure_scope: str) -> str:
    if failure_scope in {"PATH_SEGMENT", "EVIDENCE", "CONTEXT"}:
        return "INVESTIGATE"
    return "PLAN"


def node_confidence_reduction_eligible(st: EpisodeState,
                                       node_id: str) -> bool:
    explanation = st.explanation_graph
    if (explanation is None or
            explanation.scope != ExplanationScope.FULL.value or
            explanation.unexplained_symptoms):
        return False
    attempts = [attempt for attempt in st.intervention_attempts
                if attempt.intervention_target == node_id and
                attempt.execution_status == "SUCCEEDED" and
                attempt.failure_scope in {"INTERVENTION", "PATH_SEGMENT"} and
                attempt.learnable]
    independent = {(attempt.plan_id, attempt.fix_id) for attempt in attempts}
    return len(independent) >= 2
