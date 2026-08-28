"""修复前后 500 例对照：排除的取值方向检查值多少。

修复前的数据留在 eval/results/before_refute_fix/，两批各 5×100 例，
场景、种子、策略行为完全相同，唯一差别是 D2 认不认"取值方向相反的排除"。
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(d):
    rows = []
    for f in sorted((ROOT / d).glob("d2_suite_r*.json")):
        rows += [x for x in json.loads(f.read_text(encoding="utf-8"))["rows"]
                 if "error" not in x and x.get("claimed")]
    return rows


def stats(rows):
    d1w = [x for x in rows if x["dims"].get("D1") and not x["correct"]]
    caught = [x for x in d1w if not x["dims"].get("D2")]
    solo = Counter()
    for x in rows:
        if x["passed"]:
            continue
        f = [k for k, v in x["dims"].items() if not v]
        if len(f) == 1:
            solo[f[0]] += 1
    th = [x for x in rows if not x["correct"] and x["dims"].get("D2")]
    leak = [x for x in th if x["passed"]]
    ok_pass = [x for x in rows if x["correct"] and x["passed"]]
    ok_all = [x for x in rows if x["correct"]]
    return {
        "n": len(rows), "d1w": len(d1w), "caught": len(caught),
        "d2solo": solo.get("D2", 0), "d1solo": solo.get("D1", 0),
        "d5solo": solo.get("D5", 0), "th": len(th), "leak": len(leak),
        "ok_pass": len(ok_pass), "ok_all": len(ok_all),
    }


a = stats(load("eval/results/before_refute_fix"))
b = stats(load("eval/results"))

print(f"{'':<24}{'修复前':>8}{'修复后':>8}{'变化':>9}")
print("-" * 50)
for lab, k in [("有效样本", "n"),
               ("D1 为错误根因放行", "d1w"),
               ("  其中被 D2 抓回", "caught"),
               ("D2 独自拦下", "d2solo"),
               ("D1 独自拦下", "d1solo"),
               ("D5 独自拦下", "d5solo"),
               ("结论错但排查扎实", "th"),
               ("  其中被整体放行", "leak"),
               ("诊断正确的样本", "ok_all"),
               ("  其中被放行", "ok_pass")]:
    x, y = a[k], b[k]
    d = f"{y - x:+d}" if x != y else "—"
    print(f"{lab:<24}{x:>8}{y:>8}{d:>9}")

print()
print(f"D2 在 D1 误放上的拦截率   {a['caught'] / a['d1w']:>7.0%}"
      f" -> {b['caught'] / b['d1w']:>6.0%}")
print(f"错但扎实的漏网率         {a['leak'] / a['th']:>7.0%}"
      f" -> {b['leak'] / b['th']:>6.0%}")
print(f"正确诊断的放行率         {a['ok_pass'] / a['ok_all']:>7.0%}"
      f" -> {b['ok_pass'] / b['ok_all']:>6.0%}   ← 别为了拦得多把正常路径拦了")
