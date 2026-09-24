#!/usr/bin/env python3
"""确定性取证（架构评审第 9 条）：无参工具与参数由目标上下文钉死的工具不起 SDK 会话，
也能产出可合并的报告；同一轮里按工具性质排执行顺序（2026-09-24）。"""
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


print("[1] 无参集合里都是无参工具；钉参集合里都是有参工具")
isrc = inspect.getsource(investigator._tools_for)
for name in sorted(orchestrator.ARGLESS_TOOLS):
    m = re.search(r'tool\("%s",\s*".*?"(?:\s*".*?")*,\s*\{\}\)' % name, isrc, re.S)
    check(m is not None, f"{name} 在 investigator 工具表里 schema 为 {{}}")
check(orchestrator.DETERMINISTIC_TOOLS == orchestrator.ARGLESS_TOOLS | orchestrator.PINNED_ARG_TOOLS
      and not (orchestrator.ARGLESS_TOOLS & orchestrator.PINNED_ARG_TOOLS), "确定性集合 = 无参 ∪ 钉参，互不相交")
check("simulate_index" not in orchestrator.DETERMINISTIC_TOOLS, "simulate_index 要设计索引定义，仍交给子 agent")
for name in sorted(orchestrator.PINNED_ARG_TOOLS):
    check(re.search(r'tool\("%s"' % name, isrc) is not None, f"{name} 在 investigator 工具表里")

print("[1b] 钉参工具的参数全部取自目标上下文")
ctx = {"hot_query": "SELECT id FROM orders WHERE user_id = %(uid)s", "table": "orders", "probe_uid": 5150}
args = orchestrator.deterministic_args("explain_query", ctx)
check(args == {"sql": ctx["hot_query"], "params": {"uid": 5150}}, "explain_query 用热查询与注入器给的探针 uid", args)
args = orchestrator.deterministic_args("explain_query", {"hot_query": ctx["hot_query"]})
check(args["params"] == {"uid": orchestrator.DEFAULT_PROBE_UID}, "没有探针 uid 时用固定默认值", args)
for name in ("get_indexes", "get_table_stats", "get_physical_bloat"):
    check(orchestrator.deterministic_args(name, ctx) == {"table": "orders"}, f"{name} 用目标表")
check(orchestrator.deterministic_args("get_top_queries", ctx) == {"n": 5}, "get_top_queries 固定取前 5")
for name, missing in (("explain_query", {"table": "orders"}), ("get_indexes", {"hot_query": "SELECT 1"}),
                      ("get_table_stats", {}), ("get_physical_bloat", {})):
    try:
        orchestrator.deterministic_args(name, missing)
        raised = False
    except ValueError:
        raised = True
    check(raised, f"{name} 缺必需目标时抛 ValueError（不猜参数）")
for name in sorted(orchestrator.DETERMINISTIC_TOOLS):
    try:
        inspect.signature(getattr(Toolbox, name)).bind(None, **orchestrator.deterministic_args(name, ctx))
        bound = True
    except TypeError as exc:
        bound = str(exc)
    check(bound is True, f"Toolbox.{name} 接受确定性参数", bound)
from agent.tool_planner import ToolPlanningConfig, infer_target_context  # noqa: E402
check(ToolPlanningConfig().deterministic_pinned is True and ToolPlanningConfig().deterministic_argless is True,
      "两类确定性执行默认都开")
check(infer_target_context(ctx["hot_query"], probe_uid=5150).get("probe_uid") == 5150
      and infer_target_context(ctx["hot_query"], probe_uid=None).get("probe_uid") is None,
      "探针 uid 进目标上下文（没有就不带）")
lsrc = (ROOT / "agent" / "llm_policy.py").read_text(encoding="utf-8")
loop_src = (ROOT / "agent" / "loop.py").read_text(encoding="utf-8")
check('probe_uid=ctx.get("probe_uid")' in lsrc and '"probe_uid": getattr(env, "probe_uid", None)' in loop_src,
      "探针 uid 从环境经 ctx 传到取证编排")
