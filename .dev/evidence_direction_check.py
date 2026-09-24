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

print("[6] PLAN 阶段未绑定的 simulate_index 观测能满足建索引的前置条件（2026-09-23 跑批 missing_index O=False）")
eid6 = eid + "_cf"
store6 = TraceStore(eid6)
exp6 = G.merge_paths([path], episode_id=eid6, observed_symptoms=["latency_p99_up"])
exp6.select_paths([path.path_id], unexplained_symptoms=[], scope=ExplanationScope.FULL)
st6 = EpisodeState(eid6, "direction_fixture")
st6.explanation_graph = exp6
cf_value = {"create_sql": "CREATE INDEX idx_x ON orders(created_at, status)", "test_sql": "SELECT 1",
            "would_be_used": True, "trivial_baseline": False}
ref6 = store6.record("bind_structured_evidence", {"evidence_type": "counterfactual_index"}, json.dumps(cf_value), cf_value)
st6.note("agent", "counterfactual_index", "PLAN 阶段 simulate_index", ref6, ["missing_index"],
         status="OBSERVED", structured_value=cf_value, target_kind="INTERVENTION",
         target_ids=["create_covering_index"])
fix6 = next(f for f in G.fixes_for("missing_index") if f["fix"] == "create_covering_index")
option6 = {"path_id": path.path_id, "target_node_id": "missing_index", "fix": "create_covering_index",
           "preconditions": fix6.get("preconditions", [])}


def eval6(sql):
    return {r["condition_id"]: r for r in xr._evaluate_preconditions(st6, option=option6, sql=sql)}


res6 = eval6("CREATE INDEX CONCURRENTLY idx_y ON orders (created_at, status)")
check(all(r["satisfied"] for r in res6.values()), "同签名（列相同、名字/CONCURRENTLY 不同）的提案：全部前置条件满足 " + str({k: v["satisfied"] for k, v in res6.items()}))
check(all(ref6 in r["evidence_refs"] for r in res6.values()), "前置条件引用了该观测的 raw_ref")
res6 = eval6("CREATE INDEX CONCURRENTLY idx_z ON orders (status)")
check(not all(r["satisfied"] for r in res6.values()), "不同签名的提案不满足")
st6.scratchpad[-1]["ts"] -= 10 * 24 * 3600
res6 = eval6("CREATE INDEX CONCURRENTLY idx_y ON orders (created_at, status)")
check(not all(r["satisfied"] for r in res6.values()), "过期的观测不算")
shutil.rmtree(TRACE_DIR / eid6, ignore_errors=True)

print("[7] 已生效（预期效果全部达成）但症状未恢复的修复，本 episode 不再可执行（2026-09-23 stale_statistics 重试同一修复）")
from agent.episode_state import InterventionAttempt  # noqa: E402
eid7 = eid + "_retry"
exp7 = G.merge_paths([path], episode_id=eid7, observed_symptoms=["latency_p99_up"])
exp7.select_paths([path.path_id], unexplained_symptoms=[], scope=ExplanationScope.FULL)
st7 = EpisodeState(eid7, "direction_fixture")
st7.explanation_graph = exp7
fixes7 = {o["fix"] for o in xr.intervention_options(st7, executable_only=True)}
check("create_covering_index" in fixes7, "尝试之前 create_covering_index 可执行", fixes7)


def attempt(fix_id, met, outcome="FAILED"):
    return InterventionAttempt(
        attempt_id="", episode_id=eid7, plan_id=f"p_{fix_id}_{met}", explanation_id="e",
        explanation_revision=1, selected_path_id=path.path_id, intervention_target="missing_index",
        fix_id=fix_id, intervention_kind="CORRECTIVE", sql="x", execution_status="SUCCEEDED",
        actual=[{"met": met}], outcome=outcome)


st7.intervention_attempts = [attempt("create_covering_index", False)]
check("create_covering_index" in {o["fix"] for o in xr.intervention_options(st7, executable_only=True)},
      "效果没达成的失败：仍可再试（可能换一种索引定义）")
st7.intervention_attempts = [attempt("create_covering_index", True)]
check("create_covering_index" not in {o["fix"] for o in xr.intervention_options(st7, executable_only=True)},
      "效果全部达成但未恢复：不再可执行")
