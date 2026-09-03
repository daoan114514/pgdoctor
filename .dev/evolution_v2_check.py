"""Independent write/read/consume/ablation checks for v2 L1-L4 learning."""
from __future__ import annotations

import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState
from agent.explanation import CausalStatus, EvidenceNeed
from agent.state_machine import Phase, StateMachine
from agent.tool_planner import ToolPlanningConfig, plan_evidence_tasks
from agent.toolbox import Toolbox
from knowledge import case_store, evolution
from knowledge.causal_graph import graph as G
from safety import gate
from safety.gate import RemediationProposal


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<58} {detail}")


class Observer:
    def get_active_sessions(self):
        return []

    def get_blocking_chain(self):
        return []

    @staticmethod
    def extension_available(_name):
        return False


class Score:
    diagnosis = True
    outcome = True
    safe_pass = True


@dataclass
class Attempt:
    attempt_id: str
    plan_id: str
    selected_path_id: str
    fix_id: str
    outcome: str = "VERIFIED"
    learnable: bool = True
    failure_scope: str = "NONE"
    affected_edge_ids: list[str] = field(default_factory=list)
    expected: list[dict] = field(default_factory=list)
    actual: list[dict] = field(default_factory=list)


tmp = Path(tempfile.mkdtemp(prefix="pgdoctor_evolution_v2_"))
old_evolution = evolution.LEARNED
old_cases_dir = case_store.LEARNED_V2
old_cases_file = case_store.CASES_V2
try:
    evolution.LEARNED = tmp
    case_store.LEARNED_V2 = tmp / "v2"
    case_store.CASES_V2 = case_store.LEARNED_V2 / "cases.yaml"
    shutil.copytree(
        Path(__file__).resolve().parent.parent / "knowledge" / "learned" / "v2",
        tmp / "v2", dirs_exist_ok=True)

    safe_proposal = RemediationProposal(
        action_type="alter_table_options",
        sql="ALTER TABLE orders SET (autovacuum_enabled = true)",
        rollback="ALTER TABLE orders SET (autovacuum_enabled = false)",
        root_cause="autovacuum_starvation", fix_id="enable_autovacuum",
        esc_verdict="SUFFICIENT",
        evidence_refs=["trace://evolution_gate/step_001"])
    manual_proposal = RemediationProposal(
        action_type="replication_control",
        sql="SELECT pg_drop_replication_slot('stale_slot')",
        rollback="IRREVERSIBLE", root_cause="stale_replication_slot",
        fix_id="drop_replication_slot", esc_verdict="SUFFICIENT",
        evidence_refs=["trace://evolution_gate/step_002"])
    gate_before = (gate.assess(safe_proposal), gate.assess(manual_proposal))

    print("[1] L1 writes/reads paths and affects HYPOTHESIZE only")
    fingerprint = case_store.Fingerprint(
        metric_deltas={"p99_ms": "up_5x"}, wait_profile={"Lock": 1},
        query_scope="single_query_dominant", onset="sudden",
        object_scope="single_table")
    hits = case_store.search_v2(
        fingerprint, observed_symptoms=["latency_p99_up"])
    check("cold-start L1 fixture is readable", bool(hits))
    seed = G.enumerate_causal_paths(["latency_p99_up"], use_learned=False)
    case_scores = {
        path.path_id: 10.0 for path in seed
        if any(template.get("node_ids") == path.node_ids
               for template in hits[0].get("path_templates", []))
    }
    l1_on = G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=True,
        case_path_scores=case_scores, use_l3_edges=False, use_l3_paths=False)
    l1_off = G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=False,
        case_path_scores=case_scores)
    check("L1 changes a path score/rank when enabled",
          l1_on[0].path_id != l1_off[0].path_id or
          l1_on[0].score_components["l1_path_template_adjustment"] > 0)
    check("L1 off zeros only the template channel",
          all(path.score_components["l1_path_template_adjustment"] == 0
              for path in l1_off))

    write_state = EpisodeState("evolution_l1_write", "controlled")
    write_exp = G.recall_explanation(
        ["latency_p99_up"], episode_id=write_state.episode_id,
        use_learned=False)
    write_exp.p0_obligations = {}
    write_path = next(path for path in write_exp.candidate_paths
                      if path.root_node_id == "missing_index")
    write_exp.set_path_status(write_path.path_id, CausalStatus.SUPPORTED)
    write_exp.select_paths([write_path.path_id], unexplained_symptoms=[])
    write_state.explanation_graph = write_exp
    write_state.esc_reports = [{"verdict": "SUFFICIENT"}]
    write_state.intervention_attempts = [Attempt(
        attempt_id="l1_attempt", plan_id="l1_plan",
        selected_path_id=write_path.path_id,
        fix_id="create_covering_index")]
    written = case_store.write_case_v2(
        write_state, Score(),
        {"id": "controlled", "split": "train", "revision": 2})
    check("trusted train episode writes and reads a CaseV2 path",
          written is not None and
          case_store.fetch_case_v2(written.case_id) is not None and
          bool(written.candidate_paths))
    before_eval = case_store.CASES_V2.read_bytes()
    rejected_eval = case_store.write_case_v2(
        write_state, Score(),
        {"id": "held_out", "split": "eval", "revision": 2})
    check("eval split cannot enter L1 memory",
          rejected_eval is None and
          case_store.CASES_V2.read_bytes() == before_eval)

    print("\n[2] L2/L4 exact-frontier records change tool selection")
    state = EpisodeState("evolution_tools", "controlled",
                         phase=Phase.INVESTIGATE.value)
    state.incident_window["scenario_revision"] = 2
    explanation = G.recall_explanation(
        ["latency_p99_up"], episode_id=state.episode_id,
        use_learned=False)
    state.explanation_graph = explanation
    toolbox = Toolbox(Observer(), state, StateMachine(state))
    path = explanation.candidate_paths[0]
    need = EvidenceNeed.create(
        path_ids=[path.path_id], target_kind="BRANCH",
        target_ids=[path.edge_ids[0]], evidence_type="controlled_branch",
        predicate_id="controlled_branch_v2", required=False,
        freshness_seconds=60,
        candidate_tools=["get_active_sessions", "get_blocking_chain"],
        reason="controlled L2/L4 frontier")
    static = plan_evidence_tasks(
        explanation, [need], toolbox,
        config=ToolPlanningConfig(exploration_ratio=0, use_learned=False))
    static_tool = static.tasks[0].selected_tools[0]
    promoted_tool = ({"get_active_sessions", "get_blocking_chain"} -
                     {static_tool}).pop()
    context = static.tasks[0].learning_context[need.need_id]
    for index in range(5):
        state.evidence_task_audit.append({
            "event": "tool_learning_observation",
            "observation_id": f"tool_observation_{index}",
            "tool": promoted_tool, "learning_context": context,
            "collection_status": "OBSERVED", "changed_statuses": 1,
            "pruned_paths": 1, "required_fulfilled": False,
            "changed_next_decision": True, "cost": 0.05,
            "latency_s": 0.05, "covered_need_count": 1,
            "entropy_gain": 1.0, "posterior_change": 0.5,
            "duplicate_calls": 0})
    learned = evolution.learn_v2(state, Score())
    dynamic = plan_evidence_tasks(
        explanation, [need], toolbox,
        config=ToolPlanningConfig(exploration_ratio=0, use_learned=True))
    check("L2 and L4 both write auditable frontier records",
          learned["l2"] == 5 and learned["l4"] == 5, learned)
    check("L2/L4 records are read and change the next tool",
          dynamic.tasks[0].selected_tools == [promoted_tool])
    l2_off = plan_evidence_tasks(
        explanation, [need], toolbox,
        config=ToolPlanningConfig(
            exploration_ratio=0, use_learned=True, use_l2=False, use_l4=True))
    l4_off = plan_evidence_tasks(
        explanation, [need], toolbox,
        config=ToolPlanningConfig(
            exploration_ratio=0, use_learned=True, use_l2=True, use_l4=False))
    l2_components = l2_off.tasks[0].score_components[need.need_id]
    l4_components = l4_off.tasks[0].score_components[need.need_id]
    check("L2 and L4 have independent online ablations",
          l2_components["l2_conditional_policy"] == 0 and
          l2_components["l4_information_gain"] > 0 and
          l4_components["l2_conditional_policy"] > 0 and
          l4_components["l4_information_gain"] == 0)
    state.evidence_task_audit = [{
        "event": "tool_learning_observation",
        "observation_id": "l4_only_write", "tool": promoted_tool,
        "learning_context": context, "collection_status": "OBSERVED",
        "changed_statuses": 1, "pruned_paths": 1,
        "required_fulfilled": False, "changed_next_decision": True,
        "cost": 0.05, "latency_s": 0.05, "covered_need_count": 1,
        "entropy_gain": 1.0, "posterior_change": 0.5,
        "duplicate_calls": 0}]
    l4_only_write = evolution.learn_v2(
        state, Score(), enabled_layers={"l4"})
    check("disabled learning layers are not written",
          l4_only_write["l2"] == 0 and l4_only_write["l4"] == 1,
          l4_only_write)

    print("\n[3] L3 stable edge/path records are consumed and ablatable")
    target = next(path for path in G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=False)
        if path.root_node_id == "lock_contention")
    for index in range(3):
        exp = G.recall_explanation(
            ["latency_p99_up"], episode_id=f"evolution_l3_{index}",
            use_learned=False)
        exp.p0_obligations = {}
        selected = exp.path_map()[target.path_id]
        exp.set_path_status(selected.path_id, CausalStatus.SUPPORTED)
        exp.select_paths([selected.path_id], unexplained_symptoms=[])
        sample = EpisodeState(f"evolution_l3_{index}", "controlled")
        sample.explanation_graph = exp
        sample.esc_reports = [{"verdict": "SUFFICIENT"}]
        sample.intervention_attempts = [SimpleNamespace(
            attempt_id=f"l3_attempt_{index}", learnable=True,
            outcome="VERIFIED", failure_scope="NONE",
            affected_edge_ids=[], selected_path_id=selected.path_id)]
        evolution.learn_v2(sample, Score())
    edge_only = G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=True,
        use_l3_edges=True, use_l3_paths=False)
    path_only = G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=True,
        use_l3_edges=False, use_l3_paths=True)
    static_again = G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=False)
    edge_row = next(path for path in edge_only
                    if path.path_id == target.path_id).score_components
    path_row = next(path for path in path_only
                    if path.path_id == target.path_id).score_components
    check("L3 writes/reads stable edge and path IDs",
          edge_row["l3_edge_adjustment"] > 0 and
          path_row["l3_path_adjustment"] > 0)
    check("edge/path channels can be ablated independently",
          edge_row["l3_path_adjustment"] == 0 and
          path_row["l3_edge_adjustment"] == 0)
    check("learned=False restores the static score",
          all(path.score_components["l3_edge_adjustment"] == 0 and
              path.score_components["l3_path_adjustment"] == 0
              for path in static_again))

    print("\n[4] Whole-stack on/off changes policy, never the safety boundary")
    p0_on = G.recall_explanation(
        ["disk_growing"], episode_id="evolution_p0_on", use_learned=True)
    p0_off = G.recall_explanation(
        ["disk_growing"], episode_id="evolution_p0_off", use_learned=False)
    check("learning cannot remove a reachable P0 obligation",
          set(p0_on.p0_obligations) == set(p0_off.p0_obligations))
    check("whole-stack replay changes recall/tool behavior",
          (l1_on[0].path_id != l1_off[0].path_id or
           l1_on[0].score_components["l1_path_template_adjustment"] > 0) and
          dynamic.tasks[0].selected_tools != static.tasks[0].selected_tools)
    gate_after = (gate.assess(safe_proposal), gate.assess(manual_proposal))
    before_signature = [
        (decision.approved, decision.tier, decision.reason_code, decision.risk)
        for decision in gate_before]
    after_signature = [
        (decision.approved, decision.tier, decision.reason_code, decision.risk)
        for decision in gate_after]
    check("L1-L4 records cannot relax or otherwise mutate GATE",
          before_signature == after_signature and
          gate_after[0].approved and gate_after[0].tier == "CONFIRM" and
          not gate_after[1].approved and gate_after[1].tier == "DENY",
          after_signature)
finally:
    evolution.LEARNED = old_evolution
    case_store.LEARNED_V2 = old_cases_dir
    case_store.CASES_V2 = old_cases_file
    G.load.cache_clear()
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 80)
print("EVOLUTION V2:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
