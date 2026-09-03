"""Acceptance checks for scoped refutation and intervention semantics."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import esc
from agent.episode_state import EpisodeState, EvidenceStatus, Verdict
from agent.state_machine import StateMachine
from agent.toolbox import Toolbox
from knowledge.causal_graph import graph as G
from knowledge.evidence_predicates import PredicateContext, evaluate


ROOT = Path(__file__).resolve().parent.parent
nodes = yaml.safe_load((ROOT / "knowledge/causal_graph/nodes.yaml").read_text(
    encoding="utf-8"))
edges = yaml.safe_load((ROOT / "knowledge/causal_graph/edges.yaml").read_text(
    encoding="utf-8"))
fixes = {item["id"]: item for item in nodes["fixes"]}
fixed_by = {item["cause"]: item["fix"] for item in edges["fixed_by"]}
refuters = edges["refuted_by"]
failures: list[str] = []


def check(label: str, condition: bool, detail="") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<52} {detail}")
    if not condition:
        failures.append(label)


print("[1] FIXED_BY describes interventions, not generic actions")
expected = {
    "missing_index": ("create_covering_index", "CORRECTIVE"),
    "stale_statistics": ("analyze_table", "CORRECTIVE"),
    "table_bloat": ("vacuum_table", "MITIGATION"),
    "lock_contention": ("terminate_blocker", "CONTAINMENT"),
    "long_idle_transaction": ("terminate_idle_transaction", "CONTAINMENT"),
    "autovacuum_starvation": ("enable_autovacuum", "CORRECTIVE"),
    "connection_exhaustion": ("terminate_idle_backend", "CONTAINMENT"),
    "disk_pressure": ("remediate_disk_capacity", "MANUAL"),
    "stale_replication_slot": ("drop_replication_slot", "MANUAL"),
    "orphaned_prepared_transaction": ("resolve_prepared_xact", "MANUAL"),
    "work_mem_spill": ("raise_work_mem", "MITIGATION"),
    "checkpoint_pressure": ("raise_max_wal_size", "MITIGATION"),
    "deadlock": ("remediate_deadlock_pattern", "MANUAL"),
}
check("required cause/fix mappings are exact", all(
    fixed_by.get(cause) == fix_id and
    fixes[fix_id]["intervention_kind"] == kind
    for cause, (fix_id, kind) in expected.items()))
check("deadlock no longer terminates a PostgreSQL deadlock victim",
      fixed_by.get("deadlock") != "terminate_blocker")
check("every fix has executable preconditions and effect nodes", all(
    fix.get("preconditions") and fix.get("expected_effect_nodes") and
    fix.get("execution") in {"gated", "escalate_only"}
    for fix in fixes.values()))
check("vacuum_table does not promise filesystem/disk correction",
      "disk_growing" not in fixes["vacuum_table"]["expected_effect_nodes"] and
      "disk_pressure" not in fixes["vacuum_table"]["expected_effect_nodes"])
check("work_mem change is local and memory-budget bound", {
    item.get("id") for item in fixes["raise_work_mem"]["preconditions"]
} >= {"session_or_transaction_scope_only", "concurrency_bound",
      "aggregate_memory_budget_checked"})
check("lock containment is bound to the topmost fresh PID", {
    item.get("id") for item in fixes["terminate_blocker"]["preconditions"]
} >= {"concrete_pid_bound", "pid_is_topmost_blocker",
      "pid_identity_rechecked_fresh"})
check("idle transaction containment binds PID, age, role, and impact", {
    item.get("id") for item in
    fixes["terminate_idle_transaction"]["preconditions"]
} >= {"concrete_pid_bound", "transaction_age_bound", "database_role_bound",
      "blocking_or_xmin_impact_bound"})
check("idle backend containment excludes diagnostic/system sessions", {
    item.get("id") for item in fixes["terminate_idle_backend"]["preconditions"]
} >= {"pid_is_not_current_diagnostic_connection",
      "role_is_not_system_or_diagnostic",
      "pid_is_client_backend_and_state_idle"})
vacuum_predicates = {
    item.get("predicate_id"): item.get("result")
    for item in fixes["vacuum_database"]["preconditions"]
    if item.get("predicate_id")
}
check("wraparound vacuum requires all xmin holders to be absent",
      vacuum_predicates == {
          "replication_slot_age_v2": "REFUTES",
          "prepared_xact_age_v2": "REFUTES",
          "backend_xmin_age_v2": "REFUTES",
      })
check("checkpoint mitigation remains escalation-only with capacity checks",
      fixes["raise_max_wal_size"]["execution"] == "escalate_only" and {
          item.get("id") for item in
          fixes["raise_max_wal_size"]["preconditions"]
      } >= {"wal_archiving_health_checked", "filesystem_headroom_checked",
            "wal_retention_holders_checked", "checkpoint_window_bound"})

print("\n[2] REFUTED_BY is predicate- and scope-bound")
check("every refuter has a predicate and legal scope", all(
    item.get("predicate_id") and
    item.get("scope") in {"NODE", "PATH", "INTERVENTION"}
    for item in refuters))
counter = next(item for item in refuters
               if item["evidence"] == "counterfactual_index")
check("counterfactual index refutes only one intervention",
      counter.get("scope") == "INTERVENTION" and
      counter.get("target_fix") == "create_covering_index")
check("dead tuple ratio has no table-bloat refutation edge", not any(
    item["cause"] == "table_bloat" and
    item["evidence"] == "dead_tuple_ratio" for item in refuters))
check("cumulative negatives require an incident window", all(
    item.get("window_required") is True for item in refuters
    if item["evidence"] in {
        "deadlock_count", "temp_file_volume", "checkpoint_stats"}))

print("\n[3] Structured predicates preserve target and window scope")
node_ctx = PredicateContext(target_kind="NODE", target_ids=("missing_index",))
fix_ctx = PredicateContext(target_kind="INTERVENTION",
                           target_ids=("create_covering_index",))
other_fix_ctx = PredicateContext(target_kind="INTERVENTION",
                                 target_ids=("analyze_table",))
counter_value = {"would_be_used": False, "trivial_baseline": False}
check("failed index simulation refutes its concrete fix",
      evaluate("counterfactual_index_v2", counter_value,
               context=fix_ctx).result == "REFUTES")
check("the same simulation cannot refute the root node",
      evaluate("counterfactual_index_v2", counter_value,
               context=node_ctx).result == "NOT_APPLICABLE")
check("the same simulation cannot refute a different fix",
      evaluate("counterfactual_index_v2", counter_value,
               context=other_fix_ctx).result == "NOT_APPLICABLE")

window_ctx = PredicateContext(
    target_kind="PATH", target_ids=("path_fixture",),
    window_start=10.0, window_end=20.0, source_epoch="epoch_a")
no_window_ctx = PredicateContext(target_kind="PATH",
                                 target_ids=("path_fixture",))
check("zero deadlock delta refutes only with a complete window",
      evaluate("deadlock_count_v2", {"deadlocks": 0, "source_epoch": "epoch_a"},
               context=window_ctx).result == "REFUTES" and
      evaluate("deadlock_count_v2", {"deadlocks": 0},
               context=no_window_ctx).result == "NOT_APPLICABLE")
check("source-epoch mismatch is not a negative",
      evaluate("deadlock_count_v2", {"deadlocks": 0,
                                      "source_epoch": "epoch_b"},
               context=window_ctx).result == "NOT_APPLICABLE")
check("UNKNOWN never supports or refutes",
      evaluate("disk_usage_v2", {"used_pct": 10.0},
               context=PredicateContext(
                   target_kind="NODE", target_ids=("disk_pressure",),
                   collection_status="UNKNOWN")).result == "NOT_APPLICABLE")

print("\n[4] Physical bloat cannot be inferred from dead tuples")
check("physical measurement is the table-bloat required evidence",
      G.required_evidence("table_bloat") == ["physical_bloat_ratio"])
check("dead tuples alone are causally neutral for physical bloat",
      evaluate("dead_tuple_ratio_v2", {"dead_ratio": 0.95},
               context=PredicateContext(
                   target_kind="NODE", target_ids=("table_bloat",))).result ==
      "NEUTRAL")
physical_ctx = PredicateContext(target_kind="NODE",
                                target_ids=("table_bloat",))
check("unavailable physical tool remains inconclusive",
      evaluate("physical_bloat_ratio_v2", {
          "availability": "UNAVAILABLE",
          "algorithm": "pgstattuple_approx_reclaimable_pct_v1",
      }, context=physical_ctx).result == "NOT_APPLICABLE")


class _UnavailableBloatObserver:
    @staticmethod
    def get_physical_bloat(_table: str) -> dict:
        return {
            "availability": "UNAVAILABLE",
            "algorithm": "pgstattuple_approx_reclaimable_pct_v1",
            "reason": "extension is unavailable",
            "raw_ref": "trace://bloat_unavailable/step_001",
        }


unavailable_state = EpisodeState("bloat_unavailable", "fixture")
unavailable_toolbox = Toolbox(
    _UnavailableBloatObserver(), unavailable_state,
    StateMachine(unavailable_state))
unavailable_toolbox.get_physical_bloat("orders")
check("tool/extension unavailability is persisted as UNKNOWN",
      unavailable_state.scratchpad[-1]["status"] ==
      EvidenceStatus.UNKNOWN.value)
check("measured physical ratio supports/refutes by explicit thresholds",
      evaluate("physical_bloat_ratio_v2", {
          "availability": "AVAILABLE",
          "algorithm": "pgstattuple_approx_reclaimable_pct_v1",
          "reclaimable_pct": 30.0,
      }, context=physical_ctx).result == "SUPPORTS" and
      evaluate("physical_bloat_ratio_v2", {
          "availability": "AVAILABLE",
          "algorithm": "pgstattuple_approx_reclaimable_pct_v1",
          "reclaimable_pct": 5.0,
      }, context=physical_ctx).result == "REFUTES")

table_state = EpisodeState("table_bloat_contract", "fixture")
table_state.claimed_fault_class = "table_bloat"
table_state.note("fixture", "dead_tuple_ratio", "dead_ratio=0.95",
                 structured_value={"dead_ratio": 0.95})
table_report = esc.check(table_state, ["table_bloat"])
table_d1 = next(item for item in table_report.dims if item.name == "D1")
check("dead tuples cannot pass the table-bloat required gate", not table_d1.passed)

print("\n[5] Intervention refutation never cascades to root refutation")
root = EpisodeState("index_scope_contract", "fixture")
root.symptoms = ["latency p99 up"]
root.claimed_fault_class = "missing_index"
root.set_verdict("missing_index", Verdict.CONFIRMED,
                 note="sequential scan filters millions of rows")
root.note("fixture", "explain_seq_scan", "legacy",
          structured_value={"scan_types": ["Seq Scan"],
                            "rows_removed_by_filter": 1_000_000})
root.note("fixture", "index_existence", "legacy",
          structured_value={"inventory_collected": True, "indexes": []})
root.note("fixture", "counterfactual_index", "legacy",
          structured_value=counter_value, target_kind="INTERVENTION",
          target_ids=["create_covering_index"])
root_report = esc.check(root, ["missing_index"])
root_d5 = next(item for item in root_report.dims if item.name == "D5")
check("failed concrete index leaves root explanation eligible",
      not root_d5.passed and root_report.verdict == "SUFFICIENT",
      root_report.verdict)

competitor = EpisodeState("index_scope_competitor", "fixture")
competitor.claimed_fault_class = "stale_statistics"
competitor.set_verdict("stale_statistics", Verdict.CONFIRMED,
                       note="row estimates differ by more than ten times")
competitor.set_verdict("missing_index", Verdict.REFUTED,
                       note="one hypothetical definition was not selected")
competitor.note("fixture", "row_estimate_deviation", "legacy",
                structured_value={"max_ratio": 100.0})
competitor.note("fixture", "counterfactual_index", "legacy",
                structured_value=counter_value, target_kind="INTERVENTION",
                target_ids=["create_covering_index"])
competitor_report = esc.check(
    competitor, ["stale_statistics", "missing_index"], min_refute_ratio=1.0)
competitor_d2 = next(item for item in competitor_report.dims if item.name == "D2")
check("failed intervention cannot back a missing-index root refutation",
      not competitor_d2.passed and "无证据支撑" in competitor_d2.detail,
      competitor_d2.detail)

print("\n" + "=" * 76)
print("CAUSAL SEMANTICS:", "PASS" if not failures else f"FAIL {failures}")
sys.exit(1 if failures else 0)
