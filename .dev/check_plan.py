"""PLAN 阶段为何提交不出提案 —— 读 EpisodeState 的 outcome_note 与便签。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval import replay

d = json.loads(Path("/home/daoan/pgdoctor/eval/results/llm_two_v2.json")
               .read_text(encoding="utf-8"))
for e in d["episodes"]:
    print("=" * 74)
    print(e["fault_class"])
    st = replay.load(e["episode_id"])
    print(f"  outcome_note: {st.outcome_note[:400]}")
    print(f"  proposal: {st.proposal or '(空)'}")
    print(f"  repair_attempts: {st.repair_attempts}")
    print("  最后几条便签:")
    for x in st.scratchpad[-4:]:
        print(f"    [{x['evidence_type']}] {x['observation'][:150]}")
    print()

# 直接测：这两类的正解修复能不能过门
print("=" * 74)
print("直接测正解修复能否过门")
from safety import gate
from safety.gate import RemediationProposal as P

for label, p in [
    ("终止阻塞会话", P("session_control",
                        "SELECT pg_terminate_backend(12345)", "IRREVERSIBLE",
                        rationale="终止持有行锁的阻塞源")),
    ("ANALYZE 刷新统计", P("vacuum_analyze", "ANALYZE orders", "SELECT 1",
                            rationale="统计信息过期，重新收集")),
]:
    dd = gate.assess(p)
    print(f"\n  {label}: tier={dd.tier} approved={dd.approved}")
    for r in (dd.reasons + dd.shield_reasons)[:2]:
        print(f"    {r}")