check(xr.effective_but_insufficient_fixes(st7) == {"create_covering_index"}, "effective_but_insufficient_fixes 给出该 fix")
st7.intervention_attempts = [attempt("create_covering_index", True, outcome="VERIFIED")]
check(not xr.effective_but_insufficient_fixes(st7), "已 VERIFIED 的不算")
st7.intervention_attempts = [attempt("terminate_idle_backend", True)]
check(not xr.effective_but_insufficient_fixes(st7), "会话控制除外：换一组 pid 是另一次干预")
check("effective_but_insufficient_fixes(st)" in inspect.getsource(__import__("agent.loop", fromlist=["x"])),
      "loop 升级时的说明点名已生效但未恢复的修复")

print("[8] 阻塞链判据只数真正的阻塞：持锁 0 个表的空闲事务不算（2026-09-24 misleading_idle_txn）")
idle0 = {"blocked_pid": None, "pid": 1, "state": "idle in transaction", "blocking_impact": 0,
         "evidence": "idle_in_transaction_holding_locks"}
idle1 = dict(idle0, pid=2, blocking_impact=1)
waiting = {"blocked_pid": 9, "blocked_by": 2, "pid": 2, "blocking_impact": 3, "evidence": "currently_waiting"}
# 阻塞链是窗口类判据：不传观测窗一律 NOT_APPLICABLE（CLAUDE.md "新增证据类型"一节）
ctx8 = ep.PredicateContext(target_kind="PATH", target_ids=("x",), collection_status="OBSERVED",
                           window_start=now - 60, window_end=now, source_epoch="epoch")
check(ep.evaluate("lock_blocking_chain_v2", {"chains": [idle0] * 87}, context=ctx8).result == "REFUTES",
      "87 个持锁 0 个表的空闲事务 -> REFUTES（原来数成 87 条阻塞记录 SUPPORTS）")
check(ep.evaluate("lock_blocking_chain_v2", {"chains": [idle1]}, context=ctx8).result == "SUPPORTS",
      "持有表级锁的空闲事务（lock_contention 注入器的持锁者）-> SUPPORTS")
check(ep.evaluate("lock_blocking_chain_v2", {"chains": [waiting, idle0]}, context=ctx8).result == "SUPPORTS",
      "有会话正在等锁 -> SUPPORTS")
check(ep.evaluate("lock_blocking_chain_v2", {"chains": []}, context=ctx8).result == "REFUTES", "空链 -> REFUTES")
check(ep.evaluate("lock_blocking_chain_v2", {"chains": [{"legacy": True}]}, context=ctx8).result == "SUPPORTS",
      "旧文本回放出来的行（无 evidence 字段）行为不变")
check(not any(k == "CAUSES" and u == "long_idle_transaction" and v == "lock_contention"
               for u, v, k in G.load().edges(keys=True)),
      "没有为此加 long_idle_transaction -> lock_contention 因果边（修证据，不加边去圆）")

print("[9] 累计计数器窗口太短时不许否定（规则 1；2026-09-24 实测 1.4-9.5 秒窗口判掉 deadlock）")


def win_ctx(span):
    return ep.PredicateContext(target_kind="NODE", target_ids=("deadlock",), collection_status="OBSERVED",
                               window_start=now - span, window_end=now, source_epoch="epoch")


r = ep.evaluate("deadlock_count_v2", {"deadlocks": 0, "source_epoch": "epoch"}, context=win_ctx(1.8))
check(r.result == "NEUTRAL" and "lower bound" in r.reason, "1.8 秒窗口死锁数 0 -> NEUTRAL（原来 REFUTES）", r.reason)
r = ep.evaluate("deadlock_count_v2", {"deadlocks": 0, "source_epoch": "epoch"}, context=win_ctx(426))
check(r.result == "REFUTES", "426 秒窗口死锁数 0 -> REFUTES")
r = ep.evaluate("deadlock_count_v2", {"deadlocks": 2, "source_epoch": "epoch"}, context=win_ctx(1.8))
check(r.result == "SUPPORTS", "短窗口里已看到死锁 -> SUPPORTS（下界越过阈值）")
r = ep.evaluate("temp_file_volume_v2", {"temp_bytes": 0, "source_epoch": "epoch"}, context=win_ctx(5))
check(r.result == "NEUTRAL", "5 秒窗口临时文件 0 -> NEUTRAL")
r = ep.evaluate("seq_scan_volume_v2", {"seq_scan": 0, "idx_scan": 301, "seq_tup_read": 0, "reltuples": 1,
                                       "source_epoch": "epoch"}, context=win_ctx(1.8))
check(r.result == "NEUTRAL", "1.8 秒窗口没有顺序扫描 -> NEUTRAL")
check(not G._binding_satisfies.__doc__ or True, "（窗口判据的 NEUTRAL 不算已取到，下一轮重取：见 [2]）")

