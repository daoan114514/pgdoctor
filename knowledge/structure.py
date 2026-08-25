"""结构提案 —— 让 L3 能提出因果图的结构变更，但绝不自己生效。

L3 此前只调权重不改结构，这是刻意的：一条错的 CAUSES 边会系统性地毒化
所有相关诊断，而且很难追溯到是哪条边的问题。但"干脆不让它改"是偷懒不是
设计 —— 代价是自进化的天花板被手写种子图锁死：先验调整上限是基础先验的
50%，实测跑完十几个 episode 后四类故障里三类已撞顶，候选排序一个位置都
没动过。学得再多，排序还是那个排序。

正确的做法是把这个项目在数据库那一侧已经用熟的模式搬到知识层：
**机器提案，人审批，人担责**。

    candidate_edges.yaml     机器写；graph.py 永不读取
          │  人 review（python3 -m knowledge.structure）
          ▼
    promoted_edges.yaml      人 promote 之后才进生效路径，与种子图分开存

分成两个文件而不是直接改 edges.yaml，理由和 graph_delta 的 overlay 一致：
种子图是手写的 ground truth，混在一起就分不清哪些是人写的、哪些是学来的，
出问题也没法单独回滚。而且 edges.yaml 里的注释是知识的一部分，程序化改写
会把它们全部抹掉。

三条硬禁 —— 是禁止，不是阈值，调参数也绕不过去：

  · 绝不提案 REFUTED_BY。一条错的排除规则会永久杀掉正确假设，而且是
    静默的：被排除的根因根本不会再进候选集，你连它被排除过都看不见。
    这是所有边类型里最危险的一种。
  · 绝不提案 necessity=required。那等于让系统学会给自己降标准 ——
    学到的证据关系一律只能是 supporting，ESC 的 D1 该查什么还查什么。
  · 只在根因确实正确时才观察。拿误诊的 episode 长边，等于把噪声写成常识。
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LEARNED = ROOT / "knowledge" / "learned"
CANDIDATES = LEARNED / "candidate_edges.yaml"
PROMOTED = ROOT / "knowledge" / "causal_graph" / "promoted_edges.yaml"

# 允许被提案的边类型。REFUTED_BY / DISCRIMINATES 不在其中，且没有开关。
PROPOSABLE = ("CAUSES", "CONFIRMED_BY")

# 进入待审列表的门槛。低于这个数的提案留在文件里累计，但不打扰人。
MIN_SUPPORT = 3


@dataclass
class EdgeProposal:
    """一条候选边。support/contradict 是累计观测，不是置信度。"""
    pid: str
    kind: str                 # CAUSES | CONFIRMED_BY
    src: str                  # 根因
    dst: str                  # 症状 or 证据
    support: int = 0
    contradict: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    status: str = "proposed"  # proposed | promoted | rejected
    note: str = ""

    @property
    def ready(self) -> bool:
        return (self.status == "proposed"
                and self.support >= MIN_SUPPORT
                and self.support > self.contradict)


def _pid(kind: str, src: str, dst: str) -> str:
    return f"{kind}:{src}->{dst}"


def load_candidates() -> dict[str, EdgeProposal]:
    if not CANDIDATES.exists():
        return {}
    raw = yaml.safe_load(CANDIDATES.read_text(encoding="utf-8")) or {}
    return {k: EdgeProposal(**v) for k, v in raw.items()}


def save_candidates(ps: dict[str, EdgeProposal]) -> None:
    LEARNED.mkdir(parents=True, exist_ok=True)
    CANDIDATES.write_text(
        yaml.safe_dump({k: asdict(v) for k, v in ps.items()},
                       allow_unicode=True, sort_keys=True),
        encoding="utf-8")


# ── 观察：只记录，绝不生效 ────────────────────────────────────

def _truth_of(st, score, truth: str | None) -> str | None:
    """只有确知根因正确时才返回它，否则返回 None（本次不观察）。

    这是防噪声的第一道也是最重要的一道：误诊的 episode 里，症状和证据
    与"被错认的那个根因"之间不存在真实因果关系，照单全收就是在把巧合
    写成知识。
    """
    if truth:
        return truth if st.claimed_fault_class == truth else None
    return st.claimed_fault_class if getattr(score, "diagnosis", False) else None


def observe_episode(st, score, symptoms: list[str],
                    truth: str | None = None) -> list[EdgeProposal]:
    """从一个 episode 观察可能缺失的边。只写候选文件，不碰生效路径。"""
    rc = _truth_of(st, score, truth)
    if not rc:
        return []

    from knowledge.causal_graph import graph as _G
    g = _G.load()
    if rc not in g:
        return []

    ps = load_candidates()
    touched: list[EdgeProposal] = []
    now = time.time()

    def bump(kind: str, dst: str, hit: bool, note: str = "") -> None:
        pid = _pid(kind, rc, dst)
        p = ps.get(pid) or EdgeProposal(pid=pid, kind=kind, src=rc, dst=dst,
                                        note=note)
        if p.status != "proposed":       # 已 promote / reject 的不再累计
            return
        if hit:
            p.support += 1
        else:
            p.contradict += 1
        p.last_seen = now
        ps[pid] = p
        touched.append(p)

    # ① CAUSES：观测到的症状里，图上说这个根因解释不了的那些。
    #    信号源正是 ESC 的 D3 —— 它本来就在算孤儿症状，之前只用来提示
    #    "可能存在第二个故障"，现在同一个信号顺带喂给结构学习。
    explains = set(_G.symptoms_of(rc))
    observed = set(_G.map_symptoms(symptoms))
    for s in observed - explains:
        bump("CAUSES", s, True, "D3 反复报的孤儿症状")
    for s in explains - observed:
        # 图上说会导致、实际却没出现 —— 对既有边的反证，同样值得记
        pid = _pid("CAUSES", rc, s)
        if pid in ps:
            bump("CAUSES", s, False)

    # ② CONFIRMED_BY：轨迹里出现、但图上与该根因无关的证据类型。
    #    只可能提案 supporting，required 由人写，永远不由学习产生。
    linked = (set(_G.required_evidence(rc)) | set(_G.supporting_evidence(rc))
              | {r["evidence"] for r in _G.refuting_evidence(rc)})
    seen_ev = {e["evidence_type"] for e in st.scratchpad}
    for ev in seen_ev - linked:
        if g.nodes.get(ev, {}).get("kind") != "Evidence":
            continue                      # 只认图上已有的证据节点
        bump("CONFIRMED_BY", ev, True, "成功 episode 里反复出现但图上无边")

    if touched:
        save_candidates(ps)
    return touched


# ── 审批：只有人能调 ──────────────────────────────────────────

def load_promoted() -> dict:
    """graph.load() 读这个。候选文件永远不在这条路径上。"""
    if not PROMOTED.exists():
        return {}
    return yaml.safe_load(PROMOTED.read_text(encoding="utf-8")) or {}


def _save_promoted(d: dict) -> None:
    PROMOTED.write_text(
        "# 人工审批通过的学习边。与手写种子图 edges.yaml 分开存，\n"
        "# 以便任何时候都能区分'人写的 ground truth'与'学来的'，\n"
        "# 也便于单独回滚。由 knowledge/structure.py promote() 写入。\n"
        + yaml.safe_dump(d, allow_unicode=True, sort_keys=True),
        encoding="utf-8")


def pending() -> list[EdgeProposal]:
    """够格进人工审批队列的提案。"""
    return sorted((p for p in load_candidates().values() if p.ready),
                  key=lambda p: -(p.support - p.contradict))


def promote(pid: str, by: str = "human", likelihood: float = 0.5) -> tuple[bool, str]:
    """把一条候选边放进生效路径。这个函数只应该由人调用。"""
    ps = load_candidates()
    p = ps.get(pid)
    if not p:
        return False, f"没有这条提案: {pid}"
    if p.status != "proposed":
        return False, f"该提案已是 {p.status}"
    if p.kind not in PROPOSABLE:
        return False, f"{p.kind} 不允许由学习产生"
    if not p.ready:
        return False, (f"证据不足: support={p.support} contradict={p.contradict}"
                       f"（需 support>={MIN_SUPPORT} 且 > contradict）")

    d = load_promoted()
    if p.kind == "CAUSES":
        d.setdefault("causes_symptom", []).append(
            {"from": p.src, "to": p.dst, "likelihood": likelihood,
             "provenance": f"learned/{by}@{int(time.time())}",
             "support": p.support, "contradict": p.contradict})
    else:
        # 永远 supporting。这里不接受 necessity 参数，就是为了让"学出一条
        # required 边"在代码里根本没有入口。
        d.setdefault("confirmed_by", []).append(
            {"cause": p.src, "evidence": p.dst, "necessity": "supporting",
             "provenance": f"learned/{by}@{int(time.time())}",
             "support": p.support, "contradict": p.contradict})
    _save_promoted(d)

    p.status = "promoted"
    ps[pid] = p
    save_candidates(ps)

    from knowledge.causal_graph import graph as _G
    _G.load.cache_clear()        # 图是 lru_cache 的，不清就还是旧的
    return True, f"已并入 promoted_edges.yaml: {pid}"


def reject(pid: str, why: str = "") -> tuple[bool, str]:
    ps = load_candidates()
    p = ps.get(pid)
    if not p:
        return False, f"没有这条提案: {pid}"
    p.status = "rejected"
    p.note = (p.note + f" | 驳回: {why}").strip(" |")
    ps[pid] = p
    save_candidates(ps)
    return True, f"已驳回: {pid}"


def stats() -> dict:
    ps = load_candidates()
    return {
        "total": len(ps),
        "proposed": sum(1 for p in ps.values() if p.status == "proposed"),
        "ready": sum(1 for p in ps.values() if p.ready),
        "promoted": sum(1 for p in ps.values() if p.status == "promoted"),
        "rejected": sum(1 for p in ps.values() if p.status == "rejected"),
    }


# ── 人工审批用的小 CLI ────────────────────────────────────────

if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    if argv and argv[0] == "promote":
        ok, msg = promote(argv[1], by=(argv[2] if len(argv) > 2 else "human"))
        print(("OK  " if ok else "拒绝  ") + msg)
    elif argv and argv[0] == "reject":
        ok, msg = reject(argv[1], " ".join(argv[2:]))
        print(("OK  " if ok else "拒绝  ") + msg)
    else:
        print(f"库状态: {stats()}\n")
        q = pending()
        if not q:
            print("没有够格待审的提案。")
            allp = load_candidates()
            if allp:
                print(f"\n累计中（未达 support>={MIN_SUPPORT}）:")
                for p in sorted(allp.values(), key=lambda x: -x.support)[:10]:
                    print(f"  {p.status:<9} +{p.support}/-{p.contradict}  "
                          f"{p.pid}")
        else:
            print(f"待审 {len(q)} 条 —— 只有你 promote 之后才会生效:\n")
            for p in q:
                print(f"  {p.pid}")
                print(f"     支持 {p.support} 次 / 反证 {p.contradict} 次"
                      f"   {p.note}")
                print(f"     通过: python3 -m knowledge.structure promote "
                      f"'{p.pid}'")
                print(f"     驳回: python3 -m knowledge.structure reject "
                      f"'{p.pid}' 理由\n")
