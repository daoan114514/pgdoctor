"""Offline acceptance for path-level L1-L4 learning and governance."""
from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState
from agent.explanation import CausalStatus, EvidenceNeed
from agent.explanation_runtime import _case_path_scores
from agent.state_machine import Phase, StateMachine
from agent.toolbox import Toolbox
from agent.tool_planner import ToolPlanningConfig, plan_evidence_tasks
from knowledge import case_store as cases
from knowledge import evolution
from knowledge import structure
from knowledge.causal_graph import graph as G


REPO = Path(__file__).resolve().parent.parent
ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<64} {detail}")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeObserver:
    trace = None

    def extension_available(self, _name: str) -> bool:
        return True

    def __getattr__(self, name: str):
        if name.startswith("get_") or name in {"explain_query", "simulate_index"}:
            return lambda *args, **kwargs: None
        raise AttributeError(name)


class Score:
    def __init__(self, diagnosis=False, outcome=False, safe_pass=False):
        self.diagnosis = diagnosis
        self.outcome = outcome
        self.safe_pass = safe_pass


@dataclass
class CaseAttempt:
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


v1_paths = sorted((REPO / "knowledge" / "learned").glob("*.yaml"))
v1_before = {path.name: digest(path) for path in v1_paths}
tmp = Path(tempfile.mkdtemp(prefix="pgdoctor_learning_v2_"))
old_evolution_learned = evolution.LEARNED
old_cases_dir = cases.LEARNED_V2
old_cases_file = cases.CASES_V2
old_structure_learned = structure.LEARNED

