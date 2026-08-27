"""证据充分性检查（Evidence Sufficiency Check）。

针对的是 agent 的**静默失败**：查了两个视图，编出一个听起来极其合理、
格式工整、语气自信的根因，然后基于这个错根因去动生产库。没有报错、
没有异常、没有任何信号告诉你它错了。

DBA-Bench 的数字侧面印证了这件事的存在：Diagnosis 32.7% 是三率里最高的，
也就是说 agent "说"对根因的次数，明显多于它真正安全解决问题的次数。

核心设计原则：**绝不让 LLM 给自己打分**。

问模型"你觉得证据够吗"是必错的——它几乎恒答"够了"，而且越是幻觉出来的
根因，叙述往往越流畅自信。所以判据全部来自 episode 的**执行轨迹**
（实际跑了哪些查询、拿到了什么返回），这些是沙箱记录下来的客观事实，
agent 伪造不了。

它检查的是**过程可靠性**而不是结论正确性：生产环境里你无法事前知道结论
对不对，只能保证过程够扎实。这也是它区别于"用 LLM 判断答案对不对"的
根本之处。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from agent.episode_state import EpisodeState, Verdict
from knowledge.causal_graph import graph as G


class ESCVerdict(str, Enum):
    SUFFICIENT = "SUFFICIENT"       # 放行进入 PLAN
    INSUFFICIENT = "INSUFFICIENT"   # 退回取证，并给出定向指令
    AMBIGUOUS = "AMBIGUOUS"         # 多个假设证据相当，升级人工
    EXHAUSTED = "EXHAUSTED"         # 反复取证仍不足，升级人工


@dataclass
class DimResult:
    name: str
    passed: bool
    mandatory: bool
    detail: str = ""
    missing: list[str] = field(default_factory=list)


@dataclass
class ESCReport:
    verdict: str
    root_cause: str | None
    dims: list[DimResult] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)
    score: float = 0.0

    def summary(self) -> str:
        marks = " ".join(
            f"{d.name}{'✓' if d.passed else '✗'}" for d in self.dims)
        return f"{self.verdict}  [{marks}]"


# 证据取值是否真的支持该根因。
# 只查"跑过没跑过"还不够 —— 跑了但结果指向反面同样不能算数。
def _supports(evidence_type: str, observation: str, root_cause: str) -> bool:
    o = observation.lower()
    if evidence_type == "explain_seq_scan":
        m = re.search(r"rows removed by filter=([\d,]+)", o)
        n = int(m.group(1).replace(",", "")) if m else 0
        return "seq scan" in o and n > 10_000
    if evidence_type == "index_existence":
        # 观测里列出的是现有索引；对 missing_index 而言，
        # 关键是没有覆盖该谓词的索引 —— 这里做保守判断：拿到了清单即算取证，
        # 具体覆盖性由 explain 的 Seq Scan 佐证（两条 required 互为补充）
        return "索引" in observation or "index" in o
    if evidence_type == "stats_freshness":
        fresh = bool(re.search(r"last_analyze=\d{4}-\d{2}-\d{2}", o))
        if root_cause == "stale_statistics":
            return not fresh            # 确认统计过期，需要的是"不新鲜"
        return fresh                    # 用于排除时，需要的是"新鲜"
    if evidence_type == "lock_blocking_chain":
        if root_cause == "lock_contention":
            return "0 条" not in observation and "无锁等待" not in observation
        return True
    if evidence_type == "counterfactual_index":
        # 原查询本来就很快时，加索引"会被采用"说明不了任何问题
        if "成本仅" in observation or "不足以支持" in observation:
            return False
        return ("会采用=true" in o or "would_be_used': true" in o
                or "采用=True" in observation)
    if evidence_type == "idle_in_transaction":
        # 这条原来走默认分支恒为真 —— 只要调过工具就算取证，不看取值。
        # 在"长事务堆积伪装成连接打满"这类场景里恰恰是致命的：真·连接
        # 打满时 idle in transaction 接近 0，却照样能"支持"长事务根因。
        m = re.search(r"idle in transaction=(\d+)", observation)
        n = int(m.group(1)) if m else 0
        if root_cause == "long_idle_transaction":
            return n >= 3            # 得真有一批挂着的事务
        return True
    if evidence_type == "dead_tuple_ratio":
        return True
    if evidence_type == "connection_count":
        return True
    return True


def _collected(st: EpisodeState) -> dict[str, list[dict]]:
    """从执行轨迹里归集实际拿到的证据。读便签，不读 agent 的说法。"""
    out: dict[str, list[dict]] = {}
    for e in st.scratchpad:
        out.setdefault(e["evidence_type"], []).append(e)
    return out


def check(st: EpisodeState, candidates: list[str] | None = None,
          min_refute_ratio: float = 0.5) -> ESCReport:
    rc = st.claimed_fault_class
    if not rc:
        return ESCReport(ESCVerdict.INSUFFICIENT.value, None,
                         directives=["尚未声明根因"])

    got = _collected(st)
    cands = candidates or [c["root_cause"] for c in
                           G.candidate_causes(st.symptoms or
                                              ["latency_p99_up", "cpu_saturated"])]
    dims: list[DimResult] = []
    directives: list[str] = []

    # ── D1 直接证据（必需项）────────────────────────────────
    required = G.required_evidence(rc)
    missing, unsupported = [], []
    for ev in required:
        entries = got.get(ev, [])
        if not entries:
            missing.append(ev)
        elif not any(_supports(ev, e["observation"], rc) for e in entries):
            unsupported.append(ev)
    d1_ok = not missing and not unsupported
    detail = (f"必需证据 {len(required)} 项，缺 {len(missing)}，取值不支持 "
              f"{len(unsupported)}")
    dims.append(DimResult("D1", d1_ok, True, detail, missing + unsupported))
    for ev in missing:
        q = G.load().nodes.get(ev, {}).get("obtained_by", "相应工具")
        directives.append(f"缺少必需证据 {ev}，请调用 {q} 取证")
    for ev in unsupported:
        directives.append(f"证据 {ev} 的取值并不支持 {rc}，请复核或改换假设")

    # ── D2 鉴别诊断（必需项）────────────────────────────────
    competitors = [c for c in cands if c != rc]
    excluded = [c for c in competitors
                if st.ledger.get(c) and st.ledger[c].verdict in
                (Verdict.REFUTED.value, Verdict.REFUTED_BY_REMEDIATION.value)]
    ratio = (len(excluded) / len(competitors)) if competitors else 1.0
    d2_ok = ratio >= min_refute_ratio
    dims.append(DimResult(
        "D2", d2_ok, True,
        f"竞争假设 {len(competitors)} 个，已排除 {len(excluded)} 个 "
        f"({ratio:.0%})",
        [c for c in competitors if c not in excluded]))
    if not d2_ok:
        for c in competitors:
            if c in excluded:
                continue
            disc = G.best_discriminator([rc, c])
            hint = (f"（可用 {disc['obtained_by']} 取 {disc['evidence']}）"
                    if disc else "")
            directives.append(f"竞争假设 {c} 尚未排除{hint}")

    # ── D3 因果一致：有没有解释不了的孤儿症状 ─────────────────
    known = set(G.symptoms_of(rc))
    # 映射复用 graph.map_symptoms。这里原本内联了一份更旧的副本，
    # 与 loop.py 那份会各自漂移，而漂移了没有任何东西会报错。
    # fallback=False：判孤儿症状时绝不能凭空补一个没观测到的症状。
    mapped = set(G.map_symptoms(st.symptoms or [], fallback=False))
    orphans = sorted(mapped - known) if mapped else []
    dims.append(DimResult("D3", not orphans, False,
                          f"观测症状 {sorted(mapped) or '—'}，该根因已知可解释 "
                          f"{sorted(known) or '—'}", orphans))
    if orphans:
        directives.append(f"症状 {orphans} 无法由 {rc} 解释，可能存在第二个故障")

    # ── D4 时间线一致性 ────────────────────────────────────
    has_timeline = any(e["evidence_type"] in ("slow_query_ranking",
                                              "stats_freshness")
                       for e in st.scratchpad)
    dims.append(DimResult("D4", has_timeline, False,
                          "有时间相关证据" if has_timeline else "缺时间线证据"))

    # ── D5 反事实：不改生产就预先证伪 ───────────────────────
    cf = got.get("counterfactual_index", [])
    applicable = "counterfactual_index" in (G.required_evidence(rc) +
                                            G.supporting_evidence(rc))
    if not applicable:
        d5_ok, d5_detail = True, "该根因不适用反事实验证"
    elif not cf:
        d5_ok, d5_detail = False, "未做反事实验证"
        directives.append(
            f"请用 simulate_index 做反事实验证：不改数据库就能预先证伪 {rc}")
    else:
        d5_ok = any(_supports("counterfactual_index", e["observation"], rc)
                    for e in cf)
        d5_detail = "模拟显示优化器会采用" if d5_ok else "模拟显示优化器不会采用该索引"
        if not d5_ok:
            directives.append(f"反事实模拟证伪了 {rc}，应改换假设")
    dims.append(DimResult("D5", d5_ok, False, d5_detail))

    # ── 裁决 ───────────────────────────────────────────────
    # D1/D2 是必需项，不可被其他维度加权补偿 ——
    # 否则"编个自洽故事就能过"的漏洞又回来了。
    mandatory_ok = all(d.passed for d in dims if d.mandatory)
    optional = [d for d in dims if not d.mandatory]
    score = sum(1 for d in optional if d.passed) / max(len(optional), 1)

    confirmed = [c for c in cands
                 if st.ledger.get(c) and
                 st.ledger[c].verdict == Verdict.CONFIRMED.value]

    # 多根因判定必须排在 SUFFICIENT 之前。
    #
    # 原顺序是 SUFFICIENT 先判、多根因那条 elif 在后，于是只要被声明的
    # 根因证据齐备就直接放行 —— 第二个已确认的根因被静默忽略。后果不是
    # "少修一个"，而是：只修一个 -> VERIFY 见 KPI 没回基线 -> 判修复失败
    # -> ROLLBACK 把那个正确的修复撤掉并记一次失败 -> 两次后
    # REFUTED_BY_REMEDIATION 把正确根因永久封掉。"修了一半"被当成
    # "修错了"，而且污染的是跨轮次持久的台账。
    #
    # 那条 elif 只在"多个确认 且 证据还不齐"时才够得着，而那种情况其实
    # 是鉴别诊断没做干净，不是多根因。分支写对了，位置放错了。
    pool = list(dict.fromkeys(([rc] if rc else []) + confirmed))
    chain = G.collapse_chain(pool)

    if chain["kind"] == "cascade" and chain["upstream"] != rc:
        # 不是歧义，是同一条因果链：修下游只治标，根因会复发
        verdict = ESCVerdict.INSUFFICIENT.value
        directives.insert(
            0, f"{chain['upstream']} 在因果链上位于 {rc} 上游"
               f"（{' -> '.join(chain['path'])}），应改声明它 —— "
               f"修下游只治标，根因会复发")
    elif chain["kind"] == "independent":
        verdict = ESCVerdict.AMBIGUOUS.value
        directives.insert(
            0, f"多个互不相关的根因同时被确认 {chain['independent']}，"
               f"当前只支持单根因修复；按其中之一动手会修一半并被判成"
               f"修复失败，升级人工")
    elif mandatory_ok and (not applicable or dims[-1].passed):
        verdict = ESCVerdict.SUFFICIENT.value
    elif st.budget["steps"] >= st.budget["max_steps"] * 0.8:
        verdict = ESCVerdict.EXHAUSTED.value
    else:
        verdict = ESCVerdict.INSUFFICIENT.value

    return ESCReport(verdict, rc, dims, directives, round(score, 2))