check("batch_size=4" in (ROOT / "eval" / "run_suite.py").read_text(encoding="utf-8"), "跑批子 agent 并发为 4")


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

print("[3] 编排器按配置与工具集合分流，确定性任务先于子 agent、按工具性质排序")
osrc = inspect.getsource(orchestrator.run_evidence_investigation)
check("planning_config.deterministic_argless" in osrc and "planning_config.deterministic_pinned" in osrc
      and "ARGLESS_TOOLS" in osrc and "PINNED_ARG_TOOLS" in osrc, "分流看两个开关与两个工具集合")
rank = orchestrator.deterministic_rank
check(rank("get_database_stats") < rank("get_connection_stats") < rank("explain_query")
      and rank("get_table_stats") == rank("get_top_queries") == 0, "窗口读者 < 其它 < 给计数器记账的工具")
check(orchestrator.COUNTER_PERTURBING_TOOLS <= orchestrator.DETERMINISTIC_TOOLS
      and orchestrator.WINDOW_READER_TOOLS <= orchestrator.DETERMINISTIC_TOOLS, "排序只作用于确定性工具")

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from agent.investigator import EvidenceTaskResult  # noqa: E402


def _fake_task(tid, tools):
    return SimpleNamespace(task_id=tid, selected_tools=list(tools), need_ids=[f"n_{tid}"],
                           explanation_id="x", explanation_revision=1, target_context={})


fake_plan = SimpleNamespace(tasks=[
    _fake_task("t_explain", ["explain_query"]), _fake_task("t_sub", ["simulate_index"]),
    _fake_task("t_conn", ["get_connection_stats"]), _fake_task("t_db", ["get_database_stats"]),
    _fake_task("t_multi", ["get_table_stats", "explain_query"])], deferred_need_ids=[])
executed: list[str] = []


def _fake_det(_st, _tb, task):
    executed.append(task.task_id)
    return EvidenceTaskResult(need_id=task.need_ids[0], task_id=task.task_id, executor="deterministic")


async def _fake_investigate(task, *_a, **_k):
    executed.append(task.task_id)
    return EvidenceTaskResult(need_id=task.need_ids[0], task_id=task.task_id, executor="subagent")


saved = {name: getattr(orchestrator, name) for name in (
    "plan_evidence_tasks", "_run_task_deterministic", "investigate_task", "merge_evidence_task_results",
    "_mark_unavailable", "_record_tool_learning_observations")}
orchestrator.plan_evidence_tasks = lambda *a, **k: fake_plan
orchestrator._run_task_deterministic = _fake_det
orchestrator.investigate_task = _fake_investigate
orchestrator.merge_evidence_task_results = lambda *a, **k: SimpleNamespace(accepted_report_ids=[], rejected_reports=[])
orchestrator._mark_unavailable = lambda *a, **k: None
orchestrator._record_tool_learning_observations = lambda *a, **k: None
state2 = EpisodeState("det_order_fixture", "controlled_fixture", phase=Phase.INVESTIGATE.value)
state2.explanation_graph = explanation
state2.save = lambda: None
try:
    out = asyncio.run(orchestrator.run_evidence_investigation(state2, toolbox, [], "SELECT 1", max_concurrency=4))
finally:
    for name, fn in saved.items():
        setattr(orchestrator, name, fn)
check(executed[:3] == ["t_db", "t_conn", "t_explain"], f"确定性任务按 读者→其它→记账者 执行（{executed[:3]}）")
check(set(executed[3:]) == {"t_sub", "t_multi"}, "单工具 simulate_index 与多工具任务交给子 agent，且在确定性任务之后")
check([r.task_id for r in out.task_results] == [t.task_id for t in fake_plan.tasks], "结果仍按规划顺序交给合并")
order_audit = [x for x in state2.evidence_task_audit if x.get("event") == "evidence_execution_order"]
check(bool(order_audit) and order_audit[-1]["deterministic"] == ["t_db", "t_conn", "t_explain"], "执行顺序落审计")

print()
if fails:
    print(f"DETERMINISTIC EVIDENCE: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"DETERMINISTIC EVIDENCE: PASS（{checks} 项）")
