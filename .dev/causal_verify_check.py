"""Independent acceptance checks for causal VERIFY and scoped failure learning."""
from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import verification
from agent.episode_state import EpisodeState, InterventionAttempt
from agent.explanation import InterventionPlan
from knowledge.causal_graph import graph as G
from sandbox.traces import TRACE_DIR, TraceStore


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<58} {detail}")


class Observer:
    def __init__(self, episode_id):
        self.trace = TraceStore(episode_id)


def fixture(episode_id, *, scope="FULL", unexplained=None):
    path = next(path for path in G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=False)
        if path.node_ids == ["missing_index", "latency_p99_up"])
    explanation = G.merge_paths(
        [path], episode_id=episode_id,
        observed_symptoms=["latency_p99_up"])
    explanation.set_node_status("missing_index", "SUPPORTED")
    explanation.set_node_status("latency_p99_up", "SUPPORTED")
    explanation.set_edge_status(path.edge_ids[0], "SUPPORTED")
    explanation.set_path_status(path.path_id, "SUPPORTED")
    explanation.select_paths(
        [path.path_id], unexplained_symptoms=unexplained or [], scope=scope)
    state = EpisodeState(episode_id, "controlled_verify")
    state.explanation_graph = explanation
    plan = InterventionPlan.create(
        explanation_id=explanation.explanation_id,
        explanation_revision=explanation.revision,
        selected_path_id=path.path_id,
        intervention_target="missing_index", fix_id="create_covering_index",
        intervention_kind="CORRECTIVE", action_type="create_index",
        sql="CREATE INDEX CONCURRENTLY i ON orders(user_id, status)",
        rollback="DROP INDEX CONCURRENTLY i",
        expected_effect_nodes=["latency_p99_up"],
        expected_effects=[{
            "target_node_id": "latency_p99_up", "metric": "p99_ms",
            "direction": "decrease", "minimum_change": 0.2,
            "window_seconds": 1}], rationale="controlled effect")
    state.intervention_plan = plan
    return state, path, plan


episode_ids = []
try:
    print("[1] VERIFY records concrete downstream predictions")
    episode_id = f"verify_effect_{uuid.uuid4().hex}"
    episode_ids.append(episode_id)
    state, path, plan = fixture(episode_id)
    observer = Observer(episode_id)
    verification.capture_pre_intervention(
        state, observer, plan, kpi={"p99_ms": 100.0})
    result = verification.evaluate_expected_effects(
        state, observer, plan, kpi={"p99_ms": 60.0})
    effect = result["effects"][0]
    check("effect row includes expected/actual/met/raw_ref",
          {"expected", "actual", "met", "raw_ref"} <= set(effect) and
          effect["actual"] == 60.0 and effect["met"] is True and
          effect["raw_ref"].startswith(f"trace://{episode_id}/"))
    check("expected effect is limited to selected path downstream",
          effect["target_node_id"] in G.downstream_on_path(
              path.path_id, plan.intervention_target,
              state.explanation_graph))
    check("causal success cannot replace safety regression",
          not verification.verification_passed(
              recovered=True, effects_outcome="SUPPORTED",
              regression_passed=False))

    print("\n[2] Failure scope distinguishes execution/intervention/path")
    attempt = InterventionAttempt.create(
        episode_id=episode_id, plan=plan, ordinal=1)
    attempt.execution_status = "FAILED"
    check("execution failure never refutes a causal node",
          verification.classify_failure_scope(
              state, attempt, {"effects": []}) == "EXECUTION")
    attempt.execution_status = "SUCCEEDED"
    intervention_failure = {
        "effects_outcome": "REFUTED", "recovered": False,
        "regression_passed": True,
        "effects": [{"target_node_id": "missing_index", "met": False}]}
    check("failed concrete fix is intervention-scoped",
          verification.classify_failure_scope(
              state, attempt, intervention_failure) == "INTERVENTION")
    path_failure = {
        "effects_outcome": "REFUTED", "recovered": False,
        "regression_passed": True,
        "effects": [
            {"target_node_id": "missing_index", "met": True},
            {"target_node_id": "latency_p99_up", "met": False,
             "metric": "p99_ms", "raw_ref": effect["raw_ref"]},
        ]}
    attempt.failure_scope = verification.classify_failure_scope(
        state, attempt, path_failure)
    attempt.affected_edge_ids = verification.affected_edges_on_path(
        state, attempt, path_failure)
    check("target change without downstream change refutes path segment",
          attempt.failure_scope == "PATH_SEGMENT" and
          attempt.affected_edge_ids == path.edge_ids)
    verification.apply_failure_knowledge(state, attempt, path_failure)
    check("path failure updates only affected edge/path",
          state.explanation_graph.edge_status[path.edge_ids[0]] == "REFUTED" and
          state.explanation_graph.node_status["missing_index"] != "REFUTED")

    print("\n[3] PARTIAL or independent context does not injure the root")
    partial_id = f"verify_partial_{uuid.uuid4().hex}"
    episode_ids.append(partial_id)
    partial, _path, partial_plan = fixture(
        partial_id, scope="PARTIAL", unexplained=["unmapped_signal"])
    partial_attempt = InterventionAttempt.create(
        episode_id=partial_id, plan=partial_plan, ordinal=1)
    partial_attempt.execution_status = "SUCCEEDED"
    context_failure = {
        "effects_outcome": "SUPPORTED", "recovered": False,
        "regression_passed": True,
        "effects": [{"target_node_id": "latency_p99_up", "met": True}]}
    scope = verification.classify_failure_scope(
        partial, partial_attempt, context_failure)
    partial_attempt.failure_scope = scope
    partial_attempt.outcome = "FAILED"
    partial_attempt.learnable = True
    partial.record_intervention_attempt(partial_attempt)
    check("unrecovered PARTIAL episode is a context failure",
          scope == "CONTEXT")
    check("PARTIAL failure cannot lower node-level confidence",
          not verification.node_confidence_reduction_eligible(
              partial, "missing_index") and
          partial.explanation_graph.node_status["missing_index"] != "REFUTED")
finally:
    for episode_id in episode_ids:
        shutil.rmtree(TRACE_DIR / episode_id, ignore_errors=True)

print("\n" + "=" * 80)
print("CAUSAL VERIFY:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
