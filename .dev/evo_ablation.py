"""自进化到底有没有用 —— 能离线测的两层。

项目宣称四层非参数自进化，产物也确实在 knowledge/learned/ 里躺着。但
"有产物"和"有用"是两回事，后者从没测过。

能测的：
  L3 先验回写 —— 纯图计算，ground truth 已知，零成本
  L4 查询判别力 —— 学到的工具序 vs 图上默认序

测不了的：
  L2 playbook —— 它改的是给模型的提示词，效果要 LLM 才测得出
  L1 案例库 —— 0 例，无从测起

L3 的判据：拿真实 episode 观测到的症状去反查候选根因，看真根因排第几。
自进化如果有用，开启后它的名次应该更靠前。用真实轨迹里的症状而不是
场景声明的，是因为前者才是 agent 实际看到的东西。
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge.causal_graph import graph as G
from knowledge.evolution import load_delta, load_queries, top_queries_for

ROOT = Path(__file__).resolve().parent.parent
SCEN = ROOT / "sandbox/scenarios"


def truth_of(eid):
    m = re.match(r"^ep_(.+)_\d{6,}$", eid)
    if not m:
        return None
    f = SCEN / f"{m.group(1)}.yaml"
    if not f.exists():
        return None
    return yaml.safe_load(f.read_text(encoding="utf-8"))["fault_class"]


# ══ 收集真实观测到的症状 ══════════════════════════════════
from eval import replay

cases = []
for d in sorted((ROOT / "traces").iterdir()):
    if not d.is_dir() or not d.name.startswith("ep_"):
        continue
    truth = truth_of(d.name)
    if not truth:
        continue
    try:
        st = replay.load(d.name)
    except Exception:
        continue
    syms = G.map_symptoms(st.symptoms or [], fallback=False)
    if syms:
        cases.append((d.name, truth, tuple(sorted(syms))))

# 去重：同一组 (症状, 真根因) 只算一次，否则跑得多的场景会主导结论
uniq = sorted({(t, s) for _, t, s in cases})
print(f"真实轨迹 {len(cases)} 条，去重后 {len(uniq)} 组不同的（症状, 真根因）\n")

delta = load_delta()
print(f"L3 学到的先验调整: {delta.prior_adj or '（空）'}")
print(f"L3 学到的边权重调整: {len(delta.likelihood_adj)} 条\n")

# ══ L3 消融 ══════════════════════════════════════════════
print("=" * 72)
print("L3  先验回写：真根因在候选里排第几（越小越好）")
print("=" * 72)


def rank_of(syms, truth, learned):
    cands = G.candidate_causes(list(syms), top_k=99, use_learned=learned)
    names = [c["root_cause"] for c in cands]
    return names.index(truth) + 1 if truth in names else None


rows, better, worse, same = [], 0, 0, 0
for truth, syms in uniq:
    r_off = rank_of(syms, truth, False)
    r_on = rank_of(syms, truth, True)
    rows.append((truth, syms, r_off, r_on))
    if r_off is None or r_on is None:
        continue
    if r_on < r_off:
        better += 1
    elif r_on > r_off:
        worse += 1
    else:
        same += 1

print(f"{'真根因':<24} {'症状':<34} {'关闭':>5} {'开启':>5}")
for truth, syms, a, b in rows:
    mark = ""
    if a is not None and b is not None:
        mark = " ↑" if b < a else (" ↓" if b > a else "")
    print(f"{truth:<24} {','.join(syms)[:32]:<34} "
          f"{str(a):>5} {str(b):>5}{mark}")

print()
print(f"  变好 {better} 组 / 变差 {worse} 组 / 不变 {same} 组")
ok = [(a, b) for _, _, a, b in rows if a is not None and b is not None]
if ok:
    ma = sum(a for a, _ in ok) / len(ok)
    mb = sum(b for _, b in ok) / len(ok)
    t1a = sum(1 for a, _ in ok if a == 1) / len(ok)
    t1b = sum(1 for _, b in ok if b == 1) / len(ok)
    print(f"  平均名次   {ma:.2f} -> {mb:.2f}")
    print(f"  Top-1 命中 {t1a:.0%} -> {t1b:.0%}")
    if better == 0 and worse == 0:
        print("\n  → L3 对候选排序**没有任何影响**")
    elif better > worse:
        print(f"\n  → L3 让 {better} 组的真根因排得更靠前")
    else:
        print(f"\n  → L3 让 {worse} 组变差、{better} 组变好 —— 净效果为负")

# ══ L4 消融 ══════════════════════════════════════════════
print()
print("=" * 72)
print("L4  查询判别力：学到的取证工具序 vs 图上默认序")
print("=" * 72)
qs = load_queries()
print(f"查询库里有 {len(qs)} 条记录\n")

rcs = [n for n, d in G.load().nodes(data=True) if d.get("kind") == "RootCause"]
hit = miss = nodata = 0
for rc in sorted(rcs):
    learned = top_queries_for(rc)
    # 图上的默认：该根因的必需证据对应的工具
    default = []
    for ev in G.required_evidence(rc):
        t = G.load().nodes.get(ev, {}).get("obtained_by")
        if t and t not in default:
            default.append(t)
    if not learned:
        nodata += 1
        continue
    # 学到的第一个工具，是不是该根因必需证据的取法
    if learned[0] in default:
        hit += 1
        mark = "命中"
    else:
        miss += 1
        mark = "偏离"
    print(f"  {rc:<26} 学到 {str(learned[:2]):<44} "
          f"必需 {str(default[:2]):<40} {mark}")

print()
print(f"  有学习数据的根因 {hit + miss} 个，无数据 {nodata} 个")
if hit + miss:
    print(f"  学到的首选工具命中必需证据: {hit}/{hit + miss} "
          f"({hit / (hit + miss):.0%})")
else:
    print("  → L4 没有任何可用数据，无法评价")
