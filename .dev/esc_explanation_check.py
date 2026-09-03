"""Independent acceptance checks for explanation-subgraph ESC verdicts."""
from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState
from agent.esc import check_explanation
from agent.explanation import EvidenceBinding, ExplanationScope, P0Obligation
from knowledge.causal_graph import graph as G
from sandbox.traces import TRACE_DIR, TraceStore


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<58} {detail}")


def add_binding(store, explanation, *, evidence_type, predicate_id, value,
                node, edge="", result="SUPPORTS", ordinal=0):
    raw_ref = store.record(
        "fixture", {"type": evidence_type, "ordinal": ordinal},
        json.dumps(value), value)
    binding = EvidenceBinding.create(
        episode_id=store.episode_id, raw_ref=raw_ref,
        evidence_type=evidence_type, status="OBSERVED",
        observed_at=time.time(), predicate_id=predicate_id,
        predicate_result=result, structured_value=value,
        target_node_ids=[node], target_edge_ids=[edge] if edge else [],
        fresh_until=time.time() + 600)
    explanation.add_evidence_binding(binding)
    return binding


def complete_state(episode_id: str, *, scope="FULL", include_index=True,
                   include_alternative=False):
    store = TraceStore(episode_id)
    candidates = G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=False)
    selected = next(path for path in candidates if path.node_ids == [
        "missing_index", "latency_p99_up"])
    paths = [selected]
    alternative = None
    if include_alternative:
        alternative = next(path for path in candidates if path.node_ids == [
            "stale_statistics", "latency_p99_up"])
        paths.append(alternative)
    explanation = G.merge_paths(
        paths, episode_id=episode_id,
        observed_symptoms=["latency_p99_up"])
    add_binding(
        store, explanation, evidence_type="explain_seq_scan",
        predicate_id="explain_seq_scan_v2",
        value={"scan_types": ["Seq Scan"],
               "rows_removed_by_filter": 100000},
        node="missing_index", edge=selected.edge_ids[0])
    if include_index:
        add_binding(
            store, explanation, evidence_type="index_existence",
            predicate_id="index_existence_v2",
            value={"inventory_collected": True, "indexes": []},
            node="missing_index", ordinal=1)
    explanation.set_node_status("latency_p99_up", "SUPPORTED")
    explanation.set_path_status(selected.path_id, "SUPPORTED")
    if alternative is not None:
        add_binding(
            store, explanation, evidence_type="row_estimate_deviation",
            predicate_id="row_estimate_deviation_v2",
            value={"rows_est_vs_actual": [[1, 1000]]},
            node="stale_statistics", edge=alternative.edge_ids[0], ordinal=2)
        explanation.set_path_status(alternative.path_id, "SUPPORTED")
    explanation.select_paths(
        [selected.path_id], unexplained_symptoms=[], scope=scope)
    state = EpisodeState(episode_id, "controlled_esc")
    state.explanation_graph = explanation
    return state, store, selected, alternative


