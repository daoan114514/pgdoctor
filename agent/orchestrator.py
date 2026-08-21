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
from dataclasses import dataclass, field

from agent.episode_state import EpisodeState, Verdict
from agent.investigator import HypothesisVerdict, investigate_many
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
        "表是否严重膨胀。看死元组占比与表实际大小。",
    "connection_exhaustion":
        "连接数是否逼近上限、是否有大量 idle in transaction。",
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


def scratchpad_view(st: EpisodeState, limit: int = 14) -> str:
    """给子 agent 看的便签快照。

    只给结构化条目的摘要，不给原文 —— 子 agent 的上下文预算也要省。
    """
    if not st.scratchpad:
        return ""
    lines = []
    for e in st.scratchpad[-limit:]:
        bo = f" (关系到 {','.join(e['bears_on'])})" if e.get("bears_on") else ""
        lines.append(f"  - [{e['evidence_type']}]{bo} {e['observation'][:120]}")
    return "\n".join(lines)


def _converged(st: EpisodeState, candidates: list[str]) -> bool:
    """恰好一个确认、其余都已排除 —— 没必要再跑剩下的假设。"""
    confirmed = [c for c in candidates
                 if st.ledger.get(c) and
                 st.ledger[c].verdict == Verdict.CONFIRMED.value]
    untested = [c for c in candidates
                if st.ledger.get(c) and
                st.ledger[c].verdict == Verdict.UNTESTED.value]
    return len(confirmed) == 1 and not untested


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

        if pending and _converged(st, [c for c in candidates
                                       if c not in pending]):
            # 已经收敛，剩下的低先验假设不必再跑
            res.skipped = list(pending)
            if verbose:
                print(f"      早停剪枝，跳过: {pending}")
            break

    return res


def run_investigation_sync(*a, **kw) -> OrchestrationResult:
    return asyncio.run(run_investigation(*a, **kw))
