#!/usr/bin/env python3
"""SDK 级工具面必须是结构性的白名单（CLAUDE.md 硬规则 3，架构评审第 7 条）。

allowed_tools 只是免确认名单；bypassPermissions 下内建的 Bash/Read/Agent 仍在模型的
工具表里，全靠 PreToolUse hook 逐次拦（2026-09-22 实测拦了 108 次）。2026-09-23 实测
ClaudeAgentOptions(tools=["ToolSearch"]) 后模型自报的非 MCP 工具只剩 ToolSearch。
这里钉住：agent/ 里每一处 ClaudeAgentOptions 都显式给了 tools=，且只放 ToolSearch；
跑批探针给 tools=[]；agent/ 不直接调 sandbox.db（默认角色是 superuser）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.permissions import BUILTIN_ALLOW  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))


print("[1] agent/ 里的每一处 ClaudeAgentOptions 都结构性限定了内建工具")
opts_pat = re.compile(r"ClaudeAgentOptions\((.*?)\n\s*\)", re.S)
for p in sorted((ROOT / "agent").glob("*.py")):
    src = p.read_text(encoding="utf-8")
    for m in opts_pat.finditer(src):
        body = m.group(1)
        line = src[: m.start()].count("\n") + 1
        tm = re.search(r"tools=\[(.*?)\]", body)
        names = sorted(x.strip().strip("\"'") for x in tm.group(1).split(",") if x.strip()) if tm else None
        check(names is not None, f"{p.name}:{line} 显式给了 tools=")
        check(names == sorted(BUILTIN_ALLOW), f"{p.name}:{line} tools 与 permissions.BUILTIN_ALLOW 一致", names)
        check('permission_mode="bypassPermissions"' in body and "hooks=" in body,
              f"{p.name}:{line} 仍带 hook（第二道防线）")

print("[2] 跑批探针不带任何工具")
rsrc = (ROOT / "eval" / "run_suite.py").read_text(encoding="utf-8")
check("tools=[]," in rsrc, "run_suite 探针 tools=[]")

print("[3] agent/ 不直接碰 sandbox.db（默认角色是 superuser）")
hits = []
for p in sorted((ROOT / "agent").glob("*.py")):
    for i, l in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if re.search(r"\bdb\.(query|connect|execute)\(", l):
            hits.append(f"{p.name}:{i}")
check(not hits, "agent/ 里没有 db.query/connect/execute 调用", hits)

print()
if fails:
    print(f"SDK TOOL SURFACE: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"SDK TOOL SURFACE: PASS（{checks} 项）")
