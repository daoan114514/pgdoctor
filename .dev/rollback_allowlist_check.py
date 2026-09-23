#!/usr/bin/env python3
"""回滚语句白名单：按前向动作类别做 AST 配对，子串豁免不复存在。

2026-09-23 架构评审：gate 原来"回滚过不了护盾但含 DROP 就放行"，DROP TABLE orders CASCADE
也能过，而 gate.rollback() 以 agent_rw 不过盾直接执行日志里的 undo_sql。这里钉住三件事：
配对表的每个允许/拒绝形态；gate.assess 与 gate.rollback 都调用 inspect_rollback；
源码里不再有子串豁免。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from safety import gate  # noqa: E402
from safety.shield import inspect_rollback  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))


IDX = "CREATE INDEX CONCURRENTLY idx_orders_created_at ON orders (created_at)"
UPD = "UPDATE orders SET status = 'PAID' WHERE id = 42"
SET = "SET work_mem = '64MB'"
ALT = "ALTER TABLE orders SET (autovacuum_vacuum_scale_factor = 0.01)"
VAC = "ANALYZE orders"
KILL = "SELECT pg_terminate_backend(4242)"

print("[1] 建索引 ↔ DROP INDEX")
for rb, ok, why in (
    ("DROP INDEX CONCURRENTLY IF EXISTS idx_orders_created_at", True, "标准逆操作（含幂等改写后的形态）"),
    ("DROP INDEX idx_orders_created_at", True, "不带 CONCURRENTLY 也允许"),
    ("DROP INDEX public.idx_orders_created_at", True, "带 schema 前缀按最后一段比"),
    ("DROP INDEX idx_other", False, "索引名不一致"),
    ("DROP INDEX idx_orders_created_at CASCADE", False, "CASCADE 拒绝"),
    ("DROP TABLE orders CASCADE", False, "DROP TABLE 拒绝（原子串豁免的漏洞）"),
    ("DROP INDEX idx_orders_created_at; DELETE FROM orders", False, "多语句拒绝"),
    ("DELETE FROM orders -- drop", False, "含 drop 字样的 DELETE 拒绝（类别不配对且无 WHERE）"),
    ("TRUNCATE orders -- drop all", False, "TRUNCATE 拒绝"),
    ("DROP INDEX idx_orders_created_at, idx_other", False, "一次删多个索引拒绝"),
):
    v = inspect_rollback(IDX, rb)
    check(v.allowed is ok, f"{'允许' if ok else '拒绝'}: {why}", v.reasons)

print("[2] DML ↔ 过盾的 DML")
check(inspect_rollback(UPD, "UPDATE orders SET status = 'PENDING' WHERE id = 42").allowed, "带 WHERE 的反向 UPDATE 允许")
check(not inspect_rollback(UPD, "UPDATE orders SET status = 'PENDING'").allowed, "无 WHERE 的 UPDATE 拒绝")
check(not inspect_rollback(UPD, "DROP INDEX idx_x").allowed, "DML 的回滚不能是 DROP INDEX")
check(not inspect_rollback(UPD, "DELETE FROM orders WHERE id IN (SELECT id FROM orders); DROP TABLE orders").allowed, "夹带多语句拒绝")

print("[3] SET ↔ 同名 SET/RESET")
check(inspect_rollback(SET, "RESET work_mem").allowed, "RESET 同名允许")
check(inspect_rollback(SET, "SET work_mem = '4MB'").allowed, "SET 回原值允许")
check(not inspect_rollback(SET, "RESET statement_timeout").allowed, "不同参数名拒绝")
check(not inspect_rollback(SET, "ALTER SYSTEM SET work_mem = '4MB'").allowed, "ALTER SYSTEM 拒绝")

print("[4] ALTER TABLE 选项 ↔ 同表")
check(inspect_rollback(ALT, "ALTER TABLE orders RESET (autovacuum_vacuum_scale_factor)").allowed, "同表 RESET 选项允许")
check(not inspect_rollback(ALT, "ALTER TABLE users RESET (autovacuum_vacuum_scale_factor)").allowed, "不同表拒绝")
check(not inspect_rollback(ALT, "ALTER TABLE orders DROP COLUMN status").allowed, "非受控子类型拒绝")

print("[5] 标记与不配对的类别")
check(inspect_rollback(VAC, "NO_ROLLBACK_NEEDED").allowed, "自愈类 + NO_ROLLBACK_NEEDED 放行（由 gate 按类别校验）")
check(inspect_rollback(VAC, "SELECT 1").allowed, "自愈类 + 只读 SELECT 允许（无害，gate_check 的用例）")
check(inspect_rollback(VAC, "VACUUM orders").allowed, "自愈类 + 再一次 VACUUM 允许")
check(not inspect_rollback(VAC, "UPDATE orders SET status = 'X' WHERE id = 1").allowed, "自愈类 + DML 拒绝（回滚不能成为写入口）")
check(not inspect_rollback(VAC, "SELECT pg_terminate_backend(1)").allowed, "自愈类 + 带副作用函数的 SELECT 拒绝")
check(inspect_rollback(KILL, "IRREVERSIBLE").allowed, "会话控制 + IRREVERSIBLE 放行")
check(not inspect_rollback(KILL, "SELECT 1").allowed, "会话控制给了 SQL 回滚 -> 拒绝")
check(not inspect_rollback(IDX, "not sql at all").allowed, "解析失败拒绝")

print("[6] gate 两处都走白名单，子串豁免已删")
src = inspect.getsource(gate)
check(src.count("shield.inspect_rollback(") >= 2, "gate.assess 与 gate.rollback 都调用 inspect_rollback", src.count("shield.inspect_rollback("))
check('"DROP" not in' not in src, "源码里不再有 DROP 子串豁免")
rb_src = inspect.getsource(gate.rollback)
check(rb_src.index("inspect_rollback(") < rb_src.index('cur.execute(rec["undo_sql"])'), "rollback() 在执行 undo_sql 之前校验")

print()
if fails:
    print(f"ROLLBACK ALLOWLIST: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"ROLLBACK ALLOWLIST: PASS（{checks} 项）")
