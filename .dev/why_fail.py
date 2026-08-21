"""挖清楚模型在新故障类型上到底卡在哪 —— 读轨迹，不花额度。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import esc as esc_mod
from eval import replay

R = Path("/home/daoan/pgdoctor/eval/results/llm_eval_fixed.json")
d = json.loads(R.read_text(encoding="utf-8"))

for e in d["episodes"]:
    if e["diagnosis"] or not e["fired"]:
        continue
    eid = e["episode_id"]
    print("=" * 78)
    print(f"{e['fault_class']}  (声称: {e['claimed']}, {e['steps']} 步, "
          f"${e['cost_usd']})")
    print(f"  最终阶段: {e['final_phase']}  ESC: {e['esc_verdicts']}")
    if not eid:
        print("  (无轨迹)")
        continue
    try:
        st = replay.load(eid)
    except Exception as exc:
        print(f"  轨迹读取失败: {exc}")
        continue

    print(f"\n  实际取到的证据类型:")
    kinds = {}
    for x in st.scratchpad:
        kinds.setdefault(x["evidence_type"], []).append(x["observation"])
    for k, v in kinds.items():
        print(f"    {k:<24} x{len(v)}  {v[0][:88]}")

    print(f"\n  假设台账:")
    for k, v in st.ledger.items():
        print(f"    {k:<24} {v.verdict:<14} {v.note[:70]}")

    rep = esc_mod.check(st)
    print(f"\n  ESC 复算: {rep.summary()}")
    for dd in rep.dims:
        print(f"    {dd.name} {'PASS' if dd.passed else 'FAIL'}"
              f"{'(必需)' if dd.mandatory else '      '} {dd.detail}")
    for dv in rep.directives[:4]:
        print(f"    补证: {dv}")
    print()
