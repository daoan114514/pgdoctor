"""验证保留连接位机制：池子占满时诊断角色仍能连上。

这是 connection_exhaustion 这类故障的固有困境 —— 池子满了，agent
自己也连不上就没法诊断。PostgreSQL 16 的 reserved_connections +
pg_use_reserved_connections 正是为此设计：给诊断角色留位子，
而不必把它提成 superuser（那会毁掉只读权限隔离）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from sandbox import db

print("=" * 70)
maxc = int(db.query("SHOW max_connections")[0][0])
res = int(db.query("SHOW reserved_connections")[0][0])
sres = int(db.query("SHOW superuser_reserved_connections")[0][0])
print(f"max_connections={maxc} reserved={res} superuser_reserved={sres}")

granted = db.query(
    "SELECT pg_has_role('agent_ro', 'pg_use_reserved_connections', 'member')")[0][0]
print(f"agent_ro 拥有 pg_use_reserved_connections: {granted}")

# 用 app_user 填池子 —— 它没有保留位权限，正是被拒的那一方
held = []
print("\n用普通连接填满池子 ...")
for _ in range(maxc + 10):
    try:
        held.append(psycopg.connect(db.dsn("app"), autocommit=True))
    except Exception as exc:
        print(f"  填到第 {len(held)} 个时被拒: {str(exc)[:70]}")
        break

used = db.query("SELECT count(*) FROM pg_stat_activity")[0][0]
print(f"  当前连接 {used}/{maxc}")

print("\n此时诊断角色能否连上？")
try:
    c = psycopg.connect(db.dsn("ro"), connect_timeout=8)
    c.execute("SELECT 1")
    c.close()
    diag_ok = True
    print("  PASS  agent_ro 仍可连接（保留位生效）")
except Exception as exc:
    diag_ok = False
    print(f"  FAIL  agent_ro 被拒: {str(exc)[:90]}")

for c in held:
    try:
        c.close()
    except Exception:
        pass
print(f"\n已释放 {len(held)} 个连接")
print("=" * 70)
print("RESERVED SLOTS:", "PASS" if diag_ok else "FAIL")
sys.exit(0 if diag_ok else 1)
