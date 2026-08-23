"""诊断 lock_contention 与 stale_statistics 为何跑不通。

分三层看：
  1. 因果图给的候选集里有没有正确答案
  2. 子 agent 的工具能不能取到判别性证据
  3. 取到了之后裁决对不对
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.investigator import toolset_for
from agent.orchestrator import BRIEFS
from knowledge.causal_graph import graph as G

for fault, symptoms in (
        ("lock_contention", ["throughput_down", "latency_p99_up"]),
        ("stale_statistics", ["latency_p99_up", "cpu_saturated"])):
    print("=" * 78)
    print(f"{fault}")
    print("=" * 78)

    cands = G.candidate_causes(symptoms, top_k=5)
    names = [c["root_cause"] for c in cands]
    hit = fault in names
    print(f"\n[1] 候选集（症状 {symptoms}）")
    for c in cands:
        mark = " ←正确答案" if c["root_cause"] == fault else ""
        print(f"    {c['root_cause']:<24} score={c['score']:.3f}{mark}")
    print(f"    {'PASS' if hit else 'FAIL'}  正确答案在候选集内")
    if hit:
        rank = names.index(fault) + 1
        print(f"    排名第 {rank}/{len(names)}")

    print(f"\n[2] 取证能力")
    req = G.required_evidence(fault)
    ts = toolset_for(fault)
    print(f"    必需证据: {req}")
    print(f"    子 agent 工具: {ts}")
    g = G.load()
    for e in req:
        by = g.nodes.get(e, {}).get("obtained_by")
        covered = by in ts
        print(f"    {'PASS' if covered else 'FAIL'}  {e} ← {by}")

    print(f"\n[3] 调查提示")
    print(f"    {BRIEFS.get(fault, '(无)')[:200]}")

    print(f"\n[4] 反证边（用于排除其他假设）")
    for r in G.refuting_evidence(fault):
        print(f"    {r['evidence']:<28} when: {r['when']}")
    print()
