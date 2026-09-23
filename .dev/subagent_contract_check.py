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

print("[5] raw_refs 的几种写法都能解析成多条 ref（2026-09-23 冒烟：JSON 列表写法全被拒）")
two = ["trace://ep/step_019", "trace://ep/step_020"]
forms = {
    "分号分隔": "trace://ep/step_019; trace://ep/step_020",
    "JSON 列表字符串": '["trace://ep/step_019", "trace://ep/step_020"]',
    "逗号分隔": "trace://ep/step_019, trace://ep/step_020",
    "真列表": list(two),
}
for label, value in forms.items():
    rep, sink = _make_tool()
    r = asyncio.run(rep.handler(dict(base, raw_refs=value, collection_status="OBSERVED")))
    got = (sink.get("evidence_reports", {}).get("need_x") or {}).get("raw_refs")
    check(not r.get("is_error") and got == two, f"{label} -> {got}")
rep, sink = _make_tool()
r = asyncio.run(rep.handler(dict(base, raw_refs="trace://ep/step_019",
                                 limitations='["只看了一张表", "窗口 60s"]',
                                 collection_status="OBSERVED")))
got = (sink.get("evidence_reports", {}).get("need_x") or {}).get("limitations")
check(got == ["只看了一张表", "窗口 60s"], f"limitations 的 JSON 列表写法 -> {got}")
rep, sink = _make_tool()
r = asyncio.run(rep.handler(dict(base, raw_refs="trace://ep/step_019",
                                 limitations="a, b; c", collection_status="OBSERVED")))
got = (sink.get("evidence_reports", {}).get("need_x") or {}).get("limitations")
check(got == ["a, b", "c"], f"limitations 仍按分号切、逗号保留 -> {got}")

print("[6] 工具 schema 在 API 层就把列表与枚举定死（模型不必猜分隔符或同义词）")
rep, sink = _make_tool()
sch = rep.input_schema
check(sch is inv.REPORT_EVIDENCE_SCHEMA and sch.get("type") == "object",
      "report_evidence 用完整 JSON schema")
props = sch.get("properties", {})
for name in ("raw_refs", "observations", "limitations"):
    check(props.get(name, {}).get("type") == "array", f"{name} 声明为 array")
check(props.get("collection_status", {}).get("enum") == values,
      "collection_status 的 enum 就是 EvidenceStatus 的取值")
check(set(sch.get("required", [])) == set(props), "六个字段都是 required")
vprops = inv.REPORT_VERDICT_SCHEMA["properties"]
check(vprops["verdict"].get("enum") == ["CONFIRMED", "REFUTED", "INCONCLUSIVE"],
      "report_verdict 的 verdict 是 enum")
check(all(vprops[n].get("type") == "array" for n in ("incidental", "missing_evidence")),
      "report_verdict 的两个列表字段声明为 array")
rep, sink = _make_tool()
r = asyncio.run(rep.handler({"need_id": "need_x", "tool": "explain_query",
                             "raw_refs": list(two), "observations": [{"k": 1}, {"k": 2}],
                             "collection_status": "OBSERVED", "limitations": []}))
got = sink.get("evidence_reports", {}).get("need_x") or {}
check(not r.get("is_error") and got.get("raw_refs") == two and
      got.get("observations") == [{"k": 1}, {"k": 2}] and got.get("limitations") == [],
      "按 schema 传真数组时原样入账")

print()
if fails:
    print(f"SUBAGENT CONTRACT: FAIL（{len(fails)}/{checks}）")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print(f"SUBAGENT CONTRACT: PASS（{checks} 项）")
