"""L3 的学习信号必须落到能推动排序的那个通道。

消融测出 L3 对候选排序毫无影响（平均名次 2.42 -> 2.42）。原因不是数据少：

  打分 score = w × (0.5 + prior)
    边权 w 跨度 9.8×        <- 能推动排序
    先验分量跨度 1.63×       <- 推不动，学习顶到上限也才 1.52×

  而两个学习通道的分工恰好反了：
    prior_adj       每个 episode 都写，但推不动
    likelihood_adj  能推动，却只有 learn_truth 在写，
                    而它开头就是 `if claimed == truth: return`

于是占绝大多数的**正确**诊断，对有用的那个通道贡献为零。还原后的真实
学习状态正是这个形态：lock_contention 命中 5 次、stale_statistics 命中
4 次，全对，而 likelihood_adj 是 0 条。

这个测试钉住：正确诊断也要写边权，且边权调整必须双向（只增不减的话，
跑久了每条学过的边都饱和到上限，区分度归零）。
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState, Verdict
from knowledge import evolution as ev
from knowledge.causal_graph import graph as G

ROOT = Path(__file__).resolve().parent.parent
LEARNED = ROOT / "knowledge/learned"

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}   {detail}")
    if not cond:
        fails.append(name)


class Score:
    def __init__(self, diag):
        self.diagnosis = diag
        self.outcome = False
        self.safe_pass = False


def mk(claimed, symptoms):
    st = EpisodeState(episode_id="l3chk", scenario_id="x")
    st.symptoms = list(symptoms)
    st.claimed_fault_class = claimed
    st.set_verdict(claimed, Verdict.CONFIRMED, note="受控用例：依据齐备")
    return st


# 用临时目录跑，绝不碰真实学习状态
tmp = LEARNED.parent / "_l3chk"
if tmp.exists():
    shutil.rmtree(tmp)
tmp.mkdir(parents=True)
orig = ev.LEARNED
ev.LEARNED = tmp

try:
    SYM = ["p99 延迟上升", "错误 120"]
    mapped = G.map_symptoms(SYM, fallback=False)
    print(f"症状归一到 {mapped}\n")

    print("[1] 正确诊断必须写边权（原来只有误诊才写）")
    for _ in range(5):
        ev.learn_from_episode(mk("lock_contention", SYM), Score(True), SYM)
    d = ev.load_delta()
    keys = [k for k in d.likelihood_adj if k.startswith("lock_contention->")]
    check("正确诊断产生了边权调整", bool(keys), f"{keys}")
    check("先验通道也有（原有行为不变）",
          d.prior_adj.get("lock_contention", 0) > 0,
          f"prior_adj={d.prior_adj}")

    print("\n[2] 边权调整必须双向（原来只增不减会饱和）")
    before = dict(ev.load_delta().likelihood_adj)
    for _ in range(8):
        ev.learn_from_episode(mk("lock_contention", SYM), Score(False), SYM)
    after = ev.load_delta().likelihood_adj
    dropped = [k for k in before if after.get(k, 0) < before[k]]
    check("误诊会把边权调低", bool(dropped), f"下调了 {dropped}")

    print("\n[3] 调整量有上下限，学习不能让一条边彻底消失")
    for _ in range(50):
        ev.learn_from_episode(mk("lock_contention", SYM), Score(False), SYM)
    d = ev.load_delta()
    vals = [v for k, v in d.likelihood_adj.items()
            if k.startswith("lock_contention->")]
    check("边权调整夹在 ±MAX_ADJ 内",
          all(abs(v) <= ev.MAX_ADJ + 1e-9 for v in vals),
          f"取值 {vals}，上限 ±{ev.MAX_ADJ}")
    # 夹住之后候选生成里还会再夹一次到 (0.01, 0.99)，保证边不会归零
    cands = G.candidate_causes(mapped, top_k=99, use_learned=True)
    check("被压过的根因仍在候选集里",
          "lock_contention" in [c["root_cause"] for c in cands],
          "学习不该让某个根因彻底进不了候选")

    print("\n[4] 边权通道确实能推动排序（先验通道推不动）")
    shutil.rmtree(tmp)
    tmp.mkdir()
    G.load.cache_clear()
    base = [c["root_cause"] for c in
            G.candidate_causes(mapped, top_k=99, use_learned=False)]
    # 必须挑一个**对这些症状有直接边**的根因。学习调整只作用在直接边上，
    # 级联跳（cause -> cause）那一段用的是原始 likelihood —— 拿一个只能
    # 经级联够到症状的根因做实验，喂多少次都不动，那反映的是下面 [5] 要
    # 单独测的限制，不是学习没生效。
    g = G.load()
    direct = {u for sym in mapped for u, _v, k in g.in_edges(sym, keys=True)
              if k == "CAUSES"
              and g.nodes.get(u, {}).get("kind") == "RootCause"}
    ranked_direct = [c for c in base if c in direct]
    target = ranked_direct[-1]
    print(f"      基线排序: {base[:4]}")
    print(f"      有直接边的根因: {ranked_direct}")
    print(f"      拿其中垫底的 {target} 做实验，连续喂 20 次正确诊断")
    for _ in range(20):
        ev.learn_from_episode(mk(target, SYM), Score(True), SYM)
    G.load.cache_clear()
    after_rank = [c["root_cause"] for c in
                  G.candidate_causes(mapped, top_k=99, use_learned=True)]
    print(f"      学习后排序: {after_rank[:4]}")
    moved = after_rank.index(target) < base.index(target)
    check("学习把它的名次提前了", moved,
          f"{base.index(target) + 1} -> {after_rank.index(target) + 1}")
    print("\n[5] 已知限制：学习调整不作用在级联跳上")
    cascade_only = [c for c in base if c not in direct]
    if cascade_only:
        victim = cascade_only[-1]
        r0 = base.index(victim)
        shutil.rmtree(tmp); tmp.mkdir(); G.load.cache_clear()
        for _ in range(20):
            ev.learn_from_episode(mk(victim, SYM), Score(True), SYM)
        G.load.cache_clear()
        r1 = [c["root_cause"] for c in
              G.candidate_causes(mapped, top_k=99,
                                 use_learned=True)].index(victim)
        check(f"只经级联够到症状的 {victim[:22]} 名次不动（如实记录）",
              r1 == r0,
              f"{r0 + 1} -> {r1 + 1}；候选生成只在直接边上应用 ladj，"
              f"级联跳用原始 likelihood")
    else:
        print("      （这组症状下没有仅靠级联够到的根因，跳过）")

finally:
    ev.LEARNED = orig
    if tmp.exists():
        shutil.rmtree(tmp)
    G.load.cache_clear()

print()
print("=" * 66)
print("L3 CHANNEL: PASS" if not fails else f"L3 CHANNEL: FAIL {fails}")
sys.exit(1 if fails else 0)
