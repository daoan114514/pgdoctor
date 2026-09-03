"""P0 remediation contract: trusted binding, gate tiers, and escalation routing."""
from __future__ import annotations

import json
import sys
import random
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState
from agent import explanation_runtime as xr
from agent.explanation import EvidenceBinding
from agent import esc
from agent.loop import run_episode
from agent.policy import Policy
from agent.state_machine import Phase, StateMachine
from agent.toolbox import Toolbox
from knowledge.causal_graph import graph as G
from knowledge.evidence_predicates import PredicateContext, evaluate
from safety import gate
from safety.gate import RemediationProposal as Proposal
from sandbox.traces import TRACE_DIR, TraceStore

fails: list[str] = []


def check(name: str, condition: bool, detail="") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}   {detail}")
    if not condition:
        fails.append(name)


def p0(root_cause: str, fix_id: str, action_type: str, sql: str,
       rollback: str, *, esc_verdict="SUFFICIENT", evidence=True):
    return Proposal(
        action_type=action_type, sql=sql, rollback=rollback,
        root_cause=root_cause, fix_id=fix_id, esc_verdict=esc_verdict,
        evidence_refs=["trace://p0_gate/step_001"] if evidence else [])


print("[1] GATE requires graph-bound context")
unbound = gate.assess(Proposal(
    "alter_table_options",
    "ALTER TABLE orders SET (autovacuum_enabled = true)",
    "ALTER TABLE orders SET (autovacuum_enabled = false)"))
check("unbound proposal is denied", not unbound.approved, unbound.reasons)

mismatch = gate.assess(Proposal(
    "alter_table_options",
    "ALTER TABLE orders SET (autovacuum_enabled = true)",
    "ALTER TABLE orders SET (autovacuum_enabled = false)",
    root_cause="autovacuum_starvation", fix_id="create_covering_index",
    esc_verdict="SUFFICIENT", evidence_refs=["trace://p0_gate/step_001"]))
check("fix must belong to diagnosed root cause", not mismatch.approved,
      mismatch.reasons)

print("\n[2] autovacuum P0 is gated, never AUTO")
auto = p0(
    "autovacuum_starvation", "enable_autovacuum", "alter_table_options",
    "ALTER TABLE orders SET (autovacuum_enabled = true)",
    "ALTER TABLE orders SET (autovacuum_enabled = false)")
decision = gate.assess(auto)
check("sufficient autovacuum proposal reaches CONFIRM",
      decision.approved and decision.tier == "CONFIRM",
      (decision.tier, decision.reasons, decision.risk))
check("gate derives P0 severity from graph",
      decision.risk.get("severity") == "P0", decision.risk)

no_esc = gate.assess(p0(
    "autovacuum_starvation", "enable_autovacuum", "alter_table_options",
    auto.sql, auto.rollback, esc_verdict=""))
check("P0 without ESC pass is denied", not no_esc.approved, no_esc.reasons)

no_refs = gate.assess(p0(
    "autovacuum_starvation", "enable_autovacuum", "alter_table_options",
    auto.sql, auto.rollback, evidence=False))
check("P0 without raw evidence refs is denied", not no_refs.approved,
      no_refs.reasons)

print("\n[3] destructive or external P0 fixes are escalation-only")
blocked = [
    ("stale slot", p0(
        "stale_replication_slot", "drop_replication_slot",
        "replication_control", "SELECT pg_drop_replication_slot('stale_slot')",
        "IRREVERSIBLE")),
    ("prepared xact", p0(
        "orphaned_prepared_transaction", "resolve_prepared_xact",
        "session_control", "ROLLBACK PREPARED 'old_gid'", "IRREVERSIBLE")),
    ("disk capacity", p0(
        "disk_pressure", "remediate_disk_capacity", "storage_management",
        "ESCALATE STORAGE REMEDIATION FOR /pgdata", "IRREVERSIBLE")),
]
for label, proposal in blocked:
    d = gate.assess(proposal)
    check(f"{label} cannot execute", not d.approved and d.tier == "DENY",
          d.reasons)

