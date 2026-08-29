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

# 用法：scenario_probe.py [--seeds 1,2,3] [场景名 ...]
# 不给场景名就扫全部。
argv = sys.argv[1:]
SEEDS = [1]
if argv and argv[0] == "--seeds":
    SEEDS = [int(x) for x in argv[1].split(",")]
    argv = argv[2:]

ROOT = Path(__file__).resolve().parent.parent
TARGETS = argv or sorted(
    f.stem for f in (ROOT / "sandbox/scenarios").glob("*.yaml"))

print(f"场景 {len(TARGETS)} 个 × 种子 {SEEDS} = "
      f"{len(TARGETS) * len(SEEDS)} 次注入\n")

fails = []
for sc in TARGETS:
  for seed in SEEDS:
    t = time.time()
    try:
        with DBAScenarioEnv(f"sandbox/scenarios/{sc}.yaml", warmup_s=10.0,
                            degrade_timeout_s=110.0, quiet=True) as env:
            obs = env.reset(seed=seed)
            k = obs.current_kpi
            p99 = k.get("p99_ms", 0)
            err = k.get("errors", 0)
            mark = "PASS" if obs.fired else "FAIL"
            if not obs.fired:
                fails.append(f"{sc}#{seed}")
            print(f"  {mark}  {sc:<38} s{seed}  告警={obs.fired}  "
                  f"p99={p99:>8.1f}ms errors={err:<6} "
                  f"({time.time() - t:.0f}s)")
    except Exception as exc:
        fails.append(f"{sc}#{seed}")
        print(f"  FAIL  {sc:<38} s{seed}  {type(exc).__name__}: "
              f"{str(exc)[:60]}  ({time.time() - t:.0f}s)")

print()
print("=" * 70)
print("SCENARIO PROBE: PASS" if not fails
      else f"SCENARIO PROBE: FAIL —— 故障未显现: {fails}")
sys.exit(1 if fails else 0)
