"""查 lock_contention 的修复提案为什么被安全门连续拒绝。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
d = json.loads((ROOT / "eval/results/llm_two_fixed.json")
               .read_text(encoding="utf-8"))
e = next(x for x in d["episodes"] if x["fault_class"] == "lock_contention")

print("门裁决记录:")
for g in e.get("gate_decisions", []):
    print(f"  tier={g['tier']} approved={g['approved']}")
    print(f"    SQL: {g['sql'][:100]}")
    for r in g["reasons"][:3]:
        print(f"    原因: {r}")
    print()

# 这类故障的正解是终止阻塞会话，看看门会怎么裁
sys.path.insert(0, str(ROOT))
from safety import gate
from safety.gate import RemediationProposal

print("=" * 74)
print("直接测：终止阻塞会话的提案能否过门")
for sql, rb in [
    ("SELECT pg_terminate_backend(12345)", "SELECT 1"),
    ("SELECT pg_cancel_backend(12345)", "SELECT 1"),
]:
    p = RemediationProposal(
        action_type="session_control", sql=sql, rollback=rb,
        root_cause="lock_contention", fix_id="terminate_blocker")
    dd = gate.assess(p)
    print(f"\n  {sql}")
    print(f"    tier={dd.tier} approved={dd.approved}")
    for r in (dd.reasons + dd.shield_reasons)[:3]:
        print(f"    {r}")
