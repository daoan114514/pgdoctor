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

import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent.episode_state import (EpisodeState, EvidenceStatus, Verdict,
                                 evidence_is_observed)
from agent.explanation import (CausalStatus, EvidenceBinding, EvidenceNeed,
                               EvidenceTargetKind, ExplanationScope,
                               ObligationStatus, PredicateResult, stable_id)
from knowledge.causal_graph import graph as G
from knowledge.evidence_predicates import (
    PredicateContext, evaluate, legacy_structured_value,
    registered_predicates)


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


@dataclass(frozen=True)
class ExplanationESCConfig:
    """Conservative, replayable thresholds for the v2 sufficiency check."""

    major_alternative_score_ratio: float = 0.75
    unavailable_attempts_before_exhausted: int = 2
    max_directives: int = 12


DEFAULT_EXPLANATION_ESC = ExplanationESCConfig()


# 证据取值是否真的支持该根因。
# 只查"跑过没跑过"还不够 —— 跑了但结果指向反面同样不能算数。
def _supports(evidence_type: str, observation: str, root_cause: str) -> bool:
    node = G.load().nodes.get(evidence_type, {})
    predicate_id = str(node.get("predicate_id", ""))
    value = legacy_structured_value(predicate_id, observation)
    target_kind = ("INTERVENTION" if evidence_type == "counterfactual_index"
                   else "NODE")
    target_ids = (("create_covering_index",) if target_kind == "INTERVENTION"
                  else (root_cause,))
    decision = evaluate(
        predicate_id, value,
        context=PredicateContext(
            target_kind=target_kind,
            target_ids=target_ids,
            # Synthetic window metadata is v1 replay compatibility only.
            window_start=0.0, window_end=0.0, source_epoch="legacy",
        ),
    )
    return decision.result == "SUPPORTS"


# 哪些 (证据, 根因) 组合可以用来判"这次排除的取值方向对不对"。
#
# 从因果图的 REFUTED_BY 边推导，不再手工维护。手工那版和 _supports
# 漂移，害我连着犯了三次错：把"这条不查"当成"这条支持"、把"代码里没有
# 根因守卫"当成"语义上对所有根因成立"、把存在性判断当成双向取值判断。
#
# 用 REFUTED_BY 推导是语义上正确的：那条边的含义就是"这条证据在某个取值
# 下能反证这个根因"，正是方向判断需要的前提。图上没有这条边，就说明没人
# 声明过它能反证，那就不该拿它判方向。
#
# 副作用之一是 stats_freshness 自动落选 —— 这是对的。项目自己早就得出
# 结论"统计过期的判别特征是估计与实际的偏差，不是时间戳"（实测偏差
# 4200 倍而 last_analyze 看着是新的），拿它判方向等于退回那个已知的错误。


def _value_checked(evidence_type: str, root_cause: str) -> bool:
    """这个组合能不能用来判取值方向。

    两个条件都要满足：图上声明了这条证据能反证该根因，且 _supports 里
    真的为这个组合写了取值检查（没写的话它一律返回 True，那是"不查"
    的意思，拿来当"支持"会误伤正当的排除）。
    """
    if evidence_type not in _CHECKED_TYPES:
        return False
    return any(r["evidence"] == evidence_type and
               r.get("predicate_id") in registered_predicates()
               for r in G.refuting_evidence(root_cause))


# _supports 里真的实现了取值检查的证据类型。没在这里的，_supports 一律
# 返回 True 表示"不查" —— 那不是"支持"。
_CHECKED_TYPES = frozenset({
    "explain_seq_scan", "stats_freshness", "lock_blocking_chain",
    "counterfactual_index", "idle_in_transaction", "xid_age",
    "autovacuum_health", "backend_xmin_age", "replication_slot_age",
    "prepared_xact_age", "disk_usage", "deadlock_count",
    "temp_file_volume", "checkpoint_stats", "row_estimate_deviation",
    "explain_plan", "physical_bloat_ratio", "connection_count",
})


def _entry_decision(entry: dict, root_cause: str,
                    relation: dict | None = None):
    """Evaluate a persisted evidence entry without reading its summary."""
    evidence_type = entry["evidence_type"]
    node = G.load().nodes.get(evidence_type, {})
    predicate_id = str((relation or {}).get("predicate_id") or
                       entry.get("predicate_id") or
                       node.get("predicate_id", ""))
    structured = entry.get("structured_value")
    legacy = structured is None
    if legacy:
        structured = legacy_structured_value(
            predicate_id, entry.get("observation", ""))

    scope = str((relation or {}).get("scope") or
                entry.get("target_kind") or "NODE")
    if scope == "INTERVENTION":
        target_ids = tuple(filter(None, [
            str((relation or {}).get("target_fix") or "")]))
    else:
        target_ids = tuple(entry.get("target_ids") or [root_cause])

    window_start = entry.get("window_start")
    window_end = entry.get("window_end")
    source_epoch = str(entry.get("source_epoch") or "")
    if legacy:
        # Old traces have no structured window metadata.  Their timestamp is
        # accepted only by this read-only replay adapter.
        observed_at = float(entry.get("ts", 0.0) or 0.0)
        window_start = observed_at
        window_end = observed_at
        source_epoch = "legacy"
    return evaluate(
        predicate_id, structured,
        context=PredicateContext(
            target_kind=scope,
            target_ids=target_ids,
            collection_status=entry.get(
                "status", EvidenceStatus.OBSERVED.value),
            window_start=window_start,
            window_end=window_end,
            source_epoch=source_epoch,
        ),
        window_required=(relation or {}).get("window_required"),
    )


