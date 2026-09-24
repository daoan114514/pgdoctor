#!/usr/bin/env python3
"""守护看门狗的休眠判定：结果写于休眠开始之前才有效，否则作废重跑（2026-09-24 c610108 缺陷报告 P3）。

原来 60 秒一轮轮询、检测到休眠一律宣布"观测窗与 KPI 已失效，清理后重跑"，实际却按"有没有
可用结果"决定去留：missing_index 在休眠前 1 分钟完成，日志说作废、结果被保留（碰巧对）；反过来
休眠落在 episode 中间时，醒来后 run_suite 可能在看门狗察觉前跑完 VERIFY、写出横跨休眠的"完整"
结果，被当成可用（静默失败）。这里钉住 suspend_verdict 的每条分支与 run_one 的接线。
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("eval_guard_under_test", ROOT / ".dev" / "eval_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))


print("[1] suspend_verdict 的分支")
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "guard_missing_index.json"
    stem = "missing_index_eval_v1"

    def write(mtime: float, scenario: str = stem) -> None:
        path.write_text(json.dumps({"episodes": [{"scenario": scenario, "fired": True}]}), encoding="utf-8")
        os.utime(path, (mtime, mtime))

    run_start, suspend_start = 1_000.0, 2_000.0
    ok, why = guard.suspend_verdict(path, stem, run_start, suspend_start)
    check(not ok and "没有写出结果" in why, "没写结果 -> 作废", why)
    write(1_500.0)
    ok, why = guard.suspend_verdict(path, stem, run_start, suspend_start)
    check(ok and "结果有效" in why, "本次运行写的、早于休眠开始 -> 有效（2026-09-24 missing_index 的情形）", why)
    write(2_000.0)
    ok, _ = guard.suspend_verdict(path, stem, run_start, suspend_start)
    check(ok, "恰好写于休眠起点 -> 有效（边界含等号）")
    write(2_010.0)
    ok, why = guard.suspend_verdict(path, stem, run_start, suspend_start)
    check(not ok and "晚于休眠开始" in why, "写于休眠开始之后（醒来后才写完）-> 作废", why)
    write(900.0)
    ok, why = guard.suspend_verdict(path, stem, run_start, suspend_start)
    check(not ok and "不是本次的结果" in why, "早于本次运行开始（旧文件）-> 不算本次的结果", why)
    write(1_500.0, scenario="stale_statistics_eval_v1")
    ok, why = guard.suspend_verdict(path, stem, run_start, suspend_start)
    check(not ok and "没有本场景" in why, "结果里没有本场景 -> 作废", why)
    path.write_text("{not json", encoding="utf-8")
    os.utime(path, (1_500.0, 1_500.0))
    ok, _ = guard.suspend_verdict(path, stem, run_start, suspend_start)
    check(not ok, "写了一半的坏 JSON -> 作废")

print("[2] run_one 接线")
src = inspect.getsource(guard.run_one)
check(guard.POLL_S <= 10 and "time.sleep(POLL_S)" in src,
      f"轮询间隔 {guard.POLL_S}s：醒来后 run_suite 最多再跑这么久就被察觉（原来 60s）")
check("suspend_verdict(" in src and "_quarantine_suspended(" in src and "_kill_tree(proc)" in src,
      "检测到休眠：先杀进程，再按写入时刻判结果去留，作废的移入隔离区")
check(src.index("_kill_tree(proc)") < src.index("suspend_verdict("),
      "先杀进程再判定（判定之后不会再有写入）")
check("last_stall_check" in src, "卡死检查仍按分钟做，不随轮询变密")
qsrc = inspect.getsource(guard._quarantine_suspended)
check("st_mtime < run_start" in qsrc, "只移本次运行写出的文件")

print()
if fails:
    print(f"GUARD SUSPEND: FAIL（{len(fails)}/{checks}）")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print(f"GUARD SUSPEND: PASS（{checks} 项）")
