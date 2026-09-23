#!/usr/bin/env python3
"""无参工具的确定性取证（架构评审第 9 条）：不起 SDK 会话也能产出可合并的报告。"""
from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import investigator, orchestrator  # noqa: E402
from agent.episode_state import EpisodeState  # noqa: E402
from agent.explanation import EvidenceNeed  # noqa: E402
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


print("[1] 确定性工具集合都是无参工具")
isrc = inspect.getsource(investigator._tools_for)
for name in sorted(orchestrator.DETERMINISTIC_TOOLS):
    m = re.search(r'tool\("%s",\s*".*?"(?:\s*".*?")*,\s*\{\}\)' % name, isrc, re.S)
    check(m is not None, f"{name} 在 investigator 工具表里 schema 为 {{}}")


from sandbox.traces import TraceStore  # noqa: E402


class Observer:
    trace = TraceStore("det_fixture")

    def get_connection_stats(self):
        # 与 e2e 夹具 / 真观测器同形：toolbox 会读 near_limit 等键，raw_ref 由 trace 记录给出
        value = {"used": 97, "max_connections": 100, "pct": 97.0, "near_limit": True,
                 "idle_in_transaction": 0, "by_user": {"app_user": 97},
                 "by_state": {"idle": 87, "active": 10}}
        value["raw_ref"] = self.trace.record("get_connection_stats", {}, json.dumps(value), value)
        return value

    def get_blocking_chain(self):
        return []

    @staticmethod
    def extension_available(_name):
        return False


state = EpisodeState("det_fixture", "controlled_fixture", phase=Phase.INVESTIGATE.value)
state.incident_window["scenario_revision"] = 2
explanation = G.recall_explanation(["latency_p99_up"], episode_id=state.episode_id, use_learned=False)
state.explanation_graph = explanation
toolbox = Toolbox(Observer(), state, StateMachine(state))
path = next(p for p in explanation.candidate_paths if p.root_node_id == "lock_contention")
needs = [EvidenceNeed.create(
    path_ids=[path.path_id], target_kind="BRANCH", target_ids=[path.edge_ids[0]],
    evidence_type="connection_count", predicate_id="connection_count_v2", required=True,
    freshness_seconds=60, candidate_tools=["get_connection_stats"], reason="fixture")]
plan = plan_evidence_tasks(explanation, needs, toolbox, target_context={"table": "orders"})
task = next((t for t in plan.tasks if t.selected_tools == ["get_connection_stats"]), None)

print("[2] 进程内执行产出可合并的报告")
check(task is not None, "规划器给 connection_count 派了 get_connection_stats")
if task is not None:
    before = len(state.scratchpad)
    res = orchestrator._run_task_deterministic(state, toolbox, task)
    check(res.executor == "deterministic" and res.turns == 0 and res.cost_usd == 0.0, "executor=deterministic、0 轮、$0")
    check(not res.error, "进程内调用没有异常", res.error)
    check(len(res.reports) == len(task.need_ids) and all(r.collection_status == "OBSERVED" for r in res.reports),
          f"每个 need 一份 OBSERVED 报告（{len(res.reports)}）", [r.collection_status for r in res.reports] or res.error)
    refs = {str(e.get("raw_ref")) for e in state.scratchpad[before:] if e.get("raw_ref")}
    check(all(set(r.raw_refs) <= refs and r.raw_refs for r in res.reports), "报告引用的 raw_ref 都是本任务落盘的条目")
    merged = orchestrator.merge_evidence_task_results(state, plan, [res])
    check(len(merged.accepted_report_ids) == len(res.reports) and not merged.rejected_reports,
          f"合并接受全部报告（拒绝 {merged.rejected_reports}）")
    obs = [x for x in state.evidence_task_audit if x.get("event") == "evidence_merge"]
    check(bool(obs), "合并结果落审计")

print("[3] 编排器只对无参工具走确定性路径")
osrc = inspect.getsource(orchestrator.run_evidence_investigation)
check("planning_config.deterministic_argless" in osrc and "DETERMINISTIC_TOOLS" in osrc, "run_task 按配置与工具集合分流")

print()
if fails:
    print(f"DETERMINISTIC EVIDENCE: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"DETERMINISTIC EVIDENCE: PASS（{checks} 项）")