def _collected(st: EpisodeState) -> dict[str, list[dict]]:
    """只归集成功观测到的证据。UNKNOWN/ERROR 只能触发补证。"""
    out: dict[str, list[dict]] = {}
    for e in st.scratchpad:
        if not evidence_is_observed(e):
            continue
        out.setdefault(e["evidence_type"], []).append(e)
    return out


def _unavailable(st: EpisodeState) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in st.scratchpad:
        if evidence_is_observed(e):
            continue
        out.setdefault(e["evidence_type"], []).append(e)
    return out


def check(st: EpisodeState, candidates: list[str] | None = None,
          min_refute_ratio: float = 0.5) -> ESCReport:
    rc = st.claimed_fault_class
    if not rc:
        return ESCReport(ESCVerdict.INSUFFICIENT.value, None,
                         directives=["尚未声明根因"])

    got = _collected(st)
    unavailable = _unavailable(st)
    # 症状必须先归一到图上的节点 id 再去查候选根因。st.symptoms 存的是
    # 人话串（实测 "错误 5285"，数值还烧在里面），直接喂给 candidate_causes
    # 一个都命不中、返回空列表 —— 而竞争假设为空时 D2 的排除率按代码里
    # 的 `if competitors else 1.0` 默认取 1.0，于是这道"必需且不可补偿"
    # 的闸变成无条件通过。
    #
    # 44 个 episode 重放实测：D2 通过率 44/44，它一次都没拦下过任何东西，
    # 全部 5 次拦截都来自 D1。同一个 bug 还顺带废掉了 AMBIGUOUS —— 多根因
    # 检测也在这个列表上算，列表空了 confirmed 就永远是空的，于是有 episode
    # 同时 CONFIRMED 了 lock_contention 与 missing_index 却照样一路放行。
    #
    # D3 与 evolution.learn_truth 都已经改用 map_symptoms，这里是漏网的。
    # fallback=True：查候选时宁可退回默认症状，也不能因为没归一上就把
    # 竞争假设集算成空 —— 那正是这个 bug 的形态。
    if candidates is not None:
        cands = list(candidates)
    elif st.hypothesis_candidates:
        cands = list(st.hypothesis_candidates)
    else:
        # 仅供 2026-09 之前的旧轨迹 replay：当时 EpisodeState 没持久化
        # 候选集，只能按旧版固定 top-k 语义重建。在线路径绝不会走这里；
        # P0 风险召回属于假设生成阶段，不属于 ESC。
        _sym = G.map_symptoms(st.symptoms or [], fallback=True)
        cands = [c["root_cause"] for c in G.candidate_causes(
            _sym or ["latency_p99_up", "cpu_saturated"])]
    dims: list[DimResult] = []
    directives: list[str] = []

    # ── D1 直接证据（必需项）────────────────────────────────
    required = G.required_evidence(rc)
    missing, unknown, unsupported = [], [], []
    for ev in required:
        entries = got.get(ev, [])
        if not entries:
            if unavailable.get(ev):
                unknown.append(ev)
            else:
                missing.append(ev)
        elif not any(_entry_decision(e, rc).result == "SUPPORTS"
                     for e in entries):
            unsupported.append(ev)
    d1_ok = not missing and not unknown and not unsupported
    detail = (f"必需证据 {len(required)} 项，缺 {len(missing)}，"
              f"未知/错误 {len(unknown)}，取值不支持 {len(unsupported)}")
    dims.append(DimResult("D1", d1_ok, True, detail,
                          missing + unknown + unsupported))
    for ev in missing:
        q = G.load().nodes.get(ev, {}).get("obtained_by", "相应工具")
        directives.append(f"缺少必需证据 {ev}，请调用 {q} 取证")
    for ev in unknown:
        entries = unavailable[ev]
        statuses = sorted({e.get("status", EvidenceStatus.UNKNOWN.value)
                           for e in entries})
        reason = entries[-1].get("observation", "")[:100]
        q = G.load().nodes.get(ev, {}).get("obtained_by", "相应工具")
        directives.append(
            f"必需证据 {ev} 当前为 {'/'.join(statuses)}（{reason}）；"
            f"请排除观测错误并再次调用 {q}")
    for ev in unsupported:
        directives.append(f"证据 {ev} 的取值并不支持 {rc}，请复核或改换假设")

    # ── D2 鉴别诊断（必需项）────────────────────────────────
    # 剔除自己造成的下游后果：它们不是竞争解释，是已经被解释掉的结果。
    # 声称长事务时连接打满确实在发生（图上 long_idle_transaction -->
    # connection_exhaustion 是条级联边），要求 agent 去"排除"它等于要求
    # 它否认一个正在发生的事实 —— 而 connection_count 也确实显示连接满了，
    # 于是这次排除永远拿不到依据，正解被判负。
    #
    # 裁决阶段早就用 collapse_chain 处理级联（"修下游只治标"），D2 这层
    # 一直没有，同一个概念只实现了一半。
    _downstream = G.downstream_of(rc)
    competitors = [c for c in cands if c != rc and c not in _downstream]
    # P0 不和普通低先验候选一起稀释成一个比例。普通候选仍要求排除一半；
    # 每个相关 P0 则必须单独取证并排除（或被确认后触发后面的多根因裁决）。
    # 这样扩大召回不会把 D2 的普通排除负担线性抬高，也不会让 P0 只在
    # 候选列表里露个脸就被早停跳过。
    p0_competitors = [c for c in competitors if G.severity_of(c) == "P0"]
    ordinary_competitors = [c for c in competitors if c not in p0_competitors]
    def _backed(h: str) -> bool:
        """这个排除有没有证据支撑。

        判据从因果图取：只要该假设的确认／反证证据类型在轨迹里出现过，
        就算做过功。不看 note 写得多漂亮 —— 那是模型自述，正是 ESC
        从设计上就不采信的东西。

        REFUTED_BY_REMEDIATION 无条件算数：它来自一次真实的修复失败，
        是比任何只读证据都强的依据。
        """
        e = st.ledger.get(h)
        if e and e.verdict == Verdict.REFUTED_BY_REMEDIATION.value:
            return True
        # A hypothesis refutation requires an explicit REFUTED_BY relation and
        # a deterministic REFUTES result.  Supporting/discriminator evidence
        # cannot be repurposed as a negative just because a model says so.
        # Intervention-scoped negatives invalidate only that concrete fix and
        # therefore never back a node/path hypothesis refutation.
        for relation in G.refuting_evidence(h):
            if relation.get("scope") == "INTERVENTION":
                continue
            for entry in got.get(relation["evidence"], []):
                if _entry_decision(entry, h, relation).result == "REFUTES":
                    return True
        return False

    # 只数有依据的排除。原来只看 verdict 字符串，于是把竞争假设无脑标成
    # REFUTED 就能让这道闸无条件通过，一条判别证据都不用取。
    refuted_all = [c for c in competitors
                   if st.ledger.get(c) and st.ledger[c].verdict in
                   (Verdict.REFUTED.value,
                    Verdict.REFUTED_BY_REMEDIATION.value)]
    excluded = [c for c in refuted_all if _backed(c)]
    unbacked = [c for c in refuted_all if c not in excluded]
    ordinary_excluded = [c for c in excluded if c in ordinary_competitors]
    p0_excluded = [c for c in excluded if c in p0_competitors]
    ratio = (len(ordinary_excluded) / len(ordinary_competitors)
             if ordinary_competitors else 1.0)
    unresolved_p0 = [c for c in p0_competitors if c not in p0_excluded]
    d2_ok = ratio >= min_refute_ratio and not unresolved_p0
    dims.append(DimResult(
        "D2", d2_ok, True,
        f"普通竞争假设 {len(ordinary_competitors)} 个，已排除 "
        f"{len(ordinary_excluded)} 个 ({ratio:.0%})；"
        f"P0 {len(p0_competitors)} 个，已取证排除 {len(p0_excluded)} 个"
        + (f"；另有 {len(unbacked)} 个声称排除但无证据支撑：{unbacked}"
           if unbacked else ""),
        [c for c in competitors if c not in excluded]))
    if not d2_ok:
        for c in competitors:
            if c in excluded:
                continue
            disc = G.best_discriminator([rc, c])
            hint = (f"（可用 {disc['obtained_by']} 取 {disc['evidence']}）"
                    if disc else "")
            why = ("已声称排除但没有任何支撑证据，需实际取证"
                   if c in unbacked else "尚未排除")
            directives.append(f"竞争假设 {c} {why}{hint}")

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
                       and evidence_is_observed(e)
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
        relation = next((item for item in G.refuting_evidence(rc)
                         if item["evidence"] == "counterfactual_index" and
                         item.get("scope") == "INTERVENTION"), None)
        decisions = [_entry_decision(e, rc, relation) for e in cf]
        d5_ok = any(item.result == "SUPPORTS" for item in decisions)
        d5_detail = "模拟显示优化器会采用" if d5_ok else "模拟显示优化器不会采用该索引"
        if not d5_ok:
            directives.append("反事实模拟否定了当前具体索引定义；"
                              "保留 missing_index 路径并更换索引方案")
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
    elif mandatory_ok:
        verdict = ESCVerdict.SUFFICIENT.value
    elif st.budget["steps"] >= st.budget["max_steps"] * 0.8:
        verdict = ESCVerdict.EXHAUSTED.value
    else:
        verdict = ESCVerdict.INSUFFICIENT.value

    return ESCReport(verdict, rc, dims, directives, round(score, 2))


