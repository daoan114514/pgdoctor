"""Offline acceptance checks for v2 PLAN/GATE causal binding."""
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
from agent.episode_state import EpisodeState
from agent.explanation import (CausalGateContext, CausalStatus, EvidenceBinding,
                               ExplanationScope, InterventionPlan)
from agent.loop import _typed_proposal
from agent.esc import check_explanation
from knowledge.causal_graph import graph as G
from safety import gate
from safety.gate import RemediationProposal
from sandbox.traces import TRACE_DIR, TraceStore


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<58} {detail}")


def binding(store: TraceStore, *, evidence_type: str, predicate_id: str,
            value: dict, nodes: list[str], result: str = "SUPPORTS",
            edges: list[str] | None = None):
    ref = store.record("fixture", {"evidence_type": evidence_type},
                       json.dumps(value), value)
    return EvidenceBinding.create(
        episode_id=store.episode_id, raw_ref=ref,
        evidence_type=evidence_type, status="OBSERVED", observed_at=1_800_000_000,
        predicate_id=predicate_id, predicate_result=result,
        structured_value=value, target_node_ids=nodes,
        target_edge_ids=edges or [],
        fresh_until=1_900_000_000)


def missing_index_state(episode_id: str):
    store = TraceStore(episode_id)
    paths = G.enumerate_causal_paths(["latency_p99_up"], use_learned=False)
    path = next(path for path in paths if path.node_ids == [
        "missing_index", "latency_p99_up"])
    explanation = G.merge_paths(
        [path], episode_id=episode_id, observed_symptoms=["latency_p99_up"])
    for item in (
        binding(store, evidence_type="explain_seq_scan",
                predicate_id="explain_seq_scan_v2",
                value={"scan_types": ["Seq Scan on orders"],
                       "rows_removed_by_filter": 100_000},
                nodes=["missing_index"], edges=[path.edge_ids[0]]),
        binding(store, evidence_type="index_existence",
                predicate_id="index_existence_v2",
                value={"inventory_collected": True, "indexes": []},
                nodes=["missing_index"]),
        binding(store, evidence_type="counterfactual_index",
                predicate_id="counterfactual_index_v2",
                value={"create_sql": "CREATE INDEX ON orders(user_id, status)",
                       "would_be_used": True, "trivial_baseline": False},
                nodes=["create_covering_index"]),
    ):
        explanation.add_evidence_binding(item)
    explanation.set_node_status("missing_index", CausalStatus.SUPPORTED)
    explanation.set_node_status("latency_p99_up", CausalStatus.SUPPORTED)
    explanation.set_edge_status(path.edge_ids[0], CausalStatus.SUPPORTED)
    explanation.select_paths([path.path_id], unexplained_symptoms=[],
                             scope=ExplanationScope.FULL)
    st = EpisodeState(episode_id, "missing_index_fixture")
    st.explanation_graph = explanation
    report = check_explanation(st, persist=False)
    assert report["verdict"] == "SUFFICIENT", report
    st.esc_reports.append(report)
    plan = xr.create_intervention_plan(
        st, action_type="create_index",
        sql=("CREATE INDEX CONCURRENTLY idx_orders_user_status "
             "ON orders(user_id, status)"),
        rollback="DROP INDEX CONCURRENTLY idx_orders_user_status",
        rationale="create the counterfactually validated covering index",
        selected_path_id=path.path_id, fix_id="create_covering_index",
        intervention_target="missing_index")
    st.proposal = {
        "action_type": plan.action_type, "sql": plan.sql,
        "rollback": plan.rollback, "rationale": plan.rationale,
        "selected_path_id": plan.selected_path_id,
        "fix_id": plan.fix_id,
        "intervention_target": plan.intervention_target,
    }
    return st, store, path


