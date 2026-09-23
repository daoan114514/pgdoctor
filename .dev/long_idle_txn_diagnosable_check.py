#!/usr/bin/env python3
"""long_idle_transaction 必须能在它自己的场景里被诊断（架构评审第 5 条）。

edges.yaml 原来把 session_wait_profile 记为 long_idle_transaction 的 required 证据，
而 _session_wait 只在 Lock:* 等待时 SUPPORTS；空闲事务堆积的会话等在 Client:ClientRead，
于是它在自己的场景里 8 次 REFUTES，required_supported 永远不成立。
判别它的是 idle_in_transaction（power 0.95）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.episode_state import EpisodeState  # noqa: E402
from agent.explanation_runtime import recompute_statuses  # noqa: E402
from knowledge.causal_graph import graph as G  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))


print("[1] 图定义")
req = G.required_evidence("long_idle_transaction")
check("session_wait_profile" not in req, f"session_wait_profile 不再是必需证据（required={req}）")
check("idle_in_transaction" in req, "idle_in_transaction 仍是必需证据")

print("[2] 真实 trace 重放：misleading_idle_txn（修复前 session_wait_profile 8 次 REFUTES）")
EP = "ep_misleading_idle_txn_eval_v1_1790067743"
if not (ROOT / "traces" / EP / "episode_state.json").exists():
    print(f"    SKIP {EP}（trace 已不在）")
else:
    st = EpisodeState.load(EP)
    graph = st.explanation_graph
    paths = [p for p in graph.candidate_paths if p.root_node_id == "long_idle_transaction"]
    check(bool(paths), f"解释里有 long_idle_transaction 的路径（{len(paths)}）")
    # 路径的 required_evidence_types 是建图时烙进去的；按当前图刷新后再重算，
    # 相当于在新图上重放这份证据。时间取最后一次观测之后，避免新鲜窗干扰。
    for p in paths:
        p.required_evidence_types = G.required_evidence(p.root_node_id)
    latest = max((float(b.observed_at or 0) for b in graph.evidence_bindings.values()), default=0.0)
    recompute_statuses(st, now=latest + 1.0)
    statuses = {p.path_id: p.status for p in paths}
    check(all(s != "REFUTED" for s in statuses.values()),
          f"重算后没有一条 long_idle_transaction 路径被 REFUTED（{sorted(set(statuses.values()))}）")
    print(f"    （信息）重算后路径状态分布: {sorted(set(statuses.values()))}")

print()
if fails:
    print(f"LONG IDLE TXN DIAGNOSABLE: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"LONG IDLE TXN DIAGNOSABLE: PASS（{checks} 项）")