# ---------------------------------------------------------------------------
# v2 explanation-subgraph ESC
# ---------------------------------------------------------------------------

_CUMULATIVE_EPOCH_KEYS = {
    "deadlock_count": "pg_stat_database",
    "temp_file_volume": "pg_stat_database",
    "checkpoint_stats": "checkpoint_stats",
}


def _window_predicate_ids() -> set[str]:
    ids: set[str] = set()
    graph = G.load()
    for node_id, data in graph.nodes(data=True):
        if data.get("kind") != "RootCause":
            continue
        ids.update(
            str(item.get("predicate_id") or "")
            for item in G.refuting_evidence(node_id)
            if item.get("window_required")
        )
    return ids - {""}


def _binding_trust(st: EpisodeState, binding: EvidenceBinding, *,
                   now: float, window_predicates: set[str]
                   ) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    evidence_node = G.load().nodes.get(binding.evidence_type, {})
    expected_predicate = str(evidence_node.get("predicate_id") or "")
    if (evidence_node.get("kind") != "Evidence" or not expected_predicate or
            binding.predicate_id != expected_predicate):
        reasons.append("binding predicate does not match the evidence type")
    if binding.episode_id != st.episode_id:
        reasons.append("binding belongs to another episode")
    if binding.status != EvidenceStatus.OBSERVED.value:
        reasons.append(f"collection status is {binding.status}")
    if not binding.validate_raw_ref():
        reasons.append("raw_ref is not verifiable in the current episode")
    if not binding.validate_value_digest():
        reasons.append("trace value digest does not match")
    if not binding.is_fresh(now):
        reasons.append("evidence is expired")

    if binding.predicate_id in window_predicates:
        if (binding.window_start is None or binding.window_end is None or
                binding.window_end < binding.window_start or
                not binding.source_epoch):
            reasons.append("incident window or source epoch is missing")
        payload = binding._trace_payload()  # Trace is the structured authority.
        value = payload.get("digest") if isinstance(payload, dict) else None
        value_epoch = (str(value.get("source_epoch") or "")
                       if isinstance(value, dict) else "")
        if value_epoch and value_epoch != binding.source_epoch:
            reasons.append("structured value and binding source epochs differ")

        epoch_key = _CUMULATIVE_EPOCH_KEYS.get(binding.evidence_type)
        expected_epochs = st.incident_window.get("source_epochs", {})
        expected = (str(expected_epochs.get(epoch_key) or "")
                    if isinstance(expected_epochs, dict) and epoch_key else "")
        if expected and binding.source_epoch != expected:
            reasons.append("source epoch does not match the incident window")
    return not reasons, reasons