episode_id = f"causal_gate_v2_{uuid.uuid4().hex}"
try:
    print("[1] PLAN is graph-bound and validates structured effects/preconditions")
    st, store, path = missing_index_state(episode_id)
    plan = st.intervention_plan
    check("plan target and fix are attached to the selected path",
          plan.selected_path_id == path.path_id and
          plan.intervention_target == "missing_index" and
          plan.fix_id == "create_covering_index")
    check("plan stores auditable successful precondition decisions",
          plan.precondition_results and all(
              item["satisfied"] for item in plan.precondition_results),
          plan.precondition_results)
    check("expected effects are metric/direction/threshold/window objects",
          all({"metric", "direction", "minimum_change", "window_seconds"}
              <= set(item) for item in plan.expected_effects))

    incomplete_effect_rejected = False
    try:
        InterventionPlan.create(
            explanation_id="e", explanation_revision=1, selected_path_id="p",
            intervention_target="x", fix_id="f",
            intervention_kind="CORRECTIVE", action_type="create_index",
            sql="CREATE INDEX i ON t(c)", rollback="DROP INDEX i",
            expected_effect_nodes=["s"], expected_effects=[{"metric": "m"}],
            rationale="fixture")
    except ValueError as exc:
        incomplete_effect_rejected = "missing fields" in str(exc)
    check("incomplete expected-effect schemas are rejected",
          incomplete_effect_rejected)

    wrong_definition = copy.deepcopy(st)
    wrong_definition.intervention_plan = None
    wrong_precondition_rejected = False
    try:
        xr.create_intervention_plan(
            wrong_definition, action_type="create_index",
            sql="CREATE INDEX CONCURRENTLY idx_wrong ON orders(total)",
            rollback="DROP INDEX CONCURRENTLY idx_wrong",
            rationale="different definition",
            selected_path_id=path.path_id, fix_id="create_covering_index",
            intervention_target="missing_index")
    except ValueError as exc:
        wrong_precondition_rejected = "preconditions" in str(exc)
    check("counterfactual evidence cannot bless a different index definition",
          wrong_precondition_rejected)

    containment_claim_rejected = False
    try:
        InterventionPlan.create(
            explanation_id="e", explanation_revision=1, selected_path_id="p",
            intervention_target="x", fix_id="f",
            intervention_kind="CONTAINMENT", action_type="session_control",
            sql="SELECT pg_terminate_backend(42)", rollback="IRREVERSIBLE",
            expected_effect_nodes=["s"],
            expected_effects=[{"metric": "blocked", "direction": "decrease",
                               "minimum_change": 1, "window_seconds": 30}],
            rationale="root cause eliminated")
    except ValueError as exc:
        containment_claim_rejected = "root-cause" in str(exc)
    check("containment cannot claim that it eliminated the root cause",
          containment_claim_rejected)

    print("\n[2] GATE context rejects stale, unrelated, expired and spoofed state")
    context = xr.build_gate_context(st, model_payload=st.proposal)
    typed = _typed_proposal(st)
    check("trusted context uses target/fix bindings, not scratchpad tails",
          context.evidence_refs == plan.evidence_refs and
          typed.evidence_refs == context.evidence_refs)

    outside = copy.deepcopy(plan)
    outside.intervention_target = "stale_statistics"
    outside_path_rejected = False
    try:
        CausalGateContext.build(st.explanation_graph, outside,
                                context.esc_report_id)
    except ValueError as exc:
        outside_path_rejected = "outside" in str(exc)
    check("safe SQL with a target outside the selected path is rejected",
          outside_path_rejected)

    stale = copy.deepcopy(st)
    stale.explanation_graph.set_node_status(
        "missing_index", CausalStatus.INCONCLUSIVE)
    try:
        xr.build_gate_context(stale, model_payload=stale.proposal)
        stale_result = None
    except xr.CausalGateError as exc:
        stale_result = (exc.reason_code, exc.retry_phase)
    check("stale explanation revisions route to INVESTIGATE",
          stale_result == ("STALE_EXPLANATION", "INVESTIGATE"), stale_result)

    expired = copy.deepcopy(st)
    for item in expired.explanation_graph.evidence_bindings.values():
        item.fresh_until = 0
    try:
        xr.build_gate_context(expired, model_payload=expired.proposal)
        expired_result = None
    except xr.CausalGateError as exc:
        expired_result = (exc.reason_code, exc.retry_phase)
    check("expired path evidence routes to INVESTIGATE",
          expired_result == ("EVIDENCE_EXPIRED", "INVESTIGATE"), expired_result)

    spoofed = {**st.proposal, "root_cause": "stale_statistics",
               "esc_verdict": "SUFFICIENT", "evidence_refs": ["fake"]}
    try:
        xr.build_gate_context(st, model_payload=spoofed)
        spoof_result = None
    except xr.CausalGateError as exc:
        spoof_result = (exc.reason_code, exc.retry_phase)
    check("model conflicts with trusted causal fields are rejected",
          spoof_result == ("CAUSAL_BINDING_INVALID", "PLAN"), spoof_result)

    print("\n[3] Gate decisions carry deterministic retry phases")
    with patch.object(gate, "_table_rows", return_value=100):
        decision = gate.assess(typed)
    check("a fully bound safe proposal reaches the existing safety gate",
          decision.approved and decision.reason_code == "APPROVED",
          (decision.tier, decision.reasons))

    bad_sql = copy.deepcopy(typed)
    bad_sql.action_type = "vacuum_analyze"
    sql_decision = gate.assess(bad_sql)
    check("AST/action mismatch returns SQL_INVALID -> PLAN",
          (sql_decision.reason_code, sql_decision.retry_phase) ==
          ("SQL_INVALID", "PLAN"))

    bad_rollback = copy.deepcopy(typed)
    bad_rollback.rollback = ""
    rollback_decision = gate.assess(bad_rollback)
    check("rollback shape errors return ROLLBACK_INVALID -> PLAN",
          (rollback_decision.reason_code, rollback_decision.retry_phase) ==
          ("ROLLBACK_INVALID", "PLAN"))

    manual = gate.assess(RemediationProposal(
        action_type="storage_management", sql="ESCALATE", rollback="IRREVERSIBLE",
        root_cause="disk_pressure", fix_id="remediate_disk_capacity"))
    check("manual/escalate-only fixes return MANUAL/P0 -> ESCALATE",
          not manual.approved and manual.retry_phase == "ESCALATE",
          (manual.reason_code, manual.retry_phase))

    with patch.object(gate, "_table_rows", return_value=2_000_000):
        nonconcurrent = gate.assess(RemediationProposal(
            action_type="create_index", sql="CREATE INDEX i ON orders(c)",
            rollback="DROP INDEX i", root_cause="missing_index",
            fix_id="create_covering_index"))
    check("graph AUTO cannot lower AST/blast-radius denial",
          not nonconcurrent.approved and
          nonconcurrent.reason_code == "SQL_INVALID")

    print("\n[4] Manual plans remain evidence-bound and contain no SQL")
    manual_id = f"{episode_id}_manual"
    manual_store = TraceStore(manual_id)
    disk_path = next(path for path in G.enumerate_causal_paths(
        ["disk_growing"], use_learned=False)
        if path.node_ids == ["disk_pressure", "disk_growing"])
    disk_explanation = G.merge_paths(
        [disk_path], episode_id=manual_id, observed_symptoms=["disk_growing"])
    disk_explanation.add_evidence_binding(binding(
        manual_store, evidence_type="disk_usage", predicate_id="disk_usage_v2",
        value={"used_pct": 96, "free_bytes": 1024}, nodes=["disk_pressure"]))
    disk_explanation.set_node_status("disk_pressure", CausalStatus.SUPPORTED)
    disk_explanation.set_node_status("disk_growing", CausalStatus.SUPPORTED)
    disk_explanation.set_edge_status(
        disk_path.edge_ids[0], CausalStatus.SUPPORTED)
    disk_explanation.select_paths([disk_path.path_id], unexplained_symptoms=[],
                                  scope=ExplanationScope.FULL)
    disk_state = EpisodeState(manual_id, "disk_fixture")
    disk_state.explanation_graph = disk_explanation
    manual_plan = xr.create_manual_intervention_plan(disk_state)
    check("manual escalation persists a plan with evidence and no SQL",
          manual_plan.manual and manual_plan.execution == "escalate_only" and
          not manual_plan.sql and bool(manual_plan.evidence_refs),
          manual_plan.to_dict())

finally:
    for path in TRACE_DIR.glob(f"{episode_id}*"):
        if path.is_dir() and path.resolve().parent == TRACE_DIR.resolve():
            shutil.rmtree(path)

print("\n" + "=" * 78)
print("CAUSAL GATE V2:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
