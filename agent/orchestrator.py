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
        outcome.binding_ids = bind_evidence(
            st, evidence_task_ids=accepted_task_ids,
            raw_refs=accepted_raw_refs,
            base_revision=plan.explanation_revision)
        outcome.accepted_report_ids.extend(pending_report_ids)
    return outcome


def _mark_unavailable(st: EpisodeState, plan: ToolPlan,
                      needs: list[EvidenceNeed],
                      results: list[EvidenceTaskResult]) -> None:
    explanation = st.explanation_graph
    if explanation is None:
        return
    by_id = {need.need_id: need for need in needs}
    unavailable = dict(plan.unavailable_needs)
    for result in results:
        if result.error and not result.reports:
            for need_id in result.need_ids:
                unavailable.setdefault(need_id, result.error)
        for report in result.reports:
            if report.collection_status != EvidenceStatus.OBSERVED.value:
                unavailable.setdefault(
                    report.need_id,
                    f"collection status {report.collection_status}: " +
                    "; ".join(report.limitations))
    for need_id, reason in unavailable.items():
        need = by_id.get(need_id)
        if (need is not None and
                need.target_kind == EvidenceTargetKind.P0.value):
            for cause_id in need.target_ids:
                if cause_id in explanation.p0_obligations:
                    explanation.resolve_p0(
                        cause_id, ObligationStatus.UNAVAILABLE,
                        reason=f"required evidence unavailable: {reason}")
        st.evidence_task_audit.append({
            "event": "evidence_need_unavailable", "need_id": need_id,
            "reason": reason, "at": time.time(),
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
        for need_id in task.need_ids:
            need = need_map.get(need_id)
            if need is None:
                continue
            reports = reports_by_need.get(need_id, [])
            statuses = [report.collection_status for report in reports]
            if EvidenceStatus.OBSERVED.value in statuses:
                collection_status = EvidenceStatus.OBSERVED.value
            elif EvidenceStatus.ERROR.value in statuses or result.error:
                collection_status = EvidenceStatus.ERROR.value
            else:
                collection_status = EvidenceStatus.UNKNOWN.value
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
            accepted = not any(
                _report_id(report) in late_reports for report in reports)
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
                "accepted_for_causal_update": accepted,
                "changed_statuses": changed if accepted else 0,
                "pruned_paths": pruned if accepted else 0,
                "required_fulfilled": required_fulfilled and accepted,
                "entropy_gain": round(entropy_gain if accepted else 0.0, 6),
                "posterior_change": round(
                    changed / max(total_statuses, 1) if accepted else 0.0, 6),
                "changed_next_decision": bool(
                    accepted and changed and not target_still_frontier),
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
    plan = plan_evidence_tasks(
        explanation, needs, tb, target_context=context,
        incident_window=st.incident_window, config=planning_config)
    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    by_id = {need.need_id: need for need in needs}

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

    task_results = list(await asyncio.gather(*[
        run_task(task) for task in plan.tasks]))
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
