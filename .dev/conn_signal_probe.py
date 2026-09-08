"""连接类故障的可观测面到底有没有信号：按查询类型拆开看。

起因：misleading_idle_txn_eval_v1 单独用 scenario_probe（seed=1,
degrade=110s）实测 errors=551，同一份代码在 run_suite（seed=0,
warmup=15s, degrade=90s）里却 fired=False。同一个修复不该有两种表现，
所以要看清楚是"探针没打够"还是"打了但没撞上"——KPI 里的 errors 是跨
类型求和的，看不出这个区别。newconn 那一类的 n 才是探针实际尝试次数。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox import db, metrics
from sandbox.env import DBAScenarioEnv

SC = sys.argv[1] if len(sys.argv) > 1 else "misleading_idle_txn_eval_v1"
SEEDS = [int(x) for x in (sys.argv[2] if len(sys.argv) > 2 else "0").split(",")]

for seed in SEEDS:
    # 跑批用的正是这三个值
    with DBAScenarioEnv(f"sandbox/scenarios/{SC}.yaml", warmup_s=15.0,
                        degrade_timeout_s=90.0, quiet=True) as env:
        obs = env.reset(seed=seed)
        raw = json.loads(metrics.METRICS_PATH.read_text(encoding="utf-8"))
        maxc = db.query("SHOW max_connections")[0][0]
        used = db.query("SELECT count(*) FROM pg_stat_activity")[0][0]
        idle = db.query("SELECT count(*) FROM pg_stat_activity "
                        "WHERE state = 'idle in transaction'")[0][0]
        print(f"\n=== {SC} seed={seed} 告警={obs.fired} ===")
        print(f"  注入: {env.injection.notes}")
        print(f"  此刻 连接 {used}/{maxc}  idle-in-txn {idle}")
        for kind, v in sorted(raw.get("by_query", {}).items()):
            print(f"    {kind:<10} n={v['n']:>6} errors={v['errors']:>6} "
                  f"p99={v['p99_ms']:>9.1f}  {v.get('last_error','')[:60]}")
        print(f"  notes: {obs.notes}")
