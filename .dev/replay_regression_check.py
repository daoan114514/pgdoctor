#!/usr/bin/env python3
"""离线重放回归：2026-09-23 那批 5 个真实 episode，在当前因果图与判据下必须选中真根因且
ESC SUFFICIENT。改图、改判据、改 ESC 之后它先报警，不必花额度跑活库。

trace 不在本机（新克隆）时跳过该条，不算失败；在就必须对。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("replay_explanation", ROOT / ".dev" / "replay_explanation.py")
replay_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay_mod)

CASES = [
    ("ep_misleading_idle_txn_eval_v1_1790156694", "long_idle_transaction",
     "阻塞链判据曾把 87 个持锁 0 个表的空闲事务数成阻塞记录，选成 lock_contention、AMBIGUOUS"),
    ("ep_lock_contention_eval_v1_1790155901", "lock_contention",
     "deadlock 路径曾因不具判别力的锁等待支持而 INCONCLUSIVE"),
    ("ep_connection_exhaustion_eval_v1_1790155289", "connection_exhaustion", ""),
    ("ep_missing_index_eval_v1_1790169467", "missing_index", ""),
    ("ep_stale_statistics_eval_v1_1790168126", "stale_statistics", ""),
]
fails, ran, skipped = [], 0, 0
for episode_id, truth, why in CASES:
    if not (ROOT / "traces" / episode_id / "episode_state.json").exists():
        skipped += 1
        print(f"    SKIP {episode_id}（本机没有这份 trace）")
        continue
    ran += 1
    out = replay_mod.replay(episode_id)
    ok = out["claimed"] == truth and out["verdict"] == "SUFFICIENT"
    print(f"    {'OK ' if ok else 'FAIL'} {episode_id}: claimed={out['claimed']} ESC={out['verdict']}"
          + ("" if ok else f"  期望 {truth}；{why}；failed={out['failed']}"))
    if not ok:
        fails.append(episode_id)
print()
if fails:
    print(f"REPLAY REGRESSION: FAIL（{len(fails)}/{ran}，跳过 {skipped}）")
    raise SystemExit(1)
print(f"REPLAY REGRESSION: PASS（{ran} 个，跳过 {skipped}）")
