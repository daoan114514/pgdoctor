"""验证一个推断：误导性告警场景会不会真的压到 D2 这道闸。

旧的 4 类故障里 D2 是惰性的 —— 44 个样本、阈值 0.00 到 1.00，裁决
一个都没变。原因是那些场景的症状直指真根因，agent 顺手就把竞争假设
全排了，排除率恒在 1.0 附近，任何 ≤1.0 的阈值都碰不到。

误导性告警不一样：陷阱（connection_exhaustion）和真根因症状完全一致，
不主动排除它就无法区分。如果这个推断成立，D2 在这类场景里应该终于
开始起作用 —— 也就是阈值终于变得可测。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import esc
from agent.episode_state import EpisodeState, Verdict


def mk(refuted):
    st = EpisodeState(episode_id="synth",
                      scenario_id="misleading_idle_txn_eval_v1")
    st.symptoms = ["连接数逼近上限", "错误上升"]
    st.claimed_fault_class = "long_idle_transaction"
    st.note("agent", "session_wait_profile", "等待事件=无", "", [])
    st.note("agent", "idle_in_transaction",
            "连接 92/100 (92%), 逼近上限=True, idle in transaction=86", "", [])
    st.set_verdict("long_idle_transaction", Verdict.CONFIRMED,
                   note="86 个挂起事务占住连接槽")
    for r in refuted:
        st.set_verdict(r, Verdict.REFUTED, note="已排除")
    return st


CASES = [
    ("零排除", []),
    ("只排陷阱", ["connection_exhaustion"]),
    ("全排除", ["connection_exhaustion", "lock_contention"]),
]

print(f"{'排除情况':<10} {'阈值':>5}  {'裁决':<14} {'D2':<5} 细节")
print("-" * 74)
sensitive = False
for label, refuted in CASES:
    st = mk(refuted)
    seen = set()
    for thr in (0.0, 0.34, 0.5, 0.67, 1.0):
        rep = esc.check(st, min_refute_ratio=thr)
        d2 = next(d for d in rep.dims if d.name == "D2")
        mark = "通过" if d2.passed else "不过"
        seen.add(d2.passed)
        print(f"{label:<10} {thr:>5.2f}  {rep.verdict:<14} {mark:<5} {d2.detail}")
    if len(seen) > 1:
        sensitive = True
    print()

print("=" * 74)
if sensitive:
    print("D2 在这类场景里对阈值敏感 —— 阈值终于可测了")
else:
    print("D2 在这类场景里仍不敏感")
