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


def reset() -> None:
    """回滚到健康基线。episode 开始时调。"""
    if not _exists(GOLDEN):
        raise RuntimeError(f"{GOLDEN} missing — 先跑 create_golden()")
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
