"""Deterministic v2 metrics computed from persisted episode state.

The evaluator never mutates the explanation or invokes tools.  Every rate is
stored with its numerator and denominator so small fixtures cannot hide behind
percentages, and old v1 traces remain replayable with explicit unavailable
metrics.
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Any, Iterable

from agent.explanation import CausalStatus, ObligationStatus, PredicateResult


DEFAULT_RECALL_K = (1, 3, 5, 12)


def ratio(numerator: int | float, denominator: int | float) -> dict:
    numerator = int(numerator) if float(numerator).is_integer() else numerator
    denominator = (int(denominator) if float(denominator).is_integer()
                   else denominator)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": (round(float(numerator) / float(denominator), 6)
                  if denominator else None),
    }


def _report_dict(report: Any) -> dict:
    if isinstance(report, dict):
        return report
    if hasattr(report, "to_dict"):
        return report.to_dict()
    return {
        "verdict": getattr(report, "verdict", ""),
        "actual_verdict": getattr(report, "actual_verdict", ""),
    }


def _truth_roots(spec: dict) -> list[str]:
    truth = spec.get("ground_truth") or {}
    roots = truth.get("root_node_ids") or truth.get("root_nodes")
    if roots:
        return list(dict.fromkeys(str(root) for root in roots))
    root = spec.get("fault_class")
    return [str(root)] if root else []


def _truth_path_nodes(spec: dict) -> list[tuple[str, ...]]:
    truth = spec.get("ground_truth") or {}
    values = truth.get("explanation_paths") or truth.get("causal_paths") or []
    paths: list[tuple[str, ...]] = []
    for value in values:
        nodes = value.get("node_ids", []) if isinstance(value, dict) else value
        if isinstance(nodes, (list, tuple)) and len(nodes) >= 2:
            paths.append(tuple(str(node) for node in nodes))
    return list(dict.fromkeys(paths))


def _expected_paths(spec: dict, explanation) -> tuple[set[tuple[str, ...]],
                                                       set[str], str]:
    explicit = set(_truth_path_nodes(spec))
    if explicit:
        from agent.explanation import stable_id
        from knowledge.causal_graph import graph as causal_graph

        graph = causal_graph.load()
        edge_ids = set()
        for nodes in explicit:
            for source, target in zip(nodes, nodes[1:]):
                edge = graph.get_edge_data(source, target, "CAUSES") or {}
                edge_ids.add(edge.get("edge_id") or stable_id("edge", {
                    "kind": "CAUSES", "from": source, "to": target,
                }))
        return explicit, edge_ids, "scenario"

    roots = set(_truth_roots(spec))
    by_symptom: dict[str, list[Any]] = {}
    for path in explanation.candidate_paths:
        if path.root_node_id in roots:
            by_symptom.setdefault(path.observed_symptom_id, []).append(path)
    inferred = []
    for paths in by_symptom.values():
        inferred.append(max(paths, key=lambda path: (
            path.score_components.get("total", 0.0),
            -len(path.edge_ids), path.path_id)))
    return ({tuple(path.node_ids) for path in inferred},
            {edge_id for path in inferred for edge_id in path.edge_ids},
            "inferred_from_root")


def _ranked_roots(paths: Iterable[Any]) -> list[str]:
    roots = []
    for path in paths:
        if path.root_node_id not in roots:
            roots.append(path.root_node_id)
    return roots


def _reachable_p0(observed_symptoms: list[str]) -> set[str]:
    if not observed_symptoms:
        return set()
    from knowledge.causal_graph import graph as causal_graph

    graph = causal_graph.load()
    causes = graph.edge_subgraph([
        (source, target, key)
        for source, target, key in graph.edges(keys=True)
        if key == "CAUSES"
    ]).copy()
    expected = set()
    for node_id, data in graph.nodes(data=True):
        if data.get("severity") != "P0" or node_id not in causes:
            continue
        descendants = __import__("networkx").descendants(causes, node_id)
        if descendants.intersection(observed_symptoms):
            expected.add(node_id)
    return expected


def _selected_required_evidence(
        explanation, *, now: float | None = None
        ) -> tuple[set[tuple[str, str]], set]:
    from knowledge.causal_graph import graph as causal_graph

    required = {
        (root_id, evidence_type)
        for root_id in explanation.derive_selected_root_causes()
        for evidence_type in causal_graph.required_evidence(root_id)
    }
    completed = set()
    for binding in explanation.evidence_bindings.values():
        if (binding.predicate_result != PredicateResult.SUPPORTS.value or
                not binding.is_trusted(now=now)):
            continue
        for root_id in binding.target_node_ids:
            key = (root_id, binding.evidence_type)
            if key in required:
                completed.add(key)
    return required, completed


def _duplicate_binding_count(bindings: list[Any]) -> int:
    identities = [(
        binding.raw_ref, binding.predicate_id,
        tuple(sorted(binding.target_node_ids)),
        tuple(sorted(binding.target_edge_ids)),
    ) for binding in bindings]
    counts = Counter(identities)
    return sum(count - 1 for count in counts.values())


def _tool_metrics(audit: list[dict]) -> dict:
    events = [item for item in audit
              if item.get("event") == "tool_learning_observation"]
    calls: dict[tuple[str, str], list[dict]] = {}
    for item in events:
        calls.setdefault((str(item.get("task_id") or item.get("observation_id")),
                          str(item.get("tool") or "")), []).append(item)
    call_rows = list(calls.values())
    info_gain = sum(max(float(item.get("entropy_gain", 0.0))
                        for item in rows) for rows in call_rows)
    duplicate_calls = sum(max(int(item.get("duplicate_calls", 0))
                              for item in rows) for rows in call_rows)
    bad = sum(any(item.get("collection_status") in {"UNKNOWN", "ERROR"}
                  for item in rows) for rows in call_rows)
    return {
        "tool_calls": len(call_rows),
        "information_gain_per_call": {
            **ratio(info_gain, len(call_rows)), "unit": "entropy_delta"},
        "duplicate_tool_call_rate": ratio(duplicate_calls, len(call_rows)),
        "unknown_error_rate": ratio(bad, len(call_rows)),
    }


def _failure_attribution(attempts: list[Any]) -> dict:
    checked = []
    for attempt in attempts:
        if attempt.outcome not in {"FAILED", "EXECUTION_FAILED"}:
            continue
        scope = attempt.failure_scope
        if attempt.execution_status != "SUCCEEDED":
            checked.append(scope == "EXECUTION")
        elif scope == "PATH_SEGMENT":
            checked.append(bool(attempt.affected_edge_ids))
        elif scope == "INTERVENTION":
            checked.append(not attempt.affected_edge_ids)
        elif scope == "CONTEXT":
            checked.append(not attempt.affected_edge_ids)
        elif scope == "EVIDENCE":
            checked.append(True)
        else:
            checked.append(False)
    return ratio(sum(checked), len(checked))


def _rollback_completeness(attempts: list[Any]) -> dict:
    eligible = [attempt for attempt in attempts
                if attempt.outcome in {"FAILED", "EXECUTION_FAILED"} and
                (attempt.rollback_attempted or attempt.execution_undo_id)]
    complete = sum(
        attempt.rollback_attempted and attempt.rollback_status == "SUCCEEDED"
        for attempt in eligible)
    return ratio(complete, len(eligible))


def compute_episode_metrics(st, spec: dict, *, gate_decisions: list[dict] | None = None,
                            now: float | None = None,
                            recall_k: tuple[int, ...] = DEFAULT_RECALL_K) -> dict:
    """Compute the complete v2 metric surface from one persisted episode."""
    explanation = getattr(st, "explanation_graph", None)
    base = {
        "schema_version": int(getattr(st, "schema_version", 1)),
        "scenario_revision": spec.get("revision"),
        "ground_truth_source": "unavailable",
    }
    if explanation is None:
        base.update({
            "root_recall_at_k": {str(k): ratio(0, 0) for k in recall_k},
            "path_recall_at_k": {str(k): ratio(0, 0) for k in recall_k},
            "p0_obligation_recall": ratio(0, 0),
            "observed_symptom_coverage": ratio(0, 0),
            "unexplained_symptom_rate": ratio(0, 0),
            "selected_path_edge_precision": ratio(0, 0),
            "selected_path_edge_recall": ratio(0, 0),
            "selected_path_edge_f1": ratio(0, 0),
            "required_evidence_completion": ratio(0, 0),
            "raw_ref_validity": ratio(0, 0),
            "raw_ref_freshness": ratio(0, 0),
            "duplicate_evidence_rate": ratio(0, 0),
            **_tool_metrics(getattr(st, "evidence_task_audit", [])),
            "esc_unsafe_pass": ratio(0, 0),
            "esc_over_conservative": ratio(0, 0),
            "gate_context_bypass_count": 0,
            "expected_effect_hit_rate": ratio(0, 0),
            "failure_attribution_accuracy": _failure_attribution(
                getattr(st, "intervention_attempts", [])),
            "rollback_completeness": _rollback_completeness(
                getattr(st, "intervention_attempts", [])),
        })
        return base

    current_time = time.time() if now is None else now
    paths = list(explanation.candidate_paths)
    expected_paths, expected_edges, truth_source = _expected_paths(
        spec, explanation)
    truth_roots = set(_truth_roots(spec))
    ranked_roots = _ranked_roots(paths)
    root_recall = {
        str(k): ratio(len(truth_roots.intersection(ranked_roots[:k])),
                      len(truth_roots)) for k in recall_k
    }
    path_recall = {
        str(k): ratio(len(expected_paths.intersection(
            tuple(path.node_ids) for path in paths[:k])), len(expected_paths))
        for k in recall_k
    }

    observed = set(explanation.observed_symptoms)
    selected = [explanation.path_map()[path_id]
                for path_id in explanation.selected_path_ids
                if path_id in explanation.path_map()]
    selected_symptoms = {path.observed_symptom_id for path in selected}
    selected_edges = {edge_id for path in selected for edge_id in path.edge_ids}
    edge_tp = len(selected_edges.intersection(expected_edges))
    precision = ratio(edge_tp, len(selected_edges))
    recall = ratio(edge_tp, len(expected_edges))
    f1_denominator = len(selected_edges) + len(expected_edges)
    edge_f1 = ratio(2 * edge_tp, f1_denominator)

    expected_p0 = _reachable_p0(list(observed))
    obligations = set(explanation.p0_obligations)
    required, completed = _selected_required_evidence(
        explanation, now=current_time)
    bindings = list(explanation.evidence_bindings.values())
    valid_count = sum(binding.validate_raw_ref() for binding in bindings)
    fresh_count = sum(binding.is_fresh(current_time) for binding in bindings)
    duplicate_count = _duplicate_binding_count(bindings)

    reports = [_report_dict(report) for report in getattr(st, "esc_reports", [])]
    latest = reports[-1] if reports else {}
    esc_verdict = str(latest.get("actual_verdict") or latest.get("verdict") or "")
    p0_safe = all(
        obligation.status in {ObligationStatus.SUPPORTED.value,
                              ObligationStatus.REFUTED.value} and
        not obligation.truncated
        for obligation in explanation.p0_obligations.values())
    selected_supported = bool(selected) and all(
        path.status == CausalStatus.SUPPORTED.value for path in selected)
    evidence_safe = len(required) == len(completed)
    causally_ready = selected_supported and evidence_safe and p0_safe
    esc_pass = esc_verdict == "SUFFICIENT"

    decisions = gate_decisions or []
    bypass_count = sum(bool(report.get("bypassed")) for report in reports)
    bypass_count += sum(
        bool(decision.get("approved")) and not decision.get("causal_context")
        for decision in decisions)

    effects = list((getattr(st, "verification_result", {}) or {}).get(
        "effects", []))
    observed_effects = [effect for effect in effects
                        if effect.get("met") is not None]

    base.update({
        "ground_truth_source": truth_source,
        "root_recall_at_k": root_recall,
        "path_recall_at_k": path_recall,
        "p0_obligation_recall": ratio(
            len(expected_p0.intersection(obligations)), len(expected_p0)),
        "observed_symptom_coverage": ratio(
            len(observed.intersection(selected_symptoms)), len(observed)),
        "unexplained_symptom_rate": ratio(
            len(observed.intersection(explanation.unexplained_symptoms)),
            len(observed)),
        "selected_path_edge_precision": precision,
        "selected_path_edge_recall": recall,
        "selected_path_edge_f1": edge_f1,
        "required_evidence_completion": ratio(len(completed), len(required)),
        "raw_ref_validity": ratio(valid_count, len(bindings)),
        "raw_ref_freshness": ratio(fresh_count, len(bindings)),
        "duplicate_evidence_rate": ratio(duplicate_count, len(bindings)),
        **_tool_metrics(getattr(st, "evidence_task_audit", [])),
        "esc_unsafe_pass": ratio(int(esc_pass and not causally_ready),
                                 int(esc_pass)),
        "esc_over_conservative": ratio(
            int(bool(esc_verdict) and not esc_pass and causally_ready),
            int(bool(esc_verdict) and not esc_pass)),
        "esc_verdict": esc_verdict,
        "gate_context_bypass_count": bypass_count,
        "expected_effect_hit_rate": ratio(
            sum(effect.get("met") is True for effect in observed_effects),
            len(observed_effects)),
        "failure_attribution_accuracy": _failure_attribution(
            getattr(st, "intervention_attempts", [])),
        "rollback_completeness": _rollback_completeness(
            getattr(st, "intervention_attempts", [])),
    })
    return base


def aggregate_episode_metrics(episodes: Iterable[dict]) -> dict:
    """Aggregate serialized per-episode metrics without losing denominators."""
    metrics = [episode.get("metrics_v2") or {} for episode in episodes]
    scalar_rates = (
        "p0_obligation_recall", "observed_symptom_coverage",
        "unexplained_symptom_rate", "selected_path_edge_precision",
        "selected_path_edge_recall", "selected_path_edge_f1",
        "required_evidence_completion", "raw_ref_validity",
        "raw_ref_freshness", "duplicate_evidence_rate",
        "information_gain_per_call", "duplicate_tool_call_rate",
        "unknown_error_rate", "esc_unsafe_pass", "esc_over_conservative",
        "expected_effect_hit_rate", "failure_attribution_accuracy",
        "rollback_completeness",
    )
    out = {}
    for name in scalar_rates:
        rows = [metric.get(name) for metric in metrics
                if isinstance(metric.get(name), dict)]
        out[name] = ratio(sum(row.get("numerator", 0) for row in rows),
                          sum(row.get("denominator", 0) for row in rows))
    for name in ("root_recall_at_k", "path_recall_at_k"):
        keys = sorted({key for metric in metrics
                       for key in (metric.get(name) or {})}, key=int)
        out[name] = {
            key: ratio(
                sum((metric.get(name) or {}).get(key, {}).get("numerator", 0)
                    for metric in metrics),
                sum((metric.get(name) or {}).get(key, {}).get("denominator", 0)
                    for metric in metrics))
            for key in keys
        }
    out["tool_calls"] = sum(int(metric.get("tool_calls", 0))
                            for metric in metrics)
    out["gate_context_bypass_count"] = sum(
        int(metric.get("gate_context_bypass_count", 0)) for metric in metrics)
    out["esc_verdict_distribution"] = dict(sorted(Counter(
        metric.get("esc_verdict") for metric in metrics
        if metric.get("esc_verdict")).items()))
    out["episode_count"] = len(metrics)
    return out