def _matching_bindings(bindings: dict[str, EvidenceBinding], *,
                       result: str | None = None,
                       evidence_type: str | None = None,
                       node_id: str | None = None,
                       edge_id: str | None = None) -> list[EvidenceBinding]:
    matched = []
    for binding in bindings.values():
        if result is not None and binding.predicate_result != result:
            continue
        if evidence_type is not None and binding.evidence_type != evidence_type:
            continue
        if node_id is not None and node_id not in binding.target_node_ids:
            continue
        if edge_id is not None and edge_id not in binding.target_edge_ids:
            continue
        matched.append(binding)
    return matched


def _path_is_suffix(left, right) -> bool:
    """Whether either path is only an upstream extension of the other."""
    shorter, longer = ((left, right) if len(left.node_ids) <= len(right.node_ids)
                       else (right, left))
    return (shorter.node_ids == longer.node_ids[-len(shorter.node_ids):] and
            shorter.edge_ids == longer.edge_ids[-len(shorter.edge_ids):])


def _path_score(path) -> float:
    return float(path.score_components.get("total", 0.0) or 0.0)


def _major_alternatives(explanation, selected, *,
                        config: ExplanationESCConfig) -> list[tuple[Any, Any]]:
    selected_ids = {path.path_id for path in selected}
    pairs: dict[tuple[str, str], tuple[Any, Any]] = {}
    for path in selected:
        structural = [
            alternative
            for alternative in G.alternatives_for(path.path_id, explanation)
            if alternative.path_id not in selected_ids and
            not _path_is_suffix(path, alternative) and
            G.severity_of(alternative.root_node_id) != "P0"
        ]
        if not structural:
            continue
        selected_score = _path_score(path)
        best_score = max(_path_score(item) for item in structural)
        cutoff = (selected_score * config.major_alternative_score_ratio
                  if selected_score > 0 else best_score)
        for index, alternative in enumerate(structural):
            status = alternative.status
            # INCONCLUSIVE describes the evidence state; it does not make a
            # low-scoring branch a major alternative by itself.  Otherwise a
            # wider recall set forces ESC to exhaust nearly every path before
            # planning.  Keep the best structural competitor, every supported
            # competitor, and paths that clear the configured score cutoff.
            major = (
                index == 0 or status in {
                    CausalStatus.SUPPORTED.value,
                } or _path_score(alternative) >= cutoff
            )
            if major:
                pairs[(path.path_id, alternative.path_id)] = (path, alternative)
    return list(pairs.values())


def _scoped_alternative_refutation(selected, alternative,
                                    trusted: dict[str, EvidenceBinding]
                                    ) -> list[EvidenceBinding]:
    selected_targets = set(selected.node_ids + selected.edge_ids)
    alternative_targets = set(alternative.node_ids + alternative.edge_ids)
    unique_alternative = alternative_targets - selected_targets
    if not unique_alternative:
        return []
    allowed = {
        (item["evidence"], item.get("predicate_id", ""))
        for item in G.refuting_evidence(alternative.root_node_id)
        if item.get("scope") != EvidenceTargetKind.INTERVENTION.value
    }
    matches = []
    for binding in trusted.values():
        if (binding.predicate_result != PredicateResult.REFUTES.value or
                (binding.evidence_type, binding.predicate_id) not in allowed):
            continue
        targets = set(binding.target_node_ids + binding.target_edge_ids)
        if (targets.intersection(unique_alternative) and
                not targets.intersection(selected_targets)):
            matches.append(binding)
    return matches


def _need_for(*, path_ids: list[str], target_kind: str,
              target_ids: list[str], evidence_type: str, required: bool,
              reason: str) -> EvidenceNeed | None:
    graph = G.load()
    evidence = graph.nodes.get(evidence_type, {})
    predicate_id = str(evidence.get("predicate_id") or "")
    tool = str(evidence.get("obtained_by") or "")
    if not predicate_id or not tool:
        return None
    return EvidenceNeed.create(
        path_ids=path_ids,
        target_kind=target_kind,
        target_ids=target_ids,
        evidence_type=evidence_type,
        predicate_id=predicate_id,
        required=required,
        freshness_seconds=int(evidence.get("freshness_seconds", 300)),
        candidate_tools=[tool],
        reason=reason,
    )


def _need_unavailable_attempts(st: EpisodeState, need: EvidenceNeed) -> int:
    audited = sum(
        1 for item in st.evidence_task_audit
        if item.get("event") == "evidence_need_unavailable" and
        item.get("need_id") == need.need_id
    )
    targets = set(need.target_ids)
    reports = sum(
        1 for binding in (
            st.explanation_graph.evidence_bindings.values()
            if st.explanation_graph is not None else []
        )
        if binding.evidence_type == need.evidence_type and
        binding.status in {EvidenceStatus.UNKNOWN.value,
                           EvidenceStatus.ERROR.value} and
        targets.intersection(binding.target_node_ids + binding.target_edge_ids)
    )
    return audited + reports


def _directive(need: EvidenceNeed) -> str:
    tools = ",".join(need.candidate_tools) or "no currently legal tool"
    return (f"collect {need.evidence_type} for {need.target_kind} "
            f"{','.join(need.target_ids)} via {tools}: {need.reason}")


