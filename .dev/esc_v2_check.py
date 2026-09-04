"""Offline acceptance checks for explanation-subgraph ESC v2."""
from __future__ import annotations

import copy
import json
import shutil
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import esc
from agent import explanation_runtime as xr
from agent.episode_state import EpisodeState, EvidenceStatus, Verdict
from agent.explanation import (EvidenceBinding, ExplanationGraph,
                               ExplanationScope, P0Obligation,
                               PredicateResult)
from knowledge.causal_graph import graph as G
from knowledge.evidence_predicates import PredicateContext, evaluate
from safety import gate
from safety.gate import RemediationProposal
from sandbox.traces import TRACE_DIR, TraceStore


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<58} {detail}")


def find_path(node_ids: list[str]):
    symptom = node_ids[-1]
    return next(
        path for path in G.enumerate_causal_paths(
            [symptom], use_learned=False)
        if path.node_ids == node_ids
    )


VALUES = {
    "connection_count": {"near_limit": True},
    "explain_seq_scan": {
        "scan_types": ["Seq Scan"], "rows_removed_by_filter": 50000,
    },
    "index_existence": {"indexes": []},
    "row_estimate_deviation": {"max_ratio": 100.0},
    "disk_usage": {"used_pct": 92.0},
}


def add_binding(st: EpisodeState, store: TraceStore, *, evidence_type: str,
                value: dict | list, node_ids: list[str] | None = None,
                edge_ids: list[str] | None = None, raw_ref: str = "",
                status: str = "OBSERVED", result: str = "",
                require_trace: bool = True,
                window_start: float | None = None,
                window_end: float | None = None,
                source_epoch: str = "") -> EvidenceBinding:
    graph = G.load()
    predicate_id = str(graph.nodes[evidence_type]["predicate_id"])
    ref = raw_ref or store.record(
        "fixture", {"evidence_type": evidence_type},
        json.dumps(value), value)
    target_kind = "EDGE" if edge_ids else "NODE"
    target_ids = list(edge_ids or node_ids or [])
    if not result:
        result = evaluate(
            predicate_id, value,
            context=PredicateContext(
                target_kind=target_kind, target_ids=tuple(target_ids),
                collection_status=status, window_start=window_start,
                window_end=window_end, source_epoch=source_epoch),
        ).result
    binding = EvidenceBinding.create(
        episode_id=st.episode_id, raw_ref=ref,
        evidence_type=evidence_type, status=status,
        observed_at=time.time(), predicate_id=predicate_id,
        predicate_result=result, structured_value=value,
        target_node_ids=node_ids or [], target_edge_ids=edge_ids or [],
        window_start=window_start, window_end=window_end,
        source_epoch=source_epoch,
        fresh_until=time.time() + 600,
    )
    st.explanation_graph.add_evidence_binding(
        binding, require_trace=require_trace)
    return binding


def support_path(st: EpisodeState, store: TraceStore, path) -> None:
    for index, node_id in enumerate(path.node_ids[:-1]):
        for evidence_type in G.required_evidence(node_id):
            value = VALUES[evidence_type]
            raw_ref = store.record(
                "fixture", {"evidence_type": evidence_type},
                json.dumps(value), value)
            add_binding(
                st, store, evidence_type=evidence_type, value=value,
                node_ids=[node_id], raw_ref=raw_ref)
            add_binding(
                st, store, evidence_type=evidence_type, value=value,
                edge_ids=[path.edge_ids[index]], raw_ref=raw_ref)


def make_state(episode_id: str, candidates: list, selected: list,
               *, observed: list[str] | None = None,
               unexplained: list[str] | None = None,
               scope: str = "FULL",
               obligations: dict[str, P0Obligation] | None = None
               ) -> EpisodeState:
    symptoms = observed or list(dict.fromkeys(
        path.observed_symptom_id for path in candidates))
    st = EpisodeState(episode_id, "esc_v2_fixture")
    st.observed_symptom_ids = list(symptoms)
    st.explanation_graph = ExplanationGraph.create(
        graph_version=G.graph_version(), episode_id=episode_id,
        observed_symptoms=symptoms, candidate_paths=candidates,
        p0_obligations=obligations or {})
    st.explanation_graph.select_paths(
        [path.path_id for path in selected],
        unexplained_symptoms=unexplained or [], scope=scope)
    return st


