"""改过的场景必须实测确认故障真的显现。

misleading_idle_txn_train_v1 那次的教训：leave_free 从 2 调到 6，告警连续
两轮不触发，场景等于是废的，而 lint、离线检查全都过。只有真的注入一次
才知道。

这次动了 eval 集（missing_index 改成丢 created_at 索引、connection_exhaustion
的 train 改 leave_free），必须一一验过。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox.env import DBAScenarioEnv

TARGETS = sys.argv[1:] or [
    "missing_index_eval_v1",
    "missing_index_orders_user_status_v1",
    "connection_exhaustion_train_v1",
    "connection_exhaustion_eval_v1",
]

fails = []
for sc in TARGETS:
    t = time.time()
    try:
        with DBAScenarioEnv(f"sandbox/scenarios/{sc}.yaml", warmup_s=10.0,
                            degrade_timeout_s=110.0, quiet=True) as env:
            obs = env.reset(seed=1)
            k = obs.current_kpi
            p99 = k.get("p99_ms", 0)
            err = k.get("errors", 0)
            cpu = k.get("cpu_pct", 0)
            mark = "PASS" if obs.fired else "FAIL"
            if not obs.fired:
                fails.append(sc)
            print(f"  {mark}  {sc:<40} 告警={obs.fired}  "
                  f"p99={p99:>8.1f}ms errors={err:<5} cpu={cpu:>6.1f}%  "
                  f"({time.time() - t:.0f}s)")
            print(f"        注入: {env.injection.notes[:88]}")
    except Exception as exc:
        fails.append(sc)
        print(f"  FAIL  {sc:<40} {type(exc).__name__}: {exc}  "
              f"({time.time() - t:.0f}s)")

print()
print("=" * 70)
print("SCENARIO PROBE: PASS" if not fails
      else f"SCENARIO PROBE: FAIL —— 故障未显现: {fails}")
sys.exit(1 if fails else 0)
