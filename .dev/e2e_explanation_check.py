"""State-machine E2E checks for read-only multi-hop and dual-root rollback."""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import explanation_runtime as xr
from agent.episode_state import EpisodeState
from agent.explanation import CausalStatus, EvidenceNeed, EvidenceTargetKind
from agent.loop import run_episode
from agent.policy import Policy
from agent.state_machine import Phase
from agent.tool_planner import (ToolPlanningConfig, infer_target_context,
                                plan_evidence_tasks)
from knowledge.causal_graph import graph as G
from sandbox import db
from sandbox.metrics import KPI
from sandbox.observe import ExplainDigest, SessionDigest, TableStats
from sandbox.scoring import RegressionResult
from sandbox.traces import TRACE_DIR, TraceStore


INDEX_NAME = "idx_pgdoctor_e2e_dual_context"
ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<62} {detail}")


class ControlledObserver:
    """Trace-backed monitoring provider; no verdicts are injected."""

    def __init__(self, episode_id: str, *, multi_hop: bool = False):
        self.trace = TraceStore(episode_id)
        self.multi_hop = multi_hop
        self.last_raw_refs: dict[str, str] = {}
        self.stats_calls = 0

    def _record(self, tool: str, value) -> str:
        ref = self.trace.record(
            tool, {}, json.dumps(value, ensure_ascii=False, default=str), value)
        self.last_raw_refs[tool] = ref
        return ref

    def raw_ref_for(self, tool: str) -> str:
        return self.last_raw_refs.get(tool, "")

    @staticmethod
    def extension_available(_name: str) -> bool:
        return True

    def get_active_sessions(self):
        rows = ([SessionDigest(
            pid=4242, state="idle in transaction",
            wait_event="Lock:transactionid", duration_s=900.0,
            query_fingerprint="UPDATE orders SET status=status",
            role="app_user", transaction_age_seconds=900.0,
            backend_type="client backend", backend_xmin="123",
            is_current_diagnostic_connection=False,
            is_system_or_diagnostic=False, identity_rechecked=True,
        )] if self.multi_hop else [])
        self._record("get_active_sessions", {
            "sessions": [row.__dict__ for row in rows]})
        return rows

    def get_connection_stats(self):
        idle = 4 if self.multi_hop else 0
        near = self.multi_hop
        used = 95 if near else 10
        value = {
            "used": used, "max_connections": 100, "pct": float(used),
            "near_limit": near, "idle_in_transaction": idle,
            "by_user": {"app_user": used},
            "by_state": {"idle in transaction": idle, "idle": used - idle},
        }
        value["raw_ref"] = self._record("get_connection_stats", value)
        return value

    def get_blocking_chain(self):
        rows: list[dict] = []
        self._record("get_blocking_chain", {"chains": rows})
        return rows

    def explain_query(self, _sql: str, _params=None):
        value = {
            "total_time_ms": 800.0,
            "scan_types": ["Seq Scan on orders"],
            "rows_removed_by_filter": 500_000,
            "rows_est_vs_actual": [(100, 100)],
            "indexes_used": [], "parallel_workers": 0,
            "top_nodes": ["800ms Seq Scan"],
        }
        ref = self._record("explain_query", value)
        return ExplainDigest(raw_ref=ref, **value)

    def get_indexes(self, _table: str):
        rows: list[dict] = []
        self._record("get_indexes", {"indexes": rows})
        return rows

    def get_top_queries(self, _n: int = 5):
        rows = [{"queryid": "1", "calls": 20, "mean_ms": 800.0,
                 "total_ms": 16_000.0, "rows": 20,
                 "query": "SELECT * FROM orders WHERE user_id=$1"}]
        self._record("get_top_queries", {"queries": rows})
        return rows

    def get_table_stats(self, table: str):
        value = {
            "table": table, "n_live_tup": 1_000_000, "n_dead_tup": 100,
            "dead_ratio": 0.0001, "last_analyze": "2026-09-02T00:00:00",
            "last_autovacuum": "2026-09-02T00:00:00", "total_size": "1 GB",
            "autovacuum_enabled": self.multi_hop,
            "autovacuum_running": False,
            "autovacuum_trigger": 200_050,
        }
        ref = self._record("get_table_stats", value)
        return TableStats(raw_ref=ref, **value)

    def get_physical_bloat(self, table: str):
        value = {
            "table": table, "availability": "AVAILABLE",
            "algorithm": "pgstattuple_approx_reclaimable_pct_v1",
            "table_bytes": 1024, "dead_tuple_percent": 2.0,
            "free_percent": 3.0, "reclaimable_pct": 5.0,
        }
        value["raw_ref"] = self._record("get_physical_bloat", value)
        return value

    def get_vacuum_horizon(self):
        value = {
            "db_xid_age": 1000, "oldest_table": "orders",
            "oldest_table_xid_age": 1000, "freeze_max_age": 200_000_000,
            "slots": [], "prepared_xacts": [], "oldest_backend_pid": None,
            "oldest_backend_xmin_age": 0, "wraparound_pct": 0.001,
            "at_risk": False, "xmin_holders": [],
        }
        value["raw_ref"] = self._record("get_vacuum_horizon", value)
        return value

    def get_database_stats(self):
        self.stats_calls += 1
        value = {
            "deadlocks": 0, "temp_files": 0, "temp_bytes": 0,
            "blk_read_time_ms": 0.0, "blk_write_time_ms": 0.0,
            "xact_commit": self.stats_calls, "xact_rollback": 0,
            "db_stats_reset": "controlled-db-epoch",
            "ckpt_timed": 10, "ckpt_requested": 0,
            "ckpt_write_time_ms": 0.0, "ckpt_sync_time_ms": 0.0,
            "ckpt_stats_reset": "controlled-ckpt-epoch", "errors": {},
            "disk_usage": {"used_pct": 40.0, "free_bytes": 100 << 30,
                           "path": "/controlled/pgdata"},
        }
        value["raw_ref"] = self._record("get_database_stats", value)
        return value

    def simulate_index(self, create_sql: str, test_sql: str, _params=None):
        value = {
            "cost_before": 100_000.0, "cost_after": 100.0,
            "cost_reduction_pct": 99.9, "would_be_used": True,
            "trivial_baseline": False, "create_sql": create_sql,
            "test_sql": test_sql,
        }
        value["raw_ref"] = self._record("simulate_index", value)
        return value


