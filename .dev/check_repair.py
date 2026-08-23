"""诊断对了但修复没成 —— 卡在哪一环？"""
import json
from pathlib import Path

d = json.loads(Path("/home/daoan/pgdoctor/eval/results/llm_two_v2.json")
               .read_text(encoding="utf-8"))
for e in d["episodes"]:
    print("=" * 74)
    print(f"{e['fault_class']}   声称={e['claimed']}  阶段={e['final_phase']}")
    print(f"  D={e['diagnosis']} O={e['outcome']} S={e['safe_pass']}  "
          f"步数={e['steps']}")
    print(f"  ESC: {e['esc_verdicts']}")
    print(f"  已执行: {e['applied_sql'] or '(无)'}")
    print(f"  错误: {(e['error'] or '(无)')[:150]}")
    print("  门裁决:")
    for g in e.get("gate_decisions", []):
        print(f"    tier={g['tier']} approved={g['approved']}")
        print(f"      SQL: {g['sql'][:110]}")
        for r in g["reasons"][:3]:
            print(f"      原因: {r}")
    if not e.get("gate_decisions"):
        print("    (没有提案到达安全门)")
    print()
