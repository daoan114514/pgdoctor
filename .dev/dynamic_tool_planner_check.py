"""Independent acceptance checks for the v2 dynamic tool planner."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState
from agent.explanation import EvidenceNeed
from agent.permissions import Role, allowed_tools
from agent.state_machine import Phase, StateMachine
from agent.tool_planner import (ToolPlanningConfig, environment_availability,
                                plan_evidence_tasks)
from agent.toolbox import Toolbox
from knowledge.causal_graph import graph as G


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<54} {detail}")


class Observer:
    def get_active_sessions(self):
        return []

    def get_blocking_chain(self):
        return []

    def get_physical_bloat(self, _table):
        return {}

    @staticmethod
    def extension_available(_name):
        return False


state = EpisodeState("dynamic_planner", "controlled_fixture",
                     phase=Phase.INVESTIGATE.value)
state.incident_window["scenario_revision"] = 2
explanation = G.recall_explanation(
    ["latency_p99_up"], episode_id=state.episode_id, use_learned=False)
state.explanation_graph = explanation
toolbox = Toolbox(Observer(), state, StateMachine(state))
path = next(path for path in explanation.candidate_paths
            if path.root_node_id == "lock_contention")


def need(*, tools, required=False, evidence_type="fixture_branch",
         predicate="fixture_branch_v2"):
    return EvidenceNeed.create(
        path_ids=[path.path_id], target_kind="BRANCH",
        target_ids=[path.edge_ids[0]], evidence_type=evidence_type,
        predicate_id=predicate, required=required, freshness_seconds=60,
        candidate_tools=list(tools), reason="controlled branch discriminator")


print("[1] One permission authority computes all four intersections")
intersection_need = need(tools=["get_active_sessions", "get_database_stats"])
effective = allowed_tools(
    Phase.INVESTIGATE, Role.INVESTIGATOR,
    evidence_need=intersection_need,
    environment_tools={"get_active_sessions", "report_evidence"})
check("phase/role/need/environment intersection is exact",
      effective == {"get_active_sessions", "report_evidence"}, effective)
outside_phase = allowed_tools(
    Phase.DIAGNOSE, Role.INVESTIGATOR,
    evidence_need=intersection_need,
    environment_tools={"get_active_sessions", "report_evidence"})
check("investigator receives no tools outside INVESTIGATE",
      outside_phase == set())
check("v2 investigator never receives verdict/proposal powers",
      not ({"report_verdict", "set_hypothesis", "declare_root_cause",
            "submit_proposal"} & effective))

print("\n[2] Environment and target checks fail closed")
availability = environment_availability(toolbox, target_context={"table": "orders"})
check("missing observer methods are unavailable",
      not availability["get_database_stats"].available)
check("missing extension makes physical bloat unavailable",
      not availability["get_physical_bloat"].available and
      not availability["get_physical_bloat"].checks["extension_available"])
physical_need = need(
    tools=["get_physical_bloat"], required=True,
    evidence_type="physical_bloat_ratio",
    predicate="physical_bloat_ratio_v2")
unavailable_plan = plan_evidence_tasks(
    explanation, [physical_need], toolbox, target_context={"table": "orders"},
    config=ToolPlanningConfig(use_learned=False))
check("empty intersection becomes an unavailable need, not full toolbox",
      physical_need.need_id in unavailable_plan.unavailable_needs and
      unavailable_plan.tasks == [])

print("\n[3] Required fallback and one-to-three tool boundary")
required_need = need(
    tools=["get_active_sessions", "get_blocking_chain"], required=True)
for status in ("UNKNOWN", "ERROR", "UNKNOWN"):
    state.note("fixture", "fixture_branch", "collection unavailable",
               status=status, collection_tool="get_active_sessions")
required_plan = plan_evidence_tasks(
    explanation, [required_need], toolbox,
    config=ToolPlanningConfig(exploration_ratio=0.0, use_learned=False))
check("required evidence is not dropped by bad history",
      bool(required_plan.tasks) and
      required_need.need_id in required_plan.tasks[0].need_ids)
check("each subagent receives between one and three tools",
      all(1 <= len(task.selected_tools) <= 3 for task in required_plan.tasks))
scores = required_plan.tasks[0].score_components[required_need.need_id]
check("ranking exposes required bonus and error penalty",
      scores["required_evidence_bonus"] > 0 and
      "unknown_error_probability" in scores)

print("\n[4] Exploration is legal and replayable")
optional = need(tools=["get_active_sessions", "get_blocking_chain"])
config = ToolPlanningConfig(
    exploration_ratio=1.0, random_seed=20260902, use_learned=False)
first = plan_evidence_tasks(explanation, [optional], toolbox, config=config)
second = plan_evidence_tasks(explanation, [optional], toolbox, config=config)
first_tools = [task.selected_tools for task in first.tasks]
second_tools = [task.selected_tools for task in second.tasks]
check("fixed-seed exploration replays the same legal choice",
      first_tools == second_tools and bool(first_tools), first_tools)
check("exploration never escapes the need candidate set",
      set(first_tools[0]).issubset(optional.candidate_tools))

print("\n" + "=" * 76)
print("DYNAMIC TOOL PLANNER:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
