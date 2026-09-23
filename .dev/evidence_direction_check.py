#!/usr/bin/env python3
"""证据方向裁决的四条修正（2026-09-23 跑批：missing_index 在任何场景都无法被反证，ESC 空转）。

  1. 存在性门（index_existence / slow_query_ranking）的 SUPPORTS 不参与方向裁决；
  2. 非窗口判据的 NEUTRAL 算需求已满足，窗口判据的不算；
  3. explain_query 同一份计划落 explain_seq_scan 与 explain_plan 两条；
  4. explain_plan 对 missing_index 的反证范围是 NODE；
  5. 观测器自家全表扫的计数从原始计数器里扣除（结构断言；活库标定见 own_scan_live_check）。
"""
from __future__ import annotations

import inspect
import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import explanation_runtime as xr  # noqa: E402
from agent.episode_state import EpisodeState  # noqa: E402
from agent.explanation import EvidenceBinding, ExplanationScope  # noqa: E402
from knowledge import evidence_predicates as ep  # noqa: E402
from knowledge.causal_graph import graph as G  # noqa: E402
from sandbox.traces import TRACE_DIR, TraceStore  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))


eid = "ep_evidence_direction_fixture"
store = TraceStore(eid)
paths = G.enumerate_causal_paths(["latency_p99_up"], use_learned=False)
path = next(p for p in paths if p.node_ids == ["missing_index", "latency_p99_up"])
explanation = G.merge_paths([path], episode_id=eid, observed_symptoms=["latency_p99_up"])
now = time.time()


def binding(evidence_type, predicate_id, value, result, *, window=False):
    ref = store.record("fixture", {"evidence_type": evidence_type}, json.dumps(value), value)
    return EvidenceBinding.create(
        episode_id=eid, raw_ref=ref, evidence_type=evidence_type, status="OBSERVED",
        observed_at=now, predicate_id=predicate_id, predicate_result=result,
        structured_value=value, target_node_ids=["missing_index"], target_edge_ids=[],
        window_start=(now - 60 if window else None), window_end=(now if window else None),
        source_epoch=("epoch" if window else ""), fresh_until=now + 3600)


print("[1] 存在性门的 SUPPORTS 不参与方向裁决")
explanation.add_evidence_binding(binding("index_existence", "index_existence_v2",
                                         {"inventory_collected": True, "indexes": ["idx"]}, "SUPPORTS"))
explanation.add_evidence_binding(binding("seq_scan_volume", "seq_scan_volume_v2",
                                         {"seq_scan": 0, "idx_scan": 40, "seq_tup_read": 0, "reltuples": 12000000},
                                         "REFUTES", window=True))
st = EpisodeState(eid, "direction_fixture")
st.explanation_graph = explanation
xr.recompute_statuses(st)
check(explanation.node_status.get("missing_index") == "REFUTED",
      "index_existence SUPPORTS + seq_scan_volume REFUTES -> missing_index REFUTED（原来 INCONCLUSIVE）",
      explanation.node_status.get("missing_index"))
check(ep.is_gate_predicate("index_existence_v2") and ep.is_gate_predicate("slow_query_ranking_v2")
      and not ep.is_gate_predicate("seq_scan_volume_v2"), "门判据集合")
check(".add(binding.predicate_result)" not in inspect.getsource(xr.recompute_statuses), "recompute 只经 _direction_result 取方向")

print("[1b] 被污染的 REFUTES 不定方向；污染源被反证后才生效（与 ESC 同一条规则）")
paths_all = G.enumerate_causal_paths(["latency_p99_up"], use_learned=False)
stale_path = next(p for p in paths_all if p.node_ids == ["stale_statistics", "latency_p99_up"])
exp3 = G.merge_paths([path, stale_path], episode_id=eid, observed_symptoms=["latency_p99_up"])
exp3.add_evidence_binding(binding("explain_plan", "explain_plan_v2",
                                  {"indexes_used": ["idx_existing"]}, "REFUTES"))
st3b = EpisodeState(eid + "_c", "direction_fixture")
st3b.explanation_graph = exp3
xr.recompute_statuses(st3b)
check(exp3.node_status.get("missing_index") != "REFUTED",
      "stale_statistics 未反证时，explain_plan（planner_output）的 REFUTES 关不掉 missing_index",
      exp3.node_status.get("missing_index"))
check(xr.contaminated_by(exp3.evidence_bindings[list(exp3.evidence_bindings)[-1]],
                         xr.live_invalidators(exp3)) == ["stale_statistics"], "污染源是 stale_statistics")
