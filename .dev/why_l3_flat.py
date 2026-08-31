"""L3 没效果，是数据太少还是机制上推不动？

这两者处理完全不同：数据少 -> 多跑几轮就好；机制推不动 -> 再多数据也没用，
得改公式或改上限。

candidate_causes 的打分是   score = w * (0.5 + prior)
  w      症状边的 likelihood，取值 0.40 ~ 0.98
  prior  根因先验，取值 0.02 ~ 0.35，学习最多再动 ±50%

把两项各自能造成的分差算出来，就知道谁说了算。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge.causal_graph import graph as G
from knowledge.evolution import MAX_REL_ADJ, load_delta

g = G.load()
rcs = {n: d for n, d in g.nodes(data=True) if d.get("kind") == "RootCause"}

print("=" * 70)
print("打分公式  score = w × (0.5 + prior)")
print("=" * 70)

priors = sorted(float(d.get("prior", 0.1)) for d in rcs.values())
ws = [d.get("likelihood", 0.5)
      for _, _, k, d in g.edges(keys=True, data=True) if k == "CAUSES"]
ws = sorted(float(x) for x in ws)

print(f"\n先验 prior      {priors[0]:.2f} ~ {priors[-1]:.2f}")
print(f"  -> (0.5+prior) {0.5 + priors[0]:.2f} ~ {0.5 + priors[-1]:.2f}"
      f"   跨度 {(0.5 + priors[-1]) / (0.5 + priors[0]):.2f}×")
print(f"边权 w          {ws[0]:.2f} ~ {ws[-1]:.2f}"
      f"   跨度 {ws[-1] / ws[0]:.2f}×")

print("\n学习能造成多大的分差：")
for rc in ("missing_index", "lock_contention", "xid_wraparound_risk"):
    if rc not in rcs:
        continue
    p = float(rcs[rc].get("prior", 0.1))
    cap = min(0.25, p * MAX_REL_ADJ)
    lo, hi = (0.5 + p - cap), (0.5 + p + cap)
    print(f"  {rc:<24} prior={p:.2f} 上限±{cap:.3f}"
          f"  -> 分量 {lo:.3f}~{hi:.3f}  最大摆动 {hi / lo:.2f}×")

print("\n对比：换一条症状边就能造成的分差")
print(f"  最弱边 w={ws[0]:.2f} vs 最强边 w={ws[-1]:.2f}"
      f"  -> {ws[-1] / ws[0]:.2f}×")

print()
print("=" * 70)
print("结论")
print("=" * 70)
max_prior_swing = max(
    (0.5 + float(d.get("prior", 0.1)) + min(0.25, float(d.get("prior", 0.1)) * MAX_REL_ADJ))
    / (0.5 + float(d.get("prior", 0.1)) - min(0.25, float(d.get("prior", 0.1)) * MAX_REL_ADJ))
    for d in rcs.values())
w_swing = ws[-1] / ws[0]
print(f"  学习对单个根因的最大分量摆动   {max_prior_swing:.2f}×")
print(f"  症状边本身造成的分差           {w_swing:.2f}×")
if max_prior_swing < w_swing:
    print(f"\n  → 先验项的可调范围**小于**边权造成的固有分差。")
    print(f"    也就是说 L3 在这个公式里结构上就推不动排序 ——")
    print(f"    不是数据少，是再多数据也只能在同一档内微调。")
else:
    print(f"\n  → 先验项可调范围足够大，L3 没效果应归因于数据太少。")

# 具体到实测那两组
print()
print("实测那两组的分数细节：")
delta = load_delta()
for syms, truth in ((["throughput_down"], "lock_contention"),
                    (["throughput_down"], "connection_exhaustion")):
    print(f"\n  症状 {syms} / 真根因 {truth}")
    for learned in (False, True):
        cands = G.candidate_causes(syms, top_k=4, use_learned=learned)
        tag = "开启" if learned else "关闭"
        line = "  ".join(f"{c['root_cause'][:18]}={c['score']:.3f}"
                         for c in cands)
        print(f"    {tag}  {line}")
