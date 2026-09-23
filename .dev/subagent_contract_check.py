#!/usr/bin/env python3
"""子 agent 汇报约定检查：EvidenceStatus 的每个取值必须出现在子 agent 看得到的三处。

2026-09-22 实测：三处都没写取值，子 agent 猜了 45 种写法，655 次汇报 635 次被
`invalid collection status` 拒掉，12 轮预算全花在重试上 —— 取证子 agent 80% 失败、
每次 140 秒、每个 episode 折合 $6，根因就是这个。校验在 explanation.py，提示在
investigator.py，两边各自演化就会再漂开；这里把它钉住。
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import investigator as inv  # noqa: E402
from agent.episode_state import EvidenceStatus  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str) -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}")


values = [s.value for s in EvidenceStatus]
print(f"[1] EvidenceStatus 取值: {values}")

print("[2] 三处入口都列出了全部取值")
src = inspect.getsource(inv.investigate_task)
for v in values:
    check(v in inv.SUB_SYSTEM_V2, f"system prompt 含 {v}")
    check(v in inv.COLLECTION_STATUS_HELP, f"工具说明/任务 prompt 共用的 HELP 含 {v}")
check("COLLECTION_STATUS_HELP" in src, "investigate_task 的任务 prompt 引用了 HELP")

print("[3] 取值列表不是手抄的：HELP 由 EvidenceStatus 生成")
helpsrc = inspect.getsource(inv)
i = helpsrc.find("COLLECTION_STATUS_HELP = (")
check(i >= 0 and "for status in EvidenceStatus" in helpsrc[i:i + 400],
      "HELP 用 EvidenceStatus 迭代生成")

print("[4] report_evidence 工具的实际行为")


class _Sink(dict):
    pass


def _make_tool():
    # _tools_for 需要一个 toolbox；这里只要 report_evidence，传最小桩即可
    class _TB:
        def __getattr__(self, name):
            raise AssertionError(f"不该调用 toolbox.{name}")
    sink: dict = {}
    tools = inv._tools_for(_TB(), sink, include_evidence_refs=False, call_cache={})
    rep = [t for t in tools if getattr(t, "name", "") == "report_evidence"][0]
    return rep, sink


rep, sink = _make_tool()
base = {"need_id": "need_x", "tool": "explain_query",
        "raw_refs": "trace://ep/step_1", "observations": "[{\"k\": 1}]",
        "limitations": ""}


def call(status):
    args = dict(base, collection_status=status)
    return asyncio.run(rep.handler(args))


r = call("OBSERVED")
check(not r.get("is_error") and sink.get("evidence_reports", {}).get("need_x"),
      "OBSERVED 被接受并写入 sink")
r = call("observed")
check(not r.get("is_error"), "小写 observed 归一化后被接受")
r = call("COMPLETE")
txt = r["content"][0]["text"]
check(bool(r.get("is_error")) and "invalid collection status" in txt,
      "COMPLETE 仍被拒（不做同义词映射，只告诉它合法值）")
r = call("UNKNOWN")
check(not r.get("is_error"), "UNKNOWN 被接受")

print()
if fails:
    print(f"SUBAGENT CONTRACT: FAIL（{len(fails)}/{checks}）")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print(f"SUBAGENT CONTRACT: PASS（{checks} 项）")