exp3.set_path_status(stale_path.path_id, "REFUTED")
xr.recompute_statuses(st3b)
check(exp3.node_status.get("missing_index") == "REFUTED",
      "stale_statistics 路径被反证后，同一条 REFUTES 生效 -> missing_index REFUTED",
      exp3.node_status.get("missing_index"))
from agent import esc as esc_mod  # noqa: E402
check("contaminated_by(binding, live_invalidators)" in inspect.getsource(esc_mod._contaminated_by),
      "esc 的污染判定转发到 explanation_runtime.contaminated_by")

print("[2] 非窗口判据的 NEUTRAL 算需求已满足；窗口判据不算")
exp2 = G.merge_paths([path], episode_id=eid, observed_symptoms=["latency_p99_up"])
exp2.add_evidence_binding(binding("explain_seq_scan", "explain_seq_scan_v2",
                                  {"scan_types": ["Index Scan on orders"], "rows_removed_by_filter": 0}, "NEUTRAL"))
exp2.add_evidence_binding(binding("seq_scan_volume", "seq_scan_volume_v2",
                                  {"seq_scan": 0, "idx_scan": 0, "seq_tup_read": 0, "reltuples": 1}, "NEUTRAL", window=True))
check(G._binding_satisfies(exp2, evidence_type="explain_seq_scan", target_ids=["missing_index"]), "explain_seq_scan 的 NEUTRAL 已满足")
check(not G._binding_satisfies(exp2, evidence_type="seq_scan_volume", target_ids=["missing_index"]), "seq_scan_volume（窗口）的 NEUTRAL 仍要再取")
check("seq_scan_volume_v2" in G.window_predicate_ids() and "explain_seq_scan_v2" not in G.window_predicate_ids(), "窗口判据集合来自图的 window_required")
from agent import esc  # noqa: E402
check(esc._window_predicate_ids() == set(G.window_predicate_ids()), "esc 与 graph 共用同一份窗口判据集合")

print("[3] explain_query 同一份计划落两条证据")
from agent.toolbox import Toolbox  # noqa: E402
from agent.state_machine import Phase, StateMachine  # noqa: E402
from sandbox.observe import ExplainDigest  # noqa: E402
st3 = EpisodeState(eid + "_tb", "direction_fixture")
st3.budget["max_steps"] = 30
digest = ExplainDigest(total_time_ms=1.2, scan_types=["Index Scan on orders"], rows_removed_by_filter=0,
                       rows_est_vs_actual=[(10, 10)], indexes_used=["idx_orders_user"], parallel_workers=0,
                       top_nodes=["1.2ms Index Scan"], raw_ref="trace://" + eid + "_tb/step_001")
obs = SimpleNamespace(trace=TraceStore(eid + "_tb"), explain_query=lambda sql, params=None: digest)
obs.trace.record("explain_query", {"sql": "x"}, "{}", {"k": 1})
tb = Toolbox(obs, st3, StateMachine(st3))
tb.sm.goto(Phase.OBSERVE, "t") if hasattr(tb.sm, "goto") else None
try:
    tb.sm.goto(Phase.HYPOTHESIZE, "t"); tb.sm.goto(Phase.INVESTIGATE, "t")
except Exception:
    pass
tb.explain_query("SELECT 1")
kinds = [e.get("evidence_type") for e in st3.scratchpad]
check("explain_seq_scan" in kinds and "explain_plan" in kinds, f"scratchpad 同时有两条: {kinds}")

print("[4] explain_plan 对 missing_index 的反证范围")
edge = (G.load().get_edge_data("missing_index", "explain_plan") or {}).get("REFUTED_BY") or {}
check(edge.get("scope") == "NODE", "scope=NODE", edge)

print("[5] 观测器扣除自家扫描（结构）")
from sandbox import observe  # noqa: E402
src = inspect.getsource(observe.Observer._stats_range_drift)
check("pg_stat_xact_user_tables" in src and "max_parallel_workers_per_gather = 0" in src, "单事务、禁并行、读事务级计数")
check("own_before" in inspect.getsource(observe.Observer.get_table_stats), "get_table_stats 扣除此前累计的自家计数")
fields = {f.name for f in observe.TableStats.__dataclass_fields__.values()}
check({"seq_scan_raw", "own_seq_scan", "own_seq_tup_read", "own_idx_scan"} <= fields, "TableStats 记原始值与扣除量")
for d in (TRACE_DIR / eid, TRACE_DIR / (eid + "_tb")):
    shutil.rmtree(d, ignore_errors=True)

print()
if fails:
    print(f"EVIDENCE DIRECTION: FAIL（{len(fails)}/{checks}）")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print(f"EVIDENCE DIRECTION: PASS（{checks} 项）")
