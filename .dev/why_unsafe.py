"""查 connection_exhaustion 为何诊断对了却 Safe Pass 失分。"""
import json
from pathlib import Path

R = Path("/home/daoan/pgdoctor/eval/results")

for tag in ("llm_eval_final", "llm_eval_v3"):
    p = R / f"{tag}.json"
    if not p.exists():
        continue
    d = json.loads(p.read_text(encoding="utf-8"))
    for e in d["episodes"]:
        if e["fault_class"] != "connection_exhaustion":
            continue
        print("=" * 76)
        print(f"[{tag}] {e['fault_class']}")
        print(f"  声称根因: {e['claimed']}")
        print(f"  D={e['diagnosis']} O={e['outcome']} S={e['safe_pass']}")
        print(f"  最终阶段: {e['final_phase']}  步数={e['steps']}")
        print(f"  ESC: {e['esc_verdicts']}")
        print(f"  执行的修复: {e['applied_sql']}")
        print(f"  阶段违规: {e['violations']}")
        print(f"  错误: {(e['error'] or '(无)')[:200]}")
        print()
