"""新注入器冒烟：故障是否真的被制造出来、是否可诊断、是否能回滚干净。

不验这一步，后面跑批全是垃圾数据 —— 注入没生效的 episode 会被当成
"agent 没诊断出来"，把消融结论带偏。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox import db, snapshot
from sandbox.env import DBAScenarioEnv
from sandbox.observe import Observer
from sandbox.traces import TraceStore

CASES = [
    ("stale_statistics_train_v1", "stale_statistics"),
    ("lock_contention_train_v1", "lock_contention"),
    ("connection_exhaustion_train_v1", "connection_exhaustion"),
]

ok = True
print("=" * 74)
for scen, fault in CASES:
    print(f"\n--- {fault} ---")
    try:
        with DBAScenarioEnv(f"sandbox/scenarios/{scen}.yaml",
                            warmup_s=12.0, degrade_timeout_s=60.0,
                            quiet=True) as env:
            obs = env.reset()
            print(f"  注入: {env.injection.notes[:78]}")
            print(f"  告警触发={obs.fired} | p99 "
                  f"{obs.healthy_kpi['p99_ms']}ms -> {obs.current_kpi['p99_ms']}ms "
                  f"| errors={obs.current_kpi.get('errors')}")

            o = Observer(TraceStore(f"smoke_{fault}"))
            # 每类故障的判别性证据是否真的能取到
            if fault == "stale_statistics":
                stt = o.get_table_stats("orders")
                est = db.query("SELECT reltuples::bigint FROM pg_class "
                               "WHERE relname='orders'")[0][0]
                act = db.query("SELECT count(*) FROM orders")[0][0]
                dev = abs(est - act) / max(act, 1)
                print(f"  判别证据: 估计 {est:,} vs 实际 {act:,} "
                      f"偏差 {dev:.0%}")
                good = dev > 0.2
            elif fault == "lock_contention":
                chain = o.get_blocking_chain()
                sess = o.get_active_sessions()
                waits = [s.wait_event for s in sess if s.wait_event]
                print(f"  判别证据: 阻塞链 {len(chain)} 条, 等待事件 {waits[:3]}")
                good = len(chain) > 0 or any("Lock" in str(w) for w in waits)
            else:
                maxc = int(db.query("SHOW max_connections")[0][0])
                used = db.query("SELECT count(*) FROM pg_stat_activity")[0][0]
                print(f"  判别证据: 连接 {used}/{maxc}")
                good = used >= maxc * 0.85

            print(f"  {'PASS' if good else 'FAIL'}  故障可被诊断性证据识别")
            ok &= good
            # 告警不触发的话 env.reset() 会判 episode 不可用，
            # 故障"成立"但没症状同样是废的
            print(f"  {'PASS' if obs.fired else 'FAIL'}  告警实际触发")
            ok &= obs.fired
    except Exception as exc:
        print(f"  FAIL  注入过程异常: {type(exc).__name__}: {exc}")
        ok = False

    # 清理后确认回到健康态
    try:
        snapshot.reset()
        idx = [r[0] for r in db.query(
            "SELECT indexname FROM pg_indexes WHERE tablename='orders'")]
        n = db.query("SELECT count(*) FROM orders")[0][0]
        clean = "idx_orders_user_status" in idx and n < 12_500_000
        print(f"  {'PASS' if clean else 'FAIL'}  回滚干净 "
              f"(orders {n:,} 行, 索引 {len(idx)} 个)")
        ok &= clean
    except Exception as exc:
        print(f"  FAIL  回滚异常: {exc}")
        ok = False
    time.sleep(1)

print("\n" + "=" * 74)
print("INJECTOR SMOKE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
