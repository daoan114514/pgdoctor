#!/usr/bin/env python3
"""会话控制修复的合同检查：terminate_idle_backend 的前置条件必须能被观测器提供的行满足。

2026-09-22 实测：观测器把 idle 会话全过滤掉，而前置条件 pid_is_client_backend_and_state_idle
要求 pid 来自**已观测到的 idle 行** —— 这条修复在结构上永远提不出来，主策略在 PLAN 阶段
三次提交三次被拒，12 轮全空转，connection_exhaustion 诊断对了却 Outcome=False。
这里钉住两件事：过滤规则在 include_idle=True 时放行 idle；前置条件的判定函数对
观测器产出的 idle 客户端行全部为真。
"""
from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sandbox.observe import SessionDigest, keep_session  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond and detail else ""))


print("[1] 过滤规则")
check(keep_session("idle", 100.0, 1.0, include_idle=False) is False, "默认不带 idle（异常会话视图保持干净）")
check(keep_session("idle", 100.0, 1.0, include_idle=True) is True, "include_idle=True 放行 idle")
check(keep_session("active", 0.2, 1.0, include_idle=True) is False, "短 active 仍过滤（正常查询不是异常）")
check(keep_session("idle in transaction", 30.0, 1.0, include_idle=False) is True, "idle in transaction 一直算异常")
check(keep_session("active", 5.0, 1.0, include_idle=False) is True, "长 active 算异常")

print("[2] 观测器产出的 idle 客户端行能满足 terminate_idle_backend 的前置条件")
# 与 sandbox/observe.py::get_active_sessions 里构造行的方式一致
row = asdict(SessionDigest(
    4242, "idle", None, 120.0, "", role="app_user",
    transaction_age_seconds=None, backend_type="client backend", backend_xmin="",
    is_current_diagnostic_connection=False,
    is_system_or_diagnostic=False, identity_rechecked=True))
conds = {
    "pid_is_client_backend_and_state_idle": lambda r: (
        str(r.get("backend_type", "client backend")).lower() == "client backend"
        and str(r.get("state", "")).lower() == "idle"),
    "pid_is_not_current_diagnostic_connection": lambda r: not bool(
        r.get("is_current_diagnostic_connection", True)),
    "role_is_not_system_or_diagnostic": lambda r: bool(
        (r.get("role") or r.get("usename")) and not r.get("is_system_or_diagnostic", True)),
    "pid_identity_rechecked_fresh": lambda r: bool(r.get("identity_rechecked", True)),
}
# 判定函数必须与 agent/explanation_runtime.py 的 checks 逐字一致；那边改了这边要跟
import inspect  # noqa: E402
from agent import explanation_runtime as er  # noqa: E402
src = inspect.getsource(er)
for cid, fn in conds.items():
    check(fn(row), f"{cid} 对 idle 客户端行为真")
    check(f'"{cid}"' in src, f"{cid} 仍存在于 explanation_runtime 的判定表")
diag = asdict(SessionDigest(
    7, "idle", None, 5.0, "", role="agent_ro", transaction_age_seconds=None,
    backend_type="client backend", backend_xmin="",
    is_current_diagnostic_connection=False, is_system_or_diagnostic=True,
    identity_rechecked=True))
check(not conds["role_is_not_system_or_diagnostic"](diag), "诊断连接自己的 idle 行不能被终止（规则 3）")

print("[3] PLAN 阶段刚取到、尚未绑定的会话行能满足 pid 前置条件；过期行不能（2026-09-23）")
import json  # noqa: E402
import shutil  # noqa: E402
import time  # noqa: E402
from agent.episode_state import EpisodeState  # noqa: E402
from agent.explanation import ExplanationScope  # noqa: E402
from knowledge.causal_graph import graph as G  # noqa: E402
from sandbox.traces import TRACE_DIR, TraceStore  # noqa: E402

eid = "ep_session_control_contract_fixture"
store = TraceStore(eid)
paths = G.enumerate_causal_paths(["latency_p99_up"], use_learned=False)
path = next(p for p in paths if p.node_ids == ["missing_index", "latency_p99_up"])
explanation = G.merge_paths([path], episode_id=eid, observed_symptoms=["latency_p99_up"])
explanation.select_paths([path.path_id], unexplained_symptoms=[], scope=ExplanationScope.FULL)
st = EpisodeState(eid, "session_control_fixture")
st.explanation_graph = explanation
rows = [row, diag]
ref = store.record("bind_structured_evidence", {"evidence_type": "session_wait_profile"},
                   json.dumps(rows), rows)
st.note("agent", "session_wait_profile", "PLAN 阶段 include_idle 观测", ref,
        ["lock_contention"], status="OBSERVED", structured_value=rows)
PID_CONDS = ["concrete_pid_bound", "pid_is_client_backend_and_state_idle",
             "pid_is_not_current_diagnostic_connection", "role_is_not_system_or_diagnostic",
             "pid_identity_rechecked_fresh"]
option = {"path_id": path.path_id, "target_node_id": "missing_index", "fix": "create_covering_index",
          "preconditions": [{"id": cid, "required": True} for cid in PID_CONDS]}