episode_id = f"esc_v2_{uuid.uuid4().hex}"
trace_dir = TRACE_DIR / episode_id
try:
    connection = find_path(["connection_exhaustion", "conn_near_limit"])
    missing_index = find_path(["missing_index", "latency_p99_up"])
    stale_stats = find_path(["stale_statistics", "latency_p99_up"])
    disk = find_path(["disk_pressure", "disk_growing"])
    checkpoint = find_path(["checkpoint_pressure", "latency_p99_up"])

    print("[1] Trusted selected subgraph and v1-state isolation")
    st = make_state(episode_id, [connection], [connection])
    store = TraceStore(episode_id)
    support_path(st, store, st.explanation_graph.path_map()[connection.path_id])
    xr.recompute_statuses(st)
    first = esc.check_explanation(st)
    st.ledger.clear()
    st.set_verdict("connection_exhaustion", Verdict.REFUTED,
                   note="model-authored ledger refutation must be ignored")
    st.note("agent", "agent_note", "CONFIRMED REFUTED 根因已排除")
    second = esc.check_explanation(st)
    check("fresh supported path is sufficient",
          first["verdict"] == "SUFFICIENT", first["verdict"])
    check("ledger and note text cannot affect v2 ESC",
          second["verdict"] == first["verdict"])
    check("same decision persists once under a stable esc_report_id",
          len(st.esc_reports) == 1 and
          first["esc_report_id"] == second["esc_report_id"])
    check("one raw_ref is never exposed as multiple evidence refs",
          len(first["evidence_refs"]) == len(set(first["evidence_refs"])) and
          bool(first["duplicate_raw_refs"]))

    print("\n[2] Multi-root explanations and causal continuity")
    multi_id = f"{episode_id}_multi"
    multi_store = TraceStore(multi_id)
    multi = make_state(
        multi_id, [connection, missing_index], [connection, missing_index])
    for path in multi.explanation_graph.candidate_paths:
        support_path(multi, multi_store, path)
    xr.recompute_statuses(multi)
    multi_report = esc.check_explanation(multi, persist=False)
    check("independent roots for different symptoms may be sufficient",
          multi_report["verdict"] == "SUFFICIENT" and
          set(multi_report["selected_root_causes"]) == {
              "connection_exhaustion", "missing_index"})

    edge_gap = copy.deepcopy(st)
    edge_id = edge_gap.explanation_graph.selected_path_ids[0]
    selected_edge = edge_gap.explanation_graph.path_map()[edge_id].edge_ids[0]
    edge_gap.explanation_graph.evidence_bindings = {
        binding_id: binding for binding_id, binding in
        edge_gap.explanation_graph.evidence_bindings.items()
        if selected_edge not in binding.target_edge_ids
    }
    edge_report = esc.check_explanation(edge_gap, persist=False)
    check("a selected causal edge without support cannot pass",
          edge_report["verdict"] == "INSUFFICIENT" and
          edge_report["unsupported_path_ids"] and
          edge_report["evidence_need_ids"])

    print("\n[3] Episode, trace, freshness, epoch, and graph trust")
    expired = copy.deepcopy(st)
    for binding in expired.explanation_graph.evidence_bindings.values():
        binding.fresh_until = 0.0
    expired_report = esc.check_explanation(expired, persist=False)
    check("expired required evidence cannot pass",
          expired_report["verdict"] == "INSUFFICIENT" and
          expired_report["invalid_evidence_bindings"])

    cross_episode = copy.deepcopy(st)
    root_binding = next(
        binding for binding in cross_episode.explanation_graph.evidence_bindings.values()
        if "connection_exhaustion" in binding.target_node_ids)
    root_binding.episode_id = "another_episode"
    cross_report = esc.check_explanation(cross_episode, persist=False)
    check("cross-episode required evidence cannot pass",
          cross_report["verdict"] == "INSUFFICIENT")

    missing_trace = copy.deepcopy(st)
    for binding_id, binding in list(
            missing_trace.explanation_graph.evidence_bindings.items()):
        if "connection_exhaustion" in binding.target_node_ids:
            del missing_trace.explanation_graph.evidence_bindings[binding_id]
    add_binding(
        missing_trace, store, evidence_type="connection_count",
        value=VALUES["connection_count"], node_ids=["connection_exhaustion"],
        raw_ref=f"trace://{episode_id}/step_999", require_trace=False)
    missing_trace_report = esc.check_explanation(missing_trace, persist=False)
    check("a nonexistent trace cannot support required evidence",
          missing_trace_report["verdict"] == "INSUFFICIENT")

    epoch_id = f"{episode_id}_epoch"
    epoch_store = TraceStore(epoch_id)
    epoch_state = make_state(epoch_id, [checkpoint], [checkpoint])
    checkpoint_live = epoch_state.explanation_graph.path_map()[checkpoint.path_id]
    epoch_value = {
        "ckpt_requested": 9, "ckpt_timed": 1,
        "ckpt_write_time_ms": 1000, "source_epoch": "epoch_a",
    }
    epoch_ref = epoch_store.record(
        "fixture", {"evidence_type": "checkpoint_stats"},
        json.dumps(epoch_value), epoch_value)
    for target in ("node", "edge"):
        add_binding(
            epoch_state, epoch_store, evidence_type="checkpoint_stats",
            value=epoch_value,
            node_ids=["checkpoint_pressure"] if target == "node" else None,
            edge_ids=[checkpoint_live.edge_ids[0]] if target == "edge" else None,
            raw_ref=epoch_ref, window_start=10.0, window_end=20.0,
            source_epoch="epoch_a")
    epoch_state.incident_window["source_epochs"] = {
        "checkpoint_stats": "epoch_b"}
    epoch_report = esc.check_explanation(epoch_state, persist=False)
    check("cumulative evidence from a different source epoch cannot pass",
          epoch_report["verdict"] == "INSUFFICIENT" and
          any("source epoch" in reason
              for reasons in epoch_report["invalid_evidence_bindings"].values()
              for reason in reasons))

    with patch.object(G, "graph_version", return_value="graph_changed"):
        version_report = esc.check_explanation(st, persist=False)
    check("graph changes require candidate rebuild and rediagnosis",
          version_report["verdict"] == "INSUFFICIENT" and
          version_report["requires_rehypothesize"])

    print("\n[4] Every unresolved P0 state blocks independently")
    p0_id = f"{episode_id}_p0"
    p0_store = TraceStore(p0_id)
    obligation = P0Obligation(
        cause_id="disk_pressure", reachable_path_ids=[disk.path_id],
        required_evidence_types=["disk_usage"])
    p0_base = make_state(
        p0_id, [connection, disk], [connection], observed=["conn_near_limit"],
        obligations={"disk_pressure": obligation})
    support_path(
        p0_base, p0_store,
        p0_base.explanation_graph.path_map()[connection.path_id])

    p0_states = {"OPEN": copy.deepcopy(p0_base)}
    inconclusive = copy.deepcopy(p0_base)
    add_binding(
        inconclusive, p0_store, evidence_type="disk_usage",
        value={"used_pct": 80.0}, node_ids=["disk_pressure"],
        result=PredicateResult.NEUTRAL.value)
    p0_states["INCONCLUSIVE"] = inconclusive
    unavailable = copy.deepcopy(p0_base)
    add_binding(
        unavailable, p0_store, evidence_type="disk_usage",
        value={"error": "permission denied"}, node_ids=["disk_pressure"],
        status="ERROR", result=PredicateResult.NOT_APPLICABLE.value)
    p0_states["UNAVAILABLE"] = unavailable
    truncated = copy.deepcopy(p0_base)
    truncated.explanation_graph.p0_obligations["disk_pressure"].truncated = True
    add_binding(
        truncated, p0_store, evidence_type="disk_usage",
        value=VALUES["disk_usage"], node_ids=["disk_pressure"])
    p0_states["truncated"] = truncated
    for label, state in p0_states.items():
        report = esc.check_explanation(state, persist=False)
        check(f"P0 {label} cannot pass",
              report["verdict"] != "SUFFICIENT" and
              "disk_pressure" in report["unresolved_p0_causes"],
              report["verdict"])

    print("\n[5] INSUFFICIENT, AMBIGUOUS, and EXHAUSTED are distinct")
    check("a clear legal evidence need yields INSUFFICIENT",
          edge_report["verdict"] == "INSUFFICIENT" and
          bool(edge_report["evidence_needs"]))
    directed_need = next(
        item for item in edge_report["evidence_needs"]
        if selected_edge in item["target_ids"])
    edge_gap.esc_reports.append({
        **edge_report,
        "evidence_needs": [directed_need],
    })
    projection = xr.compact_projection(edge_gap)
    check("current ESC typed needs override the global frontier page",
          projection["need_source"] == "esc" and
          [item["need_id"] for item in projection["needs"]] == [
              directed_need["need_id"]])
    directed_bind = copy.deepcopy(edge_gap)
    raw_ref = store.record(
        "fixture", {"evidence_type": directed_need["evidence_type"],
                    "directed": True},
        json.dumps(VALUES[directed_need["evidence_type"]]),
        VALUES[directed_need["evidence_type"]])
    directed_bind.note(
        "fixture", directed_need["evidence_type"], "directed evidence",
        raw_ref, ["connection_exhaustion"],
        structured_value=VALUES[directed_need["evidence_type"]],
        predicate_id=directed_need["predicate_id"],
        explanation_id=directed_bind.explanation_graph.explanation_id,
        explanation_revision=directed_bind.explanation_graph.revision,
        evidence_need_ids=[directed_need["need_id"]])
    with patch.object(G, "evidence_needs", return_value=[]):
        directed_added = xr.bind_evidence(directed_bind)
    check("ESC typed needs remain bindable outside the global frontier",
          bool(directed_added) and any(
              selected_edge in binding.target_edge_ids
              for binding in directed_bind.explanation_graph.
              evidence_bindings.values()))

    all_latency = G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=False)
    selected_for_ranking = next(
        path for path in all_latency if path.root_node_id == "missing_index")
    low_inconclusive = next(
        path for path in all_latency if path.root_node_id == "table_bloat")
    low_inconclusive.status = "INCONCLUSIVE"
    ranking_exp = ExplanationGraph.create(
        graph_version=G.graph_version(), episode_id=episode_id,
        observed_symptoms=["latency_p99_up"],
        candidate_paths=all_latency)
    major_ids = {
        alternative.path_id
        for _selected, alternative in esc._major_alternatives(
            ranking_exp, [selected_for_ranking],
            config=esc.DEFAULT_EXPLANATION_ESC)
    }
    check("low-score INCONCLUSIVE paths are not major by status alone",
          low_inconclusive.path_id not in major_ids)

    ambiguous_id = f"{episode_id}_ambiguous"
    ambiguous_store = TraceStore(ambiguous_id)
    ambiguous = make_state(
        ambiguous_id, [missing_index, stale_stats], [missing_index],
        observed=["latency_p99_up"])
    for path in ambiguous.explanation_graph.candidate_paths:
        support_path(ambiguous, ambiguous_store, path)
    xr.recompute_statuses(ambiguous)
    ambiguous_report = esc.check_explanation(ambiguous, persist=False)
    check("fresh support for competing non-nested paths is AMBIGUOUS",
          ambiguous_report["verdict"] == "AMBIGUOUS" and
          stale_stats.path_id in
          ambiguous_report["unresolved_competing_path_ids"],
          ambiguous_report["verdict"])
    add_binding(
        ambiguous, ambiguous_store,
        evidence_type="row_estimate_deviation",
        value={"max_ratio": 1.0}, node_ids=["stale_statistics"])
    xr.recompute_statuses(ambiguous)
    refuted_report = esc.check_explanation(ambiguous, persist=False)
    check("a scoped REFUTED_BY closes an INCONCLUSIVE alternative",
          ambiguous.explanation_graph.path_map()[
              stale_stats.path_id].status == "INCONCLUSIVE" and
          refuted_report["verdict"] == "SUFFICIENT",
          refuted_report["verdict"])

    exhausted = copy.deepcopy(edge_gap)
    exhausted.budget["steps"] = exhausted.budget["max_steps"]
    exhausted_report = esc.check_explanation(exhausted, persist=False)
    check("an exhausted episode budget yields EXHAUSTED",
          exhausted_report["verdict"] == "EXHAUSTED")

    unavailable_long = copy.deepcopy(edge_gap)
    initial = esc.check_explanation(unavailable_long, persist=False)
    required_need = next(
        need for need in initial["evidence_needs"] if need["required"])
    unavailable_long.evidence_task_audit.extend([
        {"event": "evidence_need_unavailable",
         "need_id": required_need["need_id"]},
        {"event": "evidence_need_unavailable",
         "need_id": required_need["need_id"]},
    ])
    unavailable_report = esc.check_explanation(
        unavailable_long, persist=False)
    check("repeatedly unavailable required evidence yields EXHAUSTED",
          unavailable_report["verdict"] == "EXHAUSTED")

    # 工具在取到东西之前就失败时（只读连接 EXPLAIN 写语句、超时、对象不存在），
    # 它不知道自己在服务哪条 need，只能按自己的方式记账 —— 记下的 evidence_type
    # 与 need 要的那个对不上，need_id 通道和 binding 通道会同时失明。实测里
    # lock_contention 就因此空转 47 轮直到预算耗尽：EXHAUSTED 这个出口没通电。
    tool_dead = copy.deepcopy(edge_gap)
    tool_need = next(
        need for need in esc.check_explanation(tool_dead, persist=False)[
            "evidence_needs"]
        if need["required"] and need["candidate_tools"])
    failing_tool = tool_need["candidate_tools"][0]
    base_seq = len(tool_dead.scratchpad)
    for offset in range(2):
        tool_dead.scratchpad.append({
            "seq": base_seq + offset, "ts": time.time(), "author": "agent",
            # 关键：类型与 need 要的不同，正是两条旧通道看不见的那种记法
            "evidence_type": "explain_unavailable",
            "observation": "InsufficientPrivilege: permission denied",
            "raw_ref": f"trace://{episode_id}/tool_fail_{offset}",
            "bears_on": [], "status": EvidenceStatus.ERROR.value,
            "structured_value": None, "predicate_id": "",
            "target_kind": "NODE", "target_ids": [],
            "collection_tool": failing_tool,
        })
    tool_report = esc.check_explanation(tool_dead, persist=False)
    check("a tool that keeps failing outright yields EXHAUSTED",
          tool_report["verdict"] == "EXHAUSTED", tool_report["verdict"])

    # 反面：工具后来成功过就说明它可用，早先的失败不该继续累计 —— 否则
    # 一次抖动会永久拉黑这个工具，把还能取到的 need 误判成长期不可得。
    tool_recovered = copy.deepcopy(tool_dead)
    tool_recovered.scratchpad.append({
        "seq": base_seq + 9, "ts": time.time(), "author": "agent",
        "evidence_type": "explain_unavailable",
        "observation": "recovered", "raw_ref": f"trace://{episode_id}/tool_ok",
        "bears_on": [], "status": EvidenceStatus.OBSERVED.value,
        "structured_value": None, "predicate_id": "",
        "target_kind": "NODE", "target_ids": [],
        "collection_tool": failing_tool,
    })
    recovered_report = esc.check_explanation(tool_recovered, persist=False)
    check("a tool that recovers stops counting toward EXHAUSTED",
          recovered_report["verdict"] != "EXHAUSTED",
          recovered_report["verdict"])

    print("\n[6] PARTIAL scope is explicit and never AUTO")
    partial_id = f"{episode_id}_partial"
    partial_store = TraceStore(partial_id)
    partial = make_state(
        partial_id, [connection, missing_index], [connection],
        observed=["conn_near_limit", "latency_p99_up"],
        unexplained=["latency_p99_up"], scope=ExplanationScope.PARTIAL.value)
    support_path(
        partial, partial_store,
        partial.explanation_graph.path_map()[connection.path_id])
    missing_live = partial.explanation_graph.path_map()[missing_index.path_id]
    add_binding(
        partial, partial_store, evidence_type="explain_plan",
        value={"indexes_used": ["idx_existing"]},
        edge_ids=[missing_live.edge_ids[0]])
    xr.recompute_statuses(partial)
    partial_report = esc.check_explanation(partial, persist=False)
    check("a bounded low-risk PARTIAL explanation may be sufficient",
          partial_report["verdict"] == "SUFFICIENT" and
          partial_report["partial_fix_suspected"])

    risky_partial = copy.deepcopy(partial)
    risky_partial.explanation_graph.observed_symptoms.append("unmapped_signal")
    risky_partial.explanation_graph.unexplained_symptoms.append("unmapped_signal")
    risky_report = esc.check_explanation(risky_partial, persist=False)
    check("an unexplained unbounded symptom blocks PARTIAL sufficiency",
          risky_report["verdict"] == "INSUFFICIENT" and
          risky_report["requires_rehypothesize"])

    auto = gate.assess(RemediationProposal(
        action_type="vacuum_analyze", sql="ANALYZE orders",
        rollback="NO_ROLLBACK_NEEDED", root_cause="stale_statistics",
        fix_id="analyze_table", esc_verdict="SUFFICIENT"))
    partial_gate = gate.assess(RemediationProposal(
        action_type="vacuum_analyze", sql="ANALYZE orders",
        rollback="NO_ROLLBACK_NEEDED", root_cause="stale_statistics",
        fix_id="analyze_table", esc_verdict="SUFFICIENT",
        partial_explanation=True))
    check("PARTIAL context raises an otherwise AUTO action to CONFIRM",
          auto.tier == "AUTO" and partial_gate.tier == "CONFIRM")
finally:
    for path in TRACE_DIR.glob(f"{episode_id}*"):
        if path.is_dir() and path.resolve().parent == TRACE_DIR.resolve():
            shutil.rmtree(path)

print("\n" + "=" * 78)
print("ESC V2:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
