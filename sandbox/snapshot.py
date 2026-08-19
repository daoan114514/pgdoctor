"""快照与回滚。

每个 episode 之间必须回到完全相同的健康态，否则实验不可复现。
逻辑故障走 `CREATE DATABASE ... TEMPLATE golden` —— 秒级克隆。
(磁盘满 / 复制槽这类故障要到 W8 才需要容器级重建。)
"""
from __future__ import annotations

import time

from sandbox import db

GOLDEN = "shop_golden"


def _terminate(dbname: str) -> None:
    """踢掉目标库上的所有连接，否则 DROP/TEMPLATE 会被拒。"""
    db.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (dbname,),
        dbname="postgres",
    )


def _exists(dbname: str) -> bool:
    rows = db.query(
        "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,), dbname="postgres"
    )
    return bool(rows)


def create_golden(force: bool = False) -> None:
    """把当前 shop 的状态固化成 golden 模板。seed 完成后调一次。"""
    if _exists(GOLDEN):
        if not force:
            print(f"[snapshot] {GOLDEN} already exists")
            return
        _terminate(GOLDEN)
        db.execute(f'DROP DATABASE "{GOLDEN}"', dbname="postgres")

    _terminate(db.PG_DB)
    t0 = time.time()
    db.execute(f'CREATE DATABASE "{GOLDEN}" TEMPLATE "{db.PG_DB}"', dbname="postgres")
    print(f"[snapshot] golden created in {time.time() - t0:.1f}s")


# 集群级互斥锁的 key。DROP + CREATE DATABASE 不是原子的，两个 episode
# 并发 reset 会撞成 "database already exists"。加锁让并发跑批时自然串行化。
_RESET_LOCK = 728301


def reset() -> None:
    """回滚到健康基线。episode 开始时调。"""
    if not _exists(GOLDEN):
        raise RuntimeError(f"{GOLDEN} missing — 先跑 create_golden()")
    with db.connect(dbname="postgres") as lock_conn, lock_conn.cursor() as lc:
        lc.execute("SELECT pg_advisory_lock(%s)", (_RESET_LOCK,))
        try:
            _do_reset()
        finally:
            lc.execute("SELECT pg_advisory_unlock(%s)", (_RESET_LOCK,))


def _do_reset() -> None:
    _terminate(db.PG_DB)
    t0 = time.time()
    db.execute(f'DROP DATABASE IF EXISTS "{db.PG_DB}"', dbname="postgres")
    db.execute(f'CREATE DATABASE "{db.PG_DB}" TEMPLATE "{GOLDEN}"', dbname="postgres")
    # pg_stat_statements 是集群级的，克隆库不会清掉它 —— 必须显式重置，
    # 否则上个 episode 的慢查询统计会污染这次的观测。
    try:
        db.execute("SELECT pg_stat_statements_reset()")
    except Exception as exc:  # 扩展未就绪时不致命
        print(f"[snapshot] warn: pg_stat_statements_reset failed: {exc}")

    # pg_stat_user_tables 是每库的运行时统计，DROP/CREATE DATABASE 后归零：
    # 不补这一步，episode 开局看到的 n_live_tup 会是几百而不是一千两百万，
    # last_analyze 也是空的 —— 而鉴别诊断正是靠它来排除"统计信息过期"。
    # 真实生产库不会处于这种状态，所以补 ANALYZE 让基线更保真。
    db.execute("ANALYZE")
    print(f"[snapshot] reset to golden in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "create":
        create_golden(force="--force" in sys.argv)
    elif cmd == "reset":
        reset()
    else:
        print(f"golden exists: {_exists(GOLDEN)}")
