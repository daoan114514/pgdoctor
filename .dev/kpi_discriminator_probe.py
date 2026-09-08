"""测健康 / 故障 / 修复后三档的完整 KPI，找出真正能分开"故障"与"修复后"的字段。

起因：stale_statistics 的判据一直只看 p99，而实测 p99 在这个场景里分不开 ——
故障 771-1176ms、修复后 366-527ms，最窄处只差 1.46 倍。原因是 ANALYZE 只修
统计，注入的 40 万行还留在表里，修复后的负载本来就比健康态重（52-71 倍），
所以既不能拿健康基线做相对判据，也不能指望 p99 有干净的间隔。
换个字段也许可以，但必须先量。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox import metrics
from sandbox.env import DBAScenarioEnv

SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "stale_statistics_eval_v1"
FIX_SQL = sys.argv[2] if len(sys.argv) > 2 else "ANALYZE orders"
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 3
FIELDS = ["p50_ms", "p95_ms", "p99_ms", "qps", "errors", "cpu_pct"]

rows = []
for i in range(1, ROUNDS + 1):
    t0 = time.time()
    with DBAScenarioEnv(f"sandbox/scenarios/{SCENARIO}.yaml", warmup_s=10.0,
                        degrade_timeout_s=140.0, quiet=True) as env:
        obs = env.reset(seed=i)
        healthy, fault = obs.healthy_kpi, obs.current_kpi
        if not obs.fired:
            print(f"  [{i}] 告警未触发 fault={ {f: fault.get(f) for f in FIELDS} }")
            rows.append((healthy, fault, None))
            continue
        env.apply_sql(FIX_SQL)
        time.sleep(metrics.WINDOW_S + 5)
        fixed = metrics.collect().as_dict()
        rows.append((healthy, fault, fixed))
        print(f"  [{i}] ({time.time() - t0:.0f}s)")
        for f in FIELDS:
            print(f"        {f:<9} healthy={healthy.get(f):>10}  "
                  f"fault={fault.get(f):>10}  fixed={fixed.get(f):>10}")

good = [r for r in rows if r[2]]
print("\n" + "=" * 74)
if not good:
    print("没有可用样本")
    raise SystemExit(0)
print(f"{'字段':<10}{'故障态区间':>26}{'修复后区间':>26}{'间隔':>10}")
for f in FIELDS:
    fa = [r[1].get(f, 0) or 0 for r in good]
    fx = [r[2].get(f, 0) or 0 for r in good]
    # 故障时高、修复后低的字段，间隔 = 故障最小 / 修复最大；反之取倒数方向
    if min(fa) > max(fx):
        gap = min(fa) / max(fx) if max(fx) else float("inf")
        direction = "故障高"
    elif max(fa) < min(fx):
        gap = min(fx) / max(fa) if max(fa) else float("inf")
        direction = "故障低"
    else:
        gap, direction = 0.0, "重叠"
    print(f"{f:<10}{f'{min(fa):.1f} - {max(fa):.1f}':>26}"
          f"{f'{min(fx):.1f} - {max(fx):.1f}':>26}"
          f"{(f'{gap:.2f}x ' + direction) if gap else '  重叠':>12}")
