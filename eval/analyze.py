"""对比两个跑批，并挖清楚模型为何在新故障类型上没赢。"""
import json
from pathlib import Path

R = Path("/home/daoan/pgdoctor/eval/results")


def load(tag):
    return json.loads((R / f"{tag}.json").read_text(encoding="utf-8"))


s, l = load("scripted_eval"), load("llm_eval")
by_fault = {}
for src, d in (("scripted", s), ("llm", l)):
    for e in d["episodes"]:
        by_fault.setdefault(e["fault_class"], {})[src] = e

print("=" * 84)
print(f"{'故障类':<24} {'策略':<10} {'声称根因':<18} {'D':<3}{'O':<3}{'S':<3} "
      f"{'步数':<5} {'ESC':<28} 成本")
print("-" * 84)
for fc in sorted(by_fault):
    for src in ("scripted", "llm"):
        e = by_fault[fc].get(src)
        if not e:
            continue
        esc = ",".join(x[:4] for x in e["esc_verdicts"]) or "-"
        print(f"{fc:<24} {src:<10} {str(e['claimed']):<18} "
              f"{'Y' if e['diagnosis'] else '.':<3}"
              f"{'Y' if e['outcome'] else '.':<3}"
              f"{'Y' if e['safe_pass'] else '.':<3} "
              f"{e['steps']:<5} {esc:<28} ${e['cost_usd']}")
    print()

print("=" * 84)
print("汇总")
for src, d in (("scripted", s), ("llm", l)):
    ep = [e for e in d["episodes"] if e["fired"]]
    n = len(ep)
    print(f"  {src:<10} Diagnosis {sum(e['diagnosis'] for e in ep)}/{n}  "
          f"Outcome {sum(e['outcome'] for e in ep)}/{n}  "
          f"SafePass {sum(e['safe_pass'] for e in ep)}/{n}  "
          f"成本 ${sum(e['cost_usd'] for e in ep):.2f}  "
          f"用时 {d['elapsed_s']}s")

print()
print("=" * 84)
print("关键：模型在新故障类型上的失败模式")
for fc in ("lock_contention", "connection_exhaustion", "stale_statistics"):
    e = by_fault.get(fc, {}).get("llm")
    if not e:
        continue
    print(f"\n{fc}:")
    print(f"  声称根因: {e['claimed']}  最终阶段: {e['final_phase']}")
    print(f"  ESC 裁决: {e['esc_verdicts'] or '(未走到 ESC)'}")
    print(f"  执行的修复: {e['applied_sql'] or '(无)'}")
    if e["error"]:
        print(f"  错误: {e['error'][:150]}")