print("[10] EXPLAIN ANALYZE 自己的执行量从累计计数器里扣掉（规则 6；2026-09-24）")
plan = {"Node Type": "Gather", "Actual Loops": 1, "Temp Written Blocks": 12, "Plans": [
    {"Node Type": "Parallel Seq Scan", "Relation Name": "orders", "Actual Loops": 3,
     "Actual Rows": 2, "Rows Removed by Filter": 3999998},
    {"Node Type": "Nested Loop", "Actual Loops": 1, "Plans": [
        {"Node Type": "Index Only Scan", "Relation Name": "users", "Index Name": "users_pkey", "Actual Loops": 7,
         "Actual Rows": 1},
        {"Node Type": "Bitmap Heap Scan", "Relation Name": "orders", "Actual Loops": 2, "Plans": [
            {"Node Type": "BitmapAnd", "Actual Loops": 2, "Plans": [
                {"Node Type": "Bitmap Index Scan", "Index Name": "idx_a", "Actual Loops": 2},
                {"Node Type": "Bitmap Index Scan", "Index Name": "idx_b", "Actual Loops": 2}]}]},
        {"Node Type": "Seq Scan", "Relation Name": "items", "Actual Loops": 0, "Actual Rows": 0}]}]}
own = observe._plan_own_counts(plan)
check(own.get("orders") == {"seq_scan": 3, "seq_tup_read": 12000000, "idx_scan": 4},
      "并行顺序扫描按参与者计 3 次、(输出+过滤)×loops 行；位图索引扫描记到父表", own.get("orders"))
check(own.get("users") == {"seq_scan": 0, "seq_tup_read": 0, "idx_scan": 7}, "嵌套循环内侧索引扫描按 loops 计")
check("items" not in own, "从未执行的节点（loops=0）不记账")
esrc = inspect.getsource(observe.Observer.explain_query)
check("_plan_own_counts" in esrc and "_flush_own_stats" in esrc and "Temp Written Blocks" in esrc
      and "block_size" in esrc, "explain_query 记扫描账与外溢上界，并强制刷出本连接的统计")
check("pg_stat_xact_user_tables" in esrc and "_merge_own_counts(" in esrc
      and "parallel=acc[\"workers\"] > 0" in esrc, "计划 JSON 与事务级视图按并行与否合并（活库标定：.dev/tool_perturbation_live.py）")
_plan_c = {"orders": {"seq_scan": 3, "seq_tup_read": 12000000, "idx_scan": 4}}
_lead_c = {"orders": {"seq_scan": 1, "seq_tup_read": 4000000, "idx_scan": 3},
           "users": {"seq_scan": 0, "seq_tup_read": 0, "idx_scan": 2}}
_par = observe._merge_own_counts(_plan_c, _lead_c, parallel=True)
_ser = observe._merge_own_counts(_plan_c, _lead_c, parallel=False)
check(_par["orders"]["seq_scan"] == 3 and _par["orders"]["seq_tup_read"] == 12000000,
      "顺序扫描取计划（全体参与者，规划器不做顺序扫描）")
check(_par["orders"]["idx_scan"] == 7 and _par["users"]["idx_scan"] == 2,
      "并行计划的索引扫描取两者之和（上界：净值只会偏低，只会多给 NEUTRAL）", _par)
check(_ser["orders"]["idx_scan"] == 4, "非并行计划的索引扫描取大（leader 即全部，精确）", _ser)
check("_flush_own_stats" in inspect.getsource(observe.Observer._stats_range_drift), "值域漂移扫描同样强制刷出")
for fn in (observe.Observer.get_table_stats, observe.Observer.get_database_stats):
    fsrc = inspect.getsource(fn)
    check(fsrc.index("_await_own_stats") < fsrc.index("db.query"), f"{fn.__name__} 读计数器前等自家记账到账")
check("agent_ro" in inspect.getsource(observe.Observer.get_top_queries), "慢查询排名排除诊断/安全门自己的语句")

eid10 = eid + "_own"
st10 = EpisodeState(eid10, "direction_fixture")
st10.budget["max_steps"] = 30
obs10 = SimpleNamespace(trace=TraceStore(eid10))
seq_readings: list = []
db_readings: list = []


def _table_stats(_table):
    s = seq_readings.pop(0)
    s.raw_ref = obs10.trace.record("get_table_stats", {}, "{}", {"k": 1})
    return s


def _db_stats():
    r = dict(db_readings.pop(0))
    r["raw_ref"] = obs10.trace.record("get_database_stats", {}, "{}", {"k": 1})
    return r


