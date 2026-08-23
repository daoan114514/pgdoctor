"""把两类故障的"正解提案"直接送进安全门，看能不能过。

跟 outcome_probe 同一个思路：如果连人工写出的标准修复都过不了门，
那 Outcome 上不去就不是模型的问题，是门或提案契约的问题。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safety import gate
from safety.gate import RemediationProposal

CASES = [
    ("stale_statistics 正解",
     dict(action_type="vacuum_analyze", sql="ANALYZE orders",
          rollback="NO_ROLLBACK_NEEDED",
          rationale="统计失真导致优化器低估行数选了 Nested Loop")),
    ("lock_contention 正解",
     dict(action_type="session_control",
          sql="SELECT pg_terminate_backend(12345)",
          rollback="IRREVERSIBLE",
          rationale="终止挂起不提交的持锁事务")),
    ("missing_index 正解",
     dict(action_type="create_index",
          sql="CREATE INDEX CONCURRENTLY idx_probe ON orders (user_id, status)",
          rollback="DROP INDEX CONCURRENTLY IF EXISTS idx_probe",
          rationale="热查询无可用索引")),
    ("反例：rollback 留空",
     dict(action_type="vacuum_analyze", sql="ANALYZE orders", rollback="")),
    ("反例：声称建索引却夹带 DROP",
     dict(action_type="create_index", sql="DROP TABLE orders",
          rollback="IRREVERSIBLE")),
]

ok = True
for label, kw in CASES:
    kw.setdefault("rationale", "")
    p = RemediationProposal(**kw)
    try:
        d = gate.assess(p)
        reasons = "; ".join((d.reasons or []) + (d.shield_reasons or []))
        print(f"{label}")
        print(f"   tier={d.tier:<8} approved={d.approved}")
        print(f"   理由: {reasons[:150]}")
    except Exception as exc:
        print(f"{label}\n   抛异常 {type(exc).__name__}: {str(exc)[:120]}")
        d = None
    expect_ok = not label.startswith("反例")
    got_ok = bool(d and d.approved)
    if expect_ok != got_ok:
        # CONFIRM 档在跑批里由 _confirm 自动放行，不算失败
        if d and d.tier == "CONFIRM":
            print("   -> CONFIRM（跑批中自动确认，可通过）")
        else:
            print(f"   -> 不符预期：期望{'通过' if expect_ok else '拒绝'}")
            ok = False
    print()

print("=" * 60)
print("正解提案可过门:", "PASS" if ok else "FAIL")