def collect_tool(tool: str, tb, hot_query: str) -> None:
    if tool == "get_active_sessions":
        tb.get_active_sessions()
    elif tool == "get_connection_stats":
        tb.get_connection_stats()
    elif tool == "get_blocking_chain":
        tb.get_blocking_chain()
    elif tool == "explain_query":
        tb.explain_query(hot_query, {"uid": 4242})
    elif tool == "get_indexes":
        tb.get_indexes("orders")
    elif tool == "get_top_queries":
        tb.get_top_queries(5)
    elif tool == "get_table_stats":
        tb.get_table_stats("orders")
    elif tool == "get_physical_bloat":
        tb.get_physical_bloat("orders")
    elif tool == "get_vacuum_horizon":
        tb.get_vacuum_horizon()
    elif tool == "get_database_stats":
        tb.get_database_stats()


class ReadOnlyPolicy(Policy):
    name = "e2e-read-only-multihop"

    def __init__(self):
        self.plans: list[dict] = []

    def run_phase(self, phase, tb, st, ctx):
        if phase is Phase.MONITOR:
            return Phase.OBSERVE
        if phase is Phase.OBSERVE:
            return Phase.HYPOTHESIZE
        if phase is Phase.HYPOTHESIZE:
            return Phase.INVESTIGATE
        if phase is Phase.INVESTIGATE:
            needs = [EvidenceNeed.from_dict(item) for item in
                     ctx.get("explanation", {}).get("needs", [])]
            plan = plan_evidence_tasks(
                st.explanation_graph, needs, tb,
                target_context=infer_target_context(ctx["hot_query"]),
                config=ToolPlanningConfig(
                    exploration_ratio=0.0, use_learned=False))
            self.plans.append(plan.to_dict())
            tools = list(dict.fromkeys(
                tool for task in plan.tasks for tool in task.selected_tools))
            for tool in tools:
                collect_tool(tool, tb, ctx["hot_query"])
            return Phase.DIAGNOSE
        if phase is Phase.DIAGNOSE:
            return (Phase.REPORT if st.explanation_graph.selected_path_ids
                    else Phase.INVESTIGATE)
        raise AssertionError(phase)


