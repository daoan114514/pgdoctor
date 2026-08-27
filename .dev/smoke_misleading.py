"""对着活库验新注入器真的造出了故障 —— 而且造出的是"误导性"的那种。

91dea7d 挖出的四个 bug 全在沙箱这一侧，教训是：注入器"跑完没报错"
不等于"故障真的存在"。这里验三件事：
  1. 连接确实被占满（表象成立，否则告警都不会响）
  2. 会话确实处于 idle in transaction（真根因成立）
  3. 阻塞链是空的（没有意外退化成 lock_contention）

第 3 条最关键：如果这些事务顺手持了行锁，场景就从"干净的误导"
变成"锁竞争 + 连接打满"的混合体，鉴别诊断的考点就废了。
"""
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox import db
from sandbox.injectors.misleading import IdleTransactionPileupInjector

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}   {detail}")
    if not cond:
        fails.append(name)


def snap() -> dict:
    rows = db.query(
        "SELECT state, count(*) FROM pg_stat_activity "
        "WHERE pid <> pg_backend_pid() GROUP BY state")
    return {(r[0] or "null"): r[1] for r in rows}


SPEC = yaml.safe_load(
    (Path(__file__).resolve().parent.parent /
     "sandbox/scenarios/misleading_idle_txn_train_v1.yaml"
     ).read_text(encoding="utf-8"))

maxc = int(db.query("SHOW max_connections")[0][0])
print(f"注入前: {snap()}  (max_connections={maxc})")

inj = IdleTransactionPileupInjector(SPEC)
params = inj.params(__import__("random").Random(7))
rec = None
try:
    rec = inj.inject(params)
    print(f"\n注入记录: {rec.notes}\n")
    time.sleep(1.0)

    used = db.query("SELECT count(*) FROM pg_stat_activity")[0][0]
    idle_txn = db.query(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE state = 'idle in transaction'")[0][0]
    blocked = db.query(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE cardinality(pg_blocking_pids(pid)) > 0")[0][0]
    print(f"注入后: {snap()}")
    print()

    print("[1] 表象要成立：连接逼近上限")
    check("连接数逼近上限", used >= maxc - params["leave_free"] - 4,
          f"{used}/{maxc}")

    print("\n[2] 真根因要成立：会话挂在 idle in transaction")
    check("idle in transaction 数量达标",
          idle_txn >= params["min_idle_txn"], f"{idle_txn} 个")
    check("verify_injected 认可", inj.verify_injected(params) is True)

    print("\n[3] 不能退化成锁竞争（否则就不是干净的误导）")
    check("阻塞链为空", blocked == 0, f"被阻塞会话 {blocked} 个")

    print("\n[4] 判别证据取得到，且取值指向真根因")
    from sandbox.observe import Observer
    from agent.esc import _supports
    o = Observer()
    st = o.get_connection_stats()
    obs = (f"连接 {st['used']}/{st['max_connections']} ({st['pct']}%), "
           f"逼近上限={st['near_limit']}, "
           f"idle in transaction={st['idle_in_transaction']}, "
           f"按角色={st['by_user']}")
    print(f"    工具返回: {obs}")
    check("工具回出了 idle_in_transaction",
          st["idle_in_transaction"] >= params["min_idle_txn"])
    check("ESC 认这条证据支持真根因",
          _supports("idle_in_transaction", obs, "long_idle_transaction")
          is True)
finally:
    inj.cleanup()
    time.sleep(0.6)
    print(f"\n清理后: {snap()}")
    left = db.query("SELECT count(*) FROM pg_stat_activity "
                    "WHERE state = 'idle in transaction'")[0][0]
    check("清理后没有残留的挂起事务", left == 0, f"残留 {left} 个")

print()
print("=" * 60)
print("SMOKE MISLEADING: PASS" if not fails
      else f"SMOKE MISLEADING: FAIL {fails}")
sys.exit(1 if fails else 0)
