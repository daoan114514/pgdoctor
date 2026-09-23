#!/usr/bin/env python3
"""explain_query 对 DML 的只读代理：改写规则必须精确，且不碰非 DML。

2026-09-22 实测：只读角色对 UPDATE 连纯 EXPLAIN 都是 InsufficientPrivilege
（权限检查在 ExecutorStart），而同表同 WHERE 的 SELECT 能拿到一致的访问路径。
lock_contention 的热查询正是 UPDATE，之前三个 need 因此不可得、ESC EXHAUSTED。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sandbox.observe import _readonly_proxy  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, got=None) -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  得到: {got!r}" if not cond else ""))


print("[1] DML 改写")
sql, mode = _readonly_proxy("UPDATE orders SET status = 'PAID' WHERE id = %(uid)s")
check(mode == "select_proxy" and sql == "SELECT 1 FROM orders WHERE id = %(uid)s",
      "UPDATE … WHERE id = %(uid)s -> SELECT 1 FROM orders WHERE id = %(uid)s", (sql, mode))
sql, mode = _readonly_proxy("DELETE FROM orders WHERE created_at < now() - interval '1 day' AND status = 'X'")
check(mode == "select_proxy" and sql.startswith("SELECT 1 FROM orders WHERE") and "status = 'X'" in sql,
      "DELETE 带复合 WHERE 保留整个条件", (sql, mode))
sql, mode = _readonly_proxy("UPDATE orders SET status = 'X'")
check(mode == "select_proxy" and sql == "SELECT 1 FROM orders", "无 WHERE 的 UPDATE -> 全表 SELECT", (sql, mode))
sql, mode = _readonly_proxy("UPDATE orders o SET status = 'X' WHERE o.user_id = %(uid)s AND o.status = %(st)s")
check(mode == "select_proxy" and "%(uid)s" in sql and "%(st)s" in sql and "$1" not in sql,
      "多个占位符全部换回 %(name)s", (sql, mode))

print("[2] 非 DML 原样")
for q in ("SELECT id, total FROM orders WHERE user_id = %(uid)s AND status = 'PENDING'",
          "INSERT INTO orders (id) VALUES (1)",
          "SELECT 1 FROM orders WHERE created_at > now() - interval '1 day'"):
    sql, mode = _readonly_proxy(q)
    check(mode == "analyze" and sql == q, f"原样: {q[:50]}", (sql, mode))

print("[3] 解析失败不猜")
sql, mode = _readonly_proxy("UPDATE orders SET WHERE (")
check(mode == "analyze" and sql == "UPDATE orders SET WHERE (", "语法错误的 UPDATE 原样返回（让它去撞真实错误）", (sql, mode))

print()
if fails:
    print(f"EXPLAIN DML PROXY: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"EXPLAIN DML PROXY: PASS（{checks} 项）")
