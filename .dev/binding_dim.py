"""消融曲线是平的，那到底是哪一维在决定裁决？

平坦不等于"阈值选对了"，只等于"这批数据没碰到这道闸"。不查清哪一维
真正在起作用，"ESC 有效"就是一句没有归因的话 —— 而 ESC 是这个项目的
核心论点，这句话不能悬着。
"""
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import esc
from eval import replay

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


samples = []
for d in sorted((ROOT / "traces").iterdir()):
    if not d.is_dir() or not d.name.startswith("ep_"):
        continue
    t = truth_of(d.name)
    if not t:
        continue
    try:
        st = replay.load(d.name)
    except Exception:
        continue
    if st.claimed_fault_class and st.ledger:
        samples.append((d.name, st, t))

print(f"样本 {len(samples)} 个\n")

# ── 每一维单独的通过率 ──────────────────────────────────────
print("=" * 70)
print("各维度单独的通过率（放行 = 全部必需维通过）")
print("=" * 70)
dim_pass = Counter()
dim_total = Counter()
verdicts = Counter()
blocking = Counter()          # 拦截时，是哪一维不过

for _, st, truth in samples:
    rep = esc.check(st)
    verdicts[rep.verdict] += 1
    failed = []
    for d in rep.dims:
        dim_total[d.name] += 1
        if d.passed:
            dim_pass[d.name] += 1
        else:
            failed.append(d.name)
    if rep.verdict != esc.ESCVerdict.SUFFICIENT.value:
        blocking[",".join(failed) or "(无维度不过，另有原因)"] += 1

for d in sorted(dim_total):
    n, t = dim_pass[d], dim_total[d]
    bar = "█" * round(n / t * 30)
    print(f"  {d}  {n:>3}/{t:<3} {n/t:>6.1%}  {bar}")

print()
print("裁决分布:", dict(verdicts))
print()
print("=" * 70)
print("被拦截时，是哪一维不过")
print("=" * 70)
for k, v in blocking.most_common():
    print(f"  {v:>3} 次   {k}")

# ── 误诊的那几个具体卡在哪 ──────────────────────────────────
print()
print("=" * 70)
print("误诊样本逐个看（ESC 到底靠什么拦住的）")
print("=" * 70)
for eid, st, truth in samples:
    if st.claimed_fault_class == truth:
        continue
    rep = esc.check(st)
    marks = " ".join(f"{d.name}{'✓' if d.passed else '✗'}" for d in rep.dims)
    print(f"  {eid[:46]:<48}")
    print(f"      声称 {st.claimed_fault_class} / 实为 {truth}")
    print(f"      {rep.verdict:<14} {marks}")
    for d in rep.dims:
        if not d.passed and d.missing:
            print(f"      {d.name} 缺: {d.missing[:3]}")

# ── 正确却被拦的那个 ────────────────────────────────────────
print()
print("=" * 70)
print("过度保守的代价：诊断正确却被拦")
print("=" * 70)
found = False
for eid, st, truth in samples:
    if st.claimed_fault_class != truth:
        continue
    rep = esc.check(st)
    if rep.verdict == esc.ESCVerdict.SUFFICIENT.value:
        continue
    found = True
    marks = " ".join(f"{d.name}{'✓' if d.passed else '✗'}" for d in rep.dims)
    print(f"  {eid[:46]:<48}")
    print(f"      根因 {st.claimed_fault_class} 判对了，但 {rep.verdict}")
    print(f"      {marks}")
    for d in rep.dims:
        if not d.passed:
            print(f"      {d.name}: {d.detail}")
    for x in rep.directives[:3]:
        print(f"      指令: {x}")
if not found:
    print("  （无）")
