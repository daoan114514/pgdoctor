"""Independent acceptance checks for path-fragment subagent contracts."""
from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState
from agent.explanation import EvidenceNeed, EvidenceReport
from agent.investigator import EvidenceTaskResult
from agent.orchestrator import merge_evidence_task_results
from agent.permissions import Role, allowed_tools
from agent.state_machine import Phase, StateMachine
from agent.tool_planner import (ToolPlanningConfig, plan_evidence_tasks,
                                task_environment_tools)
from agent.toolbox import Toolbox
from knowledge.causal_graph import graph as G
from sandbox.traces import TRACE_DIR, TraceStore


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<56} {detail}")


class Observer:
    def get_active_sessions(self):
        return []

    @staticmethod
    def extension_available(_name):
        return False


episode_id = f"subagent_path_{uuid.uuid4().hex}"
try:
    state = EpisodeState(episode_id, "controlled_path_fragment",
                         phase=Phase.INVESTIGATE.value)
    explanation = G.recall_explanation(
        ["latency_p99_up"], episode_id=episode_id, use_learned=False)
    state.explanation_graph = explanation
    path = next(path for path in explanation.candidate_paths
                if path.root_node_id == "lock_contention")
    need = EvidenceNeed.create(
        path_ids=[path.path_id], target_kind="EDGE",
        target_ids=[path.edge_ids[0]], evidence_type="session_wait_profile",
        predicate_id="session_wait_profile_v2", required=False,
        freshness_seconds=60, candidate_tools=["get_active_sessions"],
        reason="distinguish lock contention on this concrete path segment")
    toolbox = Toolbox(Observer(), state, StateMachine(state))
    plan = plan_evidence_tasks(
        explanation, [need], toolbox,
        config=ToolPlanningConfig(use_learned=False))
    task = plan.tasks[0]

    print("[1] Task unit is a local path segment")
    local_paths = task.local_subgraph["paths"]
    check("task is bound to explanation id and revision",
          task.explanation_id == explanation.explanation_id and
          task.explanation_revision == explanation.revision)
    check("prompt payload contains only assigned path fragments",
          len(local_paths) == 1 and
          local_paths[0]["path_id"] == path.path_id and
          task.target_ids == [path.edge_ids[0]])
    exposed = task_environment_tools(task)
    authoritative = allowed_tools(
        Phase.INVESTIGATE, Role.INVESTIGATOR, task_context=task,
        environment_tools=exposed)
    check("subagent exposes only collection plus report_evidence",
          authoritative == {"get_active_sessions", "report_evidence"},
          authoritative)
    check("verdict, root declaration, and proposal tools are absent",
          not ({"report_verdict", "set_hypothesis", "declare_root_cause",
                "submit_proposal"} & authoritative))

    print("\n[2] EvidenceReport is observation-only")
    rejected = False
    try:
        EvidenceReport.from_dict({
            "need_id": need.need_id, "tool": "get_active_sessions",
            "raw_refs": [], "observations": [],
            "collection_status": "UNKNOWN", "limitations": [],
            "verdict": "CONFIRMED",
        })
    except ValueError:
        rejected = True
    check("report schema rejects a causal verdict", rejected)

    store = TraceStore(episode_id)
    structured = [{"pid": 77, "wait_event": "Lock:transactionid"}]
    raw_ref = store.record("get_active_sessions", {}, json.dumps(structured),
                           structured)
    state.note(
        "investigator", "session_wait_profile", "structured observation",
        raw_ref=raw_ref, structured_value=structured,
        bears_on=["lock_contention"],
        predicate_id="session_wait_profile_v2", target_kind="EDGE",
        target_ids=[path.edge_ids[0]], explanation_id=explanation.explanation_id,
        explanation_revision=explanation.revision,
        evidence_task_id=task.task_id, evidence_need_ids=[need.need_id],
        collection_tool="get_active_sessions")
    report = EvidenceReport.from_dict({
        "need_id": need.need_id, "tool": "get_active_sessions",
        "raw_refs": [raw_ref], "observations": structured,
        "collection_status": "OBSERVED", "limitations": [],
    })
    result = EvidenceTaskResult(
        need_id=need.need_id, task_id=task.task_id,
        need_ids=[need.need_id], reports=[report],
        explanation_id=plan.explanation_id,
        explanation_revision=plan.explanation_revision)
    merged = merge_evidence_task_results(state, plan, [result])
    check("main predicate layer creates the causal binding",
          len(merged.binding_ids) == 1 and
          explanation.evidence_bindings[merged.binding_ids[0]].predicate_result
          == "SUPPORTS")
    duplicate = merge_evidence_task_results(state, plan, [result])
    check("need/report/binding merge is idempotent",
          not duplicate.binding_ids and bool(duplicate.duplicate_report_ids))

    print("\n[3] Late results never overwrite a newer revision")
    late_value = [{"pid": 88, "wait_event": "Lock:tuple"}]
    late_ref = store.record("get_active_sessions", {"late": True},
                            json.dumps(late_value), late_value)
    state.note(
        "investigator", "session_wait_profile", "late observation",
        raw_ref=late_ref, structured_value=late_value,
        bears_on=["lock_contention"],
        predicate_id="session_wait_profile_v2", target_kind="EDGE",
        target_ids=[path.edge_ids[0]], explanation_id=plan.explanation_id,
        explanation_revision=plan.explanation_revision,
        evidence_task_id=task.task_id, evidence_need_ids=[need.need_id],
        collection_tool="get_active_sessions")
    late_report = EvidenceReport.from_dict({
        "need_id": need.need_id, "tool": "get_active_sessions",
        "raw_refs": [late_ref], "observations": late_value,
        "collection_status": "OBSERVED", "limitations": [],
    })
    late_result = EvidenceTaskResult(
        need_id=need.need_id, task_id=task.task_id,
        need_ids=[need.need_id], reports=[late_report],
        explanation_id=plan.explanation_id,
        explanation_revision=plan.explanation_revision)
    explanation.set_node_status(path.observed_symptom_id, "SUPPORTED")
    revision_before = explanation.revision
    late = merge_evidence_task_results(state, plan, [late_result])
    check("late report is persisted only as a deferred candidate",
          bool(late.late_report_ids) and not late.binding_ids)
    check("late report cannot mutate the current explanation revision",
          explanation.revision == revision_before)
finally:
    shutil.rmtree(TRACE_DIR / episode_id, ignore_errors=True)

print("\n" + "=" * 78)
print("SUBAGENT PATH TASK:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
