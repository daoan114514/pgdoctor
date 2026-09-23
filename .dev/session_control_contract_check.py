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


def check(cond: bool, label: str) -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}")


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
shutil.rmtree(TRACE_DIR / eid, ignore_errors=True)

print()
if fails:
    print(f"SESSION CONTROL CONTRACT: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"SESSION CONTROL CONTRACT: PASS（{checks} 项）")
