#!/usr/bin/env python3
"""证据新鲜窗的语料回放：默认窗必须覆盖真实 episode 的时长。

CLAUDE.md 硬规则 5：阈值用真实观测值标定。回放 2026-09-22 那批 eval episode：
对每条绑定按 observed_at + DEFAULT_FRESHNESS_S 重算 fresh_until，断言在该 episode
最后一个 step 落盘时刻仍然新鲜。原 300s 默认值下这四个 episode 结束时仍新鲜的绑定是
0/26、14/43、2/53、2/24 —— 全部路径 INCONCLUSIVE 的直接原因。

trace 不在了就跳过并打印，不假装通过。
"""
from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.explanation import DEFAULT_FRESHNESS_S  # noqa: E402

CORPUS = [
    "ep_connection_exhaustion_eval_v1_1790069129",
    "ep_misleading_idle_txn_eval_v1_1790067743",
    "ep_missing_index_eval_v1_1790088897",
    "ep_stale_statistics_eval_v1_1790090995",
]
fails: list[str] = []
checks = 0


def check(cond: bool, label: str) -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}")


print(f"[1] DEFAULT_FRESHNESS_S = {DEFAULT_FRESHNESS_S}s")
seen = 0
for ep in CORPUS:
    d = ROOT / "traces" / ep
    state = d / "episode_state.json"
    if not state.exists():
        print(f"    SKIP {ep}（trace 已不在）")
        continue
    seen += 1
    st = json.loads(state.read_text(encoding="utf-8"))
    bs = list((st.get("explanation_graph") or {}).get("evidence_bindings", {}).values())
    steps = glob.glob(str(d / "step_*.json"))
    end = max(os.path.getmtime(s) for s in steps) if steps else 0.0
    span = (end - float(st.get("started_at") or end)) / 60
    old = sum(1 for b in bs if b.get("fresh_until") and float(b["fresh_until"]) >= end)
    new = sum(1 for b in bs if b.get("observed_at") and
              float(b["observed_at"]) + DEFAULT_FRESHNESS_S >= end)
    check(new == len(bs),
          f"{ep[:38]:38s} 跨度 {span:4.0f} 分钟，绑定 {len(bs)}：原窗结束时新鲜 {old}，"
          f"新窗 {new}/{len(bs)}")
check(seen >= 1, "至少回放了一个真实 episode（否则这个检查没有刻度）")

print()
if fails:
    print(f"EVIDENCE FRESHNESS: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"EVIDENCE FRESHNESS: PASS（{checks} 项）")
