"""Offline acceptance checks for causal VERIFY/ROLLBACK/report v2."""
from __future__ import annotations

import copy
import shutil
import sys
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import explanation_runtime as xr
from agent import verification as V
from agent.episode_state import EpisodeState, InterventionAttempt
from agent.explanation import (CausalStatus, ExplanationGraph,
                               ExplanationScope, InterventionPlan)
from knowledge.causal_graph import graph as G
from sandbox.traces import TRACE_DIR, TraceStore


ok = True


def check(label, condition, detail=""):
    global ok
    passed = bool(condition)
    ok = ok and passed
    print(f"  {'PASS' if passed else 'FAIL'}  {label:<62} {detail}")


episode_id = "verify_v2_fixture"
trace_dir = TRACE_DIR / episode_id
if trace_dir.exists():
    shutil.rmtree(trace_dir)

try:
    paths = G.enumerate_causal_paths(
        ["latency_p99_up"], max_hops=4, use_learned=False)
    path = next(item for item in paths if len(item.node_ids) >= 3)
    explanation = ExplanationGraph.create(
        graph_version=G.graph_version(), episode_id=episode_id,
        observed_symptoms=[path.observed_symptom_id], candidate_paths=[path])
    for node_id in path.node_ids:
        explanation.set_node_status(node_id, CausalStatus.SUPPORTED)
    for edge_id in path.edge_ids:
        explanation.set_edge_status(edge_id, CausalStatus.SUPPORTED)
    explanation.set_path_status(path.path_id, CausalStatus.SUPPORTED)
    explanation.select_paths([path.path_id], scope=ExplanationScope.FULL)
    st = EpisodeState(episode_id=episode_id, scenario_id="verify_fixture",
                      explanation_graph=explanation)
    plan = InterventionPlan.create(
        explanation_id=explanation.explanation_id,
        explanation_revision=explanation.revision,
        selected_path_id=path.path_id,
        intervention_target=path.node_ids[0], fix_id="fixture_fix",
        intervention_kind="CORRECTIVE", action_type="create_index",
        sql="CREATE INDEX CONCURRENTLY fixture_idx ON orders(id)",
        rollback="DROP INDEX CONCURRENTLY fixture_idx",
        expected_effect_nodes=path.node_ids[1:],
        expected_effects=[
            {"metric": "cpu_usage_pct", "direction": "decrease",
             "minimum_change": 0.20, "window_seconds": 60,
             "target_node_id": path.node_ids[0]},
            {"metric": "latency_p99_ms", "direction": "decrease",
             "minimum_change": 0.20, "window_seconds": 300,
             "target_node_id": path.node_ids[-1]},
        ], rationale="fixture")
    st.intervention_plan = plan
    observer = SimpleNamespace(trace=TraceStore(episode_id))

    print("[1] execution success and observation window are hard prerequisites")
    failed_attempt = V.start_attempt(st, plan)
    V.mark_execution(failed_attempt, SimpleNamespace(
        executed=False, error="precondition failed", undo_id="",
        duration_s=0.1))
    check("an unsuccessful execution cannot enter causal verification",
          not V.ready_for_causal_verification(failed_attempt))
    check("the observation window is the largest configured effect window",
          V.observation_window(plan) == 300)
    check("execution failure is scoped to execution, not a causal node",
          V.classify_failure_scope(st, failed_attempt, {}) == "EXECUTION" and
          all(value == CausalStatus.SUPPORTED.value
              for value in explanation.node_status.values()))

    print("\n[2] every expected effect is trace-backed and tri-state")
    V.capture_pre_intervention(
        st, observer, plan,
        kpi={"p99_ms": 1000.0, "cpu_pct": 100.0}, hot_query="SELECT 1")
    attempt = V.start_attempt(st, plan)
    V.mark_execution(attempt, SimpleNamespace(
        executed=True, error="", undo_id="undo_fixture", duration_s=1.0))
    result = V.evaluate_expected_effects(
        st, observer, plan,
        kpi={"p99_ms": 900.0, "cpu_pct": 50.0}, hot_query="SELECT 1")
    check("each effect records expected/actual/met/raw_ref",
          all({"expected", "actual", "met", "raw_ref"}.issubset(effect) and
              effect["raw_ref"] for effect in result["effects"]),
          result["effects"])
    check("one unmet prediction makes the causal effect outcome REFUTED",
          result["effects_outcome"] == "REFUTED")
    inconclusive = copy.deepcopy(result)
    inconclusive["effects"][0]["met"] = None
    inconclusive["effects_outcome"] = "INCONCLUSIVE"
    check("INCONCLUSIVE effects never count as causal success",
          not V.verification_passed(
              recovered=True, effects_outcome=inconclusive["effects_outcome"],
              regression_passed=True))
    check("causal success cannot override a regression failure",
          not V.verification_passed(
              recovered=True, effects_outcome="SUPPORTED",
              regression_passed=False))

    print("\n[3] failure attribution is narrower than root-cause refutation")
    verification = {**result, "recovered": False, "regression_passed": True}
    attempt.actual = result["effects"]
    attempt.failure_scope = V.classify_failure_scope(st, attempt, verification)
    attempt.affected_edge_ids = V.affected_edges_on_path(
        st, attempt, verification)
    attempt.learnable = True
    V.apply_failure_knowledge(st, attempt, verification)
    st.record_intervention_attempt(attempt)
    check("target changed/downstream failed is scoped to a path segment",
          attempt.failure_scope == "PATH_SEGMENT" and
          attempt.affected_edge_ids and
          V.retry_phase_for_failure(attempt.failure_scope) == "INVESTIGATE",
          attempt.affected_edge_ids)
    check("only concrete expected-effect edges are refuted",
          all(explanation.edge_status[edge_id] == CausalStatus.REFUTED.value
              for edge_id in attempt.affected_edge_ids) and
          explanation.node_status[path.root_node_id] ==
          CausalStatus.SUPPORTED.value)
    check("failed path effects become trace-backed EvidenceBindings",
          any(binding.predicate_id == "intervention_expected_effect_v2" and
              binding.is_trusted()
              for binding in explanation.evidence_bindings.values()))

    intervention_only = copy.deepcopy(verification)
    for effect in intervention_only["effects"]:
        effect["met"] = False
    no_effect = copy.deepcopy(attempt)
    no_effect.ordinal = len(st.intervention_attempts) + 1
    no_effect.attempt_id = no_effect.expected_attempt_id()
    no_effect.failure_scope = V.classify_failure_scope(
        st, no_effect, intervention_only)
    check("a concrete intervention with no effect returns to PLAN",
          no_effect.failure_scope == "INTERVENTION" and
          V.retry_phase_for_failure(no_effect.failure_scope) == "PLAN")

    partial = copy.deepcopy(st)
    partial.explanation_graph.scope = ExplanationScope.PARTIAL.value
    partial.explanation_graph.unexplained_symptoms = ["unmapped alert"]
    context_attempt = copy.deepcopy(attempt)
    context_verification = {
        "effects": [{"met": True}], "effects_outcome": "SUPPORTED",
        "recovered": False, "regression_passed": True,
    }
    check("PARTIAL/unexplained KPI failure is contextual, not root refutation",
          V.classify_failure_scope(
              partial, context_attempt, context_verification) == "CONTEXT" and
          partial.explanation_graph.node_status[path.root_node_id] ==
          CausalStatus.SUPPORTED.value)

    repeat_a = copy.deepcopy(no_effect)
    repeat_b = copy.deepcopy(no_effect)
    repeat_a.ordinal, repeat_b.ordinal = 20, 21
    repeat_a.attempt_id = repeat_a.expected_attempt_id()
    repeat_b.attempt_id = repeat_b.expected_attempt_id()
    repeat_a.plan_id = repeat_b.plan_id = plan.plan_id
    repeat_a.fix_id = repeat_b.fix_id = plan.fix_id
    repeat_a.learnable = repeat_b.learnable = True
    repeat_a.failure_scope = repeat_b.failure_scope = "INTERVENTION"
    st.intervention_attempts.extend([repeat_a, repeat_b])
    check("repeating the same plan cannot lower node confidence",
          not V.node_confidence_reduction_eligible(st, path.root_node_id))

    print("\n[4] rollback knowledge is monotonic and the report is explanatory")
    revision_before = explanation.revision
    bindings_before = len(explanation.evidence_bindings)
    attempt.rollback_attempted = True
    attempt.rollback_status = "SUCCEEDED"
    attempt.rollback_message = "fixture rollback"
    st.record_intervention_attempt(attempt)
    st.rollback_decision = {
        "scope": attempt.failure_scope,
        "target_id": attempt.attempt_id,
        "affected_edge_ids": attempt.affected_edge_ids,
        "rollback_status": attempt.rollback_status,
        "intervention_plan": plan.to_dict(),
    }
    check("rollback preserves evidence, attempt history, and revision",
          len(explanation.evidence_bindings) >= bindings_before and
          explanation.revision >= revision_before and
          st.intervention_attempt_for(plan.plan_id) is not None)
    report = xr.final_report(st, escalated=True)
    check("report lists every selected path segment and scoped evidence",
          report["selected_paths"] and
          all("segments" in selected for selected in report["selected_paths"]) and
          report["key_evidence"])
    check("report includes all four P0 causes, including unreachable ones",
          len(report["p0_matrix"]) == 4 and
          {row["cause_id"] for row in report["p0_matrix"]} == {
              "autovacuum_starvation", "disk_pressure",
              "stale_replication_slot", "orphaned_prepared_transaction"},
          report["p0_matrix"])
    check("report directly answers chain/alternative/intervention/effect questions",
          set(report["answers"]) == {
              "why_this_chain", "why_not_alternatives",
              "intervention_location", "effect_proof"} and
          report["answers"]["why_this_chain"] and
          report["answers"]["intervention_location"] and
          report["answers"]["effect_proof"])
    check("intervention attempts survive EpisodeState round-trip",
          InterventionAttempt(**attempt.__dict__).attempt_id == attempt.attempt_id)
    st.save()
    restored = EpisodeState.load(episode_id)
    check("EpisodeState save/load preserves attempts and scoped evidence",
          restored.to_dict() == st.to_dict() and
          restored.intervention_attempts[-1].attempt_id ==
          st.intervention_attempts[-1].attempt_id)
finally:
    if trace_dir.exists() and trace_dir.resolve().parent == TRACE_DIR.resolve():
        shutil.rmtree(trace_dir)

print("\n" + "=" * 78)
print("VERIFY / ROLLBACK V2:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