def evaluate(sql):
    return {r["condition_id"]: r for r in er._evaluate_preconditions(st, option=option, sql=sql)}


res = evaluate("SELECT pg_terminate_backend(4242)")
check(all(res[c]["satisfied"] for c in PID_CONDS), "未绑定的新鲜 idle 行满足全部 pid 前置条件: " + str({c: res[c]["satisfied"] for c in PID_CONDS}))
check(all(ref in res[c]["evidence_refs"] for c in PID_CONDS if c != "concrete_pid_bound"), "前置条件引用了该观测的 raw_ref")
res = evaluate("SELECT pg_terminate_backend(7)")
check(not res["role_is_not_system_or_diagnostic"]["satisfied"], "诊断连接的 idle 行仍不能被终止")
res = evaluate("SELECT pg_terminate_backend(9999)")
check(not res["pid_is_client_backend_and_state_idle"]["satisfied"], "没观测过的 pid 不满足")
st.scratchpad[-1]["ts"] = time.time() - er.PID_ROW_FRESHNESS_S - 60
res = evaluate("SELECT pg_terminate_backend(4242)")
check(not res["pid_identity_rechecked_fresh"]["satisfied"], "超过 PID_ROW_FRESHNESS_S 的行不再满足 identity 新鲜度")
st.scratchpad[-1]["structured_value"] = [dict(row, pid=4242, state="active")]
st.scratchpad[-1]["ts"] = time.time()
res = evaluate("SELECT pg_terminate_backend(4242)")
check(not res["pid_is_client_backend_and_state_idle"]["satisfied"], "篡改过的 scratchpad 值（digest 不符）不被信任")

print("[4] 多 pid 终止：每个 pid 各自满足前置条件（2026-09-24，connection_exhaustion 单 pid 修不好）")
row2 = dict(row, pid=4343)
rows2 = [row, row2, diag]
ref2 = store.record("bind_structured_evidence", {"evidence_type": "session_wait_profile"},
                    json.dumps(rows2), rows2)
st.note("agent", "session_wait_profile", "PLAN 阶段 include_idle 观测（两行 idle）", ref2,
        ["lock_contention"], status="OBSERVED", structured_value=rows2)
res = evaluate("SELECT pg_terminate_backend(4242), pg_terminate_backend(4343)")
check(all(res[c]["satisfied"] for c in PID_CONDS), "两个都观测过的 idle pid：全部前置条件满足")
res = evaluate("SELECT pg_terminate_backend(4242), pg_terminate_backend(9999)")
check(not res["pid_is_client_backend_and_state_idle"]["satisfied"] and "9999" in res["pid_is_client_backend_and_state_idle"]["reason"],
      "其中一个没观测过：整条不满足，原因里点名该 pid")
res = evaluate("SELECT pg_terminate_backend(4242), pg_terminate_backend(7)")
check(not res["role_is_not_system_or_diagnostic"]["satisfied"], "其中一个是诊断连接：整条不满足")
check(er._sql_facts("SELECT pg_terminate_backend(4242), pg_terminate_backend(4343)")["pids"] == [4242, 4343], "_sql_facts 给出 pid 列表")
shutil.rmtree(TRACE_DIR / eid, ignore_errors=True)

print("[5] 护盾形态与 gate 的执行前复核")
from safety import gate, shield  # noqa: E402
from safety.gate import RemediationProposal  # noqa: E402
ok, _r, got = shield.inspect_session_control("SELECT pg_terminate_backend(11), pg_terminate_backend(12), pg_terminate_backend(13)")
check(ok and got == [11, 12, 13], "三个常量 pid 合规")
check(not shield.inspect_session_control("SELECT pg_terminate_backend(11), pg_terminate_backend(11)")[0], "重复 pid 拒绝")
check(not shield.inspect_session_control("SELECT pg_terminate_backend(11), pg_cancel_backend(12)")[0], "混用两种函数拒绝")
many = ", ".join(f"pg_terminate_backend({i})" for i in range(1, shield.MAX_SESSION_TARGETS + 2))
check(not shield.inspect_session_control("SELECT " + many)[0], f"超过 {shield.MAX_SESSION_TARGETS} 个拒绝")
check(not shield.inspect_session_control("SELECT pg_terminate_backend(11), pg_terminate_backend(pid) FROM pg_stat_activity")[0], "夹带非常量项拒绝")
d = gate.assess(RemediationProposal(action_type="session_control",
                                    sql="SELECT pg_terminate_backend(11), pg_terminate_backend(12)",
                                    rollback="IRREVERSIBLE", root_cause="connection_exhaustion",
                                    fix_id="terminate_idle_backend"))
