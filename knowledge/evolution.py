"""自进化的 L2 / L3 / L4 —— 让学到的东西真正回流到决策依据。

三层各自负责一件事：
  L2  技能沉淀    把成功的取证顺序固化成 playbook，下次照着走
  L3  失败驱动    从结局回写因果图的先验；结构变更只提案不生效
  L4  查询库      沉淀真正有判别力的诊断查询

共同点是它们都**不训练模型**，改的是外部知识。这样自进化是可审计的：
YAML 落盘并进 git，这周学到了什么、哪条被推翻了，都能 diff 出来。

关键约束（和 L1 的红线一致）：这些先验只影响**假设的生成与排序**，
绝不放松证据要求。ESC 的 D1/D2 仍然照查 —— 学得再多也不能替代取证。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from agent.episode_state import evidence_is_observed
from agent.explanation import stable_id

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
    steps_samples: list = field(default_factory=list)   # 求真中位数用
    success_count: int = 0
    fail_count: int = 0
    # 在哪一版场景下学到的。场景改版后这条就不再成立，见 current_revisions()
    learned_under: int = 1
    updated_at: float = field(default_factory=time.time)

    @property
    def confidence(self) -> float:
        n = self.success_count + self.fail_count
        return round(self.success_count / n, 3) if n else 0.0


def current_revisions() -> dict[str, int]:
    """每个故障类当前的场景版本号。

    场景语义变了（判据、热查询、注入方式），在旧版本下学到的东西就不
    再成立。踩过的坑：负载生成器有 bug 时，系统忠实地学到了"锁竞争修
    不好"（fail_count=2 / success_count=0）—— 自进化会把环境的 bug 一并
    学进去，而且学得很认真。
    """
    revs: dict[str, int] = {}
    for f in (ROOT / "sandbox" / "scenarios").glob("*.yaml"):
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        fc = d.get("fault_class")
        if fc:
            revs[fc] = max(revs.get(fc, 1), int(d.get("revision", 1)))
    return revs


def _is_stale(root_cause: str, learned_under: int) -> bool:
    return learned_under < current_revisions().get(root_cause, 1)



def _pb_path() -> Path:
    LEARNED.mkdir(parents=True, exist_ok=True)
    return LEARNED / "playbooks.yaml"


def load_playbooks() -> dict[str, Playbook]:
    p = _pb_path()
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out = {}
    for k, v in raw.items():
        pb = Playbook(**v)
        if _is_stale(pb.root_cause, pb.learned_under):
            continue          # 场景已改版，这条经验作废而不是继续拿来用
        out[k] = pb
    return out


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
            if not evidence_is_observed(e):
                continue
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
            if evidence_is_observed(e)
            and e["evidence_type"] in ("explain_seq_scan", "index_existence",
                                      "lock_blocking_chain", "connection_count",
                                      "row_estimate_deviation",
                                      "dead_tuple_ratio")][:4]
        if applied_sql:
            pb.typical_fix = applied_sql[-1]
            pb.fix_action_type = st.proposal.get("action_type", "")
        # 原来写的是 (median_steps + steps) // 2 —— 那是滑动平均不是中位数，
        # 喂 [10,20,30,40] 会得到 31 而真中位数是 25。滑动平均对异常值敏感，
        # 一次跑飞的 episode 会把"典型步数"永久拉高，而这个数是要写进提示
        # 给模型当参考的。留最近 15 个样本算真中位数。
        steps = st.budget.get("steps", 0)
        pb.steps_samples = (list(pb.steps_samples) + [steps])[-15:]
        _s = sorted(pb.steps_samples)
        _n = len(_s)
        pb.median_steps = (_s[_n // 2] if _n % 2
                           else (_s[_n // 2 - 1] + _s[_n // 2]) // 2)
        pb.success_count += 1
    else:
        pb.fail_count += 1

    pb.learned_under = current_revisions().get(rc, 1)
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
    learned_under: dict = field(default_factory=dict)    # root_cause: 场景版本
    updated_at: float = field(default_factory=time.time)


def _delta_path() -> Path:
    LEARNED.mkdir(parents=True, exist_ok=True)
    return LEARNED / "graph_delta.yaml"


def _valid_symptom_ids() -> set:
    try:
        from knowledge.causal_graph import graph as _G
        g = _G.load()
        return {n for n, d in g.nodes(data=True) if d.get("kind") == "Symptom"}
    except Exception:
        return set()


def load_delta() -> GraphDelta:
    p = _delta_path()
    if not p.exists():
        return GraphDelta()
    d = GraphDelta(**(yaml.safe_load(p.read_text(encoding="utf-8")) or {}))
    for rc in [r for r in d.prior_adj
               if _is_stale(r, int(d.learned_under.get(r, 1)))]:
        # 先验调整是在已作废的场景下学的，退回手工种子图的值
        d.prior_adj.pop(rc, None)
        d.observed.pop(rc, None)
        d.learned_under.pop(rc, None)

    # 自愈：丢掉症状侧不是图节点 id 的历史脏键。修复前用人话串拼键，
    # 那些条目永远命不中，留着只会让人误以为学到了东西。
    ids = _valid_symptom_ids()
    if ids:
        d.likelihood_adj = {
            k: v for k, v in d.likelihood_adj.items()
            if "->" in k and k.split("->", 1)[1] in ids}
    return d


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


def _bump_edge(d: GraphDelta, rc: str, symptoms: list[str],
               amount: float) -> None:
    """调整 根因->症状 边的权重。

    这是**能推动候选排序**的那个通道：打分 score = w × (0.5 + prior)，
    边权 w 的跨度是 9.8×，而先验分量只有 1.63× —— 先验项结构上跨不过
    一条边的差距，所以学习必须落到边权上才有意义。

    双向：命中就加强，误诊就削弱。原来只有 learn_truth 会加、且只加不减，
    跑久了每条学过的边都饱和到上限，区分度归零。
    """
    from knowledge.causal_graph import graph as _G

    for sym in _G.map_symptoms(symptoms or [], fallback=False):
        key = f"{rc}->{sym}"
        cur = d.likelihood_adj.get(key, 0.0)
        d.likelihood_adj[key] = round(
            max(-MAX_ADJ, min(MAX_ADJ, cur + amount)), 4)


def _bump(d: GraphDelta, rc: str, amount: float) -> None:
    cap = _cap_for(rc)
    cur = d.prior_adj.get(rc, 0.0)
    d.prior_adj[rc] = round(max(-cap, min(cap, cur + amount)), 4)
    d.learned_under[rc] = current_revisions().get(rc, 1)


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
    # 先验通道推不动排序（边权跨度 9.8× 对先验分量 1.63×），所以同一个
    # 信号也要落到边权上。原来只有 learn_truth 写边权，而它开头就是
    # `if claimed == truth: return` —— 占绝大多数的正确诊断一条边都不更新，
    # likelihood_adj 因此一直是空的，L3 也就一直推不动排序。
    _bump_edge(d, claimed, symptoms, LR if hit else -LR)

    # 修复失败是比"诊断没中"更强的负信号：真按这个根因动手了还是没治好。
    # 但只算得上"可归因"的失败 —— 多根因场景里修一个、KPI 回不到基线，
    # 失败的原因是另一个故障还在，不是这个根因判错了。把这种也算进去，
    # 会和台账那条 bug 一样，反复压低一个其实正确的根因。
    for a in st.attempts:
        if not a.verdict.startswith("FAILED"):
            continue
        if not getattr(a, "counts_against_root_cause", True):
            continue
        _bump(d, a.root_cause, -LR / 2)

    d.updated_at = time.time()
    save_delta(d)
    return d


def learn_truth(claimed: str | None, truth: str, symptoms: list[str],
                penalize_claimed: bool = False) -> None:
    """已知正确答案时的定向修正（跑批场景里 ground truth 是已知的）。

    只在"误诊"时补一刀：把真凶升上来。命中的情况 learn_from_episode
    已经升过一次，这里再升就是同一次成功被计两遍。

    penalize_claimed 默认 False：正常路径是 learn() 先调
    learn_from_episode（诊断没中已经扣过 -LR），这里再扣就是同一次
    误诊被罚两遍 —— 而真凶只升一次，惩罚与奖励不对称，几轮下来会把
    被误认过的根因压得过低。只有单独调用本函数（不经 learn()）时才
    需要置 True。
    """
    d = load_delta()
    if claimed == truth:
        return          # 已由 learn_from_episode 处理
    if claimed and penalize_claimed:
        _bump(d, claimed, -LR)      # 错认的降
    _bump(d, truth, LR)             # 真凶升
    # 症状必须先归一到图上的节点 id。原来直接用 st.symptoms 的人话串
    # 拼键（"lock_contention->错误 5086"），数值烧进了键里，每个 episode
    # 都产生一个全新的键 —— 既累加不起来，下次也永远命不中。
    # 走同一个双向工具函数。原来这里是 min(MAX_ADJ, cur + LR) —— 只增不减，
    # 跑久了每条学过的边都会饱和到上限，那时这层信息的区分度就归零了。
    _bump_edge(d, truth, symptoms, LR)
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
    # 各根因上的统计分别是在哪一版场景下攒的
    learned_under: dict = field(default_factory=dict)

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
    out = {}
    for k, v in raw.items():
        qs = QueryStat(**v)
        # 只摘掉已改版根因上的计数，工具本身的历史不必整条丢弃
        for rc in [r for r in qs.root_causes
                   if _is_stale(r, int(qs.learned_under.get(r, 1)))]:
            qs.root_causes.pop(rc, None)
            qs.learned_under.pop(rc, None)
        out[k] = qs
    return out


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
    "stats_range_drift": "get_table_stats",
    "seq_scan_volume": "get_table_stats",
    "physical_bloat_ratio": "get_physical_bloat",
    "autovacuum_health": "get_table_stats",
    "lock_blocking_chain": "get_blocking_chain",
    "session_wait_profile": "get_active_sessions",
    "connection_count": "get_connection_stats",
    "idle_in_transaction": "get_connection_stats",
    "slow_query_ranking": "get_top_queries",
    "counterfactual_index": "simulate_index",
    "xid_age": "get_vacuum_horizon",
    "backend_xmin_age": "get_vacuum_horizon",
    "replication_slot_age": "get_vacuum_horizon",
    "prepared_xact_age": "get_vacuum_horizon",
    "deadlock_count": "get_database_stats",
    "temp_file_volume": "get_database_stats",
    "checkpoint_stats": "get_database_stats",
    "disk_usage": "get_database_stats",
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
    seen = {e["evidence_type"] for e in st.scratchpad
            if evidence_is_observed(e)}
    for ev in seen:
        tool = TOOL_OF.get(ev)
        if not tool:
            continue
        q = qs.get(ev) or QueryStat(evidence_type=ev, tool=tool)
        if solved:
            q.used_in_success += 1
            if ev in related:          # 只有相关的才计到该根因名下
                q.root_causes[rc] = q.root_causes.get(rc, 0) + 1
                q.learned_under[rc] = current_revisions().get(rc, 1)
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
    # 结构提案：只写候选文件，绝不生效。见 knowledge/structure.py
    try:
        from knowledge.structure import observe_episode
        out["structure"] = [p.pid for p in
                            observe_episode(st, score, symptoms, truth)]
    except Exception as exc:
        out["structure_error"] = str(exc)[:120]
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


# ===========================================================================
# v2 learning.  These stores are deliberately separate from every v1 loader
# above.  There is no implicit migration or fallback path.

V2_TOOL_SCHEMA_VERSION = 2
V2_MIN_SAMPLES = 3
V2_L3_LR = 0.04
V2_L3_EDGE_RELATIVE_CAP = 0.40
V2_L3_PATH_RELATIVE_CAP = 0.25


def _v2_dir() -> Path:
    return LEARNED / "v2"


def _v2_path(name: str) -> Path:
    return _v2_dir() / name


def _load_v2_doc(name: str, default: dict) -> dict:
    path = _v2_path(name)
    if not path.exists():
        return {"schema_version": 2, **default}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if int(raw.get("schema_version", 0)) != 2:
        return {"schema_version": 2, **default}
    return raw


def _save_v2_doc(name: str, value: dict) -> None:
    _v2_dir().mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 2, **{
        key: item for key, item in value.items() if key != "schema_version"}}
    _v2_path(name).write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=True),
        encoding="utf-8")


@dataclass
class ConditionalDecisionStat:
    decision_key: str
    frontier_signature: str
    evidence_state_signature: str
    p0_signature: str
    capability_signature: str
    evidence_need_signature: str
    tool: str
    graph_version: str
    scenario_revision: int
    tool_schema_version: int = V2_TOOL_SCHEMA_VERSION
    calls: int = 0
    observed: int = 0
    unknown: int = 0
    error: int = 0
    useful_steps: int = 0
    pruned_path_total: int = 0
    required_fulfilled_total: int = 0
    changed_decision_total: int = 0
    cost_total: float = 0.0
    reward_total: float = 0.0
    observation_ids: list[str] = field(default_factory=list)
    stale: bool = False
    updated_at: float = field(default_factory=time.time)


@dataclass
class ToolInformationStat:
    stat_key: str
    frontier_signature: str
    evidence_need_signature: str
    tool: str
    graph_version: str
    scenario_revision: int
    tool_schema_version: int = V2_TOOL_SCHEMA_VERSION
    calls: int = 0
    observed: int = 0
    unknown: int = 0
    error: int = 0
    latency_total_s: float = 0.0
    covered_need_total: int = 0
    pruned_path_total: int = 0
    entropy_gain_total: float = 0.0
    posterior_change_total: float = 0.0
    changed_decision_total: int = 0
    duplicate_calls: int = 0
    observation_ids: list[str] = field(default_factory=list)
    stale: bool = False
    updated_at: float = field(default_factory=time.time)


def v2_context_signatures(explanation, need,
                          environment_tools: set[str] | list[str], *,
                          scenario_revision: int = 1,
                          tool_schema_version: int = V2_TOOL_SCHEMA_VERSION
                          ) -> dict[str, str | int]:
    """Build replayable L2/L4 keys from causal state, not natural language."""
    paths = explanation.path_map()
    relevant = [paths[path_id] for path_id in need.path_ids
                if path_id in paths]
    relevant_nodes = sorted({node for path in relevant for node in path.node_ids})
    relevant_edges = sorted({edge for path in relevant for edge in path.edge_ids})
    evidence_state = {
        "nodes": {node: explanation.node_status.get(node, "UNTESTED")
                  for node in relevant_nodes},
        "edges": {edge: explanation.edge_status.get(edge, "UNTESTED")
                  for edge in relevant_edges},
    }
    p0_state = {cause_id: obligation.status for cause_id, obligation in
                sorted(explanation.p0_obligations.items())
                if set(obligation.reachable_path_ids) & set(need.path_ids) or
                cause_id in need.target_ids}
    frontier = {
        "path_ids": sorted(need.path_ids),
        "target_kind": need.target_kind,
        "target_ids": sorted(need.target_ids),
        "viable_path_ids": sorted(
            path.path_id for path in relevant if path.status != "REFUTED"),
    }
    need_shape = {
        "target_kind": need.target_kind,
        "target_ids": sorted(need.target_ids),
        "evidence_type": need.evidence_type,
        "predicate_id": need.predicate_id,
        "required": bool(need.required),
    }
    return {
        "frontier_signature": stable_id("frontier", frontier),
        "evidence_state_signature": stable_id("evidence_state", evidence_state),
        "p0_signature": stable_id("p0_state", p0_state),
        "capability_signature": stable_id(
            "capabilities", sorted(set(environment_tools))),
        "evidence_need_signature": stable_id("evidence_need", need_shape),
        "graph_version": explanation.graph_version,
        "scenario_revision": int(scenario_revision),
        "tool_schema_version": int(tool_schema_version),
    }


def _active_v2_record(record: dict, context: dict) -> bool:
    return bool(
        not record.get("stale", False) and
        record.get("graph_version") == context.get("graph_version") and
        int(record.get("scenario_revision", 1)) ==
        int(context.get("scenario_revision", 1)) and
        int(record.get("tool_schema_version", 0)) ==
        int(context.get("tool_schema_version", V2_TOOL_SCHEMA_VERSION)))


def _l2_decision_key(context: dict, tool: str) -> str:
    return stable_id("l2_decision", {
        **{key: context[key] for key in (
            "frontier_signature", "evidence_state_signature", "p0_signature",
            "capability_signature", "evidence_need_signature",
            "graph_version", "scenario_revision", "tool_schema_version")},
        "tool": tool,
    })


def _l4_stat_key(context: dict, tool: str) -> str:
    return stable_id("l4_tool", {
        "frontier_signature": context["frontier_signature"],
        "evidence_need_signature": context["evidence_need_signature"],
        "tool": tool,
        "graph_version": context["graph_version"],
        "scenario_revision": context["scenario_revision"],
        "tool_schema_version": context["tool_schema_version"],
    })


def v2_tool_learning_components(context: dict, tool: str, *,
                                l2_cap: float = 0.75,
                                l4_cap: float = 0.75,
                                use_learned: bool = True
                                ) -> tuple[float, float, int]:
    """Return shrunken L2/L4 utilities for one exact frontier/need/tool key."""
    if not use_learned:
        return 0.0, 0.0, 0
    l2_doc = _load_v2_doc("investigation_policy.yaml", {"records": {}})
    l4_doc = _load_v2_doc("tool_information_gain.yaml", {"records": {}})
    decision_key = _l2_decision_key(context, tool)
    stat_key = _l4_stat_key(context, tool)
    l2_record = (l2_doc.get("records") or {}).get(decision_key)
    l4_record = (l4_doc.get("records") or {}).get(stat_key)
    l2_score = 0.0
    l4_score = 0.0
    samples = 0
    if l2_record and _active_v2_record(l2_record, context):
        calls = max(0, int(l2_record.get("calls", 0)))
        samples = max(samples, calls)
        if calls >= V2_MIN_SAMPLES:
            shrink = calls / (calls + V2_MIN_SAMPLES)
            empirical = float(l2_record.get("reward_total", 0.0)) / calls
            l2_score = max(-l2_cap, min(l2_cap, empirical * shrink))
    if l4_record and _active_v2_record(l4_record, context):
        calls = max(0, int(l4_record.get("calls", 0)))
        samples = max(samples, calls)
        if calls >= V2_MIN_SAMPLES:
            shrink = calls / (calls + V2_MIN_SAMPLES)
            info = (float(l4_record.get("entropy_gain_total", 0.0)) +
                    float(l4_record.get("posterior_change_total", 0.0)) +
                    float(l4_record.get("changed_decision_total", 0))) / calls
            latency = float(l4_record.get("latency_total_s", 0.0)) / calls
            bad_rate = (float(l4_record.get("unknown", 0)) +
                        float(l4_record.get("error", 0)) +
                        float(l4_record.get("duplicate_calls", 0))) / calls
            empirical = info / (1.0 + latency) - bad_rate
            # The zero-centered aggregate prior plus shrinkage prevents one
            # lucky low-sample call from monopolising a frontier.
            l4_score = max(-l4_cap, min(l4_cap, empirical * shrink))
    return round(l2_score, 6), round(l4_score, 6), samples


def _observation_is_useful(item: dict) -> bool:
    return bool(
        int(item.get("changed_statuses", 0)) > 0 or
        int(item.get("pruned_paths", 0)) > 0 or
        item.get("required_fulfilled") or
        item.get("changed_next_decision"))


def _update_v2_tool_learning(
        observations: list[dict], *, use_l2: bool = True,
        use_l4: bool = True) -> tuple[int, int]:
    l2_doc = _load_v2_doc("investigation_policy.yaml", {"records": {}})
    l4_doc = _load_v2_doc("tool_information_gain.yaml", {"records": {}})
    l2_records = l2_doc.setdefault("records", {})
    l4_records = l4_doc.setdefault("records", {})
    l2_updates = 0
    l4_updates = 0
    for item in observations:
        context = dict(item.get("learning_context") or {})
        tool = str(item.get("tool") or "")
        observation_id = str(item.get("observation_id") or "")
        if not tool or not observation_id or not context:
            continue
        status = str(item.get("collection_status") or "UNKNOWN")
        useful = _observation_is_useful(item)
        decision_key = _l2_decision_key(context, tool)
        if useful and use_l2:
            raw = l2_records.get(decision_key) or asdict(ConditionalDecisionStat(
                decision_key=decision_key,
                frontier_signature=context["frontier_signature"],
                evidence_state_signature=context["evidence_state_signature"],
                p0_signature=context["p0_signature"],
                capability_signature=context["capability_signature"],
                evidence_need_signature=context["evidence_need_signature"],
                tool=tool,
                graph_version=context["graph_version"],
                scenario_revision=int(context.get("scenario_revision", 1)),
                tool_schema_version=int(context.get(
                    "tool_schema_version", V2_TOOL_SCHEMA_VERSION)),
            ))
            if observation_id not in raw.get("observation_ids", []):
                raw["calls"] += 1
                raw[status.lower()] = int(raw.get(status.lower(), 0)) + 1
                raw["useful_steps"] += 1
                raw["pruned_path_total"] += int(item.get("pruned_paths", 0))
                raw["required_fulfilled_total"] += int(bool(
                    item.get("required_fulfilled")))
                raw["changed_decision_total"] += int(bool(
                    item.get("changed_next_decision")))
                raw["cost_total"] = round(float(raw.get("cost_total", 0.0)) +
                                           float(item.get("cost", 0.0)), 6)
                reward = (0.25 * int(item.get("changed_statuses", 0)) +
                          0.75 * int(item.get("pruned_paths", 0)) +
                          0.5 * int(bool(item.get("required_fulfilled"))) +
                          0.25 * int(bool(item.get("changed_next_decision"))) -
                          0.1 * float(item.get("cost", 0.0)))
                raw["reward_total"] = round(
                    float(raw.get("reward_total", 0.0)) + reward, 6)
                raw.setdefault("observation_ids", []).append(observation_id)
                raw["updated_at"] = time.time()
                l2_records[decision_key] = raw
                l2_updates += 1

        if not use_l4:
            continue
        stat_key = _l4_stat_key(context, tool)
        raw = l4_records.get(stat_key) or asdict(ToolInformationStat(
            stat_key=stat_key,
            frontier_signature=context["frontier_signature"],
            evidence_need_signature=context["evidence_need_signature"],
            tool=tool,
            graph_version=context["graph_version"],
            scenario_revision=int(context.get("scenario_revision", 1)),
            tool_schema_version=int(context.get(
                "tool_schema_version", V2_TOOL_SCHEMA_VERSION)),
        ))
        if observation_id in raw.get("observation_ids", []):
            continue
        raw["calls"] += 1
        raw[status.lower()] = int(raw.get(status.lower(), 0)) + 1
        raw["latency_total_s"] = round(
            float(raw.get("latency_total_s", 0.0)) +
            float(item.get("latency_s", 0.0)), 6)
        raw["covered_need_total"] += int(item.get("covered_need_count", 1))
        raw["pruned_path_total"] += int(item.get("pruned_paths", 0))
        raw["entropy_gain_total"] = round(
            float(raw.get("entropy_gain_total", 0.0)) +
            float(item.get("entropy_gain", 0.0)), 6)
        raw["posterior_change_total"] = round(
            float(raw.get("posterior_change_total", 0.0)) +
            float(item.get("posterior_change", 0.0)), 6)
        raw["changed_decision_total"] += int(bool(
            item.get("changed_next_decision")))
        raw["duplicate_calls"] += int(item.get("duplicate_calls", 0))
        raw.setdefault("observation_ids", []).append(observation_id)
        raw["updated_at"] = time.time()
        l4_records[stat_key] = raw
        l4_updates += 1
    if l2_updates:
        _save_v2_doc("investigation_policy.yaml", l2_doc)
    if l4_updates:
        _save_v2_doc("tool_information_gain.yaml", l4_doc)
    return l2_updates, l4_updates


def load_l3_v2_adjustments(graph_version: str | None = None
                           ) -> tuple[dict[str, float], dict[str, float]]:
    """Load live stable-ID edge/path adjustments for one graph version."""
    if graph_version is None:
        try:
            from knowledge.causal_graph.graph import graph_version as current
            graph_version = current()
        except Exception:
            return {}, {}
    doc = _load_v2_doc("causal_weights.yaml", {
        "processed_outcomes": [], "edge_stats": {}, "path_stats": {}})
    edge = {key: float(value.get("adjustment", 0.0))
            for key, value in (doc.get("edge_stats") or {}).items()
            if value.get("graph_version") == graph_version and
            not value.get("stale", False)}
    path = {key: float(value.get("adjustment", 0.0))
            for key, value in (doc.get("path_stats") or {}).items()
            if value.get("graph_version") == graph_version and
            not value.get("stale", False)}
    return edge, path


def _edge_manual_likelihood(edge_id: str) -> float:
    from knowledge.causal_graph import graph as causal_graph
    graph = causal_graph.load()
    for _src, _dst, key, data in graph.edges(keys=True, data=True):
        if key == "CAUSES" and data.get("edge_id") == edge_id:
            return float(data.get("likelihood", 0.5))
    return 0.0


def _bump_l3_record(records: dict, stable_key: str, *, amount: float,
                    manual_weight: float, relative_cap: float,
                    graph_version: str, outcome_id: str) -> None:
    if manual_weight <= 0:
        return
    raw = records.get(stable_key) or {
        "stable_id": stable_key,
        "graph_version": graph_version,
        "manual_weight": manual_weight,
        "positive": 0,
        "negative": 0,
        "adjustment": 0.0,
        "outcome_ids": [],
        "stale": False,
    }
    if outcome_id in raw.get("outcome_ids", []):
        return
    cap = manual_weight * relative_cap
    raw["adjustment"] = round(max(
        -cap, min(cap, float(raw.get("adjustment", 0.0)) + amount)), 8)
    raw["positive" if amount > 0 else "negative"] += 1
    raw.setdefault("outcome_ids", []).append(outcome_id)
    raw["updated_at"] = time.time()
    records[stable_key] = raw


def _update_l3_v2(st, score) -> int:
    explanation = getattr(st, "explanation_graph", None)
    if explanation is None or not explanation.selected_path_ids:
        return 0
    sufficient = any(report.get("verdict") == "SUFFICIENT"
                     for report in getattr(st, "esc_reports", []))
    verified = any(
        attempt.learnable and attempt.outcome == "VERIFIED"
        for attempt in getattr(st, "intervention_attempts", []))
    selected_supported = all(
        explanation.path_map()[path_id].status == "SUPPORTED"
        for path_id in explanation.selected_path_ids
        if path_id in explanation.path_map())
    positive = bool(sufficient and verified and selected_supported and
                    not explanation.unresolved_p0_paths() and
                    score.diagnosis and score.outcome and score.safe_pass)
    negative_attempts = [attempt for attempt in
                         getattr(st, "intervention_attempts", [])
                         if attempt.learnable and
                         attempt.failure_scope == "PATH_SEGMENT"]
    if not positive and not negative_attempts:
        return 0
    doc = _load_v2_doc("causal_weights.yaml", {
        "processed_outcomes": [], "edge_stats": {}, "path_stats": {}})
    path_map = explanation.path_map()
    updates = 0
    outcome_id = stable_id("l3_outcome", {
        "episode_id": st.episode_id,
        "explanation_id": explanation.explanation_id,
        "selected_path_ids": explanation.selected_path_ids,
        "attempts": [{"id": attempt.attempt_id,
                      "outcome": attempt.outcome,
                      "scope": attempt.failure_scope}
                     for attempt in getattr(st, "intervention_attempts", [])],
    })
    if outcome_id in doc.get("processed_outcomes", []):
        return 0
    edge_stats = doc.setdefault("edge_stats", {})
    path_stats = doc.setdefault("path_stats", {})

    def update_path(path_id: str, sign: float,
                    edge_subset: set[str] | None = None) -> None:
        nonlocal updates
        path = path_map.get(path_id)
        if path is None:
            return
        weights = [_edge_manual_likelihood(edge_id)
                   for edge_id in path.edge_ids]
        if not weights or any(weight <= 0 for weight in weights):
            return
        if edge_subset is None:
            _bump_l3_record(
                path_stats, path.path_id, amount=sign * V2_L3_LR,
                manual_weight=math.prod(weights),
                relative_cap=V2_L3_PATH_RELATIVE_CAP,
                graph_version=explanation.graph_version,
                outcome_id=outcome_id)
            updates += 1
        for edge_id, weight in zip(path.edge_ids, weights):
            if edge_subset is not None and edge_id not in edge_subset:
                continue
            _bump_l3_record(
                edge_stats, edge_id, amount=sign * V2_L3_LR,
                manual_weight=weight,
                relative_cap=V2_L3_EDGE_RELATIVE_CAP,
                graph_version=explanation.graph_version,
                outcome_id=outcome_id)
            updates += 1

    if positive:
        for path_id in explanation.selected_path_ids:
            update_path(path_id, 1.0)
    for attempt in negative_attempts:
        affected = set(attempt.affected_edge_ids)
        update_path(attempt.selected_path_id, -1.0,
                    edge_subset=affected)
    doc.setdefault("processed_outcomes", []).append(outcome_id)
    _save_v2_doc("causal_weights.yaml", doc)
    return updates


def mark_v2_stale(*, graph_version: str | None = None,
                  scenario_revision: int | None = None,
                  tool_schema_version: int | None = None) -> int:
    """Mark incompatible L2/L4 records stale without deleting audit history."""
    changed = 0
    for filename in ("investigation_policy.yaml",
                     "tool_information_gain.yaml"):
        doc = _load_v2_doc(filename, {"records": {}})
        file_changed = False
        for record in (doc.get("records") or {}).values():
            mismatch = (
                (graph_version is not None and
                 record.get("graph_version") != graph_version) or
                (scenario_revision is not None and
                 int(record.get("scenario_revision", 1)) != scenario_revision) or
                (tool_schema_version is not None and
                 int(record.get("tool_schema_version", 0)) !=
                 tool_schema_version))
            if mismatch and not record.get("stale", False):
                record["stale"] = True
                changed += 1
                file_changed = True
        if file_changed:
            _save_v2_doc(filename, doc)
    return changed


def learn_v2(st, score, *, split: str = "train",
             provenance: str = "sandbox",
             enabled_layers: set[str] | None = None) -> dict:
    """Idempotently update L2-L4 from one v2 episode.

    Eval input is rejected before any file is opened for writing.  L2/L4 only
    consume need-bound audit events; arbitrary scratchpad co-occurrence is not
    a learning source.
    """
    layers = ({"l2", "l3", "l4"} if enabled_layers is None else
              {str(layer).lower() for layer in enabled_layers})
    unknown = layers - {"l2", "l3", "l4"}
    if unknown:
        raise ValueError(f"unsupported v2 learning layers: {sorted(unknown)}")
    if split == "eval":
        return {"written": False, "reason": "eval provenance excluded",
                "l2": 0, "l3": 0, "l4": 0}
    if provenance not in {"sandbox", "production", "human_labeled"}:
        raise ValueError("unsupported v2 learning provenance")
    explanation = getattr(st, "explanation_graph", None)
    if explanation is not None:
        mark_v2_stale(
            graph_version=explanation.graph_version,
            scenario_revision=int((getattr(st, "incident_window", {}) or {}).get(
                "scenario_revision", 1)),
            tool_schema_version=V2_TOOL_SCHEMA_VERSION)
    observations = [item for item in getattr(st, "evidence_task_audit", [])
                    if item.get("event") == "tool_learning_observation"]
    l2_updates, l4_updates = _update_v2_tool_learning(
        observations, use_l2="l2" in layers, use_l4="l4" in layers)
    l3_updates = _update_l3_v2(st, score) if "l3" in layers else 0
    return {
        "written": bool(l2_updates or l3_updates or l4_updates),
        "l2": l2_updates,
        "l3": l3_updates,
        "l4": l4_updates,
        "provenance": provenance,
    }


def stats_v2() -> dict:
    l2 = _load_v2_doc("investigation_policy.yaml", {"records": {}})
    l3 = _load_v2_doc("causal_weights.yaml", {
        "processed_outcomes": [], "edge_stats": {}, "path_stats": {}})
    l4 = _load_v2_doc("tool_information_gain.yaml", {"records": {}})
    return {
        "l2_records": len(l2.get("records") or {}),
        "l3_edges": len(l3.get("edge_stats") or {}),
        "l3_paths": len(l3.get("path_stats") or {}),
        "l4_records": len(l4.get("records") or {}),
        "processed_outcomes": len(l3.get("processed_outcomes") or []),
    }


if __name__ == "__main__":
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
