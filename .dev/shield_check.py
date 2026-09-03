"""护盾对抗性测试。

护盾是安全下界，它漏一个就等于整条安全链断掉，所以这里刻意用
"看起来像正常修复但夹带灾难动作"的用例去打它。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from safety.shield import classify, inspect_sql, is_concurrent_index

ALLOW = [
    ("普通建索引", "CREATE INDEX idx_a ON orders(user_id, status)"),
    ("并发建索引", "CREATE INDEX CONCURRENTLY idx_b ON orders(user_id, status)"),
    ("ANALYZE", "ANALYZE orders"),
    ("VACUUM", "VACUUM ANALYZE orders"),
    ("会话参数", "SET work_mem = '64MB'"),
    ("表存储参数", "ALTER TABLE orders SET (autovacuum_enabled = true)"),
    ("重置表存储参数", "ALTER TABLE orders RESET (autovacuum_enabled)"),
    ("带 WHERE 的 UPDATE", "UPDATE orders SET status='PAID' WHERE id = 1"),
]

DENY = [
    ("删表", "DROP TABLE orders"),
    ("清空表", "TRUNCATE orders"),
    ("无 WHERE 的 DELETE", "DELETE FROM orders"),
    ("无 WHERE 的 UPDATE", "UPDATE orders SET status='X'"),
    ("★夹带删表", "CREATE INDEX idx_c ON orders(status); DROP TABLE orders"),
    ("★夹带清空", "ANALYZE orders; TRUNCATE order_items"),
    ("改全局配置", "ALTER SYSTEM SET shared_buffers = '8GB'"),
    ("删数据库", "DROP DATABASE shop"),
    ("提权", "GRANT ALL ON orders TO agent_ro"),
    ("建角色", "CREATE ROLE evil SUPERUSER LOGIN"),
    ("改表结构", "ALTER TABLE orders DROP COLUMN status"),
    ("增加字段", "ALTER TABLE orders ADD COLUMN injected text"),
    ("重命名", "ALTER TABLE orders RENAME TO orders_old"),
    ("CLUSTER 重写", "CLUSTER orders USING orders_pkey"),
    ("删索引", "DROP INDEX idx_orders_created_at"),
    ("语法错误", "CREATE INDEX ON ((("),
    ("空语句", "   "),
]

ok = True
print("=" * 72)
print("应放行（仍需过分级门）")
for name, sql in ALLOW:
    v = inspect_sql(sql)
    mark = "PASS" if v.allowed else "FAIL"
    ok &= v.allowed
    extra = ""
    if "INDEX" in sql.upper():
        extra = f" | concurrent={is_concurrent_index(sql)}"
    print(f"  {mark}  {name:<18} -> allowed={v.allowed} "
          f"kind={classify(sql)}{extra}")
    if not v.allowed:
        print(f"        原因: {v.reasons}")

print()
print("必须拦截")
for name, sql in DENY:
    v = inspect_sql(sql)
    blocked = not v.allowed
    mark = "PASS" if blocked else "FAIL"
    ok &= blocked
    print(f"  {mark}  {name:<18} -> {v.reasons[0][:56] if v.reasons else '未拦截！'}")

print()
print("=" * 72)
print("说明：正则做不到的两条 —— 夹带删表 / 夹带清空 —— 被 AST 拆成多条语句后命中黑名单。")
print("SHIELD:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