disk_fixes = G.fixes_for("disk_pressure")
check("disk pressure no longer maps to VACUUM",
      [f["fix"] for f in disk_fixes] == ["remediate_disk_capacity"],
      disk_fixes)

print("\n[4] Toolbox stamps context; model cannot supply it")
state = EpisodeState("p0_toolbox_binding", "p0_toolbox_binding", phase="PLAN")
state.claimed_fault_class = "autovacuum_starvation"
state.claimed_root_cause = "autovacuum disabled on orders"
state.esc_verdict = "SUFFICIENT"
state.note(
    "fixture", "autovacuum_health", "autovacuum_enabled=False backlog=2.0",
    raw_ref="trace://p0_toolbox_binding/step_001",
    bears_on=["autovacuum_starvation"])
toolbox = Toolbox(object(), state, StateMachine(state, allow_repair=True))
toolbox.submit_proposal(
    "alter_table_options", auto.sql, auto.rollback,
    rationale="enable table autovacuum")
check("proposal root cause is state-owned",
      state.proposal.get("root_cause") == "autovacuum_starvation",
      state.proposal)
check("proposal fix is resolved from graph",
      state.proposal.get("fix_id") == "enable_autovacuum", state.proposal)
check("proposal carries ESC verdict and relevant raw evidence",
      state.proposal.get("esc_verdict") == "SUFFICIENT" and
      state.proposal.get("evidence_refs") == [
          "trace://p0_toolbox_binding/step_001"], state.proposal)

print("\n[5] sufficient manual P0 paths pass ESC, then skip proposal/GATE/SQL")


class _Env:
    def __init__(self, root_cause):
        self.episode_id = f"p0_route_{root_cause}"
        self.spec = {"id": self.episode_id, "revision": 2,
                     "workload": {"hot_query": "SELECT 1"}}
        self.applied_sql = []

    @staticmethod
    def observe():
        return object()


class _Observation:
    def __init__(self, root_cause):
        self.alert = ("disk growing" if root_cause in {
            "disk_pressure", "stale_replication_slot"} else
            "prepared transaction latency")
        self.current_kpi = dict(self.healthy_kpi)
        if root_cause == "orphaned_prepared_transaction":
            self.current_kpi["p99_ms"] = 400

    healthy_kpi = {"p99_ms": 100, "cpu_pct": 20, "errors": 0}


SUPPORT_VALUES = {
    "autovacuum_health": {
        "autovacuum_enabled": False, "autovacuum_running": False,
        "backlog_ratio": 0.0,
    },
    "physical_bloat_ratio": {
        "availability": "AVAILABLE",
        "algorithm": "pgstattuple_approx_reclaimable_pct_v1",
        "reclaimable_pct": 30.0,
    },
    "replication_slot_age": {"slots": [{
        "slot_name": "pgdoctor_p0_slot", "active": False,
        "xmin_age": 0, "catalog_xmin_age": 0,
        "retained_wal_bytes": 2 * 1024 * 1024 * 1024,
    }]},
    "prepared_xact_age": {"prepared_xacts": [{
        "gid": "pgdoctor_p0_prepared", "xid_age": 10,
        "prepared_age_s": 7200,
    }]},
    "disk_usage": {"used_pct": 92.0},
}

REFUTE_VALUES = {
    "explain_plan": {"indexes_used": ["idx_existing"]},
    "row_estimate_deviation": {"max_ratio": 1.0},
    "lock_blocking_chain": {"chains": []},
    "physical_bloat_ratio": {
        "availability": "AVAILABLE",
        "algorithm": "pgstattuple_approx_reclaimable_pct_v1",
        "reclaimable_pct": 5.0,
    },
    "connection_count": {"near_limit": False},
    "idle_in_transaction": {"idle_in_transaction": 0},
    "checkpoint_stats": {"ckpt_timed": 10, "ckpt_requested": 0},
    "xid_age": {"wraparound_pct": 1.0},
    "replication_slot_age": {"slots": []},
    "prepared_xact_age": {"prepared_xacts": []},
    "deadlock_count": {"deadlocks": 0},
    "temp_file_volume": {"temp_bytes": 0},
    "autovacuum_health": {
        "autovacuum_enabled": True, "autovacuum_running": False,
        "backlog_ratio": 0.0,
    },
    "disk_usage": {"used_pct": 40.0},
}


