import json
import sys
from pathlib import Path

ROOT = Path("/mnt/c/Users/86173/Documents/github/pgdoctor")

for name in sys.argv[1:]:
    d = json.loads((ROOT / "eval/results" / f"{name}.json").read_text(
        encoding="utf-8"))
    print("=" * 78)
    print(name)
    for e in d.get("episodes", []):
        print(f"\n-- {e.get('scenario')}  D={e.get('diagnosis')} "
              f"O={e.get('outcome')}  note={e.get('outcome_note')}")
        gd = e.get("gate_decisions") or []
        if not gd:
            print("   （没有任何门决策记录 —— 说明根本没走到 GATE）")
        for g in gd:
            print(f"   tier={str(g.get('tier')):<8} approved={g.get('approved')}")
            print(f"     sql: {str(g.get('sql'))[:160]}")
            for r in g.get("reasons") or []:
                print(f"     理由: {str(r)[:160]}")
        for a in e.get("applied_sql") or []:
            print(f"   已执行: {str(a)[:130]}")
        for k in ("esc_verdicts", "final_phase", "steps"):
            if e.get(k) is not None:
                print(f"   {k}: {e[k]}")
