"""Offline acceptance checks for the thirteen-phase explanation workflow."""
from __future__ import annotations

import copy
import json
import shutil
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import explanation_runtime as xr
from agent.episode_state import EpisodeState, RemediationAttempt
from agent.explanation import CausalStatus
from agent.loop import _gate_denial_target, _rollback_route, run_episode
from agent.policy import Policy
from agent.state_machine import Phase, StateMachine
from sandbox.traces import TRACE_DIR, TraceStore


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<52} {detail}")


def record(st: EpisodeState, store: TraceStore, evidence_type: str,
           structured_value, bears_on: list[str]) -> str:
    raw_ref = store.record(
        "fixture", {"evidence_type": evidence_type},
        json.dumps(structured_value, ensure_ascii=False), structured_value)
    st.note(
        "fixture", evidence_type, "deliberately irrelevant natural-language note",
        raw_ref=raw_ref, bears_on=bears_on,
        structured_value=structured_value)
    return raw_ref


episode_id = f"mape_k_v2_{uuid.uuid4().hex}"
trace_dir = TRACE_DIR / episode_id
try:
    print("[1] DONE is the only terminal state")
    for phase in (Phase.REPORT, Phase.ESCALATE):
        state = EpisodeState(f"terminal_{phase.value}", "fixture",
                             phase=phase.value)
        machine = StateMachine(state)
        check(f"{phase.value} remains non-terminal", not machine.terminal())
        with patch.object(EpisodeState, "save", lambda self: None):
            machine.goto(Phase.DONE, "fixture finalization")
        check(f"{phase.value} can transition to DONE", machine.terminal())

    print("\n[2] HYPOTHESIZE recalls paths without confirming them")
    st = EpisodeState(episode_id, "connection_fixture")
    st.symptoms = ["连接数逼近上限"]
    explanation = xr.recall_explanation(st, use_learned=False)
    paths = {tuple(path.node_ids): path for path in explanation.candidate_paths}
    short_nodes = ("connection_exhaustion", "conn_near_limit")
    long_nodes = ("long_idle_transaction", "connection_exhaustion",
                  "conn_near_limit")
    check("one-hop and multi-hop branches are recalled",
          short_nodes in paths and long_nodes in paths, sorted(paths))
    check("recall leaves every causal path unconfirmed",
          all(path.status == "UNTESTED" for path in paths.values()))
    check("recall does not select a root string",
          not explanation.selected_path_ids and st.claimed_fault_class is None)

    print("\n[3] Structured trace evidence advances concrete path segments")
    store = TraceStore(episode_id)
    connection_ref = record(
        st, store, "connection_count", {"near_limit": True},
        ["connection_exhaustion"])
    first_bindings = xr.bind_evidence(st)
    short_path = paths[short_nodes]
    long_path = paths[long_nodes]
    upstream_edge = long_path.edge_ids[0]
    downstream_edge = long_path.edge_ids[-1]
    check("trace evidence is bound through predicates", bool(first_bindings))
    check("nearest segment becomes supported first",
          explanation.edge_status[downstream_edge] == "SUPPORTED" and
          explanation.edge_status.get(upstream_edge, "UNTESTED") == "UNTESTED")
    check("short explanation can close while upstream path stays open",
          short_path.status == "SUPPORTED" and long_path.status == "UNTESTED")

    record(st, store, "idle_in_transaction", {"idle_in_transaction": 8},
           ["long_idle_transaction"])
    xr.bind_evidence(st)
    check("missing required evidence keeps the long path open",
          long_path.status == "UNTESTED")
    session_ref = record(st, store, "session_wait_profile", [{
        "pid": 4242,
        "state": "idle in transaction",
        "wait_event": "Lock",
        "transaction_age_seconds": 900,
        "role": "app_user",
        "blocking_impact": 3,
        "identity_rechecked": True,
        "is_current_diagnostic_connection": False,
        "is_system_or_diagnostic": False,
    }], ["long_idle_transaction"])
    xr.bind_evidence(st)
    check("all required structured predicates support the long path",
          long_path.status == "SUPPORTED" and
          explanation.edge_status[upstream_edge] == "SUPPORTED")
    check("natural-language notes never become trusted bindings",
          all(binding.raw_ref for binding in
              explanation.evidence_bindings.values()))

    print("\n[4] DIAGNOSE selects the upstream-consistent minimal subgraph")
    selected = xr.select_minimal_explanation(st)
    check("supported upstream extension subsumes its short suffix",
          selected == [long_path.path_id], selected)
    check("selected roots are derived from selected paths",
          explanation.selected_root_causes == ["long_idle_transaction"] and
          st.claimed_fault_class == "long_idle_transaction")
    check("the mapped symptom is fully explained",
          explanation.scope == "FULL" and not explanation.unexplained_symptoms)

    print("\n[5] ESC consumes only the persisted explanation graph")
    esc_report = xr.assess_explanation(st)
    st.hypothesis_candidates = ["model_invented_root"]
    st.ledger.clear()
    st.claimed_fault_class = "model_invented_root"
    repeated_report = xr.assess_explanation(st)
    check("v2 ESC is sufficient for the selected supported path",
          esc_report["verdict"] == "SUFFICIENT", esc_report)
    check("v1 projections cannot change the v2 ESC result",
          repeated_report["verdict"] == esc_report["verdict"] and
          repeated_report["selected_path_ids"] == selected)
    partial_state = copy.deepcopy(st)
    partial_state.explanation_graph.observed_symptoms.append("unmapped_signal")
    partial_state.explanation_graph.unexplained_symptoms.append("unmapped_signal")
    partial_report = xr.assess_explanation(partial_state)
    check("candidate coverage gaps make ESC insufficient",
          partial_report["verdict"] == "INSUFFICIENT" and
          partial_report["coverage_missing"] == ["unmapped_signal"])
    xr.sync_v1_projection(st)

    print("\n[6] PLAN and GATE bind the current explanation revision")
    plan = xr.create_intervention_plan(
        st, action_type="session_control",
        sql="SELECT pg_terminate_backend(4242)", rollback="IRREVERSIBLE",
        rationale="terminate the specifically verified idle transaction",
        selected_path_id=long_path.path_id,
        fix_id="terminate_idle_transaction",
        intervention_target="long_idle_transaction")
    st.esc_reports.append(esc_report)
    context = xr.build_gate_context(st)
    check("GATE context is system-derived and trace-bound",
          context.explanation_revision == explanation.revision and
          session_ref in context.evidence_refs and
          connection_ref not in context.evidence_refs and
          context.selected_path_ids == selected)

    stale_state = copy.deepcopy(st)
    stale_state.explanation_graph.set_node_status(
        "conn_near_limit", CausalStatus.INCONCLUSIVE)
    stale_state.esc_reports.append({
        **esc_report,
        "report_id": "esc_report_current_revision_fixture",
        "explanation_revision": stale_state.explanation_graph.revision,
    })
    stale_rejected = False
    try:
        xr.build_gate_context(stale_state)
    except ValueError as exc:
        stale_rejected = "stale" in str(exc)
    check("GATE rejects a plan from an old explanation revision", stale_rejected)

    expired_state = copy.deepcopy(st)
    for binding in expired_state.explanation_graph.evidence_bindings.values():
        binding.fresh_until = 0.0
    expired_rejected = False
    try:
        xr.build_gate_context(expired_state)
    except ValueError as exc:
        expired_rejected = "fresh" in str(exc)
    check("GATE rejects missing or expired trusted evidence", expired_rejected)
    check("evidence denials return to INVESTIGATE",
          _gate_denial_target(["target evidence is expired"]) is
          Phase.INVESTIGATE)
    check("SQL and rollback shape denials return to PLAN",
          _gate_denial_target(["rollback statement is invalid"]) is Phase.PLAN)

    print("\n[7] ROLLBACK refutes only the justified scope")
    before_status = long_path.status
    st.record_attempt(RemediationAttempt(
        root_cause=plan.intervention_target, sql=plan.sql,
        predicted={}, actual={},
        verdict="FAILED_NO_IMPROVEMENT", rolled_back=True,
        counts_against_root_cause=False))
    target, scope = _rollback_route(st, plan, ["连接数仍逼近上限"])
    check("failed intervention returns to PLAN without refuting the path",
          (target, scope) == (Phase.PLAN, "INTERVENTION") and
          long_path.status == before_status and
          st.attempts[-1].counts_against_root_cause is False)
    report_state = copy.deepcopy(st)
    report_state.rollback_decision = {
        "scope": "INTERVENTION", "intervention_plan": plan.to_dict()}
    report_state.intervention_plan = None
    rolled_back_report = xr.final_report(report_state, escalated=True)
    check("final escalation retains the rolled-back intervention kind",
          rolled_back_report["intervention"]["intervention_kind"] ==
          "CONTAINMENT")

    refuted_state = copy.deepcopy(st)
    refuted_path = refuted_state.explanation_graph.path_map()[long_path.path_id]
    refuted_state.explanation_graph.set_path_status(
        refuted_path.path_id, CausalStatus.REFUTED)
    target, scope = _rollback_route(
        refuted_state, refuted_state.intervention_plan,
        ["连接数仍逼近上限"])
    check("a refuted path segment returns to INVESTIGATE",
          (target, scope) == (Phase.INVESTIGATE, "PATH_SEGMENT"))

    new_symptom_state = copy.deepcopy(st)
    target, scope = _rollback_route(
        new_symptom_state, new_symptom_state.intervention_plan,
        ["连接数仍逼近上限", "磁盘使用率继续增长"])
    check("a newly mapped symptom returns to HYPOTHESIZE",
          (target, scope) == (Phase.HYPOTHESIZE, "ROOT_SET") and
          "磁盘使用率继续增长" in new_symptom_state.symptoms)

    print("\n[8] REPORT and ESCALATE persist before deterministic DONE")

    class _Env:
        episode_id = f"{episode_id}_loop"
        spec = {"id": "terminal_loop_fixture",
                "workload": {"hot_query": "SELECT 1"}}
        applied_sql: list[str] = []

        @staticmethod
        def observe():
            return object()

    class _Observation:
        alert = "connection alert"
        healthy_kpi = {"p99_ms": 100, "cpu_pct": 20, "errors": 0}
        current_kpi = {"p99_ms": 100, "cpu_pct": 20, "errors": 0}

    class _EscalatingPolicy(Policy):
        name = "terminal-fixture"

        def run_phase(self, phase, tb, state, ctx):
            return Phase.ESCALATE

    with patch.object(EpisodeState, "save", lambda self: None):
        result, final_state = run_episode(
            _Env(), _Observation(), _EscalatingPolicy(),
            use_cases=False, quiet=True)
    check("loop reaches DONE after ESCALATE finalization",
          result.final_phase == "DONE" and final_state.finished)
    check("the final escalation report is persisted before DONE",
          final_state.final_report.get("kind") == "ESCALATION" and
          any(src == "ESCALATE" and dst == "DONE"
              for src, dst, _reason in result.transitions),
          result.transitions)
finally:
    if trace_dir.exists() and trace_dir.resolve().parent == TRACE_DIR.resolve():
        shutil.rmtree(trace_dir)

print("\n" + "=" * 76)
print("MAPE-K V2:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
