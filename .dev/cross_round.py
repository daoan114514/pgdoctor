"""跨轮对账：5 × 100 例。

单轮结果好看不算数 —— 要看的是换了种子之后结论稳不稳，以及随机化
到底有没有真的产生变化。三件事：

  1. 随机化生效了吗（各轮的注入参数与排除对象是否真的不同）
  2. 结论稳吗（D2 的独立贡献在五轮里波动多大）
  3. 有没有异常轮（某一轮明显偏离，说明环境或代码有问题）
"""
import json
import statistics as stat
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "eval/results"

rounds = {}
for f in sorted(RES.glob("d2_suite_r*.json")):
    r = int(f.stem.split("_r")[-1])
    rounds[r] = json.loads(f.read_text(encoding="utf-8"))["rows"]

if not rounds:
    print("没有找到跑批结果")
    sys.exit(1)

print(f"轮次 {sorted(rounds)}，每轮 {len(next(iter(rounds.values())))} 例，"
      f"共 {sum(len(v) for v in rounds.values())} 例\n")


def valid(rows):
    return [r for r in rows if "error" not in r and r.get("claimed")]


# ══ 1. 随机化生效了吗 ═════════════════════════════════════
print("=" * 74)
print("1  随机化有没有真的生效")
print("=" * 74)
sig = {}
for r, rows in rounds.items():
    # 用「每个场景的 d2_detail 序列」当指纹：排除对象变了它就会变
    sig[r] = tuple(x.get("d2_detail", "") for x in rows)
uniq = len(set(sig.values()))
print(f"  五轮的裁决指纹互不相同: {uniq}/5")
if uniq == 1:
    print("  警告：五轮完全一致，随机化没生效")
else:
    base = sig[min(sig)]
    for r in sorted(sig):
        diff = sum(1 for a, b in zip(sig[r], base) if a != b)
        print(f"    第 {r} 轮 与第 {min(sig)} 轮有 {diff:>3}/100 格不同")

# ══ 2. 逐轮的核心指标 ═════════════════════════════════════
print()
print("=" * 74)
print("2  逐轮核心指标")
print("=" * 74)
print(f"{'轮':>3} {'有效':>5} {'作废':>5} {'D1误放':>7} {'D2抓回':>7} "
      f"{'D2独拦':>7} {'D1独拦':>7} {'D5独拦':>7} {'错但扎实':>9}")
agg = defaultdict(list)
for r in sorted(rounds):
    rows = valid(rounds[r])
    void = len(rounds[r]) - len(rows) - sum(1 for x in rounds[r] if "error" in x)
    d1w = [x for x in rows if x["dims"].get("D1") and not x["correct"]]
    caught = [x for x in d1w if not x["dims"].get("D2")]
    solo = Counter()
    for x in rows:
        if x["passed"]:
            continue
        failed = [k for k, v in x["dims"].items() if not v]
        if len(failed) == 1:
            solo[failed[0]] += 1
    thorough = [x for x in rows if not x["correct"] and x["dims"].get("D2")]
    agg["d1w"].append(len(d1w)); agg["caught"].append(len(caught))
    agg["d2solo"].append(solo.get("D2", 0)); agg["d1solo"].append(solo.get("D1", 0))
    agg["d5solo"].append(solo.get("D5", 0)); agg["thorough"].append(len(thorough))
    agg["void"].append(void)
    print(f"{r:>3} {len(rows):>5} {void:>5} {len(d1w):>7} {len(caught):>7} "
          f"{solo.get('D2', 0):>7} {solo.get('D1', 0):>7} "
          f"{solo.get('D5', 0):>7} {len(thorough):>9}")


def spread(xs):
    if len(xs) < 2:
        return f"{xs[0]}"
    return (f"{stat.mean(xs):.1f} ± {stat.stdev(xs):.1f}"
            f"  (min {min(xs)}, max {max(xs)})")


print()
print("  五轮汇总:")
for k, label in (("d1w", "D1 为错误根因放行"), ("caught", "其中被 D2 抓回"),
                 ("d2solo", "D2 独自拦下"), ("d1solo", "D1 独自拦下"),
                 ("d5solo", "D5 独自拦下"),
                 ("thorough", "结论错但排查扎实"), ("void", "被证据门作废")):
    print(f"    {label:<22} {spread(agg[k])}")

# ══ 3. 有没有异常轮 ═══════════════════════════════════════
print()
print("=" * 74)
print("3  异常检查")
print("=" * 74)
issues = []
for k, label in (("d2solo", "D2 独自拦下"), ("d1w", "D1 误放")):
    xs = agg[k]
    if len(xs) >= 3 and stat.stdev(xs) > 0:
        m, sd = stat.mean(xs), stat.stdev(xs)
        for i, x in enumerate(xs, start=min(rounds)):
            if abs(x - m) > 2 * sd:
                issues.append(f"第 {i} 轮 {label}={x}，偏离均值 {m:.1f} 超过 2σ")
errs = [(r, x) for r, rows in rounds.items() for x in rows if "error" in x]
if errs:
    issues.append(f"{len(errs)} 例执行出错")
    for r, x in errs[:5]:
        issues.append(f"    第 {r} 轮 {x.get('scenario')}: {x.get('error')}")

# 每轮各场景是否都产出了 10 例
for r, rows in rounds.items():
    per = Counter(x["scenario"] for x in rows)
    short = {k: v for k, v in per.items() if v != 10}
    if short:
        issues.append(f"第 {r} 轮场景产出不足 10 例: {short}")

if issues:
    for i in issues:
        print(f"  ⚠ {i}")
else:
    print("  未发现异常：五轮均 100 例、无执行错误、指标无 2σ 离群")

# ══ 4. 500 例合并结论 ═════════════════════════════════════
print()
print("=" * 74)
print("4  500 例合并")
print("=" * 74)
allrows = [x for rows in rounds.values() for x in valid(rows)]
d1w = [x for x in allrows if x["dims"].get("D1") and not x["correct"]]
caught = [x for x in d1w if not x["dims"].get("D2")]
solo = Counter()
part = Counter()
for x in allrows:
    if x["passed"]:
        continue
    failed = [k for k, v in x["dims"].items() if not v]
    for f in failed:
        part[f] += 1
    if len(failed) == 1:
        solo[failed[0]] += 1
print(f"  有效样本 {len(allrows)} 例")
print(f"  D1 为错误根因放行 {len(d1w)} 例，其中 D2 抓回 {len(caught)} 例 "
      f"({len(caught) / len(d1w):.0%})" if d1w else "  无 D1 误放样本")
print()
print(f"  {'维度':<6} {'参与拦截':>9} {'独自拦下':>9}")
for d in sorted(part):
    print(f"  {d:<6} {part[d]:>9} {solo.get(d, 0):>9}")
thorough = [x for x in allrows if not x["correct"] and x["dims"].get("D2")]
leaked = [x for x in thorough if x["passed"]]
print()
print(f"  结论错但排查扎实 {len(thorough)} 例，其中 {len(leaked)} 例被整体放行"
      f" —— D2 结构上抓不住这类")
