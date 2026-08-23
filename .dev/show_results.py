import json
import sys
from pathlib import Path

ROOT = Path("/mnt/c/Users/86173/Documents/github/pgdoctor")

for name in sys.argv[1:]:
    f = ROOT / "eval/results" / f"{name}.json"
    if not f.exists():
        print(f"=== {name}: 不存在 ===")
        continue
    d = json.loads(f.read_text(encoding="utf-8"))
    eps = d.get("episodes", d if isinstance(d, list) else [])
    print(f"=== {name}  ({len(eps)} episodes) ===")
    for e in eps:
        scen = str(e.get("scenario") or e.get("scenario_id") or "?")[:30]
        print(f"  {scen:<30} D={str(e.get('diagnosis')):<5} "
              f"O={str(e.get('outcome')):<5} S={str(e.get('safe_pass')):<5} "
              f"claim={str(e.get('claimed_root_cause'))[:20]:<20} "
              f"phase={str(e.get('final_phase'))[:12]:<12} "
              f"usable={e.get('usable')} steps={e.get('steps')}")
        if e.get("applied_sql"):
            for s in e["applied_sql"]:
                print(f"      执行: {str(s)[:90]}")
        if e.get("esc_verdicts"):
            print(f"      ESC: {e['esc_verdicts']}")
        if e.get("error"):
            print(f"      错误: {str(e['error'])[:110]}")
    if d.get("summary"):
        print("  summary:", d["summary"])
    print()