check(d.approved and d.tier == "CONFIRM", "多 pid 提案过门且仍是 CONFIRM", d.reasons)
real_query = gate.db.query
try:
    gate.db.query = lambda *a, **k: [(11, "idle", "client backend", "app_user"),
                                     (12, "active", "client backend", "app_user")]
    p2 = RemediationProposal(action_type="session_control",
                             sql="SELECT pg_terminate_backend(11), pg_terminate_backend(12), pg_terminate_backend(13)",
                             rollback="IRREVERSIBLE", fix_id="terminate_idle_backend")
    ok2, why2 = gate._recheck_sessions(p2)
    check(not ok2 and "12" in why2 and "13" in why2, "复核：一个已变 active、一个已不存在 -> 拒绝并点名", why2)
    gate.db.query = lambda *a, **k: [(11, "idle", "client backend", "app_user"),
                                     (12, "idle", "client backend", "agent_ro")]
    ok3, why3 = gate._recheck_sessions(RemediationProposal(
        action_type="session_control", sql="SELECT pg_terminate_backend(11), pg_terminate_backend(12)",
        rollback="IRREVERSIBLE", fix_id="terminate_idle_backend"))
    check(not ok3 and "诊断" in why3, "复核：诊断连接拒绝", why3)
    gate.db.query = lambda *a, **k: [(11, "idle", "client backend", "app_user"),
                                     (12, "idle", "client backend", "app_user")]
    ok4, _w = gate._recheck_sessions(RemediationProposal(
        action_type="session_control", sql="SELECT pg_terminate_backend(11), pg_terminate_backend(12)",
        rollback="IRREVERSIBLE", fix_id="terminate_idle_backend"))
    check(ok4, "复核：都仍是 idle 客户端 -> 放行")
finally:
    gate.db.query = real_query
import inspect as _inspect  # noqa: E402
esrc = _inspect.getsource(gate.execute)
check(esrc.index("_recheck_sessions(p)") < esrc.index("undo_journal.append("), "复核在写 journal 与执行之前")

print("[6] 只占连接的空闲事务：连接逼近上限有可信证据时可终止；预期效果只留连接那一条（2026-09-24 misleading）")
eid6 = "ep_session_control_contract_fixture6"
store6 = TraceStore(eid6)
paths6 = G.enumerate_causal_paths(["throughput_down"], use_learned=False)
path6 = next(p for p in paths6 if p.node_ids == ["long_idle_transaction", "connection_exhaustion", "throughput_down"])
exp6 = G.merge_paths([path6], episode_id=eid6, observed_symptoms=["throughput_down"])
exp6.select_paths([path6.path_id], unexplained_symptoms=[], scope=ExplanationScope.FULL)
st6 = EpisodeState(eid6, "session_control_fixture6")
st6.explanation_graph = exp6
iit = asdict(SessionDigest(5151, "idle in transaction", None, 190.0, "SELECT 1", role="app_user",
                           transaction_age_seconds=190.0, backend_type="client backend", backend_xmin="",
                           is_current_diagnostic_connection=False, is_system_or_diagnostic=False,
                           identity_rechecked=True))
ref6 = store6.record("bind_structured_evidence", {"evidence_type": "session_wait_profile"},
                     json.dumps([iit]), [iit])
st6.note("agent", "session_wait_profile", "PLAN 阶段 include_idle 观测", ref6, ["long_idle_transaction"],
         status="OBSERVED", structured_value=[iit])
options6 = [o for o in er.intervention_options(st6) if o["fix"] == "terminate_idle_transaction"]
check(len(options6) == 1, "长事务→连接耗尽这条路径上有 terminate_idle_transaction 选项")
if options6:
    metrics6 = [e["metric"] for e in options6[0]["expected_effects"]]
    check(metrics6 == ["connection_usage_ratio"], f"预期效果只留证明本路径节点的那条: {metrics6}")
    res6 = {r["condition_id"]: r for r in er._evaluate_preconditions(
        st6, option=options6[0], sql="SELECT pg_terminate_backend(5151)")}
    check(not res6["session_impact_bound"]["satisfied"], "没有连接逼近上限的证据：不持锁不持快照的空闲事务不能杀")
    from agent.explanation import EvidenceBinding  # noqa: E402
    cc = {"used": 97, "max_connections": 100, "near_limit": True, "idle_in_transaction": 87}
    ref_cc = store6.record("fixture", {"evidence_type": "connection_count"}, json.dumps(cc), cc)
    exp6.add_evidence_binding(EvidenceBinding.create(
        episode_id=eid6, raw_ref=ref_cc, evidence_type="connection_count", status="OBSERVED",
        observed_at=time.time(), predicate_id="connection_count_v2", predicate_result="SUPPORTS",
        structured_value=cc, target_node_ids=["connection_exhaustion"], target_edge_ids=[],
        fresh_until=time.time() + 3600))
    res6 = {r["condition_id"]: r for r in er._evaluate_preconditions(
        st6, option=options6[0], sql="SELECT pg_terminate_backend(5151)")}
    check(res6["session_impact_bound"]["satisfied"] and ref_cc in res6["session_impact_bound"]["evidence_refs"],
          "有连接逼近上限的可信证据：占连接即算危害，引用该证据")
    check(all(r["satisfied"] for r in res6.values()), "全部前置条件满足: " + str({k: v["satisfied"] for k, v in res6.items()}))
shutil.rmtree(TRACE_DIR / eid6, ignore_errors=True)

print()
if fails:
    print(f"SESSION CONTROL CONTRACT: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"SESSION CONTROL CONTRACT: PASS（{checks} 项）")
