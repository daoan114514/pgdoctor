"""阈值敏感性为何完全平坦 —— 查每个 episode 的实际 D1/D2 情况。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import esc as esc_mod
from eval import replay

print(f"{'episode':<24} {'根因':<15} {'D1':<5} {'D2 排除率':<12} 裁决")
print("-" * 72)
d1_fail = d2_bind = 0
for eid in replay.list_episodes():
    try:
        st = replay.load(eid)
    except Exception:
        continue
    rep = esc_mod.check(st)
    d = {x.name: x for x in rep.dims}
    d1 = d.get("D1")
    d2 = d.get("D2")
    ratio = ""
    if d2 and "已排除" in d2.detail:
        import re
        m = re.search(r"\((\d+)%\)", d2.detail)
        ratio = m.group(1) + "%" if m else "?"
    if d1 and not d1.passed:
        d1_fail += 1
    if d1 and d1.passed and d2 and not d2.passed:
        d2_bind += 1
    print(f"{eid[-18:]:<24} {str(st.claimed_fault_class):<15} "
          f"{'PASS' if d1 and d1.passed else 'FAIL':<5} {ratio:<12} {rep.verdict}")

print("-" * 72)
print(f"D1 就挂掉的: {d1_fail} 个  —— 这些无论 D2 阈值怎么调都过不了")
print(f"D1 过了但被 D2 卡住的: {d2_bind} 个 —— 只有这些才对阈值敏感")
print()
print("结论：现有轨迹里 D2 排除率非 0% 即 100%，中间值没有样本，")
print("所以阈值曲线是平的。要让这个分析有信息量，需要构造排除率")
print("落在中间的 episode（部分排除），或等 W8 的多故障类型跑批。")
