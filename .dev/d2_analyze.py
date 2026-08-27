"""分析 D2 跑批：这道闸到底有没有独立价值，边界在哪。

要回答三件事：
  1. 有没有"D1 放行、D2 拦下"的案例 —— 有，D2 才算有独立价值
  2. 阈值在这批数据上还平不平 —— 这批数据是专门压它的
  3. D2 抓不住什么 —— 边界比价值更该写清楚
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
data = json.loads((ROOT / "eval/results/d2_suite.json").read_text(
    encoding="utf-8"))
rows = [r for r in data["rows"] if "error" not in r]
bad = [r for r in data["rows"] if "error" in r]
# declare_root_cause 有证据门，缺必需证据时会拒绝声明，那样 claimed 是 None。
# 这类样本是**作废**，不是"落进陷阱" —— 混进去会给陷阱桶灌水，把 D2 的
# 拦截率算高。作废本身也是个信息：说明证据门先于 D2 挡住了这一类。
void = [r for r in rows if not r.get("claimed")]
rows = [r for r in rows if r.get("claimed")]

print(f"有效样本 {len(rows)} 例"
      + (f"，出错 {len(bad)} 例" if bad else "")
      + (f"，被证据门拒绝声明而作废 {len(void)} 例" if void else ""))
print(f"  诊断正确 {sum(1 for r in rows if r['correct'])}，"
      f"落进陷阱 {sum(1 for r in rows if not r['correct'])}")
print()

# ══ 1. D2 有没有独立价值 ═════════════════════════════════
print("=" * 74)
print("1  D1 放行、D2 拦下 —— 这是 D2 存在的全部理由")
print("=" * 74)
d1_pass_wrong = [r for r in rows if r["dims"].get("D1") and not r["correct"]]
caught_by_d2 = [r for r in d1_pass_wrong if not r["dims"].get("D2")]
leaked = [r for r in d1_pass_wrong if r["dims"].get("D2")]

print(f"  D1 为错误根因放行的样本:      {len(d1_pass_wrong)} 例")
print(f"    其中被 D2 拦下:             {len(caught_by_d2)} 例  ← D2 的独立贡献")
print(f"    其中 D2 也放行（漏网）:      {len(leaked)} 例")
if d1_pass_wrong:
    print(f"    D2 在这类样本上的拦截率:     "
          f"{len(caught_by_d2) / len(d1_pass_wrong):.1%}")

# ══ 2. 按鉴别深度看 ══════════════════════════════════════
print()
print("=" * 74)
print("2  按鉴别深度：ESC 放行率")
print("=" * 74)
print(f"{'排除数':>6} {'正确目标放行':>14} {'陷阱目标放行':>14} {'区分度':>10}")
by_depth = defaultdict(lambda: {"c_pass": 0, "c_n": 0, "t_pass": 0, "t_n": 0})
for r in rows:
    k = r["depth"]
    key = "全部" if k is None else k
    d = by_depth[key]
    if r["correct"]:
        d["c_n"] += 1
        d["c_pass"] += bool(r["passed"])
    else:
        d["t_n"] += 1
        d["t_pass"] += bool(r["passed"])

for k in [0, 1, 2, 3, "全部"]:
    if k not in by_depth:
        continue
    d = by_depth[k]
    cr = d["c_pass"] / d["c_n"] if d["c_n"] else 0
    tr = d["t_pass"] / d["t_n"] if d["t_n"] else 0
    gap = cr - tr
    bar = "█" * round(abs(gap) * 20)
    print(f"{str(k):>6} {d['c_pass']:>4}/{d['c_n']:<3}{cr:>7.0%} "
          f"{d['t_pass']:>4}/{d['t_n']:<3}{tr:>7.0%} {gap:>+9.0%} {bar}")
print()
print("  区分度 = 正确目标放行率 − 陷阱目标放行率。为正说明 ESC 在做事，")
print("  为 0 说明它对这两类一视同仁 —— 那才是真没用。")

# ══ 3. 各维度的拦截贡献 ══════════════════════════════════
print()
print("=" * 74)
print("3  拦截时是哪一维不过（可多维同时不过）")
print("=" * 74)
blocked = [r for r in rows if not r["passed"]]
contrib = Counter()
solo = Counter()
for r in blocked:
    failed = [k for k, v in r["dims"].items() if not v]
    for f in failed:
        contrib[f] += 1
    if len(failed) == 1:
        solo[failed[0]] += 1
print(f"  被拦 {len(blocked)} 例")
for d in sorted(contrib):
    print(f"    {d}  参与拦截 {contrib[d]:>3} 次   "
          f"独自拦下 {solo.get(d, 0):>3} 次  {'← 不可替代' if solo.get(d) else ''}")

# ══ 4. D2 的边界 ═════════════════════════════════════════
print()
print("=" * 74)
print("4  D2 抓不住什么")
print("=" * 74)
thorough_wrong = [r for r in rows
                  if not r["correct"] and r["dims"].get("D2")]
print(f"  「结论错但排查扎实」样本: {len(thorough_wrong)} 例")
passed_wrong = [r for r in thorough_wrong if r["passed"]]
print(f"    其中被 ESC 整体放行:     {len(passed_wrong)} 例")
if passed_wrong:
    print()
    print("  这类样本 D2 一定放行 —— 它衡量的是「有没有做鉴别诊断」，")
    print("  不是「结论对不对」。把它们拦下要靠判别性证据的取值检查，")
    print("  不是靠调 D2 的阈值。举例:")
    for r in passed_wrong[:4]:
        print(f"    {r['scenario'][:34]:<36} 声称 {r['claimed']:<22} "
              f"实为 {r['truth']}")

# ══ 5. 阈值重扫 ══════════════════════════════════════════
print()
print("=" * 74)
print("5  在这批数据上重扫 min_refute_ratio")
print("=" * 74)
print("（受控近似：不重放 EpisodeState，只按记录的排除比例重算 D2，\n  其余维度固定为实测值）")
import re
print(f"{'阈值':>6} {'放行':>6} {'放行且对':>10} {'放行但错':>10} {'静默失败率':>12}")
for thr in (0.0, 0.25, 0.34, 0.5, 0.67, 0.75, 1.0):
    tp = fp = 0
    for r in rows:
        m = re.search(r"竞争假设 (\d+) 个，已排除 (\d+) 个", r.get("d2_detail", ""))
        if not m:
            continue
        n_c, n_e = int(m.group(1)), int(m.group(2))
        ratio = (n_e / n_c) if n_c else 1.0
        d2_ok = ratio >= thr
        # 其余维度按实测值固定，只变 D2 —— 第一版只检查了 D1，忽略了
        # D3/D4/D5，"放行"数因此偏高。这是个受控近似：不重放
        # EpisodeState，但除 D2 外的判定都用真实观测值。
        other_ok = all(v for k, v in r["dims"].items() if k != "D2")
        if d2_ok and other_ok:
            if r["correct"]:
                tp += 1
            else:
                fp += 1
    mark = "  <- 当前" if abs(thr - 0.5) < 1e-9 else ""
    print(f"{thr:>6.2f} {tp + fp:>6} {tp:>10} {fp:>10} "
          f"{fp / len(rows):>11.1%}{mark}")