class _RoutePolicy(Policy):
    name = "p0-manual-evidence-route"

    def __init__(self, root_cause):
        self.root_cause = root_cause
        self.plan_called = False
        self.bound = False

    @staticmethod
    def _add_binding(st, store, *, evidence_type, value,
                     node_ids=None, edge_ids=None, window=False):
        graph = G.load()
        predicate_id = str(graph.nodes[evidence_type]["predicate_id"])
        target_kind = "PATH" if edge_ids else "NODE"
        target_ids = list(edge_ids or node_ids or [])
        now = time.time()
        raw_ref = store.record(
            "p0_fixture", {"evidence_type": evidence_type},
            json.dumps(value, ensure_ascii=False), value)
        context = PredicateContext(
            target_kind=target_kind, target_ids=tuple(target_ids),
            window_start=now - 1 if window else None,
            window_end=now if window else None,
            source_epoch=st.episode_id if window else "")
        decision = evaluate(predicate_id, value, context=context)
        binding = EvidenceBinding.create(
            episode_id=st.episode_id, raw_ref=raw_ref,
            evidence_type=evidence_type, status="OBSERVED",
            observed_at=now, predicate_id=predicate_id,
            predicate_result=decision.result, structured_value=value,
            target_node_ids=node_ids or [], target_edge_ids=edge_ids or [],
            window_start=context.window_start, window_end=context.window_end,
            source_epoch=context.source_epoch, fresh_until=now + 600)
        st.explanation_graph.add_evidence_binding(binding)

    def _bind_complete_explanation(self, st):
        explanation = st.explanation_graph
        paths = [path for path in explanation.candidate_paths
                 if path.root_node_id == self.root_cause]
        selected = min(paths, key=lambda path: (len(path.edge_ids), path.path_id))
        store = TraceStore(st.episode_id)

        for index, node_id in enumerate(selected.node_ids[:-1]):
            for evidence_type in G.required_evidence(node_id):
                value = SUPPORT_VALUES[evidence_type]
                self._add_binding(
                    st, store, evidence_type=evidence_type, value=value,
                    node_ids=[node_id])
                self._add_binding(
                    st, store, evidence_type=evidence_type, value=value,
                    edge_ids=[selected.edge_ids[index]])

        selected_nodes = set(selected.node_ids)
        by_root: dict[str, list] = {}
        for path in explanation.candidate_paths:
            by_root.setdefault(path.root_node_id, []).append(path)
        for root_cause, candidate_paths in by_root.items():
            if root_cause in selected_nodes:
                continue
            relation = next((item for item in G.refuting_evidence(root_cause)
                             if item["scope"] != "INTERVENTION"), None)
            if relation is None:
                continue
            evidence_type = relation["evidence"]
            value = REFUTE_VALUES[evidence_type]
            if relation["scope"] == "NODE":
                self._add_binding(
                    st, store, evidence_type=evidence_type, value=value,
                    node_ids=[root_cause])
            else:
                for edge_id in {path.edge_ids[0] for path in candidate_paths}:
                    self._add_binding(
                        st, store, evidence_type=evidence_type, value=value,
                        edge_ids=[edge_id], window=bool(
                            relation.get("window_required")))
        xr.recompute_statuses(st)
        self.bound = True

    def run_phase(self, phase, tb, st, ctx):
        if phase is Phase.MONITOR:
            return Phase.OBSERVE
        if phase is Phase.OBSERVE:
            return Phase.HYPOTHESIZE
        if phase is Phase.HYPOTHESIZE:
            return Phase.INVESTIGATE
        if phase is Phase.INVESTIGATE:
            if not self.bound:
                self._bind_complete_explanation(st)
            return Phase.DIAGNOSE
        if phase is Phase.DIAGNOSE:
            selected_roots = st.explanation_graph.derive_selected_root_causes()
            if self.root_cause not in selected_roots:
                return Phase.INVESTIGATE
            return Phase.PLAN
        if phase is Phase.PLAN:
            self.plan_called = True
            return Phase.ESCALATE
        raise AssertionError(phase)


