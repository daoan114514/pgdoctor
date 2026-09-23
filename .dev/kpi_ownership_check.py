#!/usr/bin/env python3
"""KPI 归属与负载进程组（架构评审第 10 条）：别人的负载写的 KPI 一律按过期处理。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sandbox import metrics  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))


print("[1] read_workload 校验归属")
orig = metrics.METRICS_PATH
tmp = Path(tempfile.mkdtemp()) / "workload_metrics.json"
metrics.METRICS_PATH = tmp
try:
    tmp.write_text(json.dumps({"ts": time.time(), "window_s": 30, "episode_id": "ep_A", "owner_pid": 1,
                               "by_query": {"hot": {"p99_ms": 5.0, "errors": 0}}}), encoding="utf-8")
    check(not metrics.read_workload(expected_episode_id="ep_A")["_stale"], "归属一致 -> 不过期")
    q = metrics.read_workload(expected_episode_id="ep_B")
    check(q["_stale"] and q.get("_foreign_owner") == "ep_A", "归属不一致 -> 过期并标出真正的 owner")
    check(not metrics.read_workload()["_stale"], "不校验归属时行为不变")
    kpi = metrics.collect(expected_episode_id="ep_B")
    check(getattr(kpi, "stale", False), "collect 传归属时 KPI.stale 为真（打分会拒信）")
finally:
    metrics.METRICS_PATH = orig

print("[2] 进程组与清扫")
esrc = (ROOT / "sandbox/env.py").read_text(encoding="utf-8")
check("start_new_session=True" in esrc and '"--episode-id"' in esrc, "负载在独立进程组里启动并带归属戳")
check("killpg" in esrc, "_stop_workload 整组杀")
check("metrics.collect(expected_episode_id=self.episode_id)" in esrc and "metrics.collect()" not in esrc,
      "env 的每次采集都校验归属")
wsrc = (ROOT / "sandbox/workload.py").read_text(encoding="utf-8")
check('"episode_id": _OWNER.get("episode_id", "")' in wsrc and '"owner_pid": os.getpid()' in wsrc, "快照带 episode_id / owner_pid")
rsrc = (ROOT / "eval/run_suite.py").read_text(encoding="utf-8")
check("_sweep_orphan_workloads()" in rsrc, "跑批开跑前清扫孤儿负载")
check('"harness": _harness_identity()' in rsrc and "complete=True" in rsrc, "结果带 harness 身份与 complete 标记")

print()
if fails:
    print(f"KPI OWNERSHIP: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"KPI OWNERSHIP: PASS（{checks} 项）")
