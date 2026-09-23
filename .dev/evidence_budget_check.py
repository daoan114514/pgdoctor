#!/usr/bin/env python3
"""取证预算不得制造证据（CLAUDE.md 硬规则 6，架构评审第 2 条）。

工具调用预算的唯一扣账点是 Toolbox._enter，主策略与子 agent 共用。原来规划器不看余量，
最后一轮必然超支：RuntimeError 在子 agent 内部变成 ERROR 汇报，编排器记成
evidence_need_unavailable 与 ERROR 观测，ESC 据此 EXHAUSTED、L2/L4 据此学坏。
这里钉住：规划器按余量裁剪、裁掉的记延后不记不可得；预算耗尽的任务结果不进不可得、
不进观测；max_steps 默认值只有一份。
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import loop as loop_mod  # noqa: E402
from agent import orchestrator  # noqa: E402
from agent.episode_state import (DEFAULT_MAX_STEPS, EpisodeState,  # noqa: E402
                                 EvidenceBudgetExhausted)
from agent.explanation import EvidenceNeed  # noqa: E402
from agent.investigator import EvidenceTaskResult  # noqa: E402
from agent.permissions import Role  # noqa: E402
from agent.state_machine import Phase, StateMachine  # noqa: E402
from agent.tool_planner import plan_evidence_tasks  # noqa: E402
from agent.toolbox import Toolbox  # noqa: E402
from knowledge.causal_graph import graph as G  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))


class Observer:
    def get_active_sessions(self, include_idle=False):
        return []

    def get_blocking_chain(self):
        return []

    def get_connection_stats(self):
        return {}

    @staticmethod
    def extension_available(_name):
        return False


state = EpisodeState("budget_fixture", "controlled_fixture", phase=Phase.INVESTIGATE.value)
state.incident_window["scenario_revision"] = 2
explanation = G.recall_explanation(["latency_p99_up"], episode_id=state.episode_id, use_learned=False)
state.explanation_graph = explanation
toolbox = Toolbox(Observer(), state, StateMachine(state))
path = next(p for p in explanation.candidate_paths if p.root_node_id == "lock_contention")


def need(tools, required=False, et="fixture_branch"):
    return EvidenceNeed.create(
        path_ids=[path.path_id], target_kind="BRANCH", target_ids=[path.edge_ids[0]],
        evidence_type=et, predicate_id="fixture_branch_v2", required=required,
        freshness_seconds=60, candidate_tools=list(tools), reason="fixture")


needs = [need(["get_active_sessions"], required=True, et="fixture_a"),
         need(["get_blocking_chain"], et="fixture_b"),
         need(["get_connection_stats"], et="fixture_c")]

print("[1] 规划器按余量裁剪")
full = plan_evidence_tasks(explanation, needs, toolbox, target_context={"table": "orders"})
check(len(full.tasks) == 3 and not full.deferred_need_ids, f"余量不限时 3 个任务、无延后（{len(full.tasks)}）")
cut = plan_evidence_tasks(explanation, needs, toolbox, target_context={"table": "orders"}, remaining_calls=1)
check(len(cut.tasks) == 1, f"余量 1 -> 只留 1 个任务（{len(cut.tasks)}）")
check(any(n.required for n in needs if n.need_id in cut.tasks[0].need_ids) if cut.tasks else False,
      "留下的是必需 need 的任务")
check(len(cut.deferred_need_ids) == 2, f"裁掉的 2 个 need 记为延后（{cut.deferred_need_ids}）")
check(not any(n in cut.unavailable_needs for n in cut.deferred_need_ids), "延后的 need 不出现在 unavailable_needs")
zero = plan_evidence_tasks(explanation, needs, toolbox, target_context={"table": "orders"}, remaining_calls=0)
check(len(zero.tasks) == 0 and len(zero.deferred_need_ids) == 3, "余量 0 -> 0 任务、3 个延后")

print("[2] 子 agent 角色撞预算抛独立异常")
state.budget["max_steps"] = state.budget["steps"] + 2   # spend() 先加后比：留恰好一次合法调用
sub = toolbox.scoped(role=Role.INVESTIGATOR, task_context=full.tasks[0], environment_tools={"get_active_sessions", "report_evidence"})
sub._enter("get_active_sessions")          # 最后一次合法调用
raised = None
try:
    sub._enter("get_active_sessions")
except Exception as exc:                    # noqa: BLE001
    raised = exc
check(isinstance(raised, EvidenceBudgetExhausted), f"INVESTIGATOR 超预算抛 EvidenceBudgetExhausted（{type(raised).__name__}）")

print("[3] 预算耗尽的任务结果不进不可得、不进观测")
before_audit = len(state.evidence_task_audit)
res = EvidenceTaskResult(need_id=full.tasks[0].need_ids[0], task_id=full.tasks[0].task_id,
                         need_ids=list(full.tasks[0].need_ids), error="BUDGET: 取证预算耗尽",
                         budget_exhausted=True)
orchestrator._mark_unavailable(state, full, needs, [res])
merged = orchestrator.merge_evidence_task_results(state, full, [res])
orchestrator._record_tool_learning_observations(state, full, needs, [res], merged)
new = state.evidence_task_audit[before_audit:]
events = [x.get("event") for x in new]
check("evidence_need_unavailable" not in events, f"没有 evidence_need_unavailable（{events}）")
check("tool_learning_observation" not in events, "没有 tool_learning_observation")
check("evidence_task_budget_exhausted" in events, "记了一条 evidence_task_budget_exhausted 审计")

print("[4] max_steps 默认值只有一份")
lsrc = inspect.getsource(loop_mod)
check(not re.search(r"max_steps: int = \d", lsrc), "loop.py 没有字面量 max_steps 默认值")
check("DEFAULT_MAX_STEPS" in lsrc, "loop.py 引用 DEFAULT_MAX_STEPS")
rsrc = (ROOT / "eval" / "run_suite.py").read_text(encoding="utf-8")
check('"--max-steps", type=int, default=DEFAULT_MAX_STEPS' in rsrc, "run_suite 的 --max-steps 默认引用 DEFAULT_MAX_STEPS")
check(isinstance(DEFAULT_MAX_STEPS, int) and DEFAULT_MAX_STEPS > 0, f"DEFAULT_MAX_STEPS={DEFAULT_MAX_STEPS}")

print()
if fails:
    print(f"EVIDENCE BUDGET: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"EVIDENCE BUDGET: PASS（{checks} 项）")