def _dim(name: str, passed: bool, detail: str,
         missing: list[str] | None = None) -> DimResult:
    return DimResult(name=name, passed=passed, mandatory=True,
                     detail=detail, missing=missing or [])


def check_explanation(
        st: EpisodeState, *, persist: bool = True, now: float | None = None,
        config: ExplanationESCConfig = DEFAULT_EXPLANATION_ESC) -> dict:
    """Deterministically assess a persisted v2 explanation subgraph.

    This entry point never recalls candidates and never reads ledger verdicts,
    scratchpad notes, or model-authored summaries.  Only graph structure and
    verifiable EvidenceBindings can affect its causal decision.
    """
    current_time = time.time() if now is None else now
    explanation = st.explanation_graph
    if explanation is None:
        payload = {
            "explanation_id": "",
            "explanation_revision": -1,
            "graph_version": G.graph_version(),
            "scope": ExplanationScope.PARTIAL.value,
            "selected_path_ids": [],
            "selected_root_causes": [],
            "dimensions": [],
            "evidence_need_ids": [],
            "evidence_needs": [],
            "directives": ["generate and persist an explanation graph"],
            "unresolved_p0_paths": [],
            "unexplained_symptoms": list(st.symptoms),
            "coverage_missing": list(st.symptoms),
            "duplicate_raw_refs": [],
            "evidence_refs": [],
            "unsupported_path_ids": [],
            "unresolved_competing_path_ids": [],
            "requires_rehypothesize": True,
            "partial_fix_suspected": False,
            "verdict": ESCVerdict.INSUFFICIENT.value,
            "created_at": current_time,
        }
        payload["esc_report_id"] = stable_id("esc_report", {
            key: payload[key] for key in (
                "explanation_id", "explanation_revision", "graph_version",
                "verdict", "directives")
        })
        payload["report_id"] = payload["esc_report_id"]
        if persist and not any(
                report.get("esc_report_id", report.get("report_id")) ==
                payload["esc_report_id"] for report in st.esc_reports):
            st.esc_reports.append(payload)
        st.partial_fix_suspected = False
        return payload

    current_graph_version = G.graph_version()
    graph_current = explanation.graph_version == current_graph_version
    if graph_current:
        # Import lazily: explanation_runtime keeps the v1 projection and uses
        # this module as its compatibility wrapper.
        from agent.explanation_runtime import recompute_statuses
        recompute_statuses(st, now=current_time)

    paths = explanation.path_map()
    selected = [paths[path_id] for path_id in explanation.selected_path_ids
                if path_id in paths]
    selected_ids = {path.path_id for path in selected}
    selected_roots = explanation.derive_selected_root_causes()
    window_predicates = _window_predicate_ids()

    trust: dict[str, tuple[bool, list[str]]] = {
        binding_id: _binding_trust(
            st, binding, now=current_time,
            window_predicates=window_predicates)
        for binding_id, binding in explanation.evidence_bindings.items()
    }
    trusted = {
        binding_id: explanation.evidence_bindings[binding_id]
        for binding_id, (valid, _reasons) in trust.items() if valid
    }
    use_events: list[str] = []

    observed = list(dict.fromkeys(explanation.observed_symptoms))
    selected_symptoms = {path.observed_symptom_id for path in selected}
    explicit_unexplained = set(explanation.unexplained_symptoms)
    unrecorded = [symptom for symptom in observed
                  if symptom not in selected_symptoms and
                  symptom not in explicit_unexplained]
    selected_missing = [symptom for symptom in observed
                        if symptom not in selected_symptoms]
    scope_consistent = not (
        explanation.scope == ExplanationScope.FULL.value and
        explicit_unexplained
    )

    paths_by_symptom: dict[str, list[Any]] = {}
    for path in explanation.candidate_paths:
        paths_by_symptom.setdefault(path.observed_symptom_id, []).append(path)
    risky_unexplained: list[str] = []
    rehypothesize_symptoms: list[str] = []
    selected_root_set = set(selected_roots)
    for symptom in sorted(explicit_unexplained):
        candidates = paths_by_symptom.get(symptom, [])
        if not candidates:
            risky_unexplained.append(symptom)
            rehypothesize_symptoms.append(symptom)
            continue
        viable = [path for path in candidates
                  if path.status != CausalStatus.REFUTED.value]
        if any(G.severity_of(path.root_node_id) == "P0" or
               path.root_node_id not in selected_root_set
               for path in viable):
            risky_unexplained.append(symptom)

    coverage_ok = not unrecorded and scope_consistent
    coverage_dim = _dim(
        "SYMPTOM_COVERAGE", coverage_ok,
        f"selected={sorted(selected_symptoms)}, "
        f"explicit_unexplained={sorted(explicit_unexplained)}",
        unrecorded + ([] if scope_consistent else ["FULL scope has unexplained symptoms"]),
    )

    root_missing: list[str] = []
    gap_needs: dict[str, EvidenceNeed] = {}
    for root_id in selected_roots:
        root_path_ids = [path.path_id for path in selected
                         if path.root_node_id == root_id]
        for evidence_type in G.required_evidence(root_id):
            bindings = _matching_bindings(
                trusted, result=PredicateResult.SUPPORTS.value,
                evidence_type=evidence_type, node_id=root_id)
            if bindings:
                use_events.extend(binding.binding_id for binding in bindings[:1])
                continue
            root_missing.append(f"{root_id}:{evidence_type}")
            need = _need_for(
                path_ids=root_path_ids,
                target_kind=EvidenceTargetKind.NODE.value,
                target_ids=[root_id], evidence_type=evidence_type,
                required=True, reason="selected root required evidence")
            if need:
                gap_needs[need.need_id] = need
    root_dim = _dim(
        "ROOT_REQUIRED_EVIDENCE", bool(selected) and not root_missing,
        f"selected_roots={selected_roots}, missing={len(root_missing)}",
        (["no selected explanation path"] if not selected else []) + root_missing,
    )

    continuity_missing: list[str] = []
    unsupported_paths: list[str] = []
    for path in selected:
        path_missing = False
        for index, node_id in enumerate(path.node_ids[:-1]):
            allowed_evidence = (
                set(G.required_evidence(node_id)) |
                set(G.supporting_evidence(node_id)) |
                set(G.discriminators_of(node_id))
            )
            supports = [binding for binding in _matching_bindings(
                trusted, result=PredicateResult.SUPPORTS.value,
                node_id=node_id)
                if binding.evidence_type in allowed_evidence]
            if supports:
                use_events.append(supports[0].binding_id)
            else:
                path_missing = True
                continuity_missing.append(f"{path.path_id}:NODE:{node_id}")
                evidence_types = (G.required_evidence(node_id) or
                                  G.supporting_evidence(node_id))
                if evidence_types:
                    need = _need_for(
                        path_ids=[path.path_id],
                        target_kind=EvidenceTargetKind.NODE.value,
                        target_ids=[node_id], evidence_type=evidence_types[0],
                        required=bool(G.required_evidence(node_id)),
                        reason="support selected path mechanism")
                    if need:
                        gap_needs[need.need_id] = need

            edge_id = path.edge_ids[index]
            edge_supports = [binding for binding in _matching_bindings(
                trusted, result=PredicateResult.SUPPORTS.value,
                edge_id=edge_id)
                if binding.evidence_type in allowed_evidence]
            if edge_supports:
                use_events.append(edge_supports[0].binding_id)
            else:
                path_missing = True
                continuity_missing.append(f"{path.path_id}:EDGE:{edge_id}")
                evidence_types = (G.required_evidence(node_id) or
                                  G.supporting_evidence(node_id))
                if evidence_types:
                    need = _need_for(
                        path_ids=[path.path_id],
                        target_kind=EvidenceTargetKind.EDGE.value,
                        target_ids=[edge_id], evidence_type=evidence_types[0],
                        required=bool(G.required_evidence(node_id)),
                        reason="support selected causal edge")
                    if need:
                        gap_needs[need.need_id] = need
        if path_missing or path.status != CausalStatus.SUPPORTED.value:
            unsupported_paths.append(path.path_id)
    continuity_dim = _dim(
        "CAUSAL_CONTINUITY", bool(selected) and not continuity_missing and
        not unsupported_paths,
        f"selected_paths={len(selected)}, gaps={len(continuity_missing)}",
        continuity_missing + unsupported_paths,
    )

    alternative_pairs = _major_alternatives(
        explanation, selected, config=config)
    unresolved_alternatives: list[str] = []
    conflicting_alternatives: list[str] = []
    alternative_path_ids: set[str] = set()
    for selected_path, alternative in alternative_pairs:
        alternative_path_ids.add(alternative.path_id)
        refutations = _scoped_alternative_refutation(
            selected_path, alternative, trusted)
        # REFUTED_BY is the graph's explicit exclusion contract.  A trusted,
        # path-scoped refutation closes this alternative even when weaker
        # supporting/discriminating observations leave its aggregate status
        # INCONCLUSIVE.  Requiring the aggregate status to be REFUTED makes
        # repeated collection of the same decisive predicate a fixed point.
        if refutations:
            use_events.append(refutations[0].binding_id)
            continue
        unresolved_alternatives.append(alternative.path_id)
        if alternative.status == CausalStatus.SUPPORTED.value:
            conflicting_alternatives.append(alternative.path_id)
            # Re-running the same predicates is not a useful discriminator
            # once both mutually competing branches already have fresh,
            # positive evidence.  This is a genuine ambiguity, not a need.
            continue
        for relation in G.refuting_evidence(alternative.root_node_id):
            if relation.get("scope") == EvidenceTargetKind.INTERVENTION.value:
                continue
            target_kind = (EvidenceTargetKind.EDGE.value
                           if relation.get("scope") == "PATH" else
                           EvidenceTargetKind.NODE.value)
            selected_targets = set(selected_path.node_ids + selected_path.edge_ids)
            if target_kind == EvidenceTargetKind.EDGE.value:
                choices = [edge_id for edge_id in alternative.edge_ids
                           if edge_id not in selected_targets]
            else:
                choices = [node_id for node_id in alternative.node_ids[:-1]
                           if node_id not in selected_targets]
            if not choices:
                continue
            need = _need_for(
                path_ids=[alternative.path_id], target_kind=target_kind,
                target_ids=[choices[0]], evidence_type=relation["evidence"],
                required=False, reason="distinguish a major competing path")
            if need:
                gap_needs[need.need_id] = need
    alternatives_dim = _dim(
        "ALTERNATIVE_PATHS", not unresolved_alternatives,
        f"major={len(alternative_pairs)}, unresolved={len(unresolved_alternatives)}",
        unresolved_alternatives,
    )

    unresolved_p0_paths: list[str] = []
    unresolved_p0_causes: list[str] = []
    truncated_p0_causes: list[str] = []
    for cause_id, obligation in explanation.p0_obligations.items():
        valid = not obligation.truncated
        if obligation.truncated:
            truncated_p0_causes.append(cause_id)
        if obligation.status == ObligationStatus.SUPPORTED.value:
            for evidence_type in obligation.required_evidence_types:
                bindings = _matching_bindings(
                    trusted, result=PredicateResult.SUPPORTS.value,
                    evidence_type=evidence_type, node_id=cause_id)
                if bindings:
                    use_events.append(bindings[0].binding_id)
                else:
                    valid = False
                    need = _need_for(
                        path_ids=obligation.reachable_path_ids,
                        target_kind=EvidenceTargetKind.P0.value,
                        target_ids=[cause_id], evidence_type=evidence_type,
                        required=True, reason="prove the P0 obligation")
                    if need:
                        gap_needs[need.need_id] = need
        elif obligation.status == ObligationStatus.REFUTED.value:
            allowed_refutations = {
                (item["evidence"], item.get("predicate_id", ""))
                for item in G.refuting_evidence(cause_id)
                if item.get("scope") != EvidenceTargetKind.INTERVENTION.value
            }
            refutations = [binding for binding in _matching_bindings(
                trusted, result=PredicateResult.REFUTES.value,
                node_id=cause_id)
                if (binding.evidence_type, binding.predicate_id) in
                allowed_refutations]
            if refutations:
                use_events.append(refutations[0].binding_id)
            else:
                valid = False
        else:
            valid = False

        if not valid:
            unresolved_p0_causes.append(cause_id)
            unresolved_p0_paths.extend(obligation.reachable_path_ids)
            if not obligation.truncated:
                for evidence_type in obligation.required_evidence_types:
                    need = _need_for(
                        path_ids=obligation.reachable_path_ids,
                        target_kind=EvidenceTargetKind.P0.value,
                        target_ids=[cause_id], evidence_type=evidence_type,
                        required=True, reason="resolve the P0 obligation")
                    if need:
                        gap_needs[need.need_id] = need
    unresolved_p0_paths = list(dict.fromkeys(unresolved_p0_paths))
    p0_dim = _dim(
        "P0_OBLIGATIONS", not unresolved_p0_causes,
        f"total={len(explanation.p0_obligations)}, "
        f"unresolved={len(unresolved_p0_causes)}, truncated={truncated_p0_causes}",
        unresolved_p0_causes,
    )

    relevant_targets = {
        target for path in selected for target in path.node_ids + path.edge_ids
    } | set(explanation.p0_obligations)
    invalid_relevant: dict[str, list[str]] = {}
    trusted_signatures = {
        (binding.evidence_type, binding.predicate_id,
         tuple(binding.target_node_ids), tuple(binding.target_edge_ids),
         binding.predicate_result)
        for binding in trusted.values()
    }
    for binding_id, binding in explanation.evidence_bindings.items():
        valid, reasons = trust[binding_id]
        targets = set(binding.target_node_ids + binding.target_edge_ids)
        if (valid or not targets.intersection(relevant_targets) or not reasons or
                binding.predicate_result not in {
                    PredicateResult.SUPPORTS.value,
                    PredicateResult.REFUTES.value,
                }):
            continue
        signature = (binding.evidence_type, binding.predicate_id,
                     tuple(binding.target_node_ids), tuple(binding.target_edge_ids),
                     binding.predicate_result)
        if signature not in trusted_signatures:
            invalid_relevant[binding_id] = reasons
    trust_dim = _dim(
        "EVIDENCE_TRUST", explanation.episode_id == st.episode_id and
        not invalid_relevant,
        f"trusted={len(trusted)}, invalid_relevant={len(invalid_relevant)}",
        (["explanation belongs to another episode"]
         if explanation.episode_id != st.episode_id else []) +
        [f"{binding_id}: {'; '.join(reasons)}"
         for binding_id, reasons in invalid_relevant.items()],
    )

    graph_dim = _dim(
        "GRAPH_VERSION", graph_current,
        f"explanation={explanation.graph_version}, current={current_graph_version}",
        [] if graph_current else ["graph version changed; rebuild and rediagnose"],
    )

    partial_risk = (
        explanation.scope == ExplanationScope.PARTIAL.value and
        bool(risky_unexplained)
    )
    partial_dim = _dim(
        "PARTIAL_SCOPE", not partial_risk,
        f"scope={explanation.scope}, risky_unexplained={risky_unexplained}",
        risky_unexplained,
    )

    relevant_path_ids = (
        selected_ids | alternative_path_ids | set(unresolved_p0_paths) |
        {path.path_id for symptom in risky_unexplained
         for path in paths_by_symptom.get(symptom, [])
         if path.status != CausalStatus.REFUTED.value}
    )
    needs = {
        need.need_id: need for need in G.evidence_needs(explanation)
        if (need.target_kind == EvidenceTargetKind.P0.value or
            bool(set(need.path_ids).intersection(relevant_path_ids)))
    }
    needs.update(gap_needs)
    for cause_id in truncated_p0_causes:
        needs = {
            need_id: need for need_id, need in needs.items()
            if not (need.target_kind == EvidenceTargetKind.P0.value and
                    cause_id in need.target_ids)
        }
    ordered_needs = sorted(needs.values(), key=lambda need: (
        not need.required,
        need.target_kind != EvidenceTargetKind.P0.value,
        need.evidence_type,
        need.need_id,
    ))
    unavailable_attempts = {
        need.need_id: _need_unavailable_attempts(st, need)
        for need in ordered_needs
    }
    long_unavailable = [
        need for need in ordered_needs
        if need.required and (
            not need.candidate_tools or
            unavailable_attempts[need.need_id] >=
            config.unavailable_attempts_before_exhausted
        )
    ]
    unavailable_optional = [
        need for need in ordered_needs
        if not need.required and (
            not need.candidate_tools or
            unavailable_attempts[need.need_id] >=
            config.unavailable_attempts_before_exhausted
        )
    ]
    available_needs = [need for need in ordered_needs
                       if need not in long_unavailable and
                       need not in unavailable_optional and
                       need.candidate_tools]
    max_steps = int(st.budget.get("max_steps", 0) or 0)
    steps = int(st.budget.get("steps", 0) or 0)
    budget_exhausted = max_steps > 0 and steps >= max_steps
    budget_dim = DimResult(
        name="BUDGET_AND_AVAILABILITY",
        passed=not budget_exhausted and not long_unavailable,
        mandatory=False,
        detail=(f"steps={steps}/{max_steps}, "
                f"available_needs={len(available_needs)}, "
                f"long_unavailable={len(long_unavailable)}, "
                f"optional_unavailable={len(unavailable_optional)}"),
        missing=[need.need_id for need in long_unavailable] +
        (["episode step budget exhausted"] if budget_exhausted else []),
    )

    mandatory = [coverage_dim, root_dim, continuity_dim, alternatives_dim,
                 p0_dim, trust_dim, graph_dim, partial_dim]
    sufficient = bool(selected) and all(dim.passed for dim in mandatory)
    requires_rehypothesize = bool(
        not graph_current or unrecorded or rehypothesize_symptoms)
    if sufficient:
        verdict = ESCVerdict.SUFFICIENT.value
    elif not graph_current or not selected or requires_rehypothesize:
        verdict = ESCVerdict.INSUFFICIENT.value
    elif budget_exhausted or long_unavailable:
        verdict = ESCVerdict.EXHAUSTED.value
    elif available_needs:
        verdict = ESCVerdict.INSUFFICIENT.value
    elif (conflicting_alternatives or unresolved_alternatives or
          risky_unexplained or unresolved_p0_causes):
        verdict = ESCVerdict.AMBIGUOUS.value
    else:
        verdict = ESCVerdict.EXHAUSTED.value

    directives = [_directive(need) for need in available_needs]
    if not selected:
        directives.insert(0, "select a supported explanation subgraph in DIAGNOSE")
    if not graph_current:
        directives.insert(0, "graph version changed; rebuild candidates and rediagnose")
    if unrecorded or rehypothesize_symptoms:
        directives.insert(
            0, "re-hypothesize symptoms without a persisted explanation path: " +
            ",".join(dict.fromkeys(unrecorded + rehypothesize_symptoms)))
    if truncated_p0_causes:
        directives.insert(
            0, "P0 path enumeration was truncated; expand safely or escalate: " +
            ",".join(truncated_p0_causes))
    if conflicting_alternatives and not available_needs:
        directives.insert(
            0, "supported competing paths remain causally ambiguous: " +
            ",".join(conflicting_alternatives))
    if long_unavailable:
        directives.insert(
            0, "required evidence remains unavailable: " +
            ",".join(need.need_id for need in long_unavailable))
    if unavailable_optional:
        directives.insert(
            0, "optional discriminating evidence remains unavailable: " +
            ",".join(need.need_id for need in unavailable_optional))
    directives = list(dict.fromkeys(directives))[:config.max_directives]

    used_bindings = [explanation.evidence_bindings[binding_id]
                     for binding_id in use_events
                     if binding_id in explanation.evidence_bindings]
    raw_ref_counts = Counter(binding.raw_ref for binding in used_bindings)
    duplicate_raw_refs = sorted(
        raw_ref for raw_ref, count in raw_ref_counts.items() if count > 1)
    evidence_refs = list(dict.fromkeys(binding.raw_ref
                                       for binding in used_bindings))
    dims = mandatory + [budget_dim]
    payload = {
        "explanation_id": explanation.explanation_id,
        "explanation_revision": explanation.revision,
        "graph_version": explanation.graph_version,
        "scope": explanation.scope,
        "selected_path_ids": list(explanation.selected_path_ids),
        "selected_root_causes": selected_roots,
        "dimensions": [
            {"name": dim.name, "passed": dim.passed,
             "mandatory": dim.mandatory, "detail": dim.detail,
             "missing": list(dim.missing)}
            for dim in dims
        ],
        "evidence_need_ids": [need.need_id for need in ordered_needs],
        "evidence_needs": [need.to_dict() for need in ordered_needs],
        "directives": directives,
        "unresolved_p0_paths": unresolved_p0_paths,
        "unresolved_p0_causes": unresolved_p0_causes,
        "unexplained_symptoms": list(explanation.unexplained_symptoms),
        # v1 consumers used this field to decide whether to re-hypothesize.
        # It remains a compatibility projection, not the coverage criterion.
        "coverage_missing": selected_missing,
        "unrecorded_symptoms": unrecorded,
        "duplicate_raw_refs": duplicate_raw_refs,
        "evidence_refs": evidence_refs,
        "invalid_evidence_bindings": invalid_relevant,
        "unsupported_path_ids": list(dict.fromkeys(unsupported_paths)),
        "unresolved_competing_path_ids": list(dict.fromkeys(
            unresolved_alternatives)),
        "requires_rehypothesize": requires_rehypothesize,
        "partial_fix_suspected": (
            verdict == ESCVerdict.SUFFICIENT.value and
            explanation.scope == ExplanationScope.PARTIAL.value),
        "verdict": verdict,
        "created_at": current_time,
    }
    payload["esc_report_id"] = stable_id("esc_report", {
        key: payload[key] for key in (
            "explanation_id", "explanation_revision", "graph_version",
            "scope", "selected_path_ids", "dimensions",
            "evidence_need_ids", "unresolved_p0_paths",
            "unresolved_competing_path_ids", "verdict")
    })
    # Temporary reader compatibility for traces produced during the v2
    # migration.  New consumers use esc_report_id.
    payload["report_id"] = payload["esc_report_id"]
    st.partial_fix_suspected = bool(payload["partial_fix_suspected"])
    if persist and not any(
            report.get("esc_report_id", report.get("report_id")) ==
            payload["esc_report_id"] for report in st.esc_reports):
        st.esc_reports.append(payload)
    return payload
