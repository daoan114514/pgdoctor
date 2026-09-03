"""Independent acceptance checks for system-owned causal GATE context."""
from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import explanation_runtime as runtime
from agent.episode_state import EpisodeState
from agent.esc import check_explanation
from agent.explanation import EvidenceBinding, InterventionPlan
from knowledge.causal_graph import graph as G
from sandbox.traces import TRACE_DIR, TraceStore


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<58} {detail}")


def add(store, explanation, path, evidence_type, predicate_id, value, *,
        edge=False, ordinal=0):
    ref = store.record("fixture", {"ordinal": ordinal}, json.dumps(value), value)
    binding = EvidenceBinding.create(
        episode_id=store.episode_id, raw_ref=ref,
        evidence_type=evidence_type, status="OBSERVED",
        observed_at=time.time(), predicate_id=predicate_id,
        predicate_result="SUPPORTS", structured_value=value,
        target_node_ids=["missing_index"],
        target_edge_ids=[path.edge_ids[0]] if edge else [],
        fresh_until=time.time() + 600)
    explanation.add_evidence_binding(binding)
    return binding


def state_with_plan(episode_id):
    store = TraceStore(episode_id)
    path = next(path for path in G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=False)
        if path.node_ids == ["missing_index", "latency_p99_up"])
    explanation = G.merge_paths(
        [path], episode_id=episode_id,
        observed_symptoms=["latency_p99_up"])
    add(store, explanation, path, "explain_seq_scan",
        "explain_seq_scan_v2",
        {"scan_types": ["Seq Scan"], "rows_removed_by_filter": 100000},
        edge=True)
    add(store, explanation, path, "index_existence", "index_existence_v2",
        {"inventory_collected": True, "indexes": []}, ordinal=1)
    counterfactual_value = {
        "would_be_used": True, "trivial_baseline": False,
        "create_sql": "CREATE INDEX ON orders(user_id, status)"}
    counterfactual = store.record(
        "simulate_index", {"ordinal": 2},
        json.dumps(counterfactual_value), counterfactual_value)
    explanation.add_evidence_binding(EvidenceBinding.create(
        episode_id=episode_id, raw_ref=counterfactual,
        evidence_type="counterfactual_index", status="OBSERVED",
        observed_at=time.time(), predicate_id="counterfactual_index_v2",
        predicate_result="SUPPORTS",
        structured_value=counterfactual_value,
        target_node_ids=["create_covering_index"],
        fresh_until=time.time() + 600))
    explanation.set_node_status("latency_p99_up", "SUPPORTED")
    explanation.set_path_status(path.path_id, "SUPPORTED")
    explanation.select_paths([path.path_id], unexplained_symptoms=[])
    state = EpisodeState(episode_id, "controlled_gate")
    state.explanation_graph = explanation
    report = check_explanation(state, persist=False)
    assert report["verdict"] == "SUFFICIENT", report
    state.esc_reports.append(report)
    plan = runtime.create_intervention_plan(
        state, action_type="create_index",
        sql=("CREATE INDEX CONCURRENTLY idx_orders_user_status "
             "ON orders(user_id, status)"),
        rollback="DROP INDEX CONCURRENTLY idx_orders_user_status",
        rationale="controlled graph-bound plan",
        selected_path_id=path.path_id, fix_id="create_covering_index",
        intervention_target="missing_index")
    return state, store, path, plan


episode_ids = []
try:
    print("[1] Valid context is derived only from persisted state")
    episode_id = f"gate_context_{uuid.uuid4().hex}"
    episode_ids.append(episode_id)
    state, _store, path, plan = state_with_plan(episode_id)
    context = runtime.build_gate_context(state)
    check("context binds explanation revision/path/target/fix",
          context.explanation_id == state.explanation_graph.explanation_id and
          context.explanation_revision == state.explanation_graph.revision and
          context.selected_path_ids == [path.path_id] and
          context.intervention_target == "missing_index" and
          context.fix_id == "create_covering_index")
    check("evidence refs come from current target bindings",
          bool(context.evidence_refs) and
          set(context.evidence_refs).issubset(
              {binding.raw_ref for binding in
               state.explanation_graph.evidence_bindings.values()}))
    check("effects are bounded to the selected path downstream",
          set(context.expected_effect_nodes).issubset(
              G.downstream_on_path(path.path_id, "missing_index",
                                   state.explanation_graph)))

    print("\n[2] Model-owned trusted fields and path-external targets fail")
    forged = False
    try:
        runtime.build_gate_context(
            state, model_payload={"evidence_refs": ["trace://forged/step_001"]})
    except runtime.CausalGateError as exc:
        forged = (exc.reason_code == "CAUSAL_BINDING_INVALID" and
                  exc.retry_phase == "PLAN")
    check("forged trusted evidence refs are rejected", forged)

    wrong = InterventionPlan.create(
        explanation_id=state.explanation_graph.explanation_id,
        explanation_revision=state.explanation_graph.revision,
        selected_path_id=path.path_id,
        intervention_target="stale_statistics", fix_id="analyze_table",
        intervention_kind="CORRECTIVE", action_type="vacuum_analyze",
        sql="ANALYZE orders", rollback="NO_ROLLBACK_NEEDED",
        expected_effect_nodes=["latency_p99_up"],
        expected_effects=[{
            "metric": "p99_ms", "direction": "decrease",
            "minimum_change": 0.1, "window_seconds": 30}],
        rationale="SQL is harmless but the target is outside the selected path")
    state.intervention_plan = wrong
    outside = False
    try:
        runtime.build_gate_context(state)
    except runtime.CausalGateError as exc:
        outside = (exc.reason_code == "CAUSAL_BINDING_INVALID" and
                   exc.retry_phase == "PLAN")
    check("safe SQL with a path-external target is rejected", outside)

    print("\n[3] Revision and evidence freshness route back to INVESTIGATE")
    stale_id = f"gate_stale_{uuid.uuid4().hex}"
    episode_ids.append(stale_id)
    stale, _store, stale_path, _plan = state_with_plan(stale_id)
    stale.explanation_graph.set_edge_status(stale_path.edge_ids[0], "REFUTED")
    stale_revision = False
    try:
        runtime.build_gate_context(stale)
    except runtime.CausalGateError as exc:
        stale_revision = (exc.reason_code == "STALE_EXPLANATION" and
                          exc.retry_phase == "INVESTIGATE")
    check("old plan cannot bind a newer explanation revision", stale_revision)

    expired_id = f"gate_expired_{uuid.uuid4().hex}"
    episode_ids.append(expired_id)
    expired, _store, _path, _plan = state_with_plan(expired_id)
    for binding in expired.explanation_graph.evidence_bindings.values():
        binding.fresh_until = time.time() - 1
    evidence_expired = False
    try:
        runtime.build_gate_context(expired)
    except runtime.CausalGateError as exc:
        evidence_expired = (exc.reason_code == "EVIDENCE_EXPIRED" and
                            exc.retry_phase == "INVESTIGATE")
    check("evidence expiring after ESC returns to INVESTIGATE",
          evidence_expired)
finally:
    for episode_id in episode_ids:
        shutil.rmtree(TRACE_DIR / episode_id, ignore_errors=True)

print("\n" + "=" * 80)
print("CAUSAL GATE CONTEXT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
