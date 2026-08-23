"""回滚字段三种写法的验收。

rollback 不是随便填的：
    具体 SQL             能撤销的动作
    IRREVERSIBLE         撤不回来，人来担责（终止会话）
    NO_ROLLBACK_NEEDED   本就无需撤销（ANALYZE 只重算统计）

最后一种是补出来的：原先门判 ANALYZE 为 AUTO（"只更新统计信息，
可自动执行"），却又强制要一条回滚语句 —— 模型两次提交正确的
ANALYZE orders 都被拒，诊断明明完全正确。

最要紧的是倒数第一条：标记绝不能成为夹带破坏性动作的通道。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safety.gate import RemediationProposal, assess

# (action_type, sql, rollback, 期望放行, 说明)
CASES = [
    ("vacuum_analyze", "ANALYZE orders;", "NO_ROLLBACK_NEEDED", True,
     "ANALYZE 显式声明无需回滚"),
    ("vacuum_analyze", "ANALYZE orders;", "", False,
     "留空要拒，且提示该写哪个标记"),
    ("vacuum_analyze", "ANALYZE orders;", "IRREVERSIBLE", False,
     "用错标记要拒：ANALYZE 不是不可逆，是无需撤销"),
    ("create_index", "CREATE INDEX CONCURRENTLY i ON orders (status)",
     "NO_ROLLBACK_NEEDED", False, "会改结构的动作不许声明无需回滚"),
    ("create_index", "CREATE INDEX CONCURRENTLY i ON orders (status)",
     "DROP INDEX CONCURRENTLY IF EXISTS i", True, "建索引给出真回滚"),
    ("session_control", "SELECT pg_terminate_backend(1)", "IRREVERSIBLE",
     True, "终止会话显式声明不可逆"),
    ("session_control", "SELECT pg_terminate_backend(1)", "", False,
     "终止会话留空要拒"),
    ("dml_delete", "DELETE FROM orders", "NO_ROLLBACK_NEEDED", False,
     "标记绝不能成为夹带破坏性动作的通道"),
    ("dml_delete", "TRUNCATE orders", "NO_ROLLBACK_NEEDED", False,
     "同上，护盾必须先拦住"),
]

bad = 0
for at, sql, rb, want, why in CASES:
    d = assess(RemediationProposal(action_type=at, sql=sql, rollback=rb,
                                   rationale="验收", target={"table": "orders"}))
    ok = d.approved is want
    bad += not ok
    reason = (d.reasons + d.shield_reasons or [""])[0][:46]
    print(f"  {'OK ' if ok else '!! '}{at:<16}{rb[:20]:<22}"
          f"{d.tier:<8}approved={d.approved!s:<6}{reason}")
    if not ok:
        print(f"      期望 approved={want} —— {why}")

print()
print("ROLLBACK MARKERS:", "PASS" if bad == 0 else f"FAIL ({bad})")
sys.exit(1 if bad else 0)