class DualRootPolicy(Policy):
    name = "e2e-dual-root-context"

    def run_phase(self, phase, tb, st, ctx):
        if phase is Phase.MONITOR:
            tb.get_database_stats()
            return Phase.OBSERVE
        if phase is Phase.OBSERVE:
            return Phase.HYPOTHESIZE
        if phase is Phase.HYPOTHESIZE:
            return Phase.INVESTIGATE
        if phase is Phase.INVESTIGATE:
            if st.intervention_attempts:
                return Phase.ESCALATE
            needs = [EvidenceNeed.from_dict(item) for item in
                     ctx.get("explanation", {}).get("needs", [])]
            tools = list(dict.fromkeys(
                tool for need in needs for tool in need.candidate_tools))
            for tool in tools:
                collect_tool(tool, tb, ctx["hot_query"])
            return Phase.DIAGNOSE
        if phase is Phase.DIAGNOSE:
            roots = set(st.explanation_graph.derive_selected_root_causes())
            return (Phase.PLAN if roots >= {
                "missing_index", "autovacuum_starvation"}
                    else Phase.INVESTIGATE)
        if phase is Phase.PLAN:
            option = next(
                item for item in xr.intervention_options(
                    st, executable_only=True)
                if item["fix"] == "create_covering_index")
            evidence = G.load().nodes["counterfactual_index"]
            need = EvidenceNeed.create(
                path_ids=[option["path_id"]],
                target_kind=EvidenceTargetKind.INTERVENTION,
                target_ids=[option["fix"]],
                evidence_type="counterfactual_index",
                predicate_id=str(evidence["predicate_id"]), required=True,
                freshness_seconds=300, candidate_tools=["simulate_index"],
                reason="validate the concrete dual-root intervention")
            before = len(st.scratchpad)
            sql = (f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
                   "ON orders(user_id, status)")
            tb.simulate_index(sql, ctx["hot_query"], {"uid": 4242})
            refs = {entry["raw_ref"] for entry in st.scratchpad[before:]
                    if entry.get("evidence_type") == "counterfactual_index"}
            xr.bind_evidence(st, explicit_needs=[need], raw_refs=refs)
            report = __import__("agent.esc", fromlist=["check_explanation"])
            if report.check_explanation(st)["verdict"] != "SUFFICIENT":
                return Phase.INVESTIGATE
            tb.submit_proposal(
                "create_index", sql,
                f"DROP INDEX CONCURRENTLY {INDEX_NAME}",
                rationale="repair only the missing-index path",
                selected_path_id=option["path_id"], fix_id=option["fix"],
                intervention_target=option["target_node_id"])
            return Phase.GATE
        raise AssertionError(phase)


class Observation:
    def __init__(self, *, dual: bool):
        self.alert = ("connection near limit and p99 latency" if dual else
                      "connection near limit")
        if dual:
            self.alert = "autovacuum unhealthy and p99 latency"
        self.healthy_kpi = {
            "p50_ms": 5, "p95_ms": 10, "p99_ms": 20,
            "qps": 100, "errors": 0, "cpu_pct": 10, "samples": 100}
        self.current_kpi = dict(self.healthy_kpi)
        self.current_kpi.update({"p99_ms": 1000, "qps": 1} if dual else {})


class Env:
    def __init__(self, episode_id: str, observer, *, dual: bool):
        self.episode_id = episode_id
        self.observer = observer
        self.applied_sql: list[str] = []
        self.spec = {
            "id": episode_id, "revision": 2,
            "workload": {"hot_query": (
                "SELECT * FROM orders WHERE user_id=%(uid)s "
                "AND status='pending'")},
            "success": {"outcome": "qps >= 50"},
        }
        self.dual = dual

    def observe(self):
        return self.observer

    def verify(self, settle_s=0.0):
        assert self.dual and settle_s == 300
        return (KPI(p50_ms=5, p95_ms=10, p99_ms=100, qps=1,
                    errors=0, cpu_pct=10, samples=100),
                RegressionResult(passed=True))