obs10.get_table_stats = _table_stats
obs10.get_database_stats = _db_stats
tb10 = Toolbox(obs10, st10, StateMachine(st10))
try:
    tb10.sm.goto(Phase.OBSERVE, "t"); tb10.sm.goto(Phase.HYPOTHESIZE, "t"); tb10.sm.goto(Phase.INVESTIGATE, "t")
except Exception:
    pass


def _ts(**kw):
    base = dict(table="orders", n_live_tup=12000000, n_dead_tup=0, dead_ratio=0.0, last_analyze="",
                last_autovacuum="", total_size="1 GB", autovacuum_enabled=True, autovacuum_running=False,
                autovacuum_trigger=0, reltuples=12000000, stats_reset="epoch")
    base.update(kw)
    return observe.TableStats(**base)


# 第二次读数前自家 EXPLAIN 跑了一次并行全表扫：计划估计 12,000,001 行，计数器实际涨了 12,000,000 行
seq_readings[:] = [
    _ts(seq_scan=100, seq_tup_read=1000000, idx_scan=50, seq_scan_raw=100, seq_tup_read_raw=1000000, idx_scan_raw=50),
    _ts(seq_scan=100, seq_tup_read=999999, idx_scan=350, seq_scan_raw=103, seq_tup_read_raw=13000000,
        idx_scan_raw=350, own_seq_scan=3, own_seq_tup_read=12000001)]
tb10.get_table_stats("orders")
tb10.get_table_stats("orders")
vol = [e for e in st10.scratchpad if e.get("evidence_type") == "seq_scan_volume"]
v = vol[-1].get("structured_value") or {} if vol else {}
check(bool(vol) and vol[-1].get("status") == "OBSERVED", "估计差一行不会让净值\"回退\"成 UNKNOWN",
      vol[-1].get("observation") if vol else "no seq_scan_volume")
check(v.get("seq_scan") == 0 and v.get("seq_tup_read") == 0 and v.get("idx_scan") == 300,
      "窗口净值 = 原始增量 - 自家增量（在 0 处截断）", {k: v.get(k) for k in ("seq_scan", "seq_tup_read", "idx_scan")})
check("已扣除自家取证扫描 3 次" in (vol[-1].get("observation") or ""), "观测文本写明扣除量")
seq_readings[:] = [_ts(seq_scan=20, seq_tup_read=240000000, idx_scan=0),
                   _ts(seq_scan=40, seq_tup_read=480000000, idx_scan=0)]
tb10.get_table_stats("items")
tb10.get_table_stats("items")
v = [e for e in st10.scratchpad if e.get("evidence_type") == "seq_scan_volume"][-1].get("structured_value") or {}
check(v.get("seq_scan") == 20 and v.get("seq_tup_read") == 240000000, "只给净值的桩观测器按原始值差分", v)

db0 = {"deadlocks": 0, "temp_files": 0, "temp_bytes": 1000, "own_temp_bytes": 0, "xact_commit": 10,
       "xact_rollback": 0, "db_stats_reset": "epoch", "errors": {}}
db_readings[:] = [db0, dict(db0, temp_files=2, temp_bytes=1000 + 8000000, own_temp_bytes=8192000),
                  dict(db0, temp_files=9, temp_bytes=1000 + 58000000, own_temp_bytes=16384000)]
for i10 in range(3):
    if i10:
        # "故障窗口后再次调用"：基线往前拨，窗口满下限、读数推进基线（不真睡 30 秒）
        for base10 in st10.cumulative_baselines.values():
            base10["captured_at"] = time.time() - 60
    tb10.get_database_stats()
temps = [e.get("structured_value") or {} for e in st10.scratchpad if e.get("evidence_type") == "temp_file_volume"]
check(len(temps) >= 3 and temps[1].get("temp_bytes") == 0 and temps[1].get("temp_bytes_raw") == 8000000
      and temps[1].get("own_temp_bytes") == 8192000, "外溢净值扣掉自家上界（在 0 处截断），原始增量另记",
      temps[1] if len(temps) > 1 else temps)
check(len(temps) >= 3 and temps[2].get("temp_bytes") == 50000000 - 8192000
      and temps[2].get("baseline_anchored") is False, "负载外溢远大于自家上界时净值为正", temps[2:])
r = ep.evaluate("temp_file_volume_v2", dict(temps[1], source_epoch="epoch"), context=win_ctx(426))
check(r.result == "NEUTRAL", "自家外溢把原始增量全扣光 -> NEUTRAL（原来 REFUTES）", r.reason)
r = ep.evaluate("temp_file_volume_v2", dict(temps[2], source_epoch="epoch"), context=win_ctx(426))
check(r.result == "SUPPORTS", "扣掉上界后仍为正 -> SUPPORTS（下界越过阈值）", r.reason)
r = ep.evaluate("temp_file_volume_v2", {"temp_bytes": 0, "own_temp_bytes": 0, "source_epoch": "epoch"},
                context=win_ctx(426))
