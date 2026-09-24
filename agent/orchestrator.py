"""编排器 —— 分发假设、汇总裁决、维护共享便签。

三件事：
  1. 分批 + 早停剪枝：先跑高先验的，若已收敛就不跑剩下的。
     成本从 K 倍压到 1.5~2 倍，这在按量计费下不是小事。
  2. 共享便签：子 agent 之间看不见彼此，靠 append-only 的便签补偿。
     调查 A 时顺手看到的现象，可能正是排除 B 的决定性证据。
  3. 汇总：把结构化裁决合进台账，检测冲突。
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field

from agent.episode_state import EpisodeState, EvidenceStatus, Verdict
from agent.explanation import (EvidenceNeed, EvidenceTargetKind,
                               ObligationStatus, stable_id)
from agent.investigator import (EvidenceTaskResult, HypothesisVerdict,
                                investigate_many, investigate_task)
from agent.tool_planner import (ToolPlan, ToolPlanningConfig,
                                infer_target_context, plan_evidence_tasks)
from agent.toolbox import Toolbox
from agent.episode_state import EvidenceBudgetExhausted
from agent.explanation import EvidenceReport
from agent.investigator import task_environment_tools
from agent.permissions import Role

# 编排器进程内确定性执行的无参工具：子 agent 对它们做的事只有"调一次、把结构化观测
# 原样抄进 report_evidence"，而判定在 predicate 层。每个任务省一整个 SDK 会话
# （实测 50-140s、约 $0.1-0.3），且没有"猜枚举值"之类的合同风险。
ARGLESS_TOOLS = frozenset({"get_connection_stats", "get_vacuum_horizon",
                           "get_database_stats", "get_blocking_chain"})
# 参数完全由目标上下文决定的工具（2026-09-24）：取证子 agent 调它们时，sql / table 必须等于
# 告警的热查询 / 目标表（toolbox._enter 的对照），报告内容也不参与任何判定 —— 模型没有实际
# 选择，却要花 50-90 秒、$0.08-0.12 起一个 SDK 会话（lock_contention 一局 12 个子 agent 任务里
# 11 个是这类，占整局费用 84%）。由编排器按目标上下文直接执行。simulate_index 要设计索引
# 定义，仍交给子 agent。
PINNED_ARG_TOOLS = frozenset({"explain_query", "get_indexes", "get_table_stats",
                              "get_physical_bloat", "get_top_queries"})
DETERMINISTIC_TOOLS = ARGLESS_TOOLS | PINNED_ARG_TOOLS
# 同一轮确定性任务的执行顺序由工具性质给出（系统决定，不由模型规划 DAG）：先跑读累计
# 计数器做窗口差分的工具，最后跑会真执行查询、给这些计数器记账的工具。自家记账已在观测器
# 这个最底层读取点扣掉（CLAUDE.md 规则 4、6）；顺序是第二道 —— 扣除量来自计划 JSON 的
# 估计，窗口能不跨过它就不跨。get_table_stats 自己的值域扫描在读完计数器之后才做、按事务级
# 视图精确扣除，所以它算读者。
WINDOW_READER_TOOLS = frozenset({"get_database_stats", "get_table_stats", "get_top_queries"})
COUNTER_PERTURBING_TOOLS = frozenset({"explain_query"})


def deterministic_rank(tool: str) -> int:
    if tool in WINDOW_READER_TOOLS:
        return 0
    if tool in COUNTER_PERTURBING_TOOLS:
        return 2
    return 1


# explain_query 的 %(uid)s 取负载自己的探针 uid（注入器给出，与负载打的是同一段行）；
# 取不到时用工具的默认值 —— 原来模型随手填 1 / 1001 / 4242。
DEFAULT_PROBE_UID = 4242


def deterministic_args(tool: str, target_context: dict) -> dict:
    """确定性执行时的参数，全部取自目标上下文。取不到必需目标就抛 ValueError（与子 agent
    会被 toolbox._enter 以"目标未知"拒绝是同一个结果）。"""
    context = target_context or {}
    if tool == "explain_query":
        if not context.get("hot_query"):
            raise ValueError("目标上下文里没有热查询")
        uid = context.get("probe_uid")
        return {"sql": context["hot_query"],
                "params": {"uid": int(uid) if uid is not None else DEFAULT_PROBE_UID}}
    if tool in {"get_indexes", "get_table_stats", "get_physical_bloat"}:
        if not context.get("table"):
            raise ValueError("目标上下文里没有目标表")
        return {"table": context["table"]}
    if tool == "get_top_queries":
        return {"n": 5}
    return {}


def _run_task_deterministic(st: EpisodeState, tb: Toolbox, task) -> EvidenceTaskResult:
    tool = task.selected_tools[0]
    result = EvidenceTaskResult(
        need_id=task.need_ids[0] if task.need_ids else "", task_id=task.task_id,
        need_ids=list(task.need_ids), explanation_id=task.explanation_id,
        explanation_revision=task.explanation_revision, executor="deterministic")
    scoped = tb.scoped(role=Role.INVESTIGATOR, task_context=task,
                       environment_tools=task_environment_tools(task))
    before = len(st.scratchpad)
    started = time.monotonic()
    try:
        getattr(scoped, tool)(**deterministic_args(
            tool, getattr(task, "target_context", {}) or {}))
    except EvidenceBudgetExhausted:
        result.budget_exhausted = True
        return result
    except Exception as exc:                       # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
        result.duration_s = time.monotonic() - started
        return result
    entries = [e for e in st.scratchpad[before:] if e.get("raw_ref")]
    refs = list(dict.fromkeys(str(e["raw_ref"]) for e in entries))
    statuses = {str(e.get("status") or "") for e in entries}
    if EvidenceStatus.OBSERVED.value in statuses:
        status = EvidenceStatus.OBSERVED.value
    elif EvidenceStatus.UNKNOWN.value in statuses:
        status = EvidenceStatus.UNKNOWN.value
    else:
        status = EvidenceStatus.ERROR.value
    observations = []
    for e in entries:
        v = e.get("structured_value")
        observations.append(v if isinstance(v, dict) else {"value": v, "evidence_type": e.get("evidence_type")})
    limitations = ["deterministic in-process execution: observations copied verbatim, no narrative"]
    if status != EvidenceStatus.OBSERVED.value:
        limitations.append("; ".join(str(e.get("observation") or e.get("summary") or "")[:120] for e in entries) or f"collection status {status}")
    for need_id in task.need_ids:
        try:
            result.reports.append(EvidenceReport(
                need_id=need_id, tool=tool, raw_refs=list(refs),
                observations=list(observations), collection_status=status,
                limitations=list(limitations)))
        except (TypeError, ValueError) as exc:
            result.error = f"invalid EvidenceReport: {exc}"
    result.report = result.reports[0] if len(result.reports) == 1 else None
    result.tools_used = [tool]
    result.duration_s = time.monotonic() - started
    return result

# 每个假设一句话说明它该看什么。W6 起改由故障因果图给出
# （必需证据类型直接挂在图的边上），现在先手写。
BRIEFS = {
    "missing_index":
        "查询是否因缺少可用索引而全表扫。看 EXPLAIN 的扫描类型与 "
        "Rows Removed by Filter，并核对表上现有索引能否覆盖该谓词。",
    "stale_statistics":
        "优化器是否因统计信息过期而选了坏计划。**判别特征是 EXPLAIN 里"
        "估计行数与实际行数的偏差倍数**，不是 last_analyze 时间戳 —— "
        "刚灌过数据时时间戳可能看着很新，但统计早已失真。偏差超过 10 倍"
        "就应当确认该假设。",
    "lock_contention":
        "是否存在锁等待。看 pg_locks 的阻塞链与会话的 wait_event。"
        "阻塞链非空或出现 Lock:* 等待事件即可确认，不需要看执行计划。",
    "table_bloat":
        "表是否存在物理膨胀。必须看 physical_bloat_ratio；死元组占比只能"
        "说明清理压力，不能单独确认或反证物理膨胀。",
    "connection_exhaustion":
        "连接数是否逼近上限、是否有大量 idle in transaction。",
    "autovacuum_starvation":
        "autovacuum 是否关闭，或死元组积压是否已超过触发线两倍且没有 worker。",
    "disk_pressure":
        "PostgreSQL 数据目录所在文件系统使用率是否达到 85%。",
    "stale_replication_slot":
        "复制槽是否非活动，且 xmin horizon 过老或 WAL 滞留达到 1GB。",
    "orphaned_prepared_transaction":
        "预备事务的 XID 年龄是否超过一百万，或挂起是否达到一小时。",
}


@dataclass
class OrchestrationResult:
    verdicts: list[HypothesisVerdict] = field(default_factory=list)
    batches: int = 0
    skipped: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    turns: int = 0


@dataclass
class EvidenceMergeResult:
    binding_ids: list[str] = field(default_factory=list)
    accepted_report_ids: list[str] = field(default_factory=list)
    duplicate_report_ids: list[str] = field(default_factory=list)
    late_report_ids: list[str] = field(default_factory=list)
    rejected_reports: list[str] = field(default_factory=list)


@dataclass
class EvidenceOrchestrationResult:
    plan: ToolPlan
    task_results: list[EvidenceTaskResult] = field(default_factory=list)
    merge: EvidenceMergeResult = field(default_factory=EvidenceMergeResult)
    cost_usd: float = 0.0
    turns: int = 0


def _report_id(report) -> str:
    return stable_id("evidence_report", {
        "need_id": report.need_id,
        "tool": report.tool,
        "raw_refs": report.raw_refs,
        "collection_status": report.collection_status,
    })


def _need_facts_from_scratchpad(st: EpisodeState, task, needs_by_id: dict
                                ) -> dict[str, tuple[str | None, list[dict]]]:
    """每个 need 的采集状态与条目，从 scratchpad 推导，不读子 agent 自报的 collection_status。

    条目由 toolbox 落盘、status 经 EpisodeState.note 枚举校验；报告只是触发器。原来四个
    读取点（不可得记账、L2/L4 观测、停机判定、KPI）都读模型自报值：模型报 UNKNOWN 就把
    已绑定的证据记成不可得，报 OBSERVED 就把被合并层拒掉的报告记成工具成功
    （2026-09-23 审计）。状态：None = 工具没产出任何条目；某 need 的证据类型没有条目
    但工具有别的条目 = UNKNOWN（如无估计偏差时就没有 row_estimate_deviation）。
    """
    entries = [e for e in st.scratchpad
               if e.get("evidence_task_id") == task.task_id and e.get("raw_ref")]
    out: dict[str, tuple[str | None, list[dict]]] = {}
    for need_id in task.need_ids:
        need = needs_by_id.get(need_id)
        etype = str(getattr(need, "evidence_type", "") or "")
        mine = [e for e in entries if not etype or e.get("evidence_type") == etype]
        statuses = {str(e.get("status") or "") for e in mine}
        if not entries:
            out[need_id] = (None, [])
        elif EvidenceStatus.OBSERVED.value in statuses:
            out[need_id] = (EvidenceStatus.OBSERVED.value, mine)
        elif EvidenceStatus.UNKNOWN.value in statuses:
            out[need_id] = (EvidenceStatus.UNKNOWN.value, mine)
        elif mine:
            out[need_id] = (EvidenceStatus.ERROR.value, mine)
        else:
            out[need_id] = (EvidenceStatus.UNKNOWN.value, [])
    return out


def merge_evidence_task_results(
        st: EpisodeState, plan: ToolPlan,
        results: list[EvidenceTaskResult]) -> EvidenceMergeResult:
    """Persist reports idempotently and bind only the planned revision."""
    outcome = EvidenceMergeResult()
    explanation = st.explanation_graph
    if explanation is None:
        outcome.rejected_reports.append("current explanation is missing")
        return outcome

    known_tasks = {task.task_id: task for task in plan.tasks}
    accepted_task_ids: set[str] = set()
    accepted_raw_refs: set[str] = set()
    pending_report_ids: list[str] = []
    for result in results:
        task = known_tasks.get(result.task_id)
        if task is None:
            outcome.rejected_reports.append(
                f"unknown evidence task {result.task_id}")
            continue
        assigned = set(task.need_ids)
        collected_refs = {
            str(entry.get("raw_ref") or "") for entry in st.scratchpad
            if entry.get("evidence_task_id") == task.task_id and
            entry.get("raw_ref")
        }
        # 子 agent 引用工具自己的 trace 记录（EXPLAIN 原文）也算"本任务采集过"：
        # 2026-09-23 实测 explain_query 的报告 0 次引用 evidence_raw_refs，全被这里
        # 以 "raw_ref was not collected" 拒掉，stale_statistics 的唯一必需证据因此
        # 永远绑不上。
        collected_refs |= {str(r) for r in (result.tool_raw_refs or []) if r}
        for report in result.reports:
            report_id = _report_id(report)
            if report_id in st.evidence_reports:
                outcome.duplicate_report_ids.append(report_id)
                continue
            if report.need_id not in assigned:
                outcome.rejected_reports.append(
                    f"{report_id}: need is not assigned to task")
                continue
            if report.tool not in task.selected_tools:
                outcome.rejected_reports.append(
                    f"{report_id}: tool is not assigned to task")
                continue
            if (report.collection_status == EvidenceStatus.OBSERVED.value and
                    not report.raw_refs):
                outcome.rejected_reports.append(
                    f"{report_id}: OBSERVED report has no raw_ref")
                continue
            if not set(report.raw_refs).issubset(collected_refs):
                outcome.rejected_reports.append(
                    f"{report_id}: raw_ref was not collected by this task")
                continue
            st.evidence_reports[report_id] = {
                **report.to_dict(), "report_id": report_id,
                "task_id": task.task_id,
                "explanation_id": task.explanation_id,
                "explanation_revision": task.explanation_revision,
                "received_at": time.time(),
            }
            pending_report_ids.append(report_id)
            accepted_task_ids.add(task.task_id)
            accepted_raw_refs.update(report.raw_refs)

    if (explanation.explanation_id != plan.explanation_id or
            explanation.revision != plan.explanation_revision):
        outcome.late_report_ids.extend(pending_report_ids)
        st.evidence_task_audit.append({
            "event": "late_reports_deferred",
            "plan_explanation_id": plan.explanation_id,
            "plan_revision": plan.explanation_revision,
            "current_explanation_id": explanation.explanation_id,
            "current_revision": explanation.revision,
            "report_ids": list(pending_report_ids),
            "at": time.time(),
        })
        return outcome

    if pending_report_ids:
        from agent.explanation_runtime import bind_evidence
        # 按任务绑定，不按子 agent 引用的 ref 绑定：一次工具调用会落多条 scratchpad
        # 条目（explain_plan 之外还有派生的 row_estimate_deviation），子 agent 只引用
        # 其中一条；条目本身是 toolbox 产出、有 digest 校验的观测，报告只是触发器。
        # 原来 raw_refs=accepted_raw_refs 让派生条目永远绑不上（架构评审第 1 条）。
        outcome.binding_ids = bind_evidence(
            st, evidence_task_ids=accepted_task_ids,
            raw_refs=None,
            base_revision=plan.explanation_revision)
        outcome.accepted_report_ids.extend(pending_report_ids)
    # 合并结果落审计：被拒的报告原来只留在返回值里，trace 上看不到，今天这个
    # 问题就是因此隐形的。
    st.evidence_task_audit.append({
        "event": "evidence_merge",
        "plan_revision": plan.explanation_revision,
        "accepted": list(outcome.accepted_report_ids),
        "late": list(outcome.late_report_ids),
        "duplicate": list(outcome.duplicate_report_ids),
        "rejected": list(outcome.rejected_reports)[:50],
        "bindings": len(outcome.binding_ids),
        "at": time.time(),
    })
    return outcome


def _mark_unavailable(st: EpisodeState, plan: ToolPlan,
                      needs: list[EvidenceNeed],
                      results: list[EvidenceTaskResult]) -> None:
    explanation = st.explanation_graph
    if explanation is None:
        return
    by_id = {need.need_id: need for need in needs}
    task_map = {task.task_id: task for task in plan.tasks}
    unavailable: dict[str, dict] = {
        need_id: {"reason": str(reason), "source": "planner"}
        for need_id, reason in plan.unavailable_needs.items()}
    for result in results:
        if getattr(result, "budget_exhausted", False):
            # harness 预算到了不是证据不可得（硬规则 6）；单独记一条审计即可。
            st.evidence_task_audit.append({
                "event": "evidence_task_budget_exhausted",
                "task_id": result.task_id, "need_ids": list(result.need_ids),
                "at": time.time()})
            continue
        task = task_map.get(result.task_id)
        facts = (_need_facts_from_scratchpad(st, task, by_id) if task is not None
                 else {need_id: (None, []) for need_id in result.need_ids})
        reported = {report.need_id: report for report in result.reports}
        for need_id in result.need_ids:
            status, entries = facts.get(need_id, (None, []))
            if status == EvidenceStatus.OBSERVED.value:
                continue
            if status is None:
                row = {"reason": ("tool produced no observation" +
                                  (f": {result.error}" if result.error else "")),
                       "source": "task_error" if result.error else "collection_status"}
            else:
                summary = "; ".join(
                    str(e.get("observation") or e.get("summary") or "")[:120]
                    for e in entries) or f"no {getattr(by_id.get(need_id), 'evidence_type', '')} entry"
                row = {"reason": f"collection status {status}: {summary}",
                       "source": "collection_status"}
            if row["source"] == "task_error":
                row["infra"] = bool(getattr(result, "infra", False))
                row["error_kind"] = str(getattr(result, "error_kind", "") or "")
            report = reported.get(need_id)
            if report is not None:
                # 模型自报的状态与局限只进审计，不进 reason、不进任何判据。
                row["reported_status"] = report.collection_status
                row["reported_limitations"] = list(report.limitations)[:5]
            unavailable.setdefault(need_id, row)
    for need_id, row in unavailable.items():
        need = by_id.get(need_id)
        if (need is not None and
                need.target_kind == EvidenceTargetKind.P0.value):
            for cause_id in need.target_ids:
                if cause_id in explanation.p0_obligations:
                    explanation.resolve_p0(
                        cause_id, ObligationStatus.UNAVAILABLE,
                        reason=f"required evidence unavailable: {row['reason']}")
        st.evidence_task_audit.append({
            "event": "evidence_need_unavailable", "need_id": need_id,
            "at": time.time(), **row,
        })


def _record_tool_learning_observations(
        st: EpisodeState, plan: ToolPlan, needs: list[EvidenceNeed],
        results: list[EvidenceTaskResult], merged: EvidenceMergeResult) -> None:
    """Record deterministic before/after facts for the offline v2 learner."""
    explanation = st.explanation_graph
    if explanation is None:
        return
    need_map = {need.need_id: need for need in needs}
    result_map = {result.task_id: result for result in results}
    current_paths = explanation.path_map()
    current_frontier = __import__(
        "knowledge.causal_graph.graph", fromlist=["path_frontier"]
    ).path_frontier(explanation)
    current_targets = {(item["target_kind"], item["target_id"])
                       for item in current_frontier}
    late_reports = set(merged.late_report_ids)

    for task in plan.tasks:
        result = result_map.get(task.task_id)
        if result is None:
            continue
        if getattr(result, "budget_exhausted", False):
            continue      # 预算耗尽不是"这个工具没用"，不能喂给 L2/L4
        before_paths = {item["path_id"]: item
                        for item in task.local_subgraph.get("paths", [])}
        before_viable = sum(item.get("status") != "REFUTED"
                            for item in before_paths.values())
        after_viable = sum(
            current_paths[path_id].status != "REFUTED"
            for path_id in before_paths if path_id in current_paths)
        pruned = max(0, before_viable - after_viable)
        entropy_gain = max(0.0, math.log2(max(before_viable, 1)) -
                           math.log2(max(after_viable, 1)))
        changed = 0
        total_statuses = 0
        for item in before_paths.values():
            for node_id, before in item.get("node_status", {}).items():
                total_statuses += 1
                changed += int(explanation.node_status.get(
                    node_id, "UNTESTED") != before)
            for edge_id, before in item.get("edge_status", {}).items():
                total_statuses += 1
                changed += int(explanation.edge_status.get(
                    edge_id, "UNTESTED") != before)
        reports_by_need = {
            need_id: [report for report in result.reports
                      if report.need_id == need_id]
            for need_id in task.need_ids
        }
        facts = _need_facts_from_scratchpad(st, task, need_map)
        accepted_ids = set(merged.accepted_report_ids)
        for need_id in task.need_ids:
            need = need_map.get(need_id)
            if need is None:
                continue
            reports = reports_by_need.get(need_id, [])
            # 采集状态取系统事实（scratchpad 条目），模型自报值另存 reported_status
            status, _entries = facts.get(need_id, (None, []))
            if status is None:
                collection_status = (EvidenceStatus.ERROR.value if result.error
                                     else EvidenceStatus.UNKNOWN.value)
            else:
                collection_status = status
            reported_status = next(
                (report.collection_status for report in reports), "")
            bindings = [binding for binding in
                        explanation.evidence_bindings.values()
                        if binding.evidence_type == need.evidence_type and
                        binding.predicate_id == need.predicate_id and
                        (set(binding.target_node_ids +
                             binding.target_edge_ids) & set(need.target_ids))]
            required_fulfilled = bool(
                need.required and any(binding.predicate_result == "SUPPORTS"
                                      and binding.is_trusted()
                                      for binding in bindings))
            target_still_frontier = any(
                (need.target_kind, target_id) in current_targets
                for target_id in need.target_ids)
            # "接受"= 至少一份报告真的被合并层接受（被拒/迟到的都不算）；原来只排除迟到
            # 的，被 raw_ref 校验拒掉的报告仍记成 accepted + OBSERVED 喂给 L4。
            accepted = bool(reports) and any(
                _report_id(report) in accepted_ids for report in reports
            ) and not any(_report_id(report) in late_reports for report in reports)
            observation_id = stable_id("tool_observation", {
                "episode_id": st.episode_id,
                "task_id": task.task_id,
                "need_id": need_id,
                "tool": task.selected_tools[0],
            })
            learning_context = dict(
                task.learning_context.get(need_id, {}))
            duplicate_calls = sum(
                1 for prior in st.evidence_task_audit
                if prior.get("event") == "tool_learning_observation" and
                prior.get("tool") == task.selected_tools[0] and
                (prior.get("learning_context") or {}).get(
                    "frontier_signature") == learning_context.get(
                        "frontier_signature") and
                (prior.get("learning_context") or {}).get(
                    "evidence_need_signature") == learning_context.get(
                        "evidence_need_signature"))
            st.evidence_task_audit.append({
                "event": "tool_learning_observation",
                "observation_id": observation_id,
                "need_id": need_id,
                "task_id": task.task_id,
                "tool": task.selected_tools[0],
                "learning_context": learning_context,
                "collection_status": collection_status,
                "reported_status": reported_status,
                "accepted_for_causal_update": accepted,
                "changed_statuses": changed if accepted else 0,
                "pruned_paths": pruned if accepted else 0,
                "required_fulfilled": required_fulfilled and accepted,
                "entropy_gain": round(entropy_gain if accepted else 0.0, 6),
                "posterior_change": round(
                    changed / max(total_statuses, 1) if accepted else 0.0, 6),
                "changed_next_decision": bool(
                    accepted and changed and not target_still_frontier),
                "executor": getattr(result, "executor", "subagent"),
                "latency_s": round(float(result.duration_s), 6),
                "cost": round(float(result.cost_usd) /
                              max(len(task.need_ids), 1), 6),
                "covered_need_count": len(task.need_ids),
                "duplicate_calls": duplicate_calls,
                "at": time.time(),
            })


async def run_evidence_investigation(
        st: EpisodeState, tb: Toolbox, needs: list[EvidenceNeed],
        hot_query: str, *, max_concurrency: int = 2,
        planning_config: ToolPlanningConfig = ToolPlanningConfig(),
        target_context: dict | None = None, verbose: bool = True,
        model: str | None = None) -> EvidenceOrchestrationResult:
    explanation = st.explanation_graph
    if explanation is None:
        raise ValueError("v2 evidence investigation requires an explanation graph")
    context = target_context or infer_target_context(hot_query)
    remaining = max(0, int(st.budget.get("max_steps", 0)) - int(st.budget.get("steps", 0)))
    plan = plan_evidence_tasks(
        explanation, needs, tb, target_context=context,
        incident_window=st.incident_window, config=planning_config,
        remaining_calls=remaining)
    if plan.deferred_need_ids:
        st.evidence_task_audit.append({
            "event": "evidence_plan_truncated_by_budget",
            "remaining_calls": remaining, "planned_tasks": len(plan.tasks),
            "deferred_need_ids": list(plan.deferred_need_ids), "at": time.time()})
    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    by_id = {need.need_id: need for need in needs}

    def deterministic_tool(task) -> str:
        tool = task.selected_tools[0] if len(task.selected_tools) == 1 else ""
        if ((planning_config.deterministic_argless and tool in ARGLESS_TOOLS) or
                (planning_config.deterministic_pinned and tool in PINNED_ARG_TOOLS)):
            return tool
        return ""

    async def run_task(task):
        async with semaphore:
            started = time.monotonic()
            result = await investigate_task(
                task, [by_id[need_id] for need_id in task.need_ids
                       if need_id in by_id],
                tb, scratchpad_view(st), hot_query, verbose=verbose,
                **({"model": model} if model else {}))
            result.duration_s = time.monotonic() - started
            return result

    # 确定性任务先于子 agent 全部跑完，按 deterministic_rank 排序（同级保持规划顺序）；
    # 结果仍按规划顺序交给合并，合并不受执行顺序影响。
    order = sorted((i for i, task in enumerate(plan.tasks) if deterministic_tool(task)),
                   key=lambda i: deterministic_rank(deterministic_tool(plan.tasks[i])))
    slots: list[EvidenceTaskResult | None] = [None] * len(plan.tasks)
    for i in order:
        slots[i] = _run_task_deterministic(st, tb, plan.tasks[i])
    delegated = [i for i in range(len(plan.tasks)) if slots[i] is None]
    for i, result in zip(delegated, await asyncio.gather(*[
            run_task(plan.tasks[i]) for i in delegated])):
        slots[i] = result
    task_results = [result for result in slots if result is not None]
    st.evidence_task_audit.append({
        "event": "evidence_execution_order",
        "deterministic": [plan.tasks[i].task_id for i in order],
        "delegated": [plan.tasks[i].task_id for i in delegated], "at": time.time()})
    merged = merge_evidence_task_results(st, plan, task_results)
    _mark_unavailable(st, plan, needs, task_results)
    _record_tool_learning_observations(
        st, plan, needs, task_results, merged)
    st.save()
    return EvidenceOrchestrationResult(
        plan=plan, task_results=task_results, merge=merged,
        cost_usd=sum(item.cost_usd for item in task_results),
        turns=sum(item.turns for item in task_results))


def run_evidence_investigation_sync(*args, **kwargs
                                    ) -> EvidenceOrchestrationResult:
    return asyncio.run(run_evidence_investigation(*args, **kwargs))


def revalidate_late_task_evidence(st: EpisodeState, task_id: str) -> list[str]:
    """Retag still-relevant late evidence, then run deterministic predicates."""
    explanation = st.explanation_graph
    if explanation is None:
        return []
    from knowledge.causal_graph import graph as causal_graph
    current_needs = {need.need_id: need for need in
                     causal_graph.evidence_needs(explanation)}
    accepted_refs: set[str] = set()
    for entry in st.scratchpad:
        if entry.get("evidence_task_id") != task_id:
            continue
        relevant = set(entry.get("evidence_need_ids") or []) & set(current_needs)
        if not relevant:
            continue
        entry["explanation_id"] = explanation.explanation_id
        entry["explanation_revision"] = explanation.revision
        entry["evidence_need_ids"] = sorted(relevant)
        if entry.get("raw_ref"):
            accepted_refs.add(entry["raw_ref"])
    if not accepted_refs:
        return []
    from agent.explanation_runtime import bind_evidence
    return bind_evidence(
        st, evidence_task_ids={task_id}, raw_refs=accepted_refs,
        base_revision=explanation.revision)


def scratchpad_view(st: EpisodeState, limit: int = 14) -> str:
    """给子 agent 看的便签快照。

    只给结构化条目的摘要，不给原文 —— 子 agent 的上下文预算也要省。
    """
    if not st.scratchpad:
        return ""
    lines = []
    for e in st.scratchpad[-limit:]:
        bo = f" (关系到 {','.join(e['bears_on'])})" if e.get("bears_on") else ""
        status = e.get("status", EvidenceStatus.OBSERVED.value)
        mark = "" if status == EvidenceStatus.OBSERVED.value else f"/{status}"
        lines.append(f"  - [{e['evidence_type']}{mark}]{bo} "
                     f"{e['observation'][:120]}")
    return "\n".join(lines)


def _converged(st: EpisodeState, candidates: list[str]) -> bool:
    """达到 ESC 的分层排除下限后才允许跳过剩余低风险候选。

    P0 必须全部得到裁决；普通竞争项维持 D2 的 50% 排除率。旧实现只看
    已跑完的子集，前两个假设一收敛就能把尚未取证的 P0 全部跳过。
    """
    from knowledge.causal_graph import graph as G

    confirmed = [c for c in candidates
                 if st.ledger.get(c) and
                 st.ledger[c].verdict == Verdict.CONFIRMED.value]
    if len(confirmed) != 1:
        return False
    rc = confirmed[0]
    downstream = G.downstream_of(rc)
    competitors = [c for c in candidates if c != rc and c not in downstream]
    p0 = [c for c in competitors if G.severity_of(c) == "P0"]
    refuted = {c for c in competitors if st.ledger.get(c) and
               st.ledger[c].verdict in
               (Verdict.REFUTED.value, Verdict.REFUTED_BY_REMEDIATION.value)}
    if any(c not in refuted for c in p0):
        return False
    ordinary = [c for c in competitors if c not in p0]
    ratio = (len([c for c in ordinary if c in refuted]) / len(ordinary)
             if ordinary else 1.0)
    return ratio >= 0.5


def merge(st: EpisodeState, verdicts: list[HypothesisVerdict]) -> list[str]:
    """把子 agent 的结构化裁决合进台账与便签，返回冲突描述。"""
    conflicts: list[str] = []
    for v in verdicts:
        if v.error:
            # 失败也要进台账：留成 INCONCLUSIVE 而不是 UNTESTED，
            # 否则主 agent 会以为这条假设根本没查过
            st.set_verdict(v.hypothesis, Verdict.INCONCLUSIVE,
                           note=f"子 agent 调查失败: {v.error[:120]}")
            st.note(f"investigator:{v.hypothesis}", "subagent_error",
                    f"调查失败: {v.error[:120]}", bears_on=[v.hypothesis])
            conflicts.append(f"{v.hypothesis}: 子 agent 未给出裁决（{v.error[:60]}）")
            continue

        cur = st.ledger.get(v.hypothesis)
        if cur and cur.verdict == Verdict.REFUTED_BY_REMEDIATION.value:
            # 修复反证不能被只读证据翻案，否则重试循环又回来了
            conflicts.append(
                f"{v.hypothesis}: 子 agent 给出 {v.verdict}，但它已被修复反证，忽略")
            continue

        st.set_verdict(v.hypothesis, Verdict(v.verdict),
                       note=v.reasoning[:200])
        st.note(f"investigator:{v.hypothesis}", "subagent_verdict",
                f"{v.verdict} (置信 {v.confidence:.2f}): {v.reasoning[:110]}",
                bears_on=[v.hypothesis])

        # 顺带发现进便签，并标注它可能关系到哪些其他假设 ——
        # 这是弥补 subagent 隔离的关键：让线索能跨假设流动
        for inc in v.incidental:
            others = [h for h in BRIEFS if h != v.hypothesis]
            st.note(f"investigator:{v.hypothesis}", "incidental_finding",
                    inc[:160], bears_on=others)

    confirmed = [k for k, e in st.ledger.items()
                 if e.verdict == Verdict.CONFIRMED.value]
    if len(confirmed) > 1:
        conflicts.append(f"多个假设同时被确认: {confirmed}（可能是级联故障）")
    return conflicts


async def run_investigation(st: EpisodeState, tb: Toolbox, candidates: list[str],
                            hot_query: str, batch_size: int = 2,
                            verbose: bool = True,
                            case_prior: str = "") -> OrchestrationResult:
    st.ensure_hypotheses(candidates)
    res = OrchestrationResult()
    pending = list(candidates)

    while pending:
        batch, pending = pending[:batch_size], pending[batch_size:]
        res.batches += 1
        if verbose:
            print(f"      批次 {res.batches}: {batch}")

        items = [(h, BRIEFS.get(h, "调查该假设是否成立")) for h in batch]
        view = scratchpad_view(st)
        if case_prior:
            view = case_prior + "\n" + view
        verdicts = await investigate_many(
            items, tb, view, hot_query, verbose=verbose)
        res.verdicts.extend(verdicts)
        res.cost_usd += sum(v.cost_usd for v in verdicts)
        res.turns += sum(v.turns for v in verdicts)
        res.conflicts.extend(merge(st, verdicts))
        for v in verdicts:
            for b in v.blocked:
                res.blocked.append(f"{v.hypothesis}: {b}")
                st.note(f"investigator:{v.hypothesis}", "blocked_call",
                        b[:150], bears_on=[v.hypothesis])

        for v in verdicts:
            if verbose:
                print(f"      -> {v.hypothesis}: {v.verdict} "
                      f"(置信 {v.confidence:.2f}, {v.turns} turns, "
                      f"${v.cost_usd:.4f})")

        if pending and _converged(st, candidates):
            # 已经收敛，剩下的低先验假设不必再跑
            res.skipped = list(pending)
            if verbose:
                print(f"      早停剪枝，跳过: {pending}")
            break

    return res


def run_investigation_sync(*a, **kw) -> OrchestrationResult:
    return asyncio.run(run_investigation(*a, **kw))
