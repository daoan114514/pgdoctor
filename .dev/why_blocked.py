"""那 45 个新被拦的正确诊断，是拦对了还是拦错了？

修复把 D1 误放的拦截率从 41% 提到 77%，代价是正确诊断的放行率从 45%
掉到 22%。这个交换看起来不划算，但要先分清两种情况：

  拦对了：那些正确诊断本来就没做鉴别诊断（depth 0/1），本该被拦
  拦错了：做了正当排除，却被方向检查误判成无依据

只有后者才是 bug。按 depth 分层看就能分开。
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(d):
    out = []
    for f in sorted((ROOT / d).glob("d2_suite_r*.json")):
        out += [x for x in json.loads(f.read_text(encoding="utf-8"))["rows"]
                if "error" not in x and x.get("claimed")]
    return out




def key(x):
    return (x.get("run"), x["scenario"], x["target"], x["kind"], str(x["depth"]))


before = {key(x): x for x in load("eval/results/before_refute_fix")}
after = {key(x): x for x in load("eval/results")}
common = sorted(set(before) & set(after))
print(f"可对齐样本 {len(common)} 例\n")

# ── 按 depth 分层：正确诊断的放行率 ─────────────────────────
print("=" * 70)
print("正确诊断，按鉴别深度看放行率")
print("=" * 70)
print(f"{'排除数':>6} {'样本':>5} {'修复前放行':>11} {'修复后放行':>11}  说明")
lay = defaultdict(lambda: {"n": 0, "a": 0, "b": 0})
for k in common:
    x, y = before[k], after[k]
    if not y["correct"]:
        continue
    d = lay[k[-1]]
    d["n"] += 1
    d["a"] += bool(x["passed"])
    d["b"] += bool(y["passed"])
for depth in ("0", "1", "2", "3", "None"):
    if depth not in lay:
        continue
    d = lay[depth]
    note = "本就没做鉴别诊断，拦是对的" if depth in ("0", "1") else ""
    print(f"{depth:>6} {d['n']:>5} {d['a']:>10} {d['b']:>10}  {note}")

# ── 新被拦的正确诊断，卡在哪一维 ────────────────────────────
print()
print("=" * 70)
print("新被拦的正确诊断：从放行变成拦下的那些")
print("=" * 70)
flipped = [k for k in common
           if after[k]["correct"] and before[k]["passed"]
           and not after[k]["passed"]]
print(f"  共 {len(flipped)} 例")
by_depth = defaultdict(int)
by_dim = defaultdict(int)
samples = []
for k in flipped:
    y = after[k]
    by_depth[str(y["depth"])] += 1
    for dim, ok in y["dims"].items():
        if not ok:
            by_dim[dim] += 1
    if len(samples) < 5:
        samples.append(y)
print(f"  按深度: {dict(by_depth)}")
print(f"  卡在哪一维: {dict(by_dim)}")
print()
for y in samples:
    print(f"  {y['scenario'][:34]:<36} depth={str(y['depth']):<5} "
          f"{y['claimed']}")
    print(f"      {y['d2_detail'][:110]}")
