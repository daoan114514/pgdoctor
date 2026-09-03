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
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safety import undo_journal
from safety.gate import RemediationProposal, assess


def proposal(action_type, sql, rollback):
    if action_type == "vacuum_analyze":
        root_cause, fix_id = "stale_statistics", "analyze_table"
    elif action_type == "session_control":
        root_cause, fix_id = "lock_contention", "terminate_blocker"
    else:
        # DELETE/TRUNCATE 用例会先被护盾拦下；绑定一个真实节点，确保测到的
        # 是 SQL/marker 防线，而不是“缺少图上下文”的前置拒绝。
        root_cause, fix_id = "missing_index", "create_covering_index"
    return RemediationProposal(
        action_type=action_type, sql=sql, rollback=rollback,
        rationale="验收", target={"table": "orders"},
        root_cause=root_cause, fix_id=fix_id)

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
    d = assess(proposal(at, sql, rb))
    ok = d.approved is want
    bad += not ok
    reason = (d.reasons + d.shield_reasons or [""])[0][:46]
    print(f"  {'OK ' if ok else '!! '}{at:<16}{rb[:20]:<22}"
          f"{d.tier:<8}approved={d.approved!s:<6}{reason}")
    if not ok:
        print(f"      期望 approved={want} —— {why}")

# 兼容旧 journal：marker 曾被误当 SQL 执行并记成 UNDO_FAILED。它不是
# 未解决的数据库残留，回放时应归一为 APPLIED，同时保留 legacy_status。
original_journal = undo_journal.JOURNAL
with tempfile.TemporaryDirectory() as tmp:
    undo_journal.JOURNAL = Path(tmp) / "undo.jsonl"
    old = {
        "undo_id": "legacy_marker", "episode_id": "legacy",
        "action_type": "session_control", "forward_sql": "SELECT 1",
        "undo_sql": "IRREVERSIBLE", "status": "UNDO_FAILED",
        "error": "syntax error at or near IRREVERSIBLE",
    }
    undo_journal.JOURNAL.write_text(
        json.dumps(old, ensure_ascii=False) + "\n", encoding="utf-8")
    replayed = undo_journal.get("legacy_marker") or {}
    normalized = (replayed.get("status") == "APPLIED"
                  and replayed.get("legacy_status") == "UNDO_FAILED"
                  and not undo_journal.needs_attention())
undo_journal.JOURNAL = original_journal
print(f"  {'OK ' if normalized else '!! '}legacy marker journal normalized")
bad += not normalized

print()
print("ROLLBACK MARKERS:", "PASS" if bad == 0 else f"FAIL ({bad})")
sys.exit(1 if bad else 0)
