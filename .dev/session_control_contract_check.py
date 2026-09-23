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

print()
if fails:
    print(f"SESSION CONTROL CONTRACT: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"SESSION CONTROL CONTRACT: PASS（{checks} 项）")
