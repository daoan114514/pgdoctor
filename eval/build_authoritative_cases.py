"""Build the source-grounded 100-case replay set and reviewed L1 seeds.

The cases are controlled replays, not claims that 100 independent production
incidents were downloaded.  Public PostgreSQL/AWS/GCP material grounds the
failure mechanism and alert signature; numeric observations are deterministic
variants designed to exercise the graph and predicate boundary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import networkx as nx
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.explanation import CausalPath, stable_id
from knowledge import case_store
from knowledge.causal_graph import graph as G
from knowledge.evidence_predicates import PredicateContext, evaluate


SOURCE_FILE = ROOT / "eval" / "authoritative_sources.yaml"
OUTPUT_FILE = ROOT / "eval" / "authoritative_cases_v2.yaml"


ROOTS = {
    "missing_index": {
        "symptom": "latency_p99_up", "path": ["missing_index", "latency_p99_up"],
        "category": "query_tuning", "sources": ["pg_explain", "pg_planner_stats"],
        "fix_id": "create_covering_index", "action": "CORRECTIVE",
        "alert": "p99 latency crossed 400 ms while CPU rose above 150%",
    },
    "stale_statistics": {
        "symptom": "latency_p99_up", "path": ["stale_statistics", "latency_p99_up"],
        "category": "query_tuning", "sources": ["pg_explain", "pg_planner_stats"],
        "fix_id": "analyze_table", "action": "CORRECTIVE",
        "alert": "p99 latency regressed after a large data distribution change",
    },
    "lock_contention": {
        "symptom": "latency_p99_up", "path": ["lock_contention", "latency_p99_up"],
        "category": "system_failure", "sources": ["pg_locks", "pg_blocking_pids"],
        "fix_id": "terminate_blocker", "action": "CONTAINMENT",
        "alert": "blocked sessions exceeded threshold and p99 timed out",
    },
    "table_bloat": {
        "symptom": "disk_growing", "path": ["table_bloat", "disk_growing"],
        "category": "health_check", "sources": ["pg_routine_vacuuming"],
        "fix_id": "vacuum_table", "action": "MITIGATION",
        "alert": "disk usage is growing while the orders relation expands",
    },
    "autovacuum_starvation": {
        "symptom": "autovacuum_unhealthy",
        "path": ["autovacuum_starvation", "autovacuum_unhealthy"],
        "category": "health_check", "sources": ["pg_routine_vacuuming", "aws_autovacuum"],
        "fix_id": "enable_autovacuum", "action": "CORRECTIVE",
        "alert": "autovacuum unhealthy on orders: disabled or backlog rising",
    },
    "connection_exhaustion": {
        "symptom": "conn_near_limit",
        "path": ["connection_exhaustion", "conn_near_limit"],
        "category": "resource_governance", "sources": ["pg_connection_incident"],
        "fix_id": "terminate_idle_backend", "action": "CONTAINMENT",
        "alert": "FATAL: sorry, too many clients already; connection usage near limit",
    },
    "long_idle_transaction": {
        "symptom": "conn_near_limit",
        "path": ["long_idle_transaction", "connection_exhaustion", "conn_near_limit"],
        "category": "misleading_alerts", "sources": ["pg_routine_vacuuming", "gcp_txid_wraparound"],
        "fix_id": "terminate_idle_transaction", "action": "CONTAINMENT",
        "alert": "connection usage near limit with sessions idle in transaction",
    },
    "xid_wraparound_risk": {
        "symptom": "disk_growing", "path": ["xid_wraparound_risk", "disk_growing"],
        "category": "system_failure", "sources": ["pg_routine_vacuuming", "gcp_txid_wraparound", "aws_autovacuum"],
        "fix_id": "vacuum_database", "action": "CORRECTIVE",
        "alert": "database must be vacuumed soon; disk usage growing",
    },
    "disk_pressure": {
        "symptom": "disk_growing", "path": ["disk_pressure", "disk_growing"],
        "category": "resource_governance", "sources": ["pg_replication_slots", "pg_checkpoints"],
        "fix_id": "remediate_disk_capacity", "action": "MANUAL",
        "alert": "disk usage above 90% and still growing",
    },
    "stale_replication_slot": {
        "symptom": "disk_growing", "path": ["stale_replication_slot", "disk_growing"],
        "category": "system_failure", "sources": ["pg_replication_slots", "pg_replication_slots_view", "gcp_txid_wraparound"],
        "fix_id": "drop_replication_slot", "action": "MANUAL",
        "alert": "pg_wal disk usage growing while an inactive replication slot retains WAL",
    },
    "orphaned_prepared_transaction": {
        "symptom": "autovacuum_unhealthy",
        "path": ["orphaned_prepared_transaction", "autovacuum_starvation", "autovacuum_unhealthy"],
        "category": "composite_faults", "sources": ["pg_routine_vacuuming", "gcp_txid_wraparound"],
        "fix_id": "resolve_prepared_xact", "action": "MANUAL",
        "alert": "autovacuum unhealthy while an old prepared transaction holds the xmin horizon",
    },
    "deadlock": {
        "symptom": "queries_blocked", "path": ["deadlock", "queries_blocked"],
        "category": "system_failure", "sources": ["pg_locks", "pg_statistics"],
        "fix_id": "remediate_deadlock_pattern", "action": "MANUAL",
        "alert": "deadlock detected; blocked transactions were aborted",
    },
    "work_mem_spill": {
        "symptom": "latency_p99_up", "path": ["work_mem_spill", "latency_p99_up"],
        "category": "resource_governance", "sources": ["pg_work_mem", "pg_statistics"],
        "fix_id": "raise_work_mem", "action": "MITIGATION",
        "alert": "p99 latency increased while sort/hash temporary files grew",
    },
    "checkpoint_pressure": {
        "symptom": "latency_p99_up", "path": ["checkpoint_pressure", "latency_p99_up"],
        "category": "resource_governance", "sources": ["pg_checkpoints", "pg_statistics"],
        "fix_id": "raise_max_wal_size", "action": "MITIGATION",
        "alert": "checkpoints are occurring too frequently and p99 latency increased",
    },
}


def _graph_path(node_ids: list[str]) -> CausalPath:
    graph = G.load()
    edge_ids = []
    for source, target in zip(node_ids, node_ids[1:]):
        relation = graph.get_edge_data(source, target, key="CAUSES")
        if relation is None:
            raise ValueError(f"missing CAUSES edge {source}->{target}")
        edge_ids.append(relation["edge_id"])
    return CausalPath.create(
        graph_version=G.graph_version(), node_ids=node_ids, edge_ids=edge_ids,
        observed_symptom_id=node_ids[-1], source="case_template",
        required_evidence_types=sorted({
            evidence for cause in node_ids[:-1]
            for evidence in G.required_evidence(cause)
        }),
    )


def _alternatives(root: str, symptom: str, expected: CausalPath) -> list[CausalPath]:
    graph = G.load()
    causes = nx.DiGraph()
    causes.add_nodes_from(graph.nodes)
    causes.add_edges_from((a, b) for a, b, key in graph.edges(keys=True)
                          if key == "CAUSES")
    out = [expected]
    for candidate in sorted(ROOTS):
        if candidate == root or not nx.has_path(causes, candidate, symptom):
            continue
        paths = list(nx.all_simple_paths(causes, candidate, symptom, cutoff=4))
        if not paths:
            continue
        out.append(_graph_path(min(paths, key=lambda p: (len(p), p))))
        if len(out) == 3:
            break
    return out


def _profile(root: str, variant: int) -> dict:
    scale = variant + 1
    truth = root
    plan = {
        "total_time_ms": 18.0, "scan_types": ["Index Scan on orders"],
        "rows_removed_by_filter": 0, "rows_est_vs_actual": [[100, 95]],
        "indexes_used": ["idx_orders_user_status"], "parallel_workers": 0,
        "top_nodes": ["Index Scan"],
    }
    indexes = [{"name": "idx_orders_user_status",
                "definition": "CREATE INDEX idx_orders_user_status ON orders(user_id, status)",
                "size": "64 MB", "scans": 1000}]
    table = {
        "table": "orders", "n_live_tup": 12_000_000, "n_dead_tup": 20_000,
        "dead_ratio": 0.0017, "last_analyze": "2026-09-03T08:00:00Z",
        "last_autovacuum": "2026-09-03T08:05:00Z", "total_size": "4 GB",
        "autovacuum_enabled": True, "autovacuum_running": False,
        "autovacuum_trigger": 2_400_050,
    }
    physical = {
        "availability": "AVAILABLE",
        "algorithm": "pgstattuple_approx_reclaimable_pct_v1",
        "table_bytes": 4_294_967_296, "dead_tuple_percent": 1.0,
        "free_percent": 2.0, "reclaimable_pct": 3.0,
    }
    sessions: list[dict] = []
    blockers: list[dict] = []
    connections = {
        "used": 18, "max_connections": 100, "pct": 18.0,
        "by_user": {"app": 12, "agent_ro": 1},
        "by_state": {"active": 8, "idle": 10},
        "idle_in_transaction": 0, "near_limit": False,
    }
    horizon = {
        "db_xid_age": 10_000_000, "oldest_table": "orders",
        "oldest_table_xid_age": 9_000_000, "freeze_max_age": 200_000_000,
        "slots": [], "prepared_xacts": [], "oldest_backend_pid": None,
        "oldest_backend_xmin_age": 0, "wraparound_pct": 5.0,
        "at_risk": False, "xmin_holders": [],
    }
    stats_delta = {
        "deadlocks": 0, "temp_files": 0, "temp_bytes": 0,
        "xact_commit": 1000 + scale, "xact_rollback": 0,
        "ckpt_timed": 4, "ckpt_requested": 0,
        "ckpt_write_time_ms": 100.0, "ckpt_sync_time_ms": 10.0,
    }
    disk = {"used_pct": 42.0, "free_bytes": 120 * 1024**3,
            "path": "/var/lib/postgresql/16/main"}
    simulated = {"would_be_used": False, "cost_before": 100.0,
                 "cost_after": 100.0, "cost_reduction_pct": 0.0,
                 "trivial_baseline": False}

    if truth == "missing_index":
        plan.update(total_time_ms=700 + scale * 20,
                    scan_types=["Seq Scan on orders"],
                    rows_removed_by_filter=500_000 + scale * 120_000,
                    indexes_used=[], rows_est_vs_actual=[[120, 118]])
        indexes = [{"name": "orders_pkey", "definition":
                    "CREATE UNIQUE INDEX orders_pkey ON orders(id)",
                    "size": "256 MB", "scans": 5000}]
        simulated.update(would_be_used=True, cost_before=90_000.0,
                         cost_after=400.0, cost_reduction_pct=99.5)
    elif truth == "stale_statistics":
        plan.update(total_time_ms=900 + scale * 15,
                    scan_types=["Index Scan on orders"],
                    rows_est_vs_actual=[[10, 5000 + scale * 1000]])
        table["last_analyze"] = ""
    elif truth == "lock_contention":
        sessions = [{"pid": 5100 + scale, "state": "active",
                     "wait_event": "Lock:transactionid", "duration_s": 35.0,
                     "query_fingerprint": "UPDATE orders SET status = ?",
                     "role": "app", "transaction_age_seconds": 40.0,
                     "backend_type": "client backend"}]
        blockers = [{"blocked_pid": 5200 + scale, "blocked_by": 5100 + scale,
                     "pid": 5100 + scale, "wait": "Lock:transactionid",
                     "role": "app", "state": "idle in transaction",
                     "transaction_age_seconds": 45, "backend_type": "client backend",
                     "is_topmost_blocker": True, "blocking_impact": 4,
                     "identity_rechecked": True}]
    elif truth == "table_bloat":
        table.update(n_dead_tup=4_000_000 + scale * 50_000, dead_ratio=0.33)
        physical.update(dead_tuple_percent=25.0 + scale,
                        free_percent=12.0, reclaimable_pct=37.0 + scale)
    elif truth == "autovacuum_starvation":
        table.update(n_dead_tup=6_000_000 + scale * 100_000,
                     dead_ratio=0.5, autovacuum_enabled=(variant % 2 == 1),
                     autovacuum_running=False, autovacuum_trigger=2_000_000,
                     last_autovacuum="")
    elif truth == "connection_exhaustion":
        used = 94 + variant % 5
        connections.update(used=used, pct=float(used), near_limit=True,
                           by_user={"app": used - 2, "agent_ro": 1},
                           by_state={"idle": used - 12, "active": 12})
    elif truth == "long_idle_transaction":
        idle = 4 + variant % 4
        connections.update(used=88 + variant % 5, pct=90.0, near_limit=True,
                           idle_in_transaction=idle,
                           by_state={"idle in transaction": idle, "idle": 70,
                                     "active": 14})
        sessions = [{"pid": 6100 + scale, "state": "idle in transaction",
                     "wait_event": "Lock:transactionid", "duration_s": 7200.0,
                     "query_fingerprint": "UPDATE orders SET status = ?",
                     "role": "app", "transaction_age_seconds": 7200.0,
                     "backend_type": "client backend", "backend_xmin": "700"}]
        horizon.update(oldest_backend_pid=6100 + scale,
                       oldest_backend_xmin_age=2_000_000 + scale * 100_000,
                       xmin_holders=["long_transaction"])
    elif truth == "xid_wraparound_risk":
        horizon.update(db_xid_age=150_000_000 + scale * 2_000_000,
                       oldest_table_xid_age=145_000_000,
                       wraparound_pct=75.0 + scale, at_risk=True)
    elif truth == "disk_pressure":
        disk.update(used_pct=90.0 + variant, free_bytes=(8 - variant % 4) * 1024**3)
    elif truth == "stale_replication_slot":
        horizon["slots"] = [{
            "slot_name": f"orders_replica_{scale}", "active": False,
            "xmin_age": 1_500_000 + scale * 100_000, "catalog_xmin_age": 0,
            "retained_wal_bytes": (2 + variant) * 1024**3,
        }]
        horizon["xmin_holders"] = ["replication_slot"]
        disk.update(used_pct=86.0 + variant, free_bytes=12 * 1024**3)
    elif truth == "orphaned_prepared_transaction":
        horizon["prepared_xacts"] = [{
            "gid": f"orders_2pc_{scale}", "xid_age": 1_400_000 + scale * 100_000,
            "prepared_age_s": 4000 + scale * 600,
        }]
        horizon["xmin_holders"] = ["prepared_transaction"]
        table.update(n_dead_tup=5_000_000, autovacuum_enabled=True,
                     autovacuum_running=False, autovacuum_trigger=2_000_000)
    elif truth == "deadlock":
        stats_delta.update(deadlocks=1 + variant % 3, xact_rollback=2 + variant % 3)
    elif truth == "work_mem_spill":
        stats_delta.update(temp_files=3 + scale,
                           temp_bytes=(256 + variant * 64) * 1024**2)
    elif truth == "checkpoint_pressure":
        stats_delta.update(ckpt_timed=1, ckpt_requested=5 + scale,
                           ckpt_write_time_ms=12_000 + variant * 1000)

    return {
        "explain_query": plan, "get_indexes": indexes,
        "get_table_stats": table, "get_physical_bloat": physical,
        "get_active_sessions": sessions, "get_blocking_chain": blockers,
        "get_connection_stats": connections, "get_vacuum_horizon": horizon,
        "get_database_stats": {"delta": stats_delta, "disk_usage": disk,
                               "source_epoch": f"epoch_{root}_{variant}"},
        "simulate_index": simulated,
        "get_top_queries": [{"queryid": "42", "calls": 500,
                              "mean_ms": plan["total_time_ms"],
                              "total_ms": plan["total_time_ms"] * 500,
                              "rows": 1,
                              "query": "SELECT id,total FROM orders WHERE user_id=$1 AND status='PENDING'"}],
    }


def _value_for_evidence(evidence_type: str, profile: dict) -> object:
    mapping = {
        "explain_seq_scan": profile["explain_query"],
        "explain_plan": profile["explain_query"],
        "index_existence": {"table": "orders", "indexes": profile["get_indexes"],
                            "inventory_collected": True},
        "stats_freshness": profile["get_table_stats"],
        "row_estimate_deviation": {
            **profile["explain_query"],
            "max_ratio": max((max(a / max(b, 1), b / max(a, 1))
                              for a, b in profile["explain_query"]["rows_est_vs_actual"]),
                             default=0.0),
        },
        "lock_blocking_chain": {"chains": profile["get_blocking_chain"]},
        "session_wait_profile": profile["get_active_sessions"],
        "physical_bloat_ratio": profile["get_physical_bloat"],
        "dead_tuple_ratio": profile["get_table_stats"],
        "autovacuum_health": profile["get_table_stats"],
        "connection_count": profile["get_connection_stats"],
        "idle_in_transaction": profile["get_connection_stats"],
        "xid_age": profile["get_vacuum_horizon"],
        "backend_xmin_age": profile["get_vacuum_horizon"],
        "replication_slot_age": profile["get_vacuum_horizon"],
        "prepared_xact_age": profile["get_vacuum_horizon"],
        "deadlock_count": profile["get_database_stats"]["delta"],
        "temp_file_volume": profile["get_database_stats"]["delta"],
        "checkpoint_stats": profile["get_database_stats"]["delta"],
        "disk_usage": profile["get_database_stats"]["disk_usage"],
        "slow_query_ranking": profile["get_top_queries"],
        "counterfactual_index": profile["simulate_index"],
    }
    return mapping[evidence_type]


def _binding(case_id: str, target: str, evidence_type: str,
             predicate_id: str, value: object, ordinal: int) -> dict:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    windowed = predicate_id in {
        "lock_blocking_chain_v2", "deadlock_count_v2",
        "temp_file_volume_v2", "checkpoint_stats_v2",
    }
    decision = evaluate(
        predicate_id, value,
        context=PredicateContext(
            target_kind="NODE", target_ids=(target,),
            collection_status="OBSERVED",
            window_start=1.0 if windowed else None,
            window_end=2.0 if windowed else None,
            source_epoch=f"epoch_{case_id}" if windowed else ""))
    return {
        "binding_id": stable_id("source_binding", {
            "case_id": case_id, "target": target,
            "evidence_type": evidence_type}),
        "episode_id": case_id,
        "raw_ref": f"source-trace://{case_id}/observation_{ordinal:02d}",
        "evidence_type": evidence_type,
        "status": "OBSERVED",
        "predicate_id": predicate_id,
        "predicate_result": decision.result,
        "target_node_ids": [target], "target_edge_ids": [],
        "structured_value": value,
        "value_digest": hashlib.sha256(canonical.encode()).hexdigest(),
        "summary": decision.reason,
        "window_start": 1.0 if windowed else None,
        "window_end": 2.0 if windowed else None,
        "source_epoch": f"epoch_{case_id}" if windowed else "",
    }


def build() -> dict:
    source_doc = yaml.safe_load(SOURCE_FILE.read_text(encoding="utf-8"))
    source_catalog = source_doc["sources"]
    cases = []
    ordered_roots = list(ROOTS)
    for root_index, (root, config) in enumerate(ROOTS.items()):
        count = 8 if root in {"missing_index", "long_idle_transaction"} else 7
        for variant in range(count):
            split = "train" if variant < 5 else "eval"
            case_id = f"authority_{root}_{variant + 1:02d}"
            expected = _graph_path(config["path"])
            candidates = _alternatives(root, config["symptom"], expected)
            profile = _profile(root, variant)
            refs = [{"source_id": source_id, **source_catalog[source_id],
                     "verified_at": source_doc["verified_at"]}
                    for source_id in config["sources"]]
            required = list(expected.required_evidence_types)
            bindings = []
            graph = G.load()
            binding_keys: set[tuple[str, str]] = set()

            def add_binding(target: str, evidence_type: str) -> dict:
                key = (target, evidence_type)
                if key in binding_keys:
                    return next(item for item in bindings
                                if item["target_node_ids"] == [target] and
                                item["evidence_type"] == evidence_type)
                binding_keys.add(key)
                predicate_id = graph.nodes[evidence_type]["predicate_id"]
                item = _binding(
                    case_id, target, evidence_type, predicate_id,
                    _value_for_evidence(evidence_type, profile),
                    len(bindings) + 1)
                bindings.append(item)
                return item

            for cause in expected.node_ids[:-1]:
                for evidence_type in G.required_evidence(cause):
                    item = add_binding(cause, evidence_type)
                    if item["predicate_result"] != "SUPPORTS":
                        raise ValueError(
                            f"{case_id}: selected {cause} lacks supporting "
                            f"{evidence_type}: {item['predicate_result']}")

            p0_expectations = {}
            cause_graph = nx.DiGraph()
            cause_graph.add_nodes_from(graph.nodes)
            cause_graph.add_edges_from((a, b) for a, b, key in
                                       graph.edges(keys=True)
                                       if key == "CAUSES")
            for p0, node_data in graph.nodes(data=True):
                if node_data.get("severity") != "P0":
                    continue
                if not nx.has_path(cause_graph, p0, config["symptom"]):
                    continue
                p0_bindings = [add_binding(p0, evidence_type)
                               for evidence_type in G.required_evidence(p0)]
                results = {item["predicate_result"] for item in p0_bindings}
                status = ("SUPPORTED" if results == {"SUPPORTS"} else
                          "REFUTED" if "REFUTES" in results else
                          "INCONCLUSIVE")
                p0_expectations[p0] = {
                    "status": status,
                    "required_evidence_types": list(G.required_evidence(p0)),
                    "evidence_binding_ids": [item["binding_id"]
                                             for item in p0_bindings],
                }

            p99_factor = [2.5, 5.5, 22.0, 8.0, 3.5, 6.0, 25.0, 4.0][variant]
            cpu_factor = 1 if root in {"lock_contention", "connection_exhaustion",
                                      "long_idle_transaction", "deadlock"} else 2
            fingerprint = {
                "metric_deltas": {
                    "p99_ms": case_store._bucket(p99_factor),
                    "cpu_pct": case_store._bucket(cpu_factor),
                },
                "wait_profile": ({"Lock": 1} if root in {
                    "lock_contention", "long_idle_transaction"}
                                 else {"none": 1}),
                "query_scope": "single_query_dominant",
                "onset": ("gradual" if root in {
                    "table_bloat", "xid_wraparound_risk", "disk_pressure",
                    "stale_replication_slot"} else "sudden"),
                "object_scope": "single_table",
            }
            cases.append({
                "case_id": case_id, "revision": 2, "split": split,
                "fault_class": root, "category": config["category"],
                "difficulty": "hard" if len(config["path"]) > 2 or
                              root in {"xid_wraparound_risk", "stale_replication_slot",
                                       "orphaned_prepared_transaction"} else "easy",
                "fidelity": "source_grounded_controlled_replay",
                "source_grounding": (
                    "Public sources ground the mechanism and alert pattern; "
                    "numeric values are deterministic controlled-test variants."),
                "source_refs": refs,
                "fingerprint": fingerprint,
                "alert": f"{config['alert']} [variant {variant + 1}]",
                "observed_symptoms": [config["symptom"]],
                "metrics": {
                    "baseline": {"p50_ms": 8.0, "p95_ms": 16.0,
                                 "p99_ms": 25.0, "qps": 500.0,
                                 "errors": 0, "cpu_pct": 25.0, "samples": 1000,
                                 "stale": False},
                    "fault": {"p50_ms": 20.0, "p95_ms": 100.0,
                              "p99_ms": 25.0 * p99_factor, "qps": 120.0,
                              "errors": (4 + variant if root in {
                                  "connection_exhaustion", "deadlock", "lock_contention"}
                                         else 0),
                              "cpu_pct": 25.0 * cpu_factor, "samples": 300,
                              "stale": False},
                    "recovered": {"p50_ms": 9.0, "p95_ms": 18.0,
                                  "p99_ms": 35.0, "qps": 450.0,
                                  "errors": 0, "cpu_pct": 30.0,
                                  "samples": 1000, "stale": False},
                },
                "hot_query": "SELECT id,total FROM orders WHERE user_id=%(uid)s AND status='PENDING'",
                "expected": {
                    "root_cause": root,
                    "path_id": expected.path_id,
                    "node_ids": expected.node_ids,
                    "edge_ids": expected.edge_ids,
                    "required_evidence_types": required,
                    "fix_id": config["fix_id"],
                    "intervention_kind": config["action"],
                    "automated_outcome_possible": config["action"] != "MANUAL",
                },
                "candidate_paths": [{
                    "path_id": path.path_id, "node_ids": path.node_ids,
                    "edge_ids": path.edge_ids,
                    "observed_symptom_id": path.observed_symptom_id,
                    "selected": path.path_id == expected.path_id,
                } for path in candidates],
                "decisive_evidence_bindings": bindings,
                "p0_expectations": p0_expectations,
                "observations": profile,
                "seed": root_index * 100 + variant,
            })
    assert len(cases) == 100
    assert sum(case["split"] == "train" for case in cases) == 70
    assert sum(case["split"] == "eval" for case in cases) == 30
    return {
        "schema_version": 2,
        "dataset_id": "pgdoctor_authoritative_replay_v2",
        "revision": 2,
        "generated_from_graph_version": G.graph_version(),
        "source_catalog": str(SOURCE_FILE.relative_to(ROOT)).replace("\\", "/"),
        "fidelity_notice": (
            "These are source-grounded controlled replays, not the unpublished "
            "official DBA-Bench scenarios and not 100 independent production incidents."),
        "splits": {"train": 70, "eval": 30},
        "cases": cases,
    }


def install_l1_seeds(document: dict) -> None:
    existing = {case.case_id: case for case in case_store.load_cases_v2()}
    for case_id, case in list(existing.items()):
        if case_id.startswith("authoritative_seed_"):
            del existing[case_id]
        elif case_id.startswith("fixture_") and not case.source_refs:
            case.status = "quarantined"
            case.review_status = "quarantined"
            case.evidence_quality = "legacy_unverified_fixture"
            case.training_eligible = False

    for item in document["cases"]:
        if item["split"] != "train":
            continue
        selected = [path for path in item["candidate_paths"] if path["selected"]]
        seed = case_store.CaseV2(
            case_id=f"authoritative_seed_{item['case_id']}",
            scenario_id=item["case_id"], provenance="human_labeled",
            split="train",
            fingerprint=item["fingerprint"],
            env={"pg_major": "16", "scenario_revision": item["revision"],
                 "fidelity": item["fidelity"]},
            graph_version=document["generated_from_graph_version"],
            observed_symptoms=item["observed_symptoms"],
            candidate_paths=item["candidate_paths"],
            selected_path_ids=[path["path_id"] for path in selected],
            decisive_evidence_bindings=item["decisive_evidence_bindings"],
            excluded_branches=[{"path_id": path["path_id"],
                                "status": "HUMAN_LABELED_ALTERNATIVE"}
                               for path in item["candidate_paths"]
                               if not path["selected"]],
            p0_obligations=item["p0_expectations"],
            outcome="SOURCE_GROUNDED_HUMAN_LABEL",
            source_refs=item["source_refs"],
            trace_ref=f"eval://authoritative_cases_v2.yaml#{item['case_id']}",
            evidence_quality="source_grounded_structured_replay",
            review_status="approved", training_eligible=True,
            utility_score=0.55, status="active",
        )
        existing[seed.case_id] = seed
    case_store.save_cases_v2(list(existing.values()))


def _sync_manifest_graph_version() -> str:
    """把 manifest 里记的 graph_version 同步成当前图的版本。

    以前这步靠手改。改图 -> 数据集失效 -> 重生成 -> 忘了改 manifest，
    是个必然会踩的顺序；而 manifest 记的版本正是给 reader 判模板是否
    过期用的，记错了不会立刻报错，只会让 L1 召回悄悄失效。生成器自己
    知道当前版本，让它顺手写掉。
    """
    from knowledge.causal_graph import graph as causal_graph
    version = causal_graph.graph_version()
    manifest = ROOT / "knowledge" / "learned" / "v2" / "manifest.yaml"
    if not manifest.exists():
        return ""
    text = manifest.read_text(encoding="utf-8")
    updated = re.sub(r"graph_version: graph_[0-9a-f]+",
                     f"graph_version: {version}", text)
    if updated != text:
        manifest.write_text(updated, encoding="utf-8")
    return version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-l1-seeds", action="store_true")
    args = parser.parse_args()
    document = build()
    OUTPUT_FILE.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False,
                       width=100), encoding="utf-8")
    if args.install_l1_seeds:
        install_l1_seeds(document)
    manifest_synced = _sync_manifest_graph_version()
    print(json.dumps({
        "output": str(OUTPUT_FILE), "cases": len(document["cases"]),
        "train": document["splits"]["train"],
        "eval": document["splits"]["eval"],
        "l1_installed": args.install_l1_seeds,
        "manifest_graph_version": manifest_synced,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
