"""数据库连接层。

三套凭据对应三种身份，这个划分是整个安全架构的物理基础：
  - superuser : 沙箱自身用（注入故障、建快照、判分），agent 永远拿不到
  - agent_ro  : agent 唯一持有的连接，纯只读
  - agent_rw  : 仅安全门 (remediation_server) 持有，agent 永远拿不到
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg

PG_HOST = os.getenv("PGDOCTOR_HOST", "localhost")
PG_PORT = int(os.getenv("PGDOCTOR_PORT", "55432"))
PG_DB = os.getenv("PGDOCTOR_DB", "shop")

_CREDS = {
    "super": ("postgres", "postgres"),
    "ro": ("agent_ro", "ro_pw_dev_only"),
    "rw": ("agent_rw", "rw_pw_dev_only"),
    # 业务应用角色：无保留连接位，池子满时它先被拒 ——
    # 这样"应用挂了但诊断还能进去"才成立
    "app": ("app_user", "app_pw_dev_only"),
}


def dsn(role: str = "super", dbname: str | None = None) -> str:
    user, pw = _CREDS[role]
    return (
        f"host={PG_HOST} port={PG_PORT} dbname={dbname or PG_DB} "
        f"user={user} password={pw}"
    )


@contextmanager
def connect(role: str = "super", dbname: str | None = None, autocommit: bool = True):
    conn = psycopg.connect(dsn(role, dbname), autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()


def query(sql: str, params=None, role: str = "super", dbname: str | None = None):
    """跑一条查询，返回 list[tuple]。沙箱内部用，不是 agent 的工具。"""
    with connect(role, dbname) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def execute(sql: str, params=None, role: str = "super", dbname: str | None = None):
    with connect(role, dbname) as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def wait_ready(timeout_s: float = 120.0) -> bool:
    """等数据库可连接。容器刚起来时用。"""
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            query("SELECT 1")
            return True
        except Exception:
            time.sleep(1.0)
    return False
