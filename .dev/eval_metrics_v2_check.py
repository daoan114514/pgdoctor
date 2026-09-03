"""Acceptance checks for v2 run-suite and replay metric contracts."""
from __future__ import annotations

import shutil
import sys
import time
import uuid
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState
from agent.explanation import EvidenceBinding
from eval.metrics_v2 import aggregate_episode_metrics, compute_episode_metrics
from eval.replay import replay_esc
from knowledge.causal_graph import graph as G
from sandbox.traces import TRACE_DIR, TraceStore


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<58} {detail}")


episode_ids = []
try:
    print("[1] Every small-sample rate carries numerator/denominator")
    episode_id = f"eval_metrics_{uuid.uuid4().hex}"
    episode_ids.append(episode_id)
    state = EpisodeState(episode_id, "controlled_metrics")
    explanation = G.recall_explanation(
        ["disk_growing"], episode_id=episode_id, use_learned=False)
    state.explanation_graph = explanation
    state.evidence_task_audit.extend([
        {"event": "tool_learning_observation", "task_id": "task-1",
         "tool": "get_database_stats", "entropy_gain": 0.5,
         "collection_status": "OBSERVED", "duplicate_calls": 0},
        {"event": "tool_learning_observation", "task_id": "task-1",
         "tool": "get_database_stats", "entropy_gain": 0.5,
         "collection_status": "OBSERVED", "duplicate_calls": 0},
        {"event": "tool_learning_observation", "task_id": "task-2",
         "tool": "get_vacuum_horizon", "entropy_gain": 0.0,
         "collection_status": "UNKNOWN", "duplicate_calls": 0},
    ])
    spec = {
        "id": "controlled_metrics", "fault_class": "disk_pressure",
        "revision": 2,
        "ground_truth": {
            "root_node_ids": ["disk_pressure"],
            "explanation_paths": [["disk_pressure", "disk_growing"]],
        },
    }
    metrics = compute_episode_metrics(state, spec)
    rate_names = [
        "p0_obligation_recall", "observed_symptom_coverage",
        "unexplained_symptom_rate", "selected_path_edge_precision",
        "selected_path_edge_recall", "selected_path_edge_f1",
        "required_evidence_completion", "raw_ref_validity",
        "raw_ref_freshness", "duplicate_evidence_rate",
        "information_gain_per_call", "duplicate_tool_call_rate",
        "unknown_error_rate", "esc_unsafe_pass", "esc_over_conservative",
        "expected_effect_hit_rate", "failure_attribution_accuracy",
        "rollback_completeness",
    ]
    check("all v2 rates expose numerator and denominator",
          all({"numerator", "denominator", "value"} <= set(metrics[name])
              for name in rate_names))
    check("all four reachable P0 obligations are recalled",
          metrics["p0_obligation_recall"]["numerator"] ==
          metrics["p0_obligation_recall"]["denominator"] == 4)
    check("merged needs count as one physical tool call",
          metrics["tool_calls"] == 2 and
          metrics["information_gain_per_call"]["numerator"] == 0.5)
    check("UNKNOWN tool collection is measured, not treated as refutation",
          metrics["unknown_error_rate"] == {
              "numerator": 1, "denominator": 2, "value": 0.5})

    print("\n[2] A completely missed truth path keeps its denominator")
    direct_path = next(path for path in explanation.candidate_paths
                       if path.node_ids == ["disk_pressure", "disk_growing"])
    missed = EpisodeState(f"{episode_id}_missed", "controlled_metrics")
    missed.explanation_graph = G.merge_paths(
        [direct_path], episode_id=missed.episode_id,
        observed_symptoms=["disk_growing"])
    missed_metrics = compute_episode_metrics(missed, {
        "revision": 2,
        "ground_truth": {"explanation_paths": [[
            "stale_replication_slot", "autovacuum_starvation",
            "table_bloat", "disk_pressure", "disk_growing",
        ]]},
    })
    check("missed path recall is 0/1 instead of unavailable",
          missed_metrics["path_recall_at_k"]["12"] == {
              "numerator": 0, "denominator": 1, "value": 0.0})
    check("truth edge recall denominator is independent of candidates",
          missed_metrics["selected_path_edge_recall"]["denominator"] == 4)

    print("\n[3] Replay time controls required-evidence freshness")
    evidence_id = f"eval_evidence_{uuid.uuid4().hex}"
    episode_ids.append(evidence_id)
    evidence_state = EpisodeState(evidence_id, "controlled_metrics")
    evidence_path = next(path for path in G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=False)
        if path.node_ids == ["missing_index", "latency_p99_up"])
    evidence_graph = G.merge_paths(
        [evidence_path], episode_id=evidence_id,
        observed_symptoms=["latency_p99_up"])
    evidence_graph.set_path_status(evidence_path.path_id, "SUPPORTED")
    evidence_graph.select_paths([evidence_path.path_id])
    store = TraceStore(evidence_id)
    observed_at = time.time()
    graph = G.load()
    values = {
        "explain_seq_scan": {
            "scan_types": ["Seq Scan"], "rows_removed_by_filter": 100000},
        "index_existence": {"inventory_collected": True, "indexes": []},
    }
    for ordinal, (evidence_type, value) in enumerate(values.items(), 1):
        raw_ref = store.record(
            "fixture", {"ordinal": ordinal}, str(value), value)
        evidence_graph.add_evidence_binding(EvidenceBinding.create(
            episode_id=evidence_id, raw_ref=raw_ref,
            evidence_type=evidence_type, status="OBSERVED",
            observed_at=observed_at,
            predicate_id=str(graph.nodes[evidence_type]["predicate_id"]),
            predicate_result="SUPPORTS", structured_value=value,
            target_node_ids=["missing_index"],
            fresh_until=observed_at + 60))
    evidence_state.explanation_graph = evidence_graph
    evidence_spec = {
        "revision": 2, "fault_class": "missing_index",
        "ground_truth": {"explanation_paths": [[
            "missing_index", "latency_p99_up"]]},
    }
    fresh_metrics = compute_episode_metrics(
        evidence_state, evidence_spec, now=observed_at + 30)
    stale_metrics = compute_episode_metrics(
        evidence_state, evidence_spec, now=observed_at + 90)
    check("fresh replay counts both required evidence types",
          fresh_metrics["required_evidence_completion"] == {
              "numerator": 2, "denominator": 2, "value": 1.0})
    check("stale replay retains the denominator but removes completion",
          stale_metrics["required_evidence_completion"] == {
              "numerator": 0, "denominator": 2, "value": 0.0})

    print("\n[4] Aggregation preserves denominators and verdict distribution")
    aggregate = aggregate_episode_metrics([
        {"metrics_v2": metrics}, {"metrics_v2": metrics}])
    check("aggregate sums numerator and denominator",
          aggregate["p0_obligation_recall"]["numerator"] == 8 and
          aggregate["p0_obligation_recall"]["denominator"] == 8)
    check("GATE context bypass target remains an explicit count",
          aggregate["gate_context_bypass_count"] == 0)

    print("\n[5] Replay reads both v2 and legacy traces")
    replay_id = f"eval_replay_{uuid.uuid4().hex}"
    episode_ids.append(replay_id)
    replay_state = EpisodeState(
        replay_id, "missing_index_eval_v1", schema_version=2)
    replay_path = next(path for path in G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=False)
        if path.node_ids == ["missing_index", "latency_p99_up"])
    replay_state.explanation_graph = G.merge_paths(
        [replay_path], episode_id=replay_id,
        observed_symptoms=["latency_p99_up"])
    replay_state.save()
    replayed_v2 = replay_esc(replay_id)
    # revision 从场景文件读，不写死。上一次写死成 2，改判据 bump 到 3
    # 之后这条就红了 —— 而它想验的是"replay 能把 revision 读出来"，
    # 不是"revision 恰好等于某个数"。
    expected_revision = yaml.safe_load(
        (Path(__file__).resolve().parent.parent /
         "sandbox/scenarios/missing_index_eval_v1.yaml").read_text(
            encoding="utf-8")).get("revision")
    check("v2 replay recomputes explanation metrics and revision",
          replayed_v2.metrics_v2["schema_version"] == 2 and
          replayed_v2.scenario_revision == expected_revision,
          (replayed_v2.scenario_revision, expected_revision))

    legacy_id = f"eval_legacy_{uuid.uuid4().hex}"
    episode_ids.append(legacy_id)
    legacy = EpisodeState(
        legacy_id, "missing_index_eval_v1", schema_version=1)
    legacy.save()
    replayed_v1 = replay_esc(legacy_id)
    check("legacy replay remains readable without invented path metrics",
          replayed_v1.metrics_v2["schema_version"] == 1 and
          replayed_v1.metrics_v2["path_recall_at_k"]["1"][
              "denominator"] == 0)
finally:
    for episode_id in episode_ids:
        shutil.rmtree(TRACE_DIR / episode_id, ignore_errors=True)

print("\n" + "=" * 80)
print("EVAL METRICS V2:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
