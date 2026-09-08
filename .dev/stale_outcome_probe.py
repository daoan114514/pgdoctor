"""实测 stale_statistics 的健康 / 故障 / 修复后三档 p99，用来标定判据。

起因：告警和成功判据都写死成 p99_ms > 800 / < 800 —— 同一个绝对阈值、
零迟滞。翻 110 个历史 episode 的实测值发现 800 正好落在故障态的低尾里
(654.9 / 802.9 / 826.3 三例贴着或低于它)，而健康态只有 2.7-18.0ms，
"降到 800" 等于"降到健康态的一百倍就算修好"。

要改判据就得先有修复后的数字，而历史 110 个 episode 一次修复都没应用过，
所以只能自己造一次：注入、ANALYZE、等滚动窗口被修复后的样本填满、再测。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox import metrics
from sandbox.env import DBAScenarioEnv

SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "stale_statistics_eval_v1"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 3

rows = []
for i in range(1, ROUNDS + 1):
    t0 = time.time()
    with DBAScenarioEnv(f"sandbox/scenarios/{SCENARIO}.yaml", warmup_s=10.0,
                        degrade_timeout_s=140.0, quiet=True) as env:
        obs = env.reset(seed=i)
        healthy = obs.healthy_kpi
        fault = obs.current_kpi
        if not obs.fired:
            print(f"  [{i}] 告警未触发，跳过 (fault p99={fault.get('p99_ms')})")
            rows.append((healthy, fault, None, False))
            continue
        env.apply_sql("ANALYZE orders")
        # 修复是瞬时的，滚动窗口不是。不等满窗就采，读到的还是故障期样本，
        # 这正是 env.reset 在注入侧已经处理过的同一个坑。
        time.sleep(metrics.WINDOW_S + 5)
        fixed = metrics.collect().as_dict()
        rows.append((healthy, fault, fixed, True))
        print(f"  [{i}] healthy p99={healthy['p99_ms']:>7.2f}  "
              f"fault p99={fault['p99_ms']:>9.2f}  "
              f"fixed p99={fixed['p99_ms']:>8.2f}  "
              f"fixed/healthy={fixed['p99_ms'] / max(healthy['p99_ms'], 1e-9):>6.2f}x  "
              f"({time.time() - t0:.0f}s)")

good = [r for r in rows if r[3] and r[2]]
print()
if good:
    fh = [r[2]["p99_ms"] / max(r[0]["p99_ms"], 1e-9) for r in good]
    dh = [r[1]["p99_ms"] / max(r[0]["p99_ms"], 1e-9) for r in good]
    print(f"修复后/健康 倍数: {[round(x, 2) for x in fh]}  最大 {max(fh):.2f}")
    print(f"故障态/健康 倍数: {[round(x, 1) for x in dh]}  最小 {min(dh):.1f}")
    print(f"修复后 p99 绝对值: {[round(r[2]['p99_ms'], 1) for r in good]}")
    print(f"故障态 p99 绝对值: {[round(r[1]['p99_ms'], 1) for r in good]}")
    print(f"健康   p99 绝对值: {[round(r[0]['p99_ms'], 2) for r in good]}")
    print(f"\n两簇之间空 {min(dh) / max(fh):.1f} 倍")
else:
    print("没有可用样本")
