"""人工执行"正解修复"，看能不能达到成功判据。

先分清责任：如果人工执行正解都达不到判据，那问题在判据或注入器，
不在 agent —— 让 agent 去够一个够不到的目标毫无意义。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from sandbox import db, metrics
from sandbox.env import DBAScenarioEnv

CASES = [
    ("lock_contention_eval_v1", "终止阻塞源会话"),
    ("stale_statistics_eval_v1", "ANALYZE 刷新统计"),
    ("misleading_idle_txn_eval_v1", "终止挂起的事务（而非调大连接上限）"),
]


def apply_fix(fault: str) -> str:
    if fault == "lock_contention":
        # 不能靠 pg_blocking_pids 找阻塞源：它只在别人"正在等"的那一瞬间
        # 有值，热查询一旦 statement_timeout 超时，等待者消失、阻塞链就空了。
        # 稳定特征是"持有行锁且事务挂着不动"，这也是真实 DBA 看的东西。
        rows = db.query(
            "SELECT DISTINCT a.pid FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid WHERE l.granted AND a.datname = current_database()   AND a.state = 'idle in transaction'   AND l.locktype IN ('transactionid', 'tuple', 'relation')   AND a.pid <> pg_backend_pid()")
        if not rows:
            return "没有找到挂起的持锁事务"
        killed, failed = [], []
        for (pid,) in rows:
            try:
                db.execute("SELECT pg_terminate_backend(%s)", (pid,), role="rw")
                killed.append(pid)
            except Exception as exc:
                failed.append(f"{pid}: {str(exc)[:60]}")
        return f"终止了 {killed}" + (f"；失败 {failed}" if failed else "")
    if fault == "long_idle_transaction":
        # 与 lock_contention 的正解形似而不同：这里的会话并不持有行锁，
        # 判据是"事务挂着不动"本身。按"连接打满"去修（只终止 idle 连接、
        # 调大上限）对它们完全无效 —— 这正是这个场景要考的那个区别。
        rows = db.query(
            "SELECT pid FROM pg_stat_activity "
            "WHERE state = 'idle in transaction' "
            "AND datname = current_database() "
            "AND pid <> pg_backend_pid()")
        if not rows:
            return "没有找到挂起的事务"
        killed, failed = [], []
        for (pid,) in rows:
            try:
                db.execute("SELECT pg_terminate_backend(%s)", (pid,), role="rw")
                killed.append(pid)
            except Exception as exc:
                failed.append(f"{pid}: {str(exc)[:60]}")
        return (f"终止了 {len(killed)} 个挂起事务"
                + (f"；失败 {failed}" if failed else ""))
    if fault == "stale_statistics":
        db.execute("ANALYZE orders", role="rw")
        return "ANALYZE orders 完成"
    return "未知故障类"


ok = True
for scen, label in CASES:
    spec = yaml.safe_load(
        (Path(__file__).resolve().parent.parent /
         f"sandbox/scenarios/{scen}.yaml").read_text(encoding="utf-8"))
    fault = spec["fault_class"]
    print("=" * 76)
    print(f"{fault} —— 人工执行：{label}")
    print("=" * 76)

    with DBAScenarioEnv(f"sandbox/scenarios/{scen}.yaml",
                        warmup_s=15.0, degrade_timeout_s=90.0,
                        quiet=True) as env:
        obs = env.reset()
        print(f"  注入后: p99={obs.current_kpi['p99_ms']}ms "
              f"errors={obs.current_kpi['errors']} "
              f"cpu={obs.current_kpi['cpu_pct']}%  告警={obs.fired}")
        print(f"  成功判据: {spec['success']['outcome']}")

        print(f"\n  执行修复 ...")
        msg = apply_fix(fault)
        print(f"  {msg}")

        # 分几个时间点看，确认是"要等"还是"根本不生效"
        for wait in (15, 20, 25):
            time.sleep(wait)
            kpi = metrics.collect()
            try:
                passed = metrics.eval_expr(spec["success"]["outcome"], kpi)
            except Exception as exc:
                passed = False
                print(f"    判据求值失败: {exc}")
            elapsed = sum([15, 20, 25][:[15, 20, 25].index(wait) + 1])
            print(f"    +{elapsed:>3}s  p99={kpi.p99_ms:>8.1f}ms "
                  f"errors={kpi.errors:>4} cpu={kpi.cpu_pct:>6.1f}%  "
                  f"达标={passed}")
            if passed:
                break

        # 错误到底出在哪类查询上
        import json
        mp = Path(__file__).resolve().parent.parent / "traces/workload_metrics.json"
        try:
            bq = json.loads(mp.read_text())["by_query"]
            print("    按查询类拆分:")
            for k, v in sorted(bq.items()):
                line = (f"    {k:<10} n={v['n']:<6} err={v['errors']:<6} "
                        f"p99={v['p99_ms']}ms")
                if v["errors"] and v.get("last_error"):
                    line += f"\n      └─ {v['last_error']}"
                print(line)
        except Exception as exc:
            print(f"    读指标文件失败: {exc}")

        kpi, reg = env.verify()
        try:
            final = metrics.eval_expr(spec["success"]["outcome"], kpi)
        except Exception:
            final = False
        print(f"\n  最终: p99={kpi.p99_ms}ms errors={kpi.errors} "
              f"cpu={kpi.cpu_pct}%")
        print(f"  {'PASS' if final else 'FAIL'}  人工正解能否达到成功判据")
        print(f"  回归套件: {reg.passed}")
        ok &= final
    print()

print("=" * 76)
print("人工正解可达性:", "PASS" if ok else "FAIL")
print("若为 FAIL，说明问题在判据或注入器，不在 agent")