episode_ids = []
try:
    print("[1] FULL and PARTIAL sufficient reports")
    full_id = f"esc_full_{uuid.uuid4().hex}"
    episode_ids.append(full_id)
    full, _store, path, _ = complete_state(full_id)
    sufficient = check_explanation(full, persist=True)
    check("complete selected chain is SUFFICIENT/FULL",
          sufficient["verdict"] == "SUFFICIENT" and
          sufficient["scope"] == "FULL")
    dims = {item["name"]: item["passed"]
            for item in sufficient["dimensions"]}
    check("coverage, continuity, alternatives and P0 all pass",
          all(dims[name] for name in (
              "SYMPTOM_COVERAGE", "ROOT_REQUIRED_EVIDENCE",
              "CAUSAL_CONTINUITY", "ALTERNATIVE_PATHS",
              "P0_OBLIGATIONS", "EVIDENCE_TRUST", "GRAPH_VERSION")))
    check("report persists a stable esc_report_id",
          full.esc_reports[-1]["esc_report_id"] ==
          sufficient["esc_report_id"])

    partial_id = f"esc_partial_{uuid.uuid4().hex}"
    episode_ids.append(partial_id)
    partial, _store, _path, _ = complete_state(
        partial_id, scope=ExplanationScope.PARTIAL.value)
    partial_report = check_explanation(partial, persist=False)
    check("PARTIAL never silently presents as FULL",
          partial_report["verdict"] == "SUFFICIENT" and
          partial_report["scope"] == "PARTIAL" and
          partial_report["partial_fix_suspected"] is True)

    print("\n[2] Missing evidence returns actionable INSUFFICIENT")
    missing_id = f"esc_missing_{uuid.uuid4().hex}"
    episode_ids.append(missing_id)
    missing, _store, _path, _ = complete_state(
        missing_id, include_index=False)
    insufficient = check_explanation(missing, persist=False)
    check("fillable required evidence yields INSUFFICIENT",
          insufficient["verdict"] == "INSUFFICIENT")
    check("INSUFFICIENT returns typed prioritized needs",
          any(item["evidence_type"] == "index_existence" and item["required"]
              for item in insufficient["evidence_needs"]))

    print("\n[3] Supported alternatives produce AMBIGUOUS, not root counting")
    ambiguous_id = f"esc_ambiguous_{uuid.uuid4().hex}"
    episode_ids.append(ambiguous_id)
    ambiguous, _store, _path, alternative = complete_state(
        ambiguous_id, include_alternative=True)
    # Both branches already have current positive evidence and this controlled
    # environment exposes no additional legal discriminator.
    with patch.object(G, "evidence_needs", return_value=[]):
        ambiguous_report = check_explanation(ambiguous, persist=False)
    check("mutually competing supported path remains AMBIGUOUS",
          ambiguous_report["verdict"] == "AMBIGUOUS",
          ambiguous_report["verdict"])
    check("report identifies the concrete competing path",
          alternative.path_id in
          ambiguous_report["unresolved_competing_path_ids"])

    print("\n[4] Independent roots for distinct symptoms are merged")
    multi_id = f"esc_multi_root_{uuid.uuid4().hex}"
    episode_ids.append(multi_id)
    multi_store = TraceStore(multi_id)
    multi_paths = G.enumerate_causal_paths(
        ["latency_p99_up", "cpu_saturated"], use_learned=False)
    index_path = next(path for path in multi_paths if path.node_ids == [
        "missing_index", "latency_p99_up"])
    stats_path = next(path for path in multi_paths if path.node_ids == [
        "stale_statistics", "cpu_saturated"])
    multi_exp = G.merge_paths(
        [index_path, stats_path], episode_id=multi_id,
        observed_symptoms=["latency_p99_up", "cpu_saturated"])
    add_binding(
        multi_store, multi_exp, evidence_type="explain_seq_scan",
        predicate_id="explain_seq_scan_v2",
        value={"scan_types": ["Seq Scan"],
               "rows_removed_by_filter": 100000},
        node="missing_index", edge=index_path.edge_ids[0])
    add_binding(
        multi_store, multi_exp, evidence_type="index_existence",
        predicate_id="index_existence_v2",
        value={"inventory_collected": True}, node="missing_index", ordinal=1)
    add_binding(
        multi_store, multi_exp, evidence_type="row_estimate_deviation",
        predicate_id="row_estimate_deviation_v2",
        value={"rows_est_vs_actual": [[1, 1000]]},
        node="stale_statistics", edge=stats_path.edge_ids[0], ordinal=2)
    for symptom in ("latency_p99_up", "cpu_saturated"):
        multi_exp.set_node_status(symptom, "SUPPORTED")
    for selected_path in (index_path, stats_path):
        multi_exp.set_path_status(selected_path.path_id, "SUPPORTED")
    multi_exp.select_paths(
        [index_path.path_id, stats_path.path_id], unexplained_symptoms=[])
    multi_state = EpisodeState(multi_id, "controlled_multi_root")
    multi_state.explanation_graph = multi_exp
    multi_report = check_explanation(multi_state, persist=False)
    check("two sufficient independent roots form one FULL subgraph",
          multi_report["verdict"] == "SUFFICIENT" and
          set(multi_report["selected_root_causes"]) == {
              "missing_index", "stale_statistics"})

    print("\n[5] P0 and exhausted evidence are explicit obligations")
    p0_id = f"esc_p0_{uuid.uuid4().hex}"
    episode_ids.append(p0_id)
    p0_state, _store, selected, _ = complete_state(p0_id)
    p0_state.explanation_graph.p0_obligations["autovacuum_starvation"] = (
        P0Obligation(
            cause_id="autovacuum_starvation",
            reachable_path_ids=[selected.path_id], status="OPEN",
            required_evidence_types=["autovacuum_health"]))
    p0_report = check_explanation(p0_state, persist=False)
    check("an OPEN P0 obligation cannot pass ESC",
          p0_report["verdict"] == "INSUFFICIENT" and
          p0_report["unresolved_p0_causes"] == [
              "autovacuum_starvation"])

    exhausted_id = f"esc_exhausted_{uuid.uuid4().hex}"
    episode_ids.append(exhausted_id)
    exhausted, _store, _path, _ = complete_state(
        exhausted_id, include_index=False)
    exhausted.budget = {"steps": 10, "max_steps": 10}
    exhausted_report = check_explanation(exhausted, persist=False)
    check("budget exhaustion yields EXHAUSTED",
          exhausted_report["verdict"] == "EXHAUSTED")

    print("\n[6] Notes cannot manufacture sufficiency")
    missing.note("model", "index_existence",
                 "CONFIRMED: the index is definitely missing")
    still_missing = check_explanation(missing, persist=False)
    check("scratchpad prose without a binding is ignored",
          still_missing["verdict"] == "INSUFFICIENT")
finally:
    for episode_id in episode_ids:
        shutil.rmtree(TRACE_DIR / episode_id, ignore_errors=True)

print("\n" + "=" * 80)
print("ESC EXPLANATION:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