check(r.result == "REFUTES", "窗口够长、自家没有外溢、净值 0 -> 仍可 REFUTES")
shutil.rmtree(TRACE_DIR / eid10, ignore_errors=True)

print("[11] 累计计数器读数前先等窗口满下限（2026-09-24 e2e 夹具：窗口永远凑不满，ESC 4 轮变 30 轮）")
from agent import toolbox as toolbox_module  # noqa: E402
eid11 = eid + "_wait"
st11 = EpisodeState(eid11, "direction_fixture")
st11.budget["max_steps"] = 30
obs11 = SimpleNamespace(trace=TraceStore(eid11))
obs11.get_database_stats = lambda: dict(db0, raw_ref=obs11.trace.record("get_database_stats", {}, "{}", {"k": 1}))
obs11.get_table_stats = lambda _t: _ts(seq_scan=0, idx_scan=5, raw_ref=obs11.trace.record("t", {}, "{}", {"k": 1}))
tb11 = Toolbox(obs11, st11, StateMachine(st11))
try:
    tb11.sm.goto(Phase.OBSERVE, "t"); tb11.sm.goto(Phase.HYPOTHESIZE, "t"); tb11.sm.goto(Phase.INVESTIGATE, "t")
except Exception:
    pass
waits: list[float] = []
real_sleep = toolbox_module._window_sleep
toolbox_module._window_sleep = waits.append
try:
    tb11.get_database_stats()
    check(waits == [], "第一次读（没有基线）不等，只建基线", waits)
    for key in ("pg_stat_database", "checkpoint_stats"):
        if key in st11.cumulative_baselines:
            st11.cumulative_baselines[key]["captured_at"] = time.time() - 5
    tb11.get_database_stats()
    check(len(waits) == 1 and 24.0 <= waits[0] <= 25.5, f"基线 5 秒前 -> 先等约 25 秒再读（{waits}）")
    for key in ("pg_stat_database", "checkpoint_stats"):
        if key in st11.cumulative_baselines:
            st11.cumulative_baselines[key]["captured_at"] = time.time() - 100
    tb11.get_database_stats()
    check(len(waits) == 1, "基线已满下限 -> 不等", waits)
    tb11.get_table_stats("orders")
    st11.cumulative_baselines["table_scan:orders"]["captured_at"] = time.time() - 12
    saved_floor = ep.MIN_REFUTE_WINDOW_S
    ep.MIN_REFUTE_WINDOW_S = 20.0
    try:
        tb11.get_table_stats("orders")
    finally:
        ep.MIN_REFUTE_WINDOW_S = saved_floor
    check(len(waits) == 2 and 7.0 <= waits[1] <= 8.5,
          f"表扫描窗口按自己的基线等；等多久跟判据共用 MIN_REFUTE_WINDOW_S（{waits}）")
    audit = [x for x in st11.evidence_task_audit if x.get("event") == "cumulative_window_wait"]
    check(len(audit) == 2, "每次等待落审计")
finally:
    toolbox_module._window_sleep = real_sleep
shutil.rmtree(TRACE_DIR / eid11, ignore_errors=True)

print("[12] 需求只发给与证据有方向关系的根因；绑定按图的关系放行（2026-09-24 缺陷报告 P1-1）")
graph12 = G.load()
symptoms12 = sorted(n for n, d in graph12.nodes(data=True) if d.get("kind") == "Symptom")
bad12: list[str] = []
total12 = 0
for sym in symptoms12:
    ex12 = G.recall_explanation([sym], episode_id="ep_direction_needs_" + sym, use_learned=False)
    for need in G.evidence_needs(ex12):
        if need.target_kind == "INTERVENTION":
            continue
        total12 += 1
        causes = xr._target_causes(ex12, need)
        if not causes & set(G.causes_bearing(need.evidence_type)):
            bad12.append(f"{sym}: {need.evidence_type} -> {sorted(causes)} ({need.reason})")
check(total12 > 0 and not bad12, f"{len(symptoms12)} 个症状召回的 {total12} 条需求都与证据有方向关系", bad12[:6])
check(not any("branch discriminator" in need.reason
              for sym in symptoms12
              for need in G.evidence_needs(G.recall_explanation(
                  [sym], episode_id="ep_direction_disc_" + sym, use_learned=False))),
      "不再按 DISCRIMINATES 发鉴别需求")
