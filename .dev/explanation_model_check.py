"""Acceptance checks for causal explanation v2 data contracts."""
from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState, EvidenceStatus, Verdict
from agent.explanation import (
    CausalGateContext,
    CausalPath,
    CausalStatus,
    DynamicRole,
    EvidenceBinding,
    EvidenceNeed,
    EvidenceReport,
    EvidenceTargetKind,
    ExplanationGraph,
    ExplanationScope,
    InterventionKind,
    InterventionPlan,
    ObligationStatus,
    P0Obligation,
    PredicateResult,
)
from sandbox.traces import TRACE_DIR, TraceStore


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<46} {detail}")


episode_id = f"explanation_model_{uuid.uuid4().hex}"
trace_dir = TRACE_DIR / episode_id
try:
    print("[1] Enums and stable path-local roles")
    check("EvidenceStatus remains three-state",
          {s.value for s in EvidenceStatus} == {"OBSERVED", "UNKNOWN", "ERROR"})
    check("causal and collection status remain separate",
          CausalStatus.SUPPORTED.value == "SUPPORTED" and
          "ERROR" not in {s.value for s in CausalStatus})
    check("all new enum values are available",
          (DynamicRole.MECHANISM.value == "MECHANISM" and
           ObligationStatus.UNAVAILABLE.value == "UNAVAILABLE" and
           InterventionKind.MANUAL.value == "MANUAL" and
           ExplanationScope.PARTIAL.value == "PARTIAL"))

    p_short = CausalPath.create(
        graph_version="graph-fixture-v1",
        node_ids=["autovacuum_starvation", "table_bloat", "disk_growing"],
        edge_ids=["e_auto_bloat", "e_bloat_disk"],
        required_evidence_types=["autovacuum_health"],
        source="graph",
    )
    p_long = CausalPath.create(
        graph_version="graph-fixture-v1",
        node_ids=["stale_replication_slot", "autovacuum_starvation",
                  "table_bloat", "disk_growing"],
        edge_ids=["e_slot_auto", "e_auto_bloat", "e_bloat_disk"],
        required_evidence_types=["replication_slot_health", "autovacuum_health"],
        source="graph",
    )
    p_duplicate = CausalPath.create(
        graph_version="graph-fixture-v1",
        node_ids=list(p_short.node_ids), edge_ids=list(p_short.edge_ids),
        source="exploration",
    )
    check("same structure produces the same path_id",
          p_short.path_id == p_duplicate.path_id, p_short.path_id)
    check("root role is path-local",
          p_short.node_roles["autovacuum_starvation"] == "ROOT_CAUSE")
    check("the same node can be a mechanism on a longer path",
          p_long.node_roles["autovacuum_starvation"] == "MECHANISM")
    check("paths are stored upstream to observed symptom",
          p_long.root_node_id == p_long.node_ids[0] and
          p_long.observed_symptom_id == p_long.node_ids[-1])
    rejected_single = False
    try:
        CausalPath.create(graph_version="g", node_ids=["disk_growing"], edge_ids=[])
    except ValueError:
        rejected_single = True
    check("single-node explanations are rejected", rejected_single)

    print("\n[2] Explanation merge, P0 obligations, and revision")
    graph = ExplanationGraph.create(
        graph_version="graph-fixture-v1",
        episode_id=episode_id,
        observed_symptoms=["disk_growing", "unmapped_alert"],
        candidate_paths=[p_short, p_duplicate, p_long],
        p0_obligations={
            "stale_replication_slot": P0Obligation(
                cause_id="stale_replication_slot",
                reachable_path_ids=[p_long.path_id],
                required_evidence_types=["replication_slot_health"],
                truncated=True,
            )
        },
        created_at=1000.0,
    )
    merged = graph.path_map()[p_short.path_id]
    check("duplicate structural paths are merged", len(graph.candidate_paths) == 2)
    check("recall sources survive path merge",
          merged.source == ["graph", "exploration"], merged.source)
    check("unmapped symptoms stay explicit",
          graph.unexplained_symptoms == ["unmapped_alert"])
    check("truncated P0 is never resolved",
          not graph.p0_obligations["stale_replication_slot"].resolved)

    before = graph.revision
    changed = graph.set_edge_status("e_slot_auto", CausalStatus.SUPPORTED)
    same = graph.set_edge_status("e_slot_auto", CausalStatus.SUPPORTED)
    check("trusted state changes increment revision once",
          changed and not same and graph.revision == before + 1, graph.revision)
    graph.select_paths([p_short.path_id, p_long.path_id],
                       unexplained_symptoms=["unmapped_alert"],
                       scope=ExplanationScope.PARTIAL)
    check("selected roots are path-derived",
          graph.selected_root_causes == ["autovacuum_starvation",
                                         "stale_replication_slot"],
          graph.selected_root_causes)
    spoofed = graph.to_dict()
    spoofed["selected_root_causes"] = ["model_invented_root"]
    restored_spoof = ExplanationGraph.from_dict(spoofed)
    check("serialized root-cause spoofing is ignored",
          "model_invented_root" not in restored_spoof.selected_root_causes)

    print("\n[3] Evidence binding trust, digest, and idempotence")
    structured = {"slot_count": 1, "oldest_xmin_age": 250000000}
    store = TraceStore(episode_id)
    raw_ref = store.record("get_vacuum_horizon", {}, json.dumps(structured), structured)
    binding = EvidenceBinding.create(
        episode_id=episode_id,
        raw_ref=raw_ref,
        evidence_type="replication_slot_health",
        status=EvidenceStatus.OBSERVED,
        observed_at=1100.0,
        window_start=1000.0,
        window_end=1100.0,
        source_epoch="postgres-start-1",
        target_node_ids=["stale_replication_slot"],
        target_edge_ids=["e_slot_auto"],
        predicate_id="replication_slot_state_v2",
        predicate_result=PredicateResult.SUPPORTS,
        structured_value=structured,
        summary="one stale replication slot",
        fresh_until=time.time() + 300,
    )
    binding_again = EvidenceBinding.from_dict(binding.to_dict())
    check("binding ID is stable for raw_ref/predicate/target",
          binding.binding_id == binding_again.binding_id, binding.binding_id)
    check("current-episode raw_ref and digest are trusted", binding.is_trusted())
    revision_before_binding = graph.revision
    first_add = graph.add_evidence_binding(binding)
    duplicate_add = graph.add_evidence_binding(binding_again)
    check("duplicate binding is not counted twice",
          first_add and not duplicate_add and
          graph.revision == revision_before_binding + 1)
    check("binding is attached only to its relevant path",
          (binding.binding_id in graph.path_map()[p_long.path_id].evidence_binding_ids and
           binding.binding_id not in graph.path_map()[p_short.path_id].evidence_binding_ids and
           len(graph.candidate_paths) == 2))

    wrong_episode = EvidenceBinding.create(
        episode_id="another_episode", raw_ref=raw_ref,
        evidence_type="replication_slot_health", status="OBSERVED",
        observed_at=1100.0, target_node_ids=["stale_replication_slot"],
        predicate_id="replication_slot_state_v2",
        predicate_result="SUPPORTS", structured_value=structured,
    )
    check("cross-episode raw_ref is rejected",
          not wrong_episode.validate_raw_ref())
    tampered = EvidenceBinding.from_dict(binding.to_dict())
    tampered.value_digest = "0" * 64
    check("tampered structured digest is untrusted", not tampered.is_trusted())
    unknown_rejected = False
    try:
        EvidenceBinding.create(
            episode_id=episode_id, raw_ref=raw_ref, evidence_type="x",
            status="UNKNOWN", observed_at=1100.0, target_node_ids=["x"],
            predicate_id="x", predicate_result="REFUTES",
            structured_value=structured)
    except ValueError:
        unknown_rejected = True
    check("UNKNOWN cannot be converted into a refutation", unknown_rejected)

    print("\n[4] Need/report and system-owned gate context")
    need = EvidenceNeed.create(
        path_ids=[p_long.path_id], target_kind=EvidenceTargetKind.BRANCH,
        target_ids=["e_slot_auto"], evidence_type="replication_slot_health",
        predicate_id="replication_slot_state_v2", required=True,
        freshness_seconds=300, candidate_tools=["get_vacuum_horizon"],
        reason="distinguish the upstream P0 branch")
    check("EvidenceNeed ID survives round-trip",
          EvidenceNeed.from_dict(need.to_dict()).need_id == need.need_id)
    report = EvidenceReport.from_dict({
        "need_id": need.need_id,
        "tool": "get_vacuum_horizon",
        "raw_refs": [raw_ref],
        "observations": [structured],
        "collection_status": "OBSERVED",
        "limitations": [],
    })
    check("EvidenceReport carries observations but no verdict",
          "verdict" not in report.to_dict())
    report_verdict_rejected = False
    try:
        EvidenceReport.from_dict({**report.to_dict(), "verdict": "CONFIRMED"})
    except ValueError:
        report_verdict_rejected = True
    check("subagent verdict fields are rejected", report_verdict_rejected)

    graph.select_paths([p_long.path_id],
                       unexplained_symptoms=["unmapped_alert"],
                       scope=ExplanationScope.PARTIAL)
    plan = InterventionPlan.create(
        explanation_id=graph.explanation_id,
        explanation_revision=graph.revision,
        selected_path_id=p_long.path_id,
        intervention_target="stale_replication_slot",
        fix_id="drop_replication_slot",
        intervention_kind=InterventionKind.MANUAL,
        action_type="replication_slot_management",
        sql="",
        rollback="IRREVERSIBLE",
        expected_effect_nodes=["autovacuum_starvation", "table_bloat",
                               "disk_growing"],
        expected_effects=[{
            "metric": "dead_tuple_ratio", "direction": "decrease",
            "minimum_change": 0.1, "window_seconds": 300,
        }],
        rationale="escalate the selected upstream slot intervention",
    )
    gate_context = CausalGateContext.build(graph, plan, "esc_fixture")
    check("gate context is deterministically state-owned",
          gate_context.explanation_revision == graph.revision and
          gate_context.evidence_refs == [raw_ref])
    forged_rejected = False
    try:
        CausalGateContext.build(
            graph, plan, "esc_fixture",
            model_payload={"explanation_revision": graph.revision + 99})
    except ValueError:
        forged_rejected = True
    check("conflicting model-owned trusted fields are rejected", forged_rejected)

    print("\n[5] EpisodeState v2 round-trip and missing fields")
    state = EpisodeState(episode_id=episode_id, scenario_id="contract_fixture")
    state.symptoms = ["disk_growing", "unmapped_alert"]
    state.hypothesis_candidates = ["autovacuum_starvation"]
    state.ensure_hypotheses(state.hypothesis_candidates)
    state.set_verdict("autovacuum_starvation", Verdict.CONFIRMED)
    state.explanation_graph = graph
    state.esc_reports = [{"report_id": "esc_fixture", "verdict": "SUFFICIENT",
                          "explanation_revision": graph.revision}]
    state.intervention_plan = plan
    state.note("agent", "agent_note", "no raw_ref means scratchpad only")
    state.save()
    first = state.to_dict()
    loaded = EpisodeState.load(episode_id)
    second = loaded.to_dict()
    loaded.save()
    loaded_again = EpisodeState.load(episode_id)
    check("save/load/save is structurally stable",
          first == second == loaded_again.to_dict())
    check("nested contracts restore their types",
          isinstance(loaded.explanation_graph, ExplanationGraph) and
          isinstance(loaded.intervention_plan, InterventionPlan))
    check("scratchpad notes never become trusted bindings",
          len(loaded.explanation_graph.evidence_bindings) == 1)
    rendered = loaded.render_context()
    check("v2 context prioritizes explanation paths and P0",
          "解释子图" in rendered and "未决分叉" in rendered and "P0 义务" in rendered)

    missing_id = f"missing_fields_{uuid.uuid4().hex}"
    missing_dir = TRACE_DIR / missing_id
    missing_dir.mkdir(parents=True)
    (missing_dir / "episode_state.json").write_text(json.dumps({
        "schema_version": 2, "episode_id": missing_id, "scenario_id": "missing"
    }), encoding="utf-8")
    missing = EpisodeState.load(missing_id)
    check("missing optional v2 fields receive defaults",
          missing.explanation_graph is None and missing.esc_reports == [] and
          missing.intervention_plan is None)
    shutil.rmtree(missing_dir)

    print("\n[6] Legacy trace compatibility and repeated restore")
    legacy_id = f"legacy_state_{uuid.uuid4().hex}"
    legacy_dir = TRACE_DIR / legacy_id
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "episode_state.json").write_text(json.dumps({
        "episode_id": legacy_id,
        "scenario_id": "legacy",
        "symptoms": ["legacy symptom"],
        "hypothesis_candidates": ["missing_index"],
        "ledger": {
            "missing_index": {"verdict": "CONFIRMED", "evidence": [], "note": ""}
        },
    }), encoding="utf-8")
    legacy = EpisodeState.load(legacy_id)
    projection = legacy.v1_readonly_projection()
    check("unversioned traces load as v1", legacy.schema_version == 1)
    check("v1 projection does not invent paths or edge validation",
          projection.candidate_paths == [] and projection.edge_status == {} and
          projection.selected_root_causes == [])
    check("v1 context still renders the legacy ledger",
          "假设台账" in legacy.render_context())
    legacy_first = legacy.to_dict()
    legacy.save()
    check("legacy restore is repeatable",
          EpisodeState.load(legacy_id).to_dict() == legacy_first)
    shutil.rmtree(legacy_dir)

finally:
    shutil.rmtree(trace_dir, ignore_errors=True)

print("\n" + "=" * 72)
print("EXPLANATION MODEL:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
