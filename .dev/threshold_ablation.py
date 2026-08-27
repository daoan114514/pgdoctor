"""阈值消融：用重放跑，零 API 成本。

README 一直承认"ESC 带来过度保守"，却从没报过具体比例；而
min_refute_ratio = 0.5 这个数是我拍的，从没做过敏感性分析。
拍的阈值 + 没量化的代价，是这类系统里最容易被追问穿的地方。

轨迹都在盘上，所以这件事根本不需要重新跑模型：把 episode 重放出来，
换不同阈值重算 ESC 裁决，和 ground truth 对一遍，就能画出完整的
精度/召回权衡。

四个格子：
  放行且正确  真通过 —— agent 自主解决
  放行但错误  **静默失败** —— ESC 存在的全部理由就是压这一格
  拦截但正确  过度保守的代价 —— 本可自主解决却升级了人工
  拦截且错误  真拦截 —— ESC 干了活
"""
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import esc
from eval import replay
from sandbox.scoring import _diagnosis_strict

ROOT = Path(__file__).resolve().parent.parent
SCEN = ROOT / "sandbox/scenarios"


def truth_of(episode_id: str) -> tuple[str, dict] | tuple[None, None]:
    """从 episode 目录名回推场景，再取 ground truth。

    不做字符串猜测：回到场景 yaml 里读 fault_class，否则
    misleading_idle_txn 这种"名字里的类 ≠ 真根因"的场景会被判错。
    """
    m = re.match(r"^ep_(.+)_\d{6,}$", episode_id)
    if not m:
        return None, None
    f = SCEN / f"{m.group(1)}.yaml"
    if not f.exists():
        return None, None
    spec = yaml.safe_load(f.read_text(encoding="utf-8"))
    return spec["fault_class"], spec


# ── 收集可用样本 ────────────────────────────────────────────
samples = []
for d in sorted((ROOT / "traces").iterdir()):
    if not d.is_dir() or not d.name.startswith("ep_"):
        continue
    truth, spec = truth_of(d.name)
    if not truth:
        continue
    try:
        st = replay.load(d.name)
    except Exception:
        continue
    if not st.claimed_fault_class or not st.ledger:
        continue
    samples.append((d.name, st, truth, spec))

print(f"可用样本 {len(samples)} 个 "
      f"（诊断正确 {sum(1 for _, st, t, _ in samples if st.claimed_fault_class == t)}，"
      f"误诊 {sum(1 for _, st, t, _ in samples if st.claimed_fault_class != t)}）")
print()

# ══ 消融 1：ESC 的 min_refute_ratio ═══════════════════════
print("=" * 74)
print("消融 1  ESC D2 鉴别诊断门槛 min_refute_ratio")
print("=" * 74)
print(f"{'阈值':>6} {'放行':>5} {'放行且对':>9} {'放行但错':>9} "
      f"{'拦截但对':>9} {'拦截且错':>9} {'静默失败率':>11} {'过度保守率':>11}")

rows = []
for thr in (0.0, 0.25, 0.34, 0.5, 0.67, 0.75, 1.0):
    tp = fp = tb = fb = 0
    for _, st, truth, _ in samples:
        rep = esc.check(st, min_refute_ratio=thr)
        passed = rep.verdict == esc.ESCVerdict.SUFFICIENT.value
        correct = (st.claimed_fault_class == truth)
        if passed and correct:
            tp += 1
        elif passed and not correct:
            fp += 1
        elif not passed and correct:
            fb += 1
        else:
            tb += 1
    n = len(samples)
    n_correct = tp + fb
    silent = fp / n if n else 0.0
    over = fb / n_correct if n_correct else 0.0
    rows.append((thr, tp, fp, fb, tb, silent, over))
    mark = "  <- 当前" if abs(thr - 0.5) < 1e-9 else ""
    print(f"{thr:>6.2f} {tp + fp:>5} {tp:>9} {fp:>9} {fb:>9} {tb:>9} "
          f"{silent:>10.1%} {over:>10.1%}{mark}")

print()
best = [r for r in rows if r[2] == 0]
if best:
    loosest = max(best, key=lambda r: -r[3])
    print(f"零静默失败的最宽松阈值: {loosest[0]:.2f}"
          f"（过度保守率 {loosest[5]:.1%}）")
    cur = next(r for r in rows if abs(r[0] - 0.5) < 1e-9)
    if loosest[0] < 0.5:
        print(f"→ 当前的 0.50 比必要值更严: 多拦了 {cur[3] - loosest[3]} 个"
              f"本可自主解决的 episode，换不来更低的静默失败率")
    elif loosest[0] > 0.5:
        print(f"→ 当前的 0.50 偏松: 0.50 时仍有 {cur[2]} 个静默失败")
    else:
        print("→ 当前的 0.50 恰好是零静默失败的最宽松点")
else:
    print("没有任何阈值能做到零静默失败 —— D2 单独不足以拦住全部误诊")

# ══ 消融 2：严格诊断的 F1 门槛 ════════════════════════════
print()
print("=" * 74)
print("消融 2  严格诊断的 F1 门槛 STRICT_F1")
print("=" * 74)
print(f"{'阈值':>6} {'判过':>5} {'判过且对':>9} {'判过但错':>9} {'区分度':>9}")

for thr in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
    ok_correct = ok_wrong = 0
    for _, st, truth, spec in samples:
        passed, det = _diagnosis_strict(spec, st.claimed_fault_class, st.ledger)
        passed = (det["f1"] >= thr and det["critical_ok"]
                  and not det["contradictory"])
        if passed and st.claimed_fault_class == truth:
            ok_correct += 1
        elif passed:
            ok_wrong += 1
    n_pass = ok_correct + ok_wrong
    prec = ok_correct / n_pass if n_pass else 0.0
    mark = "  <- 当前(取自 DBA-Bench)" if abs(thr - 0.8) < 1e-9 else ""
    print(f"{thr:>6.2f} {n_pass:>5} {ok_correct:>9} {ok_wrong:>9} "
          f"{prec:>8.1%}{mark}")

# ══ 分布：F1 实际落在哪 ═══════════════════════════════════
print()
print("=" * 74)
print("参考  样本的 F1 实际分布")
print("=" * 74)
buckets = {}
for _, st, truth, spec in samples:
    _, det = _diagnosis_strict(spec, st.claimed_fault_class, st.ledger)
    b = round(det["f1"] * 10) / 10
    key = (b, st.claimed_fault_class == truth)
    buckets[key] = buckets.get(key, 0) + 1
for b in sorted({k[0] for k in buckets}):
    c = buckets.get((b, True), 0)
    w = buckets.get((b, False), 0)
    print(f"  F1≈{b:.1f}  正确 {'█' * c}{c or ''}  误诊 {'▒' * w}{w or ''}")