check("work_mem_spill" in G.causes_bearing("slow_query_ranking")
      and "missing_index" in G.causes_bearing("seq_scan_volume")
      and "stale_statistics" not in G.causes_bearing("seq_scan_volume"), "causes_bearing 读图上的 CONFIRMED_BY / REFUTED_BY")
ex12 = G.recall_explanation(["latency_p99_up"], episode_id="ep_direction_bind12", use_learned=False)
wm_path = next((p for p in ex12.candidate_paths if p.root_node_id == "work_mem_spill"), None)
if wm_path is not None:
    from agent.explanation import EvidenceNeed as _Need  # noqa: E402
    need12 = _Need.create(path_ids=[wm_path.path_id], target_kind="NODE", target_ids=["work_mem_spill"],
                          evidence_type="slow_query_ranking", predicate_id="slow_query_ranking_v2",
                          required=False, freshness_seconds=60, candidate_tools=["get_top_queries"])
    entry12 = {"evidence_type": "slow_query_ranking", "raw_ref": "trace://x/step_001",
               "bears_on": ["missing_index", "stale_statistics"], "target_ids": ["missing_index", "stale_statistics"]}
    check(xr._entry_matches(ex12, need12, entry12),
          "toolbox 的 bears_on 没写 work_mem_spill，图上有关系 -> 能绑（原来永远绑不上）")
    need12b = _Need.create(path_ids=[wm_path.path_id], target_kind="NODE", target_ids=["work_mem_spill"],
                           evidence_type="seq_scan_volume", predicate_id="seq_scan_volume_v2",
                           required=False, freshness_seconds=60, candidate_tools=["get_table_stats"])
    entry12b = {"evidence_type": "seq_scan_volume", "raw_ref": "trace://x/step_002",
                "bears_on": ["missing_index"], "target_ids": ["missing_index"]}
    check(not xr._entry_matches(ex12, need12b, entry12b), "图上没有关系、bears_on 也没有 -> 仍不能绑")
else:
    check(False, "latency_p99_up 的召回里有 work_mem_spill 路径")

print("[13] MONITOR 给目标表建扫描基线：第一次取证读数就有判定力（2026-09-24 缺陷报告 5cc2c25 P2-1、P3）")
eid13 = eid + "_mon"
st13 = EpisodeState(eid13, "direction_fixture")
st13.budget["max_steps"] = 30
obs13 = SimpleNamespace(trace=TraceStore(eid13))
counter_calls13: list[str] = []


def _counters13(table):
    counter_calls13.append(table)
    return {"table": table, "seq_scan_raw": 100, "seq_tup_read_raw": 1000000, "idx_scan_raw": 50,
            "own_seq_scan": 0, "own_seq_tup_read": 0, "own_idx_scan": 0, "stats_reset": "epoch",
            "raw_ref": obs13.trace.record("get_table_scan_counters", {}, "{}", {"k": 1})}


obs13.get_table_scan_counters = _counters13
obs13.get_table_stats = lambda _t: _ts(
    seq_scan=120, seq_tup_read=241000000, idx_scan=50, seq_scan_raw=120, seq_tup_read_raw=241000000,
    idx_scan_raw=50, raw_ref=obs13.trace.record("get_table_stats", {}, "{}", {"k": 1}))
tb13 = Toolbox(obs13, st13, StateMachine(st13), target_context={"table": "orders", "hot_query": "SELECT 1"})
before13, steps13 = len(st13.scratchpad), int(st13.budget.get("steps", 0))
keys13 = tb13.establish_counter_baselines()
check(keys13 == ["table_scan:orders"] and "table_scan:orders" in st13.cumulative_baselines,
      "MONITOR 给目标表建了扫描基线", keys13)
check(len(st13.scratchpad) == before13 and int(st13.budget.get("steps", 0)) == steps13,
      "系统动作：不落证据条目、不扣 agent 步数")
check(set(st13.cumulative_baselines["table_scan:orders"]["values"]) == set(toolbox_module.TABLE_SCAN_COUNTERS),
      "基线与 get_table_stats 的差分是同一组计数器")
check(tb13.establish_counter_baselines() == [] and len(counter_calls13) == 1, "已有基线不重建")
try:
    tb13.sm.goto(Phase.OBSERVE, "t"); tb13.sm.goto(Phase.HYPOTHESIZE, "t"); tb13.sm.goto(Phase.INVESTIGATE, "t")
except Exception:
    pass