try:
    evolution.LEARNED = tmp
    cases.LEARNED_V2 = tmp / "v2"
    cases.CASES_V2 = cases.LEARNED_V2 / "cases.yaml"
    structure.LEARNED = tmp
    cases.LEARNED_V2.mkdir(parents=True)
    shutil.copyfile(
        REPO / "knowledge" / "learned" / "v2" / "cases.yaml",
        cases.CASES_V2)

    print("[1] L1 retrieves path templates and changes HYPOTHESIZE ranking")
    common = dict(metric_deltas={"p99_ms": "up_5x"},
                  query_scope="single_query_dominant", onset="sudden",
                  object_scope="single_table")
    no_wait = cases.Fingerprint(wait_profile={"none": 1}, **common)
    lock_wait = cases.Fingerprint(wait_profile={"Lock": 1}, **common)
    no_wait_hits = cases.search_v2(
        no_wait, observed_symptoms=["latency_p99_up"])
    lock_hits = cases.search_v2(
        lock_wait, observed_symptoms=["latency_p99_up"])
    check("cold-start fixture contains at least two usable v2 cases",
          len(cases.load_cases_v2()) >= 2)
    check("same symptom with different wait profile retrieves different path",
          no_wait_hits[0]["case"].case_id != lock_hits[0]["case"].case_id,
          (no_wait_hits[0]["case"].case_id,
           lock_hits[0]["case"].case_id))

    seed = G.enumerate_causal_paths(["latency_p99_up"], use_learned=False)
    lock_scores = _case_path_scores(seed, lock_hits)
    learned_rank = G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=True,
        case_path_scores=lock_scores)
    static_rank = G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=False,
        case_path_scores=lock_scores)
    check("L1 on promotes the matching lock path",
          learned_rank[0].root_node_id == "lock_contention",
          [path.root_node_id for path in learned_rank[:3]])
    check("L1 off restores manual path order",
          static_rank[0].root_node_id == "missing_index",
          [path.root_node_id for path in static_rank[:3]])
    write_state = EpisodeState("write_case_v2", "fixture")
    write_explanation = G.recall_explanation(
        ["latency_p99_up"], episode_id=write_state.episode_id,
        use_learned=False)
    write_explanation.p0_obligations = {}
    write_path = next(path for path in write_explanation.candidate_paths
                      if path.root_node_id == "missing_index")
    write_explanation.set_path_status(write_path.path_id,
                                      CausalStatus.SUPPORTED)
    write_explanation.select_paths([write_path.path_id],
                                   unexplained_symptoms=[])
    write_state.explanation_graph = write_explanation
    write_state.esc_reports = [{"verdict": "SUFFICIENT"}]
    write_state.intervention_attempts = [CaseAttempt(
        attempt_id="case_attempt", plan_id="case_plan",
        selected_path_id=write_path.path_id,
        fix_id="create_covering_index")]
    written_case = cases.write_case_v2(
        write_state, Score(True, True, True),
        {"id": "fixture", "split": "train"})
    check("trusted episode writes a path-level CaseV2",
          written_case is not None and
          written_case.candidate_paths and
          written_case.selected_path_ids == [write_path.path_id])
    reused = no_wait_hits[0]["case"]
    utility_before = cases.utility_of_v2(reused)
    help_before = reused.recall_help_count
    saved_before = reused.tool_calls_saved
    cases.record_reuse_v2(
        reused.case_id,
        recalled_path_ids=[write_path.path_id],
        selected_path_ids=[write_path.path_id], tool_calls_saved=1)
    after = cases.fetch_case_v2(reused.case_id)
    # 断言落在计数器上，而不是效用分本身。效用分是有界的平滑帮助率：
    # 一个一直帮得上忙的案例会越来越贴近上限、增幅越来越小，拿严格大于
    # 去卡它早晚会因分辨率不够而假失败。旧的累加公式更直接 —— 帮上 6 次
    # 就顶满 1.0，此后这条断言永远不成立（这轮 E2E 就是这么跑红的）。
    check("L1 reuse utility tracks path help and saved calls",
          after.recall_help_count == help_before + 1 and
          after.tool_calls_saved == saved_before + 1 and
          after.utility_score >= utility_before,
          (utility_before, after.utility_score,
           after.recall_help_count, after.tool_calls_saved))
    before_eval = digest(cases.CASES_V2)
    eval_state = EpisodeState("eval_case", "eval")
    wrote_eval = cases.write_case_v2(
        eval_state, Score(True, True, True),
        {"id": "eval", "split": "eval"})
    check("eval provenance never writes an L1 case",
          wrote_eval is None and digest(cases.CASES_V2) == before_eval)

    print("\n[2] L2/L4 exact frontier policy changes the online tool choice")
    state = EpisodeState("tool_learning", "scenario", phase=Phase.INVESTIGATE.value)
    state.incident_window["scenario_revision"] = 1
    explanation = G.recall_explanation(
        ["latency_p99_up"], episode_id=state.episode_id,
        use_learned=False)
    state.explanation_graph = explanation
    toolbox = Toolbox(FakeObserver(), state, StateMachine(state))
    path = explanation.candidate_paths[0]
    need = EvidenceNeed.create(
        path_ids=[path.path_id], target_kind="BRANCH",
        target_ids=[path.edge_ids[0]], evidence_type="fixture_branch",
        predicate_id="fixture_branch_v2", required=False,
        freshness_seconds=60,
        candidate_tools=["get_active_sessions", "get_blocking_chain"])
    static_plan = plan_evidence_tasks(
        explanation, [need], toolbox,
        config=ToolPlanningConfig(exploration_ratio=0.0, use_learned=False))
    static_tool = static_plan.tasks[0].selected_tools[0]
    context = static_plan.tasks[0].learning_context[need.need_id]
    for index in range(5):
        state.evidence_task_audit.append({
            "event": "tool_learning_observation",
            "observation_id": f"frontier_a_blocking_{index}",
            "tool": "get_blocking_chain",
            "learning_context": context,
            "collection_status": "OBSERVED",
            "changed_statuses": 1,
            "pruned_paths": 1,
            "required_fulfilled": False,
            "changed_next_decision": True,
            "cost": 0.1,
            "latency_s": 0.1,
            "covered_need_count": 1,
            "entropy_gain": 1.0,
            "posterior_change": 0.5,
            "duplicate_calls": 0,
        })
    learning_result = evolution.learn_v2(state, Score())
    learned_plan = plan_evidence_tasks(
        explanation, [need], toolbox,
        config=ToolPlanningConfig(exploration_ratio=0.0, use_learned=True))
    learned_tool = learned_plan.tasks[0].selected_tools[0]
    check("need-bound observations write both L2 and L4",
          learning_result["l2"] == 5 and learning_result["l4"] == 5,
          learning_result)
    check("L2/L4 are consumed by the planner and change its choice",
          static_tool == "get_active_sessions" and
          learned_tool == "get_blocking_chain",
          (static_tool, learned_tool))
    learned_components = learned_plan.tasks[0].score_components[need.need_id]
    check("online score exposes nonzero conditional policy and information gain",
          learned_components["l2_conditional_policy"] > 0 and
          learned_components["l4_information_gain"] > 0,
          learned_components)
    l2_ablated = plan_evidence_tasks(
        explanation, [need], toolbox,
        config=ToolPlanningConfig(
            exploration_ratio=0.0, use_learned=True,
            use_l2=False, use_l4=True))
    l4_ablated = plan_evidence_tasks(
        explanation, [need], toolbox,
        config=ToolPlanningConfig(
            exploration_ratio=0.0, use_learned=True,
            use_l2=True, use_l4=False))
    l2_off_components = l2_ablated.tasks[0].score_components[need.need_id]
    l4_off_components = l4_ablated.tasks[0].score_components[need.need_id]
    check("L2 and L4 each have an independent online ablation",
          l2_off_components["l2_conditional_policy"] == 0 and
          l2_off_components["l4_information_gain"] > 0 and
          l4_off_components["l2_conditional_policy"] > 0 and
          l4_off_components["l4_information_gain"] == 0)
    replay_result = evolution.learn_v2(state, Score())
    check("replaying identical tool observations is idempotent",
          replay_result["l2"] == 0 and replay_result["l4"] == 0,
          replay_result)
    ablated_plan = plan_evidence_tasks(
        explanation, [need], toolbox,
        config=ToolPlanningConfig(exploration_ratio=0.0, use_learned=False))
    ablated_components = ablated_plan.tasks[0].score_components[need.need_id]
    check("learned=False zeros L2/L4 and restores static discriminator order",
          ablated_plan.tasks[0].selected_tools[0] == static_tool and
          ablated_components["l2_conditional_policy"] == 0 and
          ablated_components["l4_information_gain"] == 0)
    check("L4 actually limits each Subagent to one-to-three tools",
          all(1 <= len(task.selected_tools) <= 3
              for task in learned_plan.tasks))
    check("L4 exposes fewer tools than the legal candidate set",
          len(learned_plan.tasks[0].selected_tools) <
          len(need.candidate_tools),
          (need.candidate_tools, learned_plan.tasks[0].selected_tools))

    second_path = explanation.candidate_paths[1]
    second_need = EvidenceNeed.create(
        path_ids=[second_path.path_id], target_kind="BRANCH",
        target_ids=[second_path.edge_ids[0]], evidence_type="fixture_branch",
        predicate_id="fixture_branch_v2", required=False,
        freshness_seconds=60,
        candidate_tools=["get_active_sessions", "get_blocking_chain"])
    second_static = plan_evidence_tasks(
        explanation, [second_need], toolbox,
        config=ToolPlanningConfig(exploration_ratio=0.0, use_learned=False))
    second_context = second_static.tasks[0].learning_context[second_need.need_id]
    state.evidence_task_audit = []
    for index in range(5):
        state.evidence_task_audit.append({
            "event": "tool_learning_observation",
            "observation_id": f"frontier_b_sessions_{index}",
            "tool": "get_active_sessions",
            "learning_context": second_context,
            "collection_status": "OBSERVED",
            "changed_statuses": 1,
            "pruned_paths": 1,
            "required_fulfilled": False,
            "changed_next_decision": True,
            "cost": 0.1,
            "latency_s": 0.1,
            "covered_need_count": 1,
            "entropy_gain": 1.0,
            "posterior_change": 0.5,
            "duplicate_calls": 0,
        })
    evolution.learn_v2(state, Score())
    second_learned = plan_evidence_tasks(
        explanation, [second_need], toolbox,
        config=ToolPlanningConfig(exploration_ratio=0.0, use_learned=True))
    check("different frontier signatures learn different tool sets",
          second_learned.tasks[0].selected_tools !=
          learned_plan.tasks[0].selected_tools,
          (learned_plan.tasks[0].selected_tools,
           second_learned.tasks[0].selected_tools))

    print("\n[3] L3 learns stable cause-to-cause edges and whole paths")

    def learn_path(episode_id: str, target_path_id: str, *,
                   verified: bool, affected_edges=None,
                   symptom: str = "latency_p99_up"):
        exp = G.recall_explanation(
            [symptom], episode_id=episode_id,
            use_learned=False)
        exp.p0_obligations = {}
        target = exp.path_map()[target_path_id]
        exp.set_path_status(target.path_id, CausalStatus.SUPPORTED)
        exp.select_paths([target.path_id], unexplained_symptoms=[])
        st = EpisodeState(episode_id, "scenario")
        st.explanation_graph = exp
        st.esc_reports = [{"verdict": "SUFFICIENT"}]
        st.intervention_attempts = [SimpleNamespace(
            attempt_id=f"attempt_{episode_id}", learnable=True,
            outcome="VERIFIED" if verified else "FAILED",
            failure_scope="NONE" if verified else "PATH_SEGMENT",
            affected_edge_ids=list(affected_edges or []),
            selected_path_id=target.path_id,
        )]
        score = Score(verified, verified, verified)
        return evolution.learn_v2(st, score), exp, target

    static_latency = G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=False)
    lock_path = next(path for path in static_latency
                     if path.root_node_id == "lock_contention")
    for index in range(4):
        learn_path(f"lock_positive_{index}", lock_path.path_id, verified=True)
    boosted = G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=True)
    check("controlled positive outcomes change path ranking",
          boosted[0].path_id == lock_path.path_id,
          [path.root_node_id for path in boosted[:3]])
    edge_only = G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=True,
        use_l3_edges=True, use_l3_paths=False)
    path_only = G.enumerate_causal_paths(
        ["latency_p99_up"], use_learned=True,
        use_l3_edges=False, use_l3_paths=True)
    edge_score = next(path for path in edge_only
                      if path.path_id == lock_path.path_id).score_components
    path_score = next(path for path in path_only
                      if path.path_id == lock_path.path_id).score_components
    check("edge and whole-path channels have independent ablations",
          edge_score["l3_edge_adjustment"] > 0 and
          edge_score["l3_path_adjustment"] == 0 and
          path_score["l3_edge_adjustment"] == 0 and
          path_score["l3_path_adjustment"] > 0)
    before_negative = next(path for path in boosted
                           if path.path_id == lock_path.path_id
                           ).score_components["total"]
    learn_path("lock_negative_1", lock_path.path_id, verified=False,
               affected_edges=lock_path.edge_ids)
    learn_path("lock_negative_2", lock_path.path_id, verified=False,
               affected_edges=lock_path.edge_ids)
    after_negative = next(
        path for path in G.enumerate_causal_paths(
            ["latency_p99_up"], use_learned=True)
        if path.path_id == lock_path.path_id).score_components["total"]
    check("scoped negative outcome symmetrically lowers the affected path score",
          after_negative < before_negative,
          (before_negative, after_negative))

    disk_paths = G.enumerate_causal_paths(
        ["disk_growing"], use_learned=False)
    multi = next(path for path in disk_paths if len(path.edge_ids) >= 3)
    result, multi_exp, multi_path = learn_path(
        "cause_to_cause_positive", multi.path_id, verified=True,
        symptom="disk_growing")
    edge_adjustments, _ = evolution.load_l3_v2_adjustments(
        multi_exp.graph_version)
    graph = G.load()
    cause_edge_ids = [
        edge_id for src, dst, edge_id in zip(
            multi_path.node_ids, multi_path.node_ids[1:], multi_path.edge_ids)
        if graph.nodes[src].get("kind") == "RootCause" and
        graph.nodes[dst].get("kind") == "RootCause"
    ]
    check("L3 writes and consumes cause-to-cause stable edge IDs",
          bool(cause_edge_ids) and
          all(edge_id in edge_adjustments for edge_id in cause_edge_ids),
          cause_edge_ids)
    check("L3 keys contain no runtime-value prose",
          all(key.startswith("edge_") for key in edge_adjustments))
    check("a mismatched graph version consumes no L3 adjustment",
          evolution.load_l3_v2_adjustments("graph_stale") == ({}, {}))

    print("\n[4] Pollution and version governance")
    manifest = yaml.safe_load((
        REPO / "knowledge" / "learned" / "v2" / "manifest.yaml"
    ).read_text(encoding="utf-8"))
    check("v2 manifest disables implicit v1 migration",
          manifest["schema_version"] == 2 and
          manifest["v1_import"]["enabled"] is False)
    check("manifest declares an existing schema and every v2 layer",
          (REPO / "knowledge" / "learned" / "v2" /
           manifest["schema"]).exists() and
          all((REPO / "knowledge" / "learned" / "v2" / filename).exists()
              for filename in manifest["layers"].values()))
    polluted = EpisodeState("cooccurrence", "scenario")
    polluted.note("test", "session_wait_profile", "unrelated high frequency")
    graph_version_before = G.graph_version()
    touched = structure.observe_episode_v2(polluted)
    check("unrelated scratchpad co-occurrence creates no v2 edge proposal",
          touched == [] and structure.load_candidates_v2() == {})
    try:
        structure.propose_v2(
            kind="FIXED_BY", src="missing_index", dst="create_covering_index",
            episode_id="e", scenario_id="s")
        fixed_by_blocked = False
    except ValueError:
        fixed_by_blocked = True
    check("FIXED_BY has no automatic proposal path", fixed_by_blocked)
    for index in range(3):
        proposal = structure.propose_v2(
            kind="CAUSES", src="missing_index", dst="disk_growing",
            episode_id=f"struct_{index}",
            scenario_id=f"scenario_{index % 2}",
            temporal_order=True, reduces_orphan_symptom=True)
    check("explicit cross-episode causal observations only become review-ready",
          proposal.status == "ready_for_review" and proposal.ready,
          proposal.status)
    check("candidate/ready proposals never enter the live graph",
          G.graph_version() == graph_version_before and
          not G.load().has_edge("missing_index", "disk_growing", key="CAUSES"))
    l2_doc = evolution._load_v2_doc(
        "investigation_policy.yaml", {"records": {}})
    one_record = next(iter(l2_doc["records"].values()))
    stale_context = dict(context)
    stale_context["graph_version"] = "graph_changed"
    stale_scores = evolution.v2_tool_learning_components(
        stale_context, one_record["tool"])
    check("graph/scenario/tool version mismatch suppresses old policies",
          stale_scores == (0.0, 0.0, 0), stale_scores)

    p0_off = G.recall_explanation(
        ["disk_growing"], episode_id="p0_off", use_learned=False)
    p0_on = G.recall_explanation(
        ["disk_growing"], episode_id="p0_on", use_learned=True)
    check("learning does not reduce reachable P0 recall",
          set(p0_off.p0_obligations) == set(p0_on.p0_obligations) and
          all(set(p0_off.p0_obligations[key].reachable_path_ids) ==
              set(p0_on.p0_obligations[key].reachable_path_ids)
              for key in p0_off.p0_obligations))

    eval_learning = evolution.learn_v2(
        state, Score(), split="eval", provenance="sandbox")
    check("eval provenance is excluded before L2-L4 writes",
          eval_learning["written"] is False)

finally:
    evolution.LEARNED = old_evolution_learned
    cases.LEARNED_V2 = old_cases_dir
    cases.CASES_V2 = old_cases_file
    structure.LEARNED = old_structure_learned
    shutil.rmtree(tmp, ignore_errors=True)

v1_after = {path.name: digest(path) for path in v1_paths}
print("\n[5] v1 audit stores remain untouched")
check("all v1 learned YAML hashes are unchanged", v1_before == v1_after)

print("\n" + "=" * 80)
print("LEARNING V2: " + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