for root_cause in (
        "disk_pressure", "stale_replication_slot",
        "orphaned_prepared_transaction"):
    policy = _RoutePolicy(root_cause)
    trace_dir = TRACE_DIR / f"p0_route_{root_cause}"
    try:
        result, state = run_episode(
            _Env(root_cause), _Observation(root_cause), policy,
            allow_repair=True, use_esc=True, use_cases=False,
            use_learned=False, quiet=True, max_steps=40)
        manual = state.intervention_plan
        check(f"{root_cause} has sufficient trace-backed ESC evidence",
              state.esc_reports and
              state.esc_reports[-1]["verdict"] == "SUFFICIENT" and
              state.claimed_fault_class == root_cause,
              (state.esc_verdict, state.claimed_fault_class))
        check(f"{root_cause} creates only a manual escalation plan",
              result.final_phase == "DONE" and
              state.final_report.get("kind") == "ESCALATION" and
              manual is not None and manual.manual and
              manual.execution == "escalate_only" and not manual.sql and
              not state.proposal and not result.gate_decisions and
              not result.applied_sql and not policy.plan_called,
              (result.final_phase, state.outcome_note))
    finally:
        shutil.rmtree(trace_dir, ignore_errors=True)

if "--live" in sys.argv:
    print("\n[6] live autovacuum P0: v2 explanation -> ESC -> PLAN -> GATE -> VERIFY")
    from agent import explanation_runtime as xr
    from sandbox.injectors.p0 import AutovacuumStarvationInjector
    from sandbox.metrics import KPI
    from sandbox.observe import Observer
    from sandbox.scoring import RegressionResult
    from sandbox.traces import TraceStore

    scenario_path = (Path(__file__).resolve().parent.parent /
                     "sandbox/scenarios/p0/autovacuum_starvation.yaml")
    spec = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    injector = AutovacuumStarvationInjector(spec)
    params = injector.params(random.Random(0))
    episode_id = f"p0_autovac_gate_live_{int(time.time())}"

    class _LiveObservation:
        alert = "autovacuum unhealthy on orders"
        healthy_kpi = {
            "p50_ms": 5, "p95_ms": 10, "p99_ms": 20,
            "qps": 100, "errors": 0, "cpu_pct": 10, "samples": 100,
        }
        current_kpi = dict(healthy_kpi)

    class _LiveEnv:
        def __init__(self):
            self.episode_id = episode_id
            self.spec = {
                "id": spec["id"], "revision": spec.get("revision", 1),
                "workload": {"hot_query": "SELECT 1"},
                "success": {"outcome": "errors == 0"},
            }
            self.applied_sql = []
            self.observer = Observer(TraceStore(episode_id))

        def observe(self):
            return self.observer

        @staticmethod
        def verify(settle_s=0.0):
            return (KPI(
                p50_ms=5, p95_ms=10, p99_ms=20, qps=100,
                errors=0, cpu_pct=10, samples=100),
                RegressionResult(passed=True))

    class _AutovacuumPolicy(Policy):
        name = "p0-autovacuum-live-v2"

        def __init__(self):
            self.saw_autovacuum_need = False

        def run_phase(self, phase, tb, st, ctx):
            if phase is Phase.MONITOR:
                return Phase.OBSERVE
            if phase is Phase.OBSERVE:
                return Phase.HYPOTHESIZE
            if phase is Phase.HYPOTHESIZE:
                return Phase.INVESTIGATE
            if phase is Phase.INVESTIGATE:
                needs = ctx.get("explanation", {}).get("needs", [])
                self.saw_autovacuum_need |= any(
                    item.get("evidence_type") == "autovacuum_health" and
                    "get_table_stats" in item.get("candidate_tools", [])
                    for item in needs)
                tools = list(dict.fromkeys(
                    item.get("candidate_tools", [""])[0]
                    for item in needs if item.get("candidate_tools")))
                for tool in tools:
                    if tool == "get_table_stats":
                        tb.get_table_stats("orders")
                    elif tool == "get_vacuum_horizon":
                        tb.get_vacuum_horizon()
                    elif tool == "get_connection_stats":
                        tb.get_connection_stats()
                    elif tool == "get_active_sessions":
                        tb.get_active_sessions()
                return Phase.DIAGNOSE
            if phase is Phase.DIAGNOSE:
                explanation = st.explanation_graph
                selected = (explanation.path_map().get(
                    explanation.selected_path_ids[0])
                    if explanation and explanation.selected_path_ids else None)
                if selected is None:
                    return Phase.INVESTIGATE
                check("DIAGNOSE selects the direct autovacuum P0 path",
                      selected.node_ids == [
                          "autovacuum_starvation", "autovacuum_unhealthy"],
                      selected.node_ids)
                return Phase.PLAN
            if phase is Phase.PLAN:
                option = next(item for item in xr.intervention_options(
                    st, executable_only=True)
                    if item["fix"] == "enable_autovacuum")
                tb.submit_proposal(
                    "alter_table_options", auto.sql, auto.rollback,
                    rationale="restore table autovacuum",
                    selected_path_id=option["path_id"],
                    fix_id=option["fix"],
                    intervention_target=option["target_node_id"])
                return Phase.GATE
            raise AssertionError(phase)

    state = None
    try:
        injector.cleanup()
        injector.inject(params)
        policy = _AutovacuumPolicy()
        result, state = run_episode(
            _LiveEnv(), _LiveObservation(), policy,
            allow_repair=True, confirm_cb=lambda proposal, decision: True,
            use_cases=False, use_learned=False, max_steps=30)
        explanation = state.explanation_graph
        obligation = explanation.p0_obligations.get(
            "autovacuum_starvation") if explanation else None
        check("frontier assigns autovacuum evidence to get_table_stats",
              policy.saw_autovacuum_need)
        check("v2 ESC resolves the P0 obligation and reaches DONE",
              result.final_phase == "DONE" and obligation is not None and
              obligation.resolved and state.finished,
              (result.final_phase, obligation.to_dict() if obligation else {}))
        check("causal GATE receives the current explanation revision",
              len(result.gate_decisions) == 1 and
              result.gate_decisions[0]["tier"] == "CONFIRM" and
              result.gate_decisions[0]["approved"] and
              result.gate_decisions[0]["causal_context"][
                  "explanation_revision"] == explanation.revision,
              result.gate_decisions)
        effects = result.audit.get("verify", {}).get("effects", [])
        check("VERIFY observes autovacuum_enabled false -> true",
              len(effects) == 1 and effects[0]["before"] == 0.0 and
              effects[0]["actual"] == 1.0 and effects[0]["met"] is True,
              effects)
        check("agent_rw executes only the graph-bound repair",
              result.applied_sql == [auto.sql] and
              not injector.verify_injected(params), result.applied_sql)

        undo_id = state.undo_refs[-1] if state.undo_refs else ""
        rolled_back, rollback_note = gate.rollback(undo_id)
        check("post-acceptance rollback restores the injected state",
              rolled_back and injector.verify_injected(params), rollback_note)
    finally:
        injector.cleanup()
        trace_dir = Path(__file__).resolve().parent.parent / "traces" / episode_id
        shutil.rmtree(trace_dir, ignore_errors=True)
    check("live cleanup restores healthy autovacuum state",
          not injector.verify_injected(params))

print("\n" + "=" * 68)
print("P0 GATE: PASS" if not fails else f"P0 GATE: FAIL {fails}")
sys.exit(1 if fails else 0)