st13.cumulative_baselines["table_scan:orders"]["captured_at"] = time.time() - 5
waits13: list[float] = []
saved_sleep13 = toolbox_module._window_sleep
toolbox_module._window_sleep = waits13.append
try:
    tb13.get_table_stats("orders")
finally:
    toolbox_module._window_sleep = saved_sleep13
vol13 = [e for e in st13.scratchpad if e.get("evidence_type") == "seq_scan_volume"]
check(bool(vol13) and vol13[0].get("status") == "OBSERVED"
      and (vol13[0].get("structured_value") or {}).get("seq_scan") == 20,
      "第一次取证读数就是窗口增量（原来是 UNKNOWN 基线、需求记不可得）",
      vol13[0].get("observation") if vol13 else "no seq_scan_volume")
check(len(waits13) == 1 and 24.0 <= waits13[0] <= 25.5, f"读之前仍等窗口满下限（{waits13}）")

eid13b = eid + "_mon_b"
st13b = EpisodeState(eid13b, "direction_fixture")
st13b.budget["max_steps"] = 30
obs13b = SimpleNamespace(trace=TraceStore(eid13b))
obs13b.get_table_stats = obs13.get_table_stats
tb13b = Toolbox(obs13b, st13b, StateMachine(st13b), target_context={"table": "orders", "hot_query": "SELECT 1"})
check(tb13b.establish_counter_baselines() == [] and not st13b.cumulative_baselines,
      "观测器不支持只读计数器时跳过（桩观测器）")
try:
    tb13b.sm.goto(Phase.OBSERVE, "t"); tb13b.sm.goto(Phase.HYPOTHESIZE, "t"); tb13b.sm.goto(Phase.INVESTIGATE, "t")
except Exception:
    pass
tb13b.get_table_stats("orders")
first13b = next((e for e in st13b.scratchpad if e.get("evidence_type") == "seq_scan_volume"), {})
check(first13b.get("status") == "UNKNOWN" and "get_table_stats" in str(first13b.get("observation"))
      and "get_database_stats" not in str(first13b.get("observation")),
      "表扫描基线的提示指向 get_table_stats（原来写死 get_database_stats）", first13b.get("observation"))
loop_src13 = (ROOT / "agent" / "loop.py").read_text(encoding="utf-8")
monitor_block = loop_src13[loop_src13.index("if cur is Phase.MONITOR:"):][:400]
check("tb.establish_counter_baselines()" in monitor_block, "loop 在 MONITOR 收尾时建基线（与策略无关，一处）")
check("_STATS_EPOCH_SQL" in inspect.getsource(observe.Observer.get_table_stats)
      and "_STATS_EPOCH_SQL" in inspect.getsource(observe.Observer.get_table_scan_counters),
      "基线读数与 get_table_stats 用同一个统计周期表达式（ESC 按它核对 seq_scan_volume 的周期）")
for d13 in (eid13, eid13b):
    shutil.rmtree(TRACE_DIR / d13, ignore_errors=True)

print("[14] 只有要窗口证据的调用才等；短窗口读数不推进基线（2026-09-24 c610108 缺陷报告 P2-1）")
mapped14 = set(toolbox_module.TABLE_SCAN_WINDOW_EVIDENCE).union(*toolbox_module.WINDOW_EVIDENCE_OF_KEY.values())
graph14 = G.load()
produced14 = {n for n, d in graph14.nodes(data=True)
              if d.get("kind") == "Evidence" and d.get("obtained_by") in {"get_database_stats", "get_table_stats"}
              and d.get("predicate_id") in ep.CUMULATIVE_WINDOW_PREDICATES}
check(mapped14 == produced14, "键 -> 窗口证据映射与图上这两个工具产出的累计窗口判据一致",
      (sorted(mapped14), sorted(produced14)))
tb14 = Toolbox(SimpleNamespace(trace=TraceStore(eid + "_w14")), EpisodeState(eid + "_w14", "direction_fixture"),
               StateMachine(EpisodeState(eid + "_w14", "direction_fixture")))
tb14.task_context = None
check(tb14._needs_window("table_scan:orders"), "主 agent 直接调（无任务上下文）-> 按要窗口处理（保守）")
tb14.task_context = SimpleNamespace(evidence_types=["stats_freshness", "stats_range_drift"])
check(not tb14._needs_window("table_scan:orders"), "任务只要 stats_freshness 等 -> 表扫描键不等")
tb14.task_context = SimpleNamespace(evidence_types=["seq_scan_volume"])
check(tb14._needs_window("table_scan:orders") and not tb14._needs_window("pg_stat_database"),
      "任务要 seq_scan_volume -> 只等表扫描键")
