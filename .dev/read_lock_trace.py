"""读 lock_contention 最近一次的完整轨迹，看主 agent 为何误诊。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval import replay

R = Path("/home/daoan/pgdoctor/eval/results/llm_eval_final.json")
d = json.loads(R.read_text(encoding="utf-8"))
ep = next(e for e in d["episodes"] if e["fault_class"] == "lock_contention")

print(f"episode: {ep['episode_id']}")
print(f"声称: {ep['claimed']}  阶段: {ep['final_phase']}  ESC: {ep['esc_verdicts']}")
print()

st = replay.load(ep["episode_id"])

print("=" * 78)
print("假设台账（含完整理由）")
print("=" * 78)
for k, v in st.ledger.items():
    print(f"\n{k}: {v.verdict}")
    print(f"  理由: {v.note[:400] or '(空)'}")

print()
print("=" * 78)
print("便签全文（按顺序）")
print("=" * 78)
for e in st.scratchpad:
    print(f"[{e['author']}] {e['evidence_type']}")
    print(f"   {e['observation'][:260]}")
