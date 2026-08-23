"""自进化的 L2 / L3 / L4 —— 让学到的东西真正回流到决策依据。

三层各自负责一件事：
  L2  技能沉淀    把成功的取证顺序固化成 playbook，下次照着走
  L3  失败驱动    从结局回写因果图的先验与必需证据
  L4  查询库      沉淀真正有判别力的诊断查询

共同点是它们都**不训练模型**，改的是外部知识。这样自进化是可审计的：
YAML 落盘并进 git，这周学到了什么、哪条被推翻了，都能 diff 出来。

关键约束（和 L1 的红线一致）：这些先验只影响**假设的生成与排序**，
绝不放松证据要求。ESC 的 D1/D2 仍然照查 —— 学得再多也不能替代取证。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LEARNED = ROOT / "knowledge" / "learned"


# ══ L2 技能沉淀 ═════════════════════════════════════════════

@dataclass
class Playbook:
    """一个根因的可复用诊断流程。

    只从"确实解决了"的 episode 沉淀 —— 诊断对但没修好的不算数，
    否则会把运气好的路径固化下来。
    """
    root_cause: str
    evidence_order: list[str] = field(default_factory=list)  # 有效取证顺序
    decisive_evidence: list[str] = field(default_factory=list)
    typical_fix: str = ""
    fix_action_type: str = ""
    median_steps: int = 0
    success_count: int = 0
    fail_count: int = 0
    updated_at: float = field(default_factory=time.time)

    @property
    def confidence(self) -> float:
        n = self.success_count + self.fail_count
        return round(self.success_count / n, 3) if n else 0.0


def _pb_path() -> Path:
    LEARNED.mkdir(parents=True, exist_ok=True)
    return LEARNED / "playbooks.yaml"


def load_playbooks() -> dict[str, Playbook]:
    p = _pb_path()
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {k: Playbook(**v) for k, v in raw.items()}


def save_playbooks(pbs: dict[str, Playbook]) -> None:
    _pb_path().write_text(
        yaml.safe_dump({k: asdict(v) for k, v in pbs.items()},
                       allow_unicode=True, sort_keys=True),
        encoding="utf-8")


def sediment_playbook(st, score, applied_sql: list[str]) -> Playbook | None:
    """从一个 episode 沉淀/更新 playbook。"""
    rc = st.claimed_fault_class
    if not rc:
        return None
    solved = bool(score.diagnosis and score.outcome and score.safe_pass)

    pbs = load_playbooks()
    pb = pbs.get(rc) or Playbook(root_cause=rc)

    if solved:
        # 只有真解决了才更新流程本身，否则只累计失败计数
        order, seen = [], set()
        for e in st.scratchpad:
            k = e["evidence_type"]
            if k.startswith(("subagent_", "incidental", "blocked_",
                             "remediation_", "proposal_")):
                continue
            if k not in seen:
                seen.add(k)
                order.append(k)
        pb.evidence_order = order[:8]
        pb.decisive_evidence = [
            e["evidence_type"] for e in st.scratchpad
            if e["evidence_type"] in ("explain_seq_scan", "index_existence",
                                      "lock_blocking_chain", "connection_count",
                                      "row_estimate_deviation",
                                      "dead_tuple_ratio")][:4]
        if applied_sql:
            pb.typical_fix = applied_sql[-1]
            pb.fix_action_type = st.proposal.get("action_type", "")
        steps = st.budget.get("steps", 0)
        pb.median_steps = (steps if not pb.median_steps
                           else (pb.median_steps + steps) // 2)
        pb.success_count += 1
    else:
        pb.fail_count += 1

    pb.updated_at = time.time()
    pbs[rc] = pb
    save_playbooks(pbs)
    return pb


def render_playbook_hint(candidates: list[str], budget: int = 420) -> str:
    """把 playbook 压成提示。只给顺序与决定性证据，不给结论。"""
    pbs = load_playbooks()
    picks = [pbs[c] for c in candidates
             if c in pbs and pbs[c].success_count > 0]
    if not picks:
        return ""
    picks.sort(key=lambda p: -p.confidence)
    lines = ["[历史有效流程] 同类根因过去是这样查出来的:"]
    for pb in picks[:3]:
        lines.append(
            f"  · {pb.root_cause}（成功 {pb.success_count} 次，"
            f"置信 {pb.confidence}，中位 {pb.median_steps} 步）")
        if pb.evidence_order:
            lines.append(f"    取证顺序: {' → '.join(pb.evidence_order[:5])}")
        if pb.decisive_evidence:
            lines.append(f"    决定性证据: {pb.decisive_evidence}")
    lines.append("  （流程只是参考，结论仍需你自己取证）")
    return "\n".join(lines)[:budget]


# ══ L3 失败驱动的先验更新 ════════════════════════════════════

@dataclass
class GraphDelta:
    """对因果图的一次学习性修改。

    单独存成 overlay 而不是直接改种子图：种子图是手工写的 ground truth，
    混在一起就分不清"人写的"和"学来的"，出了问题也没法回滚。
    """
    likelihood_adj: dict = field(default_factory=dict)   # "cause->symptom": 调整量
    prior_adj: dict = field(default_factory=dict)        # root_cause: 调整量
    observed: dict = field(default_factory=dict)         # root_cause: {hit, miss}
    updated_at: float = field(default_factory=time.time)


def _delta_path() -> Path:
    LEARNED.mkdir(parents=True, exist_ok=True)
    return LEARNED / "graph_delta.yaml"


def load_delta() -> GraphDelta:
    p = _delta_path()
    if not p.exists():
        return GraphDelta()
    return GraphDelta(**(yaml.safe_load(p.read_text(encoding="utf-8")) or {}))


def save_delta(d: GraphDelta) -> None:
    _delta_path().write_text(
        yaml.safe_dump(asdict(d), allow_unicode=True, sort_keys=True),
        encoding="utf-8")


# 单次调整的步长与总量上限。
# 不设上限的话，几次连续失败就能把一个根因的先验压到 0，
# 之后它永远进不了候选集 —— 学习不该让系统丧失能力。
LR = 0.05
MAX_ADJ = 0.25

# 更重要的是相对上限：调整量不得超过该根因基础先验的这个比例。
# 基础先验量级是 0.02~0.35，固定的 ±0.25 足以让学习完全颠覆手工
# 先验（实测 missing_index 从第一掉到第三）。种子图是人写的
# ground truth，学习应当精调而不是推翻。
MAX_REL_ADJ = 0.5


def _base_prior(rc: str) -> float:
    try:
        from knowledge.causal_graph import graph as _G
        return float(_G.load().nodes.get(rc, {}).get("prior", 0.1))
    except Exception:
        return 0.1


def _cap_for(rc: str) -> float:
    return min(MAX_ADJ, _base_prior(rc) * MAX_REL_ADJ)


def _bump(d: GraphDelta, rc: str, amount: float) -> None:
    cap = _cap_for(rc)
    cur = d.prior_adj.get(rc, 0.0)
    d.prior_adj[rc] = round(max(-cap, min(cap, cur + amount)), 4)


def learn_from_episode(st, score, symptoms: list[str]) -> GraphDelta:
    """从 episode 结局更新先验。

    要点是区分三种结局，它们的信息量完全不同：
      诊断正确    该根因在这组症状下确实更可能 -> 升
      诊断错误    被错认的降（真凶由 learn_truth 单独升）
      没有结论    对先验没有信息量 -> 不动

    第一版把"没诊断出来"和"诊断错了"都算成 miss 并同等降权，结果
    missing_index 这个最常见、也最常被正确诊断的根因反而被压到负值，
    只出现过两次的 lock_contention 却顶到上限 —— 把"这次没查出来"
    当成了"这个根因不太可能"，两者根本不是一回事。
    """
    d = load_delta()
    claimed = st.claimed_fault_class
    if not claimed:
        # 没有结论就没有信息量。硬记一笔只会把噪声写进先验。
        d.observed.setdefault("_no_conclusion", {"hit": 0, "miss": 0})
        d.observed["_no_conclusion"]["miss"] += 1
        save_delta(d)
        return d

    hit = bool(score.diagnosis)
    obs = d.observed.setdefault(claimed, {"hit": 0, "miss": 0})
    obs["hit" if hit else "miss"] += 1
    _bump(d, claimed, LR if hit else -LR)

    # 修复失败是比"诊断没中"更强的负信号：真按这个根因动手了还是没治好
    for a in st.attempts:
        if a.verdict.startswith("FAILED"):
            _bump(d, a.root_cause, -LR / 2)

    d.updated_at = time.time()
    save_delta(d)
    return d


def learn_truth(claimed: str | None, truth: str, symptoms: list[str]) -> None:
    """已知正确答案时的定向修正（跑批场景里 ground truth 是已知的）。

    只在"误诊"时补一刀：把真凶升上来。命中的情况 learn_from_episode
    已经升过一次，这里再升就是同一次成功被计两遍。
    """
    d = load_delta()
    if claimed == truth:
        return          # 已由 learn_from_episode 处理
    if claimed:
        _bump(d, claimed, -LR)      # 错认的降
    _bump(d, truth, LR)             # 真凶升
    for s in symptoms:
        key = f"{truth}->{s}"
        d.likelihood_adj[key] = round(
            min(MAX_ADJ, d.likelihood_adj.get(key, 0.0) + LR), 4)
    d.updated_at = time.time()
    save_delta(d)


# ══ L4 诊断查询库 ═══════════════════════════════════════════

@dataclass
class QueryStat:
    """一条诊断查询的历史表现。

    判别力 = 它出现在成功 episode 里的比例。查了却没帮上忙的查询
    会自然沉底，agent 就不会再优先用它。
    """
    evidence_type: str
    tool: str
    used_in_success: int = 0
    used_in_failure: int = 0
    root_causes: dict = field(default_factory=dict)   # 它帮着确认了哪些根因

    @property
    def discriminative_power(self) -> float:
        """出现在成功 episode 里的比例。

        注意这个指标只说明"用它的时候多半会成功"，不代表它对某个具体
        根因有判别力 —— 到处都被调用的工具会天然拿高分。按根因排序时
        必须用 power_for()，别用这个。
        """
        n = self.used_in_success + self.used_in_failure
        return round(self.used_in_success / n, 3) if n else 0.0

    def power_for(self, root_cause: str, total_success: int) -> float:
        """对某个具体根因的判别力。

        用"它在该根因上的命中占比"减去"它的全局出场率"：
        到处都出现的工具全局出场率高，净值就低；只在某类故障里出现的
        工具净值高。这样 simulate_index 这种几乎每次都被调用的工具，
        就不会因为沾光而排到锁竞争的第一位。
        """
        hit = self.root_causes.get(root_cause, 0)
        if not hit:
            return 0.0
        base = (self.used_in_success / total_success) if total_success else 0.0
        return round(hit / max(sum(self.root_causes.values()), 1) - base * 0.5, 4)


def _ql_path() -> Path:
    LEARNED.mkdir(parents=True, exist_ok=True)
    return LEARNED / "query_library.yaml"


def load_queries() -> dict[str, QueryStat]:
    p = _ql_path()
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {k: QueryStat(**v) for k, v in raw.items()}


def save_queries(qs: dict[str, QueryStat]) -> None:
    _ql_path().write_text(
        yaml.safe_dump({k: asdict(v) for k, v in qs.items()},
                       allow_unicode=True, sort_keys=True),
        encoding="utf-8")


TOOL_OF = {
    "explain_seq_scan": "explain_query", "explain_plan": "explain_query",
    "row_estimate_deviation": "explain_query",
    "index_existence": "get_indexes", "stats_freshness": "get_table_stats",
    "dead_tuple_ratio": "get_table_stats",
    "lock_blocking_chain": "get_blocking_chain",
    "session_wait_profile": "get_active_sessions",
    "connection_count": "get_connection_stats",
    "slow_query_ranking": "get_top_queries",
    "counterfactual_index": "simulate_index",
}


def _evidence_of(root_cause: str) -> set[str]:
    """图上与该根因直接相关的证据类型。"""
    try:
        from knowledge.causal_graph import graph as _G
        return set(_G.required_evidence(root_cause)) | set(
            _G.supporting_evidence(root_cause)) | {
            r["evidence"] for r in _G.refuting_evidence(root_cause)}
    except Exception:
        return set()


def record_queries(st, score) -> dict[str, QueryStat]:
    """统计每种证据的表现。

    归因只算图上与该根因相关的证据 —— 一个 episode 里各个子 agent 会
    取一大堆证据，全记在当次根因名下的话，到处出现的工具就会在每个
    根因上都沾光（实测 lock_contention 的偏好里排前面的是
    simulate_index，而真正判别锁竞争的 get_blocking_chain 反而不在）。
    """
    qs = load_queries()
    solved = bool(score.diagnosis)
    rc = st.claimed_fault_class or "?"
    related = _evidence_of(rc) if rc != "?" else set()
    seen = {e["evidence_type"] for e in st.scratchpad}
    for ev in seen:
        tool = TOOL_OF.get(ev)
        if not tool:
            continue
        q = qs.get(ev) or QueryStat(evidence_type=ev, tool=tool)
        if solved:
            q.used_in_success += 1
            if ev in related:          # 只有相关的才计到该根因名下
                q.root_causes[rc] = q.root_causes.get(rc, 0) + 1
        else:
            q.used_in_failure += 1
        qs[ev] = q
    save_queries(qs)
    return qs


def top_queries_for(root_cause: str, k: int = 4) -> list[str]:
    """历史上对该根因真正有判别力的查询。

    按 power_for 排序而不是按出现次数 —— 后者会把"到处都用"错当成
    "对这个根因有用"。
    """
    qs = load_queries()
    total_success = sum(q.used_in_success for q in qs.values()) or 1
    rel = [(q, q.power_for(root_cause, total_success))
           for q in qs.values() if root_cause in q.root_causes]
    rel = [(q, p) for q, p in rel if p > 0]
    rel.sort(key=lambda x: -x[1])
    out, seen = [], set()
    for q, _ in rel:
        if q.tool not in seen:
            seen.add(q.tool)
            out.append(q.tool)
    return out[:k]


# ══ 统一入口 ════════════════════════════════════════════════

def learn(st, score, applied_sql: list[str], symptoms: list[str],
          truth: str | None = None) -> dict:
    """episode 结束时调一次，三层一起更新。"""
    out = {}
    try:
        pb = sediment_playbook(st, score, applied_sql)
        out["playbook"] = pb.root_cause if pb else None
    except Exception as exc:
        out["playbook_error"] = str(exc)[:120]
    try:
        learn_from_episode(st, score, symptoms)
        if truth:
            learn_truth(st.claimed_fault_class, truth, symptoms)
        out["delta"] = load_delta().prior_adj
    except Exception as exc:
        out["delta_error"] = str(exc)[:120]
    try:
        record_queries(st, score)
        out["queries"] = len(load_queries())
    except Exception as exc:
        out["query_error"] = str(exc)[:120]
    return out


def stats() -> dict:
    pbs, d, qs = load_playbooks(), load_delta(), load_queries()
    return {
        "playbooks": {k: {"成功": v.success_count, "失败": v.fail_count,
                          "置信": v.confidence, "步数": v.median_steps}
                      for k, v in pbs.items()},
        "prior_adj": d.prior_adj,
        "observed": d.observed,
        "queries": {k: {"判别力": v.discriminative_power,
                        "成功": v.used_in_success, "失败": v.used_in_failure}
                    for k, v in sorted(
                        qs.items(),
                        key=lambda x: -x[1].discriminative_power)},
    }


if __name__ == "__main__":
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
