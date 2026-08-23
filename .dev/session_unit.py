"""会话控制的门禁验证，外加护盾回归 —— 别为了放行新动作而放松旧防线。"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safety import gate
from safety.gate import RemediationProposal as P
from safety.shield import classify, inspect_sql

ok = True
print("=" * 74)
print("[1] 语法是 SELECT、语义有副作用的函数要被正确归类")
for sql, want in [
    ("SELECT pg_terminate_backend(123)", "session_control"),
    ("SELECT pg_cancel_backend(123)", "session_control"),
    ("SELECT pg_reload_conf()", "config_reload"),
    ("SELECT count(*) FROM orders", "select"),
    ("SELECT id FROM orders WHERE status = 'PENDING'", "select"),
]:
    got = classify(sql)
    good = got == want
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {sql[:44]:<46} -> {got}")

print("\n[2] 终止会话的提案能过门（需确认档）")
p = P("session_control", "SELECT pg_terminate_backend(12345)", "IRREVERSIBLE",
      rationale="终止持有行锁不放的阻塞源会话")
d = gate.assess(p)
good = d.approved and d.tier == "CONFIRM"
ok &= good
print(f"  {'PASS' if good else 'FAIL'}  tier={d.tier} approved={d.approved}")
for r in d.reasons[:2]:
    print(f"        {r}")

print("\n[3] 不写 IRREVERSIBLE 也不给回滚 -> 应被拒并说明怎么改")
d2 = gate.assess(P("session_control", "SELECT pg_terminate_backend(1)", ""))
ok &= not d2.approved
print(f"  {'PASS' if not d2.approved else 'FAIL'}  {d2.reasons[0][:80]}")

print("\n[4] 只有会话控制能声明不可逆，别的动作不行")
d3 = gate.assess(P("create_index",
                   "CREATE INDEX CONCURRENTLY i ON orders(status)",
                   "IRREVERSIBLE"))
ok &= not d3.approved
print(f"  {'PASS' if not d3.approved else 'FAIL'}  {d3.reasons[0][:80]}")

print("\n[5] 伪装成只读的会话控制 -> 类型不符应被拒")
d4 = gate.assess(P("select", "SELECT pg_terminate_backend(999)", "SELECT 1"))
ok &= not d4.approved
print(f"  {'PASS' if not d4.approved else 'FAIL'}  {d4.reasons[0][:80]}")

print("\n[6] 护盾回归：23 项对抗测试")
r = subprocess.run([sys.executable, ".dev/shield_check.py"],
                   cwd="/home/daoan/pgdoctor", capture_output=True, text=True)
passed = "SHIELD: PASS" in r.stdout
ok &= passed
print(f"  {'PASS' if passed else 'FAIL'}  {r.stdout.strip().splitlines()[-1]}")
if not passed:
    print(r.stdout[-1200:])

print("\n[7] ESC 回归：六个离线场景")
r2 = subprocess.run([sys.executable, ".dev/esc_check.py"],
                    cwd="/home/daoan/pgdoctor", capture_output=True, text=True)
passed2 = "ESC OFFLINE: PASS" in r2.stdout
ok &= passed2
print(f"  {'PASS' if passed2 else 'FAIL'}  {r2.stdout.strip().splitlines()[-1]}")

print("\n" + "=" * 74)
print("SESSION CONTROL:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
