#!/usr/bin/env python3
"""ESC 引导与 EXHAUSTED 范围（架构评审第 4 条）。

(a) DIAGNOSE 没选出路径时也要过 ESC，否则 directives 从不生成、esc_retries 从不增加，
    EXHAUSTED/AMBIGUOUS 出口不可达（stale_statistics 32 步 esc=[]）。
(b) 只有选中路径与未决 P0 上的必需证据长期不可得才算 EXHAUSTED；未选中备选路径的
    不可得不再触发升级。2026-09-22 lock_contention 的 trace 正是反例语料。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import esc as esc_mod  # noqa: E402
from agent import loop as loop_mod  # noqa: E402
from agent.episode_state import EpisodeState  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))


print("[1] 结构约束")
lsrc = inspect.getsource(loop_mod.run_episode)
check("nxt is Phase.INVESTIGATE and st.schema_version == 2" in lsrc,
      "主循环在 DIAGNOSE -> INVESTIGATE（v2）时也跑 ESC")
esrc = inspect.getsource(esc_mod)
check("unavailable_alternatives" in esrc and "blocking_path_ids" in esrc,
      "ESC 把备选路径的不可得与阻塞性不可得分开")

print("[2] 真实 trace 重放：lock_contention rev4（选中路径 SUPPORTED，备选 explain 被拒）")
EP = "ep_lock_contention_eval_v1_1790092275"
if not (ROOT / "traces" / EP / "episode_state.json").exists():
    print(f"    SKIP {EP}（trace 已不在）")
else:
    st = EpisodeState.load(EP)
    old = (st.esc_reports or [None])[-1]
    check(old is not None and old.get("verdict") == "EXHAUSTED",
          "修复前该 episode 的最后一次 ESC 是 EXHAUSTED（语料前提）",
          old and old.get("verdict"))
    rep = esc_mod.check_explanation(st)
    check(rep["verdict"] != "EXHAUSTED",
          f"重算后不再 EXHAUSTED（实得 {rep['verdict']}）", rep["verdict"])
    dim = next((d for d in rep.get("dimensions", [])
                if isinstance(d, dict) and d.get("name") == "BUDGET_AND_AVAILABILITY"), None)
    check(dim is not None and "alternatives_unavailable=3" in str(dim.get("detail")),
          "3 个备选路径的不可得被记为 alternatives_unavailable", dim and dim.get("detail"))
    check(dim is not None and not dim.get("missing"),
          "阻塞性 long_unavailable 为空", dim and dim.get("missing"))

print()
if fails:
    print(f"ESC STEERING: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"ESC STEERING: PASS（{checks} 项）")
