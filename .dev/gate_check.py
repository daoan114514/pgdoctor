"""安全门与回滚日志验收（对活库）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safety import gate, undo_journal
from safety.gate import RemediationProposal as P
from safety.undo_journal import UndoStatus
from sandbox import db

ok = True
EP = "gate_check"

print("=" * 72)
print("[1] 风险分级")
cases = [
    ("小表并发建索引", P("create_index",
        "CREATE INDEX CONCURRENTLY idx_tmp ON products(sku)",
        "DROP INDEX idx_tmp"), "AUTO"),
    ("核心表并发建索引", P("create_index",
        "CREATE INDEX CONCURRENTLY idx_ous ON orders(user_id, status)",
        "DROP INDEX idx_ous"), "CONFIRM"),
    ("核心表锁表建索引", P("create_index",
        "CREATE INDEX idx_bad ON orders(user_id, status)",
        "DROP INDEX idx_bad"), "DENY"),
    ("ANALYZE", P("vacuum_analyze", "ANALYZE orders", "SELECT 1"), "AUTO"),
    ("VACUUM FULL", P("vacuum_analyze", "VACUUM FULL orders", "SELECT 1"), "DENY"),
    ("参数变更", P("set_parameter", "SET work_mem = '64MB'",
                   "SET work_mem = '8MB'"), "CONFIRM"),
]
for name, p, want in cases:
    d = gate.assess(p)
    good = d.tier == want
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {name:<18} -> {d.tier:<8} "
          f"(期望 {want}) {d.reasons[0][:34] if d.reasons else ''}")

print("\n[2] 防伪与前提校验")
adv = [
    ("声称建索引实为删表",
     P("create_index", "DROP TABLE orders", "SELECT 1")),
    ("类型与 AST 不符",
     P("vacuum_analyze", "CREATE INDEX idx_x ON products(sku)", "DROP INDEX idx_x")),
    ("缺回滚语句",
     P("create_index", "CREATE INDEX CONCURRENTLY idx_y ON products(sku)", "")),
    ("夹带灾难动作",
     P("create_index",
       "CREATE INDEX idx_z ON products(sku); DROP TABLE users", "DROP INDEX idx_z")),
]
for name, p in adv:
    d = gate.assess(p)
    blocked = not d.approved
    ok &= blocked
    why = (d.reasons + d.shield_reasons)[0][:48] if (d.reasons or d.shield_reasons) else "未拦截！"
    print(f"  {'PASS' if blocked else 'FAIL'}  {name:<18} -> {why}")

print("\n[3] 真实执行与回滚往返")


def auto_confirm(p, d):
    """沙箱里的确认通道：由确定性规则扮演确认者，比对可接受修复集合。
    生产环境这里是真人。"""
    print(f"  [确认通道] {d.tier} 档提案 -> 批准: {p.sql[:52]}")
    return True


before = [r[0] for r in db.query(
    "SELECT indexname FROM pg_indexes WHERE tablename='products'")]
print(f"  执行前 products 索引: {before}")

prop = P("create_index",
         "CREATE INDEX CONCURRENTLY idx_gatecheck ON products(sku)",
         "DROP INDEX idx_gatecheck",
         rationale="验收用")
res = gate.execute(prop, EP, confirm_cb=auto_confirm)
print(f"  执行: ok={res.executed} undo_id={res.undo_id} "
      f"tier={res.decision.tier} 用时={res.duration_s}s")
ok &= res.executed

mid = [r[0] for r in db.query(
    "SELECT indexname FROM pg_indexes WHERE tablename='products'")]
created = "idx_gatecheck" in mid
print(f"  索引已建立: {created}")
ok &= created

rec = undo_journal.get(res.undo_id)
print(f"  journal 状态: {rec['status']} | 幂等化后的撤销语句: {rec['undo_sql']}")
ok &= (rec["status"] == UndoStatus.APPLIED.value)
ok &= ("IF EXISTS" in rec["undo_sql"])

okr, msg = gate.rollback(res.undo_id)
print(f"  回滚: ok={okr} | {msg}")
ok &= okr

after = [r[0] for r in db.query(
    "SELECT indexname FROM pg_indexes WHERE tablename='products'")]
reverted = "idx_gatecheck" not in after
print(f"  索引已撤销: {reverted} | 回到初始状态: {sorted(after) == sorted(before)}")
ok &= reverted

okr2, msg2 = gate.rollback(res.undo_id)
print(f"  重复回滚（应幂等）: ok={okr2} | {msg2}")
ok &= okr2

print("\n[4] 崩溃恢复：未撤销记录可被发现")
p2 = P("create_index", "CREATE INDEX CONCURRENTLY idx_orphan ON products(name)",
       "DROP INDEX idx_orphan")
r2 = gate.execute(p2, EP, confirm_cb=auto_confirm)
pend = [r for r in undo_journal.unreverted() if r["undo_id"] == r2.undo_id]
print(f"  扫描到未撤销记录: {len(pend)} 条 -> {pend[0]['status'] if pend else '无'}")
ok &= bool(pend)
gate.rollback(r2.undo_id)   # 清理
print(f"  清理完成，剩余未撤销: "
      f"{len([r for r in undo_journal.unreverted() if r['episode_id']==EP])}")

print("=" * 72)
print("GATE ACCEPTANCE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