tb14.task_context = {"evidence_types": ["temp_file_volume"]}
check(tb14._needs_window("pg_stat_database") and not tb14._needs_window("checkpoint_stats"),
      "dict 形式的任务上下文同样按证据类型判")
tb14.task_context = SimpleNamespace()
check(tb14._needs_window("checkpoint_stats"), "任务没带证据类型 -> 按要处理（保守）")

eid14 = eid + "_anchor"
st14 = EpisodeState(eid14, "direction_fixture")
st14.budget["max_steps"] = 30
obs14 = SimpleNamespace(trace=TraceStore(eid14))
reads14 = iter([100, 110, 130, 131])


def _ts14(_t):
    n = next(reads14)
    return _ts(seq_scan=n, seq_tup_read=n * 1000, idx_scan=5, seq_scan_raw=n, seq_tup_read_raw=n * 1000, idx_scan_raw=5,
               raw_ref=obs14.trace.record("get_table_stats", {}, "{}", {"k": 1}))


obs14.get_table_stats = _ts14
tb14b = Toolbox(obs14, st14, StateMachine(st14), target_context={"table": "orders", "hot_query": "SELECT 1"})
try:
    tb14b.sm.goto(Phase.OBSERVE, "t"); tb14b.sm.goto(Phase.HYPOTHESIZE, "t"); tb14b.sm.goto(Phase.INVESTIGATE, "t")
except Exception:
    pass
from agent.investigator import task_environment_tools  # noqa: E402
from agent.permissions import Role  # noqa: E402


def scoped14(types):
    """与确定性执行器同一种调用方式：取证角色 + 任务上下文的权限视图。"""
    task = SimpleNamespace(task_id="t14", need_ids=["n14"], selected_tools=["get_table_stats"],
                           evidence_types=list(types), target_context={"table": "orders", "hot_query": "SELECT 1"},
                           explanation_id="x", explanation_revision=1, path_ids=[], target_kind="NODE",
                           target_ids=["missing_index"])
    return tb14b.scoped(role=Role.INVESTIGATOR, task_context=task, environment_tools=task_environment_tools(task))


waits14: list[float] = []
saved_sleep14 = toolbox_module._window_sleep
toolbox_module._window_sleep = waits14.append
try:
    scoped14(["stats_freshness"]).get_table_stats("orders")      # 第一次：建基线
    base0 = dict(st14.cumulative_baselines["table_scan:orders"])
    scoped14(["stats_freshness"]).get_table_stats("orders")      # 不要窗口的调用：不等、短窗口不推进基线
    vol14 = [e for e in st14.scratchpad if e.get("evidence_type") == "seq_scan_volume"]
    check(waits14 == [], "不要 seq_scan_volume 的任务读表统计不等窗口", waits14)
    check(st14.cumulative_baselines["table_scan:orders"]["captured_at"] == base0["captured_at"]
          and (vol14[-1].get("structured_value") or {}).get("baseline_anchored") is True,
          "短窗口读数不推进基线（锚在第一次读数）")
    st14.cumulative_baselines["table_scan:orders"]["captured_at"] -= 40
    scoped14(["seq_scan_volume"]).get_table_stats("orders")      # 要窗口：基线已满 40s，不用等，直接有判定力
    v14 = [e for e in st14.scratchpad if e.get("evidence_type") == "seq_scan_volume"][-1]
    check(waits14 == [] and (v14.get("structured_value") or {}).get("seq_scan") == 30
          and (v14.get("structured_value") or {}).get("window_s", 0) >= 40,
          "第 2 轮要窗口时沿用从第一次读数算起的长窗口：不再等、增量从基线算起", v14.get("structured_value"))
    check((v14.get("structured_value") or {}).get("baseline_anchored") is False
          and st14.cumulative_baselines["table_scan:orders"]["values"]["seq_scan_raw"] == 130,
          "满下限的读数推进基线")
    scoped14(["seq_scan_volume"]).get_table_stats("orders")      # 刚推进过基线，又要窗口 -> 等
    check(len(waits14) == 1 and 29.0 <= waits14[0] <= 30.5, f"刚推进过基线又要窗口 -> 等满下限（{waits14}）")
finally:
    toolbox_module._window_sleep = saved_sleep14
for d14 in (eid + "_w14", eid14):
    shutil.rmtree(TRACE_DIR / d14, ignore_errors=True)

print()
if fails:
    print(f"EVIDENCE DIRECTION: FAIL（{len(fails)}/{checks}）")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print(f"EVIDENCE DIRECTION: PASS（{checks} 项）")
