#!/usr/bin/env python3
"""派生证据必须能绑上：在真实 trace 上重放 bind_evidence，并钉住合并路径的三处改动。

2026-09-23 架构评审第 1 条：merge_evidence_task_results 只绑子 agent 引用过的 raw_ref，
而 toolbox 为 explain_query 另落一条派生的 row_estimate_deviation（自己的 raw_ref），
子 agent 从不引用它 —— stale_statistics 唯一的必需证据永远绑不上，need 每轮重派、
同一 explain 反复跑。昨晚 stale_statistics 32 步、esc=[]、claimed=None 就是它。
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import investigator, orchestrator  # noqa: E402
from agent.episode_state import EpisodeState  # noqa: E402
from agent.explanation_runtime import bind_evidence  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))


print("[1] 合并路径的结构约束")
src = inspect.getsource(orchestrator.merge_evidence_task_results)
check("raw_refs=None" in src, "按任务绑定（bind_evidence 不再按引用的 raw_ref 过滤）")
check("tool_raw_refs" in src, "接受工具级 raw_ref（EXPLAIN 记录本身）")
check('"event": "evidence_merge"' in src, "合并结果（含被拒原因）落审计")
isrc = inspect.getsource(investigator._tools_for)
check(isrc.index('"evidence_raw_refs": refs') < isrc.index('"result": payload'),
      "工具返回里 evidence_raw_refs 排在 result 之前，截断不会吃掉它")
check("tool_raw_refs" in inspect.getsource(investigator.investigate_task),
      "investigate_task 把工具级 raw_ref 交回任务结果")

print("[2] 真实 trace 重放：row_estimate_deviation 能绑上")
EP = "ep_stale_statistics_eval_v1_1790090995"
state_path = ROOT / "traces" / EP / "episode_state.json"
if not state_path.exists():
    print(f"    SKIP {EP}（trace 已不在）")
else:
    st = EpisodeState.load(EP)
    graph = st.explanation_graph
    before = sum(1 for b in graph.evidence_bindings.values()
                 if b.evidence_type == "row_estimate_deviation")
    entries = [e for e in st.scratchpad if e.get("evidence_type") == "row_estimate_deviation"
               and e.get("evidence_task_id")]
    check(bool(entries), f"trace 里有派生条目（{len(entries)} 条）")
    check(before == 0, f"修复前该 episode 一条都没绑上（实际 {before}）")
    added_total = 0
    for entry in entries:
        added = bind_evidence(
            st, evidence_task_ids={entry["evidence_task_id"]}, raw_refs=None,
            base_revision=entry.get("explanation_revision"))
        added_total += len(added)
    after = [b for b in graph.evidence_bindings.values()
             if b.evidence_type == "row_estimate_deviation"]
    check(len(after) >= 1, f"按任务重放后绑上 {len(after)} 条 row_estimate_deviation（新增绑定 {added_total}）")
    decisive = [b for b in after if b.predicate_result in ("SUPPORTS", "REFUTES")]
    check(len(decisive) >= 1, f"其中有判定性的（SUPPORTS/REFUTES）{len(decisive)} 条",
          [b.predicate_result for b in after][:5])
    # 只在内存里重放，不写回 trace
print()
if fails:
    print(f"EVIDENCE BINDING: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"EVIDENCE BINDING: PASS（{checks} 项）")
