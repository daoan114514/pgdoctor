"""正解修复能不能过安全门？

诊断准确但修复动作被门 DENY 的话，Outcome 永远拿不到 —— 而这种失败
看起来和"模型不会修"一模一样，必须先排除。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safety.gate import RemediationProposal, assess

CASES = [
    ("missing_index",
     "CREATE INDEX CONCURRENTLY idx_probe ON orders (status)",
     "DROP INDEX CONCURRENTLY IF EXISTS idx_probe"),
    ("stale_statistics", "ANALYZE orders", "SELECT 1"),
    ("lock_contention", "SELECT pg_terminate_backend(32363)", "IRREVERSIBLE"),
]

for fault, sql, undo in CASES:
    p = RemediationProposal(
        action_type="", sql=sql, rollback=undo,
        rationale="探针：人工正解修复", target={"table": "orders"})
    # action_type 交给门自己按 AST 对齐（之前踩过 analyze/vacuum_analyze
    # 命名不一致导致提案连拒两次的坑）
    for at in ("create_index", "vacuum_analyze", "analyze", "session_control", "terminate_session",
               "kill_session"):
        p.action_type = at
        d = assess(p)
        if d.approved or d.tier != "DENY":
            break
    print(f"\n{fault}")
    print(f"  SQL       {sql}")
    print(f"  裁决      tier={d.tier}  approved={d.approved}  "
          f"(action_type={p.action_type})")
    print(f"  风险      {d.risk}")
    for r in d.reasons:
        print(f"  理由      {r}")
    for r in d.shield_reasons:
        print(f"  护盾      {r}")