episode_ids = [
    f"e2e_readonly_multihop_{int(time.time())}",
    f"e2e_dual_root_{int(time.time())}",
]
try:
    print("[1] read-only multi-hop: recall -> dynamic evidence -> ESC -> REPORT -> DONE")
    read_observer = ControlledObserver(episode_ids[0], multi_hop=True)
    read_policy = ReadOnlyPolicy()
    read_result, read_state = run_episode(
        Env(episode_ids[0], read_observer, dual=False),
        Observation(dual=False), read_policy, allow_repair=False,
        use_cases=False, use_learned=False, max_steps=60, quiet=False)
    selected = [read_state.explanation_graph.path_map()[path_id]
                for path_id in read_state.explanation_graph.selected_path_ids]
    planned_tools = {tool for plan in read_policy.plans
                     for task in plan["tasks"]
                     for tool in task["selected_tools"]}
    check("multi-hop upstream root is selected",
          len(selected) == 1 and selected[0].node_ids == [
              "long_idle_transaction", "connection_exhaustion",
              "conn_near_limit"], [path.node_ids for path in selected])
    check("frontier planner dynamically selects only read tools",
          {"get_connection_stats", "get_active_sessions"} <= planned_tools and
          not (planned_tools & {"submit_proposal", "execute_sql"}),
          sorted(planned_tools))
    check("read-only diagnosis passes ESC before final report",
          read_state.esc_reports and
          read_state.esc_reports[-1]["verdict"] == "SUFFICIENT" and
          not read_result.gate_decisions and not read_result.applied_sql)
    check("REPORT is persisted and deterministically reaches DONE",
          read_result.final_phase == "DONE" and
          read_state.final_report.get("kind") == "REPORT" and
          ("REPORT", "DONE") in {
              (src, dst) for src, dst, _reason in read_result.transitions})

    print("\n[2] dual root: one path effect succeeds, context remains, rollback is narrow")
    db.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    dual_observer = ControlledObserver(episode_ids[1])
    dual_result, dual_state = run_episode(
        Env(episode_ids[1], dual_observer, dual=True), Observation(dual=True),
        DualRootPolicy(), allow_repair=True, confirm_cb=lambda _p, _d: True,
        use_cases=False, use_learned=False, max_steps=100, quiet=False)
    roots = set(dual_state.explanation_graph.derive_selected_root_causes())
    attempt = dual_state.intervention_attempts[0]
    check("both independent supported roots survive diagnosis",
          roots >= {"missing_index", "autovacuum_starvation"} and
          all(dual_state.explanation_graph.node_status[root] ==
              CausalStatus.SUPPORTED.value for root in roots), sorted(roots))
    check("targeted path effect succeeds while total KPI remains unhealthy",
          attempt.actual and all(effect["met"] is True
                                 for effect in attempt.actual) and
          dual_state.verification_result.get("recovered") is False,
          dual_state.verification_result)
    check("failure is CONTEXT and refutes neither root nor path segment",
          attempt.failure_scope == "CONTEXT" and
          not attempt.affected_edge_ids and
          all(dual_state.explanation_graph.node_status[root] !=
              CausalStatus.REFUTED.value for root in roots),
          (attempt.failure_scope, attempt.affected_edge_ids))
    index_exists = bool(db.query(
        "SELECT 1 FROM pg_indexes WHERE indexname=%s", (INDEX_NAME,)))
    check("context failure rolls back the concrete index and reaches DONE",
          attempt.rollback_status == "SUCCEEDED" and not index_exists and
          dual_result.rollbacks and dual_result.final_phase == "DONE")
finally:
    try:
        db.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    except Exception:
        pass
    for episode_id in episode_ids:
        trace_dir = TRACE_DIR / episode_id
        if trace_dir.exists() and trace_dir.resolve().parent == TRACE_DIR.resolve():
            shutil.rmtree(trace_dir)

print("\n" + "=" * 80)
print("EXPLANATION E2E:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
