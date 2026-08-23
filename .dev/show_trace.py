import json
import sys
from pathlib import Path

ROOT = Path("/mnt/c/Users/86173/Documents/github/pgdoctor")

for name in sys.argv[1:]:
    d = ROOT / "traces" / name
    print("=" * 80)
    print(d.name)
    print("=" * 80)
    st = json.loads((d / "episode_state.json").read_text(encoding="utf-8"))
    print(f"phase={st.get('phase')}  root_cause={st.get('confirmed_root_cause')}")
    for k in ("esc_verdicts", "hypotheses", "remediation_attempts",
              "gate_decisions", "proposals", "notes", "escalation_reason"):
        if st.get(k):
            print(f"\n-- {k} --")
            v = st[k]
            if isinstance(v, list):
                for item in v:
                    print("   ", json.dumps(item, ensure_ascii=False)[:400])
            else:
                print("   ", json.dumps(v, ensure_ascii=False)[:600])
    other = {k: v for k, v in st.items()
             if k not in ("esc_verdicts", "hypotheses", "remediation_attempts",
                          "gate_decisions", "proposals", "notes", "phase",
                          "confirmed_root_cause", "escalation_reason",
                          "evidence", "scratchpad", "tool_trace")}
    print("\n-- 其他字段 --")
    for k, v in other.items():
        print(f"   {k}: {json.dumps(v, ensure_ascii=False)[:200]}")

    steps = sorted(d.glob("step_*.json"))
    print(f"\n-- {len(steps)} 个 step --")
    for f in steps:
        s = json.loads(f.read_text(encoding="utf-8"))
        print(f"   {f.stem}: phase={s.get('phase')} "
              f"tools={[t.get('name') for t in s.get('tool_calls', [])]}"[:220])
    print()
