#!/usr/bin/env python3
"""根角色反证（edges.yaml scope: ROOT）只关"以该节点为根"的路径（2026-09-24 缺陷报告 P1-2）。

misleading_idle_txn 的真路径 long_idle_transaction -> connection_exhaustion -> throughput_down
与独立根因路径 connection_exhaustion -> throughput_down 共享节点和边，只差谁是根。NODE / PATH
范围的反证都会连带反证真路径上的中间机制，所以严格诊断一直差这一条。这里钉住：

  1. connection_residual 判据的方向与安全边界（没逼近上限 / 缺字段 -> NEUTRAL）
  2. ROOT 反证只把以 connection_exhaustion 为根的路径判 REFUTED，节点、共享边、真路径都不动
  3. 反向（扣掉长事务后仍打满）不反证；被活着的污染源污染时同样不生效（与节点/边同一套规则）
  4. v1 台账投影：以它为根的路径全关 -> connection_exhaustion 记 REFUTED（严格诊断读这个）
  5. 需求只挂以它为根的路径、落在根节点；ESC 的竞争路径关闭认 ROOT 反证
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import esc as esc_mod  # noqa: E402
from agent import explanation_runtime as xr  # noqa: E402
from agent.episode_state import EpisodeState  # noqa: E402
from agent.explanation import EvidenceBinding  # noqa: E402
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


def decide(value: dict) -> str:
    return ep.evaluate("connection_residual_v2", value, context=ep.PredicateContext(
        target_kind="NODE", target_ids=("connection_exhaustion",))).result


print("[1] connection_residual 判据")
check(decide({"used": 95, "max_connections": 100, "idle_in_transaction_long": 70}) == "REFUTES",
      "打满、扣掉 70 个长事务后只剩 25% -> REFUTES（打满由上游解释）")
check(decide({"used": 97, "max_connections": 100, "idle_in_transaction_long": 0}) == "SUPPORTS",
      "打满、没有长事务 -> SUPPORTS（真·连接打满）")
check(decide({"used": 97, "max_connections": 100, "idle_in_transaction_long": 5}) == "SUPPORTS",
      "少量长事务扣不下阈值 -> SUPPORTS")
check(decide({"used": 40, "max_connections": 100, "idle_in_transaction_long": 30}) == "NEUTRAL",
      "没逼近上限 -> NEUTRAL（交给 connection_count）")
check(decide({"used": 95, "max_connections": 100}) == "NEUTRAL", "缺长事务计数 -> NEUTRAL（不猜）")
rel = [item for item in G.refuting_evidence("connection_exhaustion") if item["evidence"] == "connection_residual"]
check(len(rel) == 1 and rel[0].get("scope") == "ROOT", "图上 connection_exhaustion <- connection_residual 是 ROOT 范围")
check(G.load().nodes["connection_residual"].get("provenance") == "live_state"
      and not G.invalidators_of("connection_residual"), "live_state，不引入污染源")

eid = "ep_root_scope_fixture"
store = TraceStore(eid)
now = time.time()


def bind(st, evidence_type, value, *, nodes=(), edges=()):
    graph = G.load()
    predicate_id = str(graph.nodes[evidence_type]["predicate_id"])
    ref = store.record("fixture", {"evidence_type": evidence_type}, json.dumps(value), value)
    result = ep.evaluate(predicate_id, value, context=ep.PredicateContext(
        target_kind="NODE" if nodes else "EDGE", target_ids=tuple(nodes or edges))).result
    st.explanation_graph.add_evidence_binding(EvidenceBinding.create(
        episode_id=eid, raw_ref=ref, evidence_type=evidence_type, status="OBSERVED",
        observed_at=now, predicate_id=predicate_id, predicate_result=result,
        structured_value=value, target_node_ids=list(nodes), target_edge_ids=list(edges),
        fresh_until=now + 3600))


def fresh_state():
    st = EpisodeState(eid, "root_scope_fixture")
    st.observed_symptom_ids = ["throughput_down"]
    st.explanation_graph = G.recall_explanation(["throughput_down"], episode_id=eid, use_learned=False)
    return st


st = fresh_state()
ex = st.explanation_graph
true_path = next((p for p in ex.candidate_paths
                  if p.node_ids == ["long_idle_transaction", "connection_exhaustion", "throughput_down"]), None)
alt_path = next((p for p in ex.candidate_paths
                 if p.node_ids == ["connection_exhaustion", "throughput_down"]), None)
print("[2] ROOT 反证只关以该节点为根的路径")
check(true_path is not None and alt_path is not None, "throughput_down 的召回里有真路径与独立根因路径")
if true_path is not None and alt_path is not None:
    shared_edge = alt_path.edge_ids[0]
    check(shared_edge in true_path.edge_ids, "两条路径共享边 connection_exhaustion -> throughput_down")
    # 真路径的机制证据：长事务支持、连接打满支持
    busy = {"used": 95, "max_connections": 100, "pct": 95.0, "near_limit": True,
            "idle_in_transaction": 70, "idle_in_transaction_long": 70}
    bind(st, "idle_in_transaction", busy, nodes=["long_idle_transaction"])
    bind(st, "idle_in_transaction", busy, edges=[true_path.edge_ids[0]])
    bind(st, "connection_count", busy, nodes=["connection_exhaustion"])
    bind(st, "connection_count", busy, edges=[shared_edge])
    xr.recompute_statuses(st)
    before = (ex.path_map()[alt_path.path_id].status, ex.path_map()[true_path.path_id].status)
    check(before == ("SUPPORTED", "SUPPORTED"), f"反证之前两条都 SUPPORTED（这正是严格诊断差的那条）{before}")
    bind(st, "connection_residual", busy, nodes=["connection_exhaustion"])
    xr.recompute_statuses(st)
    xr.sync_v1_projection(st)
    check(ex.path_map()[alt_path.path_id].status == "REFUTED", "以 connection_exhaustion 为根的路径 -> REFUTED")
    check(ex.path_map()[true_path.path_id].status == "SUPPORTED", "真路径仍 SUPPORTED")
    check(ex.node_status.get("connection_exhaustion") == "SUPPORTED", "中间机制节点状态不动")
    check(ex.edge_status.get(shared_edge) == "SUPPORTED", "共享边状态不动")
    check(st.ledger["connection_exhaustion"].verdict.startswith("REFUTED"),
          "台账：以它为根的路径全关 -> REFUTED（严格诊断读这个）", st.ledger["connection_exhaustion"].verdict)
    check(st.ledger["long_idle_transaction"].verdict == "CONFIRMED", "台账：真根因 CONFIRMED")

print("[3] 反向不反证；被污染时不生效")
st2 = fresh_state()
if alt_path is not None:
    pure = {"used": 97, "max_connections": 100, "pct": 97.0, "near_limit": True,
            "idle_in_transaction": 0, "idle_in_transaction_long": 0}
    bind(st2, "connection_count", pure, nodes=["connection_exhaustion"])
    bind(st2, "connection_count", pure, edges=[alt_path.edge_ids[0]])
    bind(st2, "connection_residual", pure, nodes=["connection_exhaustion"])
    xr.recompute_statuses(st2)
    check(st2.explanation_graph.path_map()[alt_path.path_id].status == "SUPPORTED",
          "真·连接打满（没有长事务）-> 独立根因路径仍 SUPPORTED")
    st3 = fresh_state()
    bind(st3, "connection_residual", {"used": 95, "max_connections": 100, "idle_in_transaction_long": 70},
         nodes=["connection_exhaustion"])
    saved = xr._direction_result
    xr._direction_result = lambda binding, live, edge_sources: "NEUTRAL"
    try:
        xr.recompute_statuses(st3)
    finally:
        xr._direction_result = saved
    check(st3.explanation_graph.path_map()[alt_path.path_id].status != "REFUTED",
          "方向被污染规则降为 NEUTRAL 时 ROOT 反证不生效（与节点/边同一套 _direction_result）")

print("[4] 需求只挂以它为根的路径、落在根节点；ESC 认 ROOT 反证")
needs = [n for n in G.evidence_needs(fresh_state().explanation_graph)
         if n.evidence_type == "connection_residual"]
rooted = {p.path_id for p in ex.candidate_paths if p.root_node_id == "connection_exhaustion"}
check(bool(needs) and all(n.target_kind == "NODE" and n.target_ids == ["connection_exhaustion"]
                          and set(n.path_ids) <= rooted for n in needs),
      "根角色需求：NODE connection_exhaustion，只挂以它为根的路径", [(n.target_kind, n.target_ids, n.path_ids) for n in needs])
if true_path is not None and alt_path is not None:
    trusted = {b.binding_id: b for b in ex.evidence_bindings.values()}
    check(bool(esc_mod._scoped_alternative_refutation(true_path, alt_path, trusted)),
          "竞争根不同 -> ROOT 反证关闭竞争路径（即使根节点也在被选路径上）")
    check(not esc_mod._scoped_alternative_refutation(alt_path, alt_path, trusted), "同根不适用")
    check(not any(item.get("scope") == "ROOT"
                  for item in G.refuting_evidence("long_idle_transaction")), "ROOT 关系只挂在需要它的根因上")

shutil.rmtree(TRACE_DIR / eid, ignore_errors=True)
print()
if fails:
    print(f"ROOT SCOPE: FAIL（{len(fails)}/{checks}）")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print(f"ROOT SCOPE: PASS（{checks} 项）")
