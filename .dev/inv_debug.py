"""最小用例：单个 investigator，把工具调用的真实入参与返回全打出来。

目的是分清三种可能：
  a) 模型压根没调 report_verdict
  b) 调了但 schema 校验挡住，handler 没跑到
  c) handler 跑了但 sink 没传出去
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                              TextBlock, ToolResultBlock, ToolUseBlock,
                              create_sdk_mcp_server, query, tool)

from agent.hooks import make_phase_hook
from agent.state_machine import Phase

sink: dict = {}
trace: list[str] = []


@tool("get_indexes", "列出表上的索引", {"table": str})
async def get_indexes(args):
    trace.append(f"get_indexes(args={args})")
    return {"content": [{"type": "text",
                         "text": '["orders_pkey","idx_orders_created_at"]'}]}


# 全部用 str，避免 float/list 之类的 schema 校验把调用挡在 handler 之前
@tool("report_verdict",
      "汇报调查结论。verdict 取 CONFIRMED / REFUTED / INCONCLUSIVE。",
      {"verdict": str, "confidence": str, "reasoning": str})
async def report_verdict(args):
    trace.append(f"report_verdict(args={args})")
    sink.update(args)
    return {"content": [{"type": "text", "text": "已记录"}]}


PROXY = {k: os.environ[k] for k in
         ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "no_proxy")
         if k in os.environ}


async def stream(t):
    yield {"type": "user", "message": {"role": "user", "content": t},
           "parent_tool_use_id": None, "session_id": "invdebug"}


async def main():
    blocked: list[str] = []
    srv = create_sdk_mcp_server("pgdoctor", "1.0.0",
                                [get_indexes, report_verdict])
    opts = ClaudeAgentOptions(
        model=os.getenv("PGDOCTOR_SUB_MODEL", "claude-haiku-4-5-20251001"),
        system_prompt=("你只调查一个假设。取证后必须调用 report_verdict 汇报，"
                       "这是你唯一的输出方式。工具很少，直接调用。"),
        mcp_servers={"pgdoctor": srv},
        allowed_tools=["mcp__pgdoctor__get_indexes",
                       "mcp__pgdoctor__report_verdict"],
        hooks=make_phase_hook(Phase.INVESTIGATE, blocked,
                              extra_denied={"set_hypothesis",
                                            "declare_root_cause",
                                            "submit_proposal"}),
        max_turns=16,
        permission_mode="bypassPermissions",
        setting_sources=None,
        env=PROXY,
    )
    prompt = ("假设：orders 表缺少 (user_id, status) 索引。\n"
              "先调用 get_indexes(table='orders') 看现有索引，"
              "然后调用 report_verdict 汇报你的裁决。")
    turns = 0
    async for msg in query(prompt=stream(prompt), options=opts):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    print(f"  [请求] {b.name.split('__')[-1]} "
                          f"{json.dumps(b.input, ensure_ascii=False)[:150]}")
                elif isinstance(b, TextBlock) and b.text.strip():
                    print(f"  [文本] {b.text.strip()[:120]}")
        elif hasattr(msg, "content") and not isinstance(msg, AssistantMessage):
            for b in (msg.content if isinstance(msg.content, list) else []):
                if isinstance(b, ToolResultBlock):
                    c = b.content
                    txt = (c[0].get("text", "") if isinstance(c, list) and c
                           and isinstance(c[0], dict) else str(c))
                    flag = " ERROR" if getattr(b, "is_error", False) else ""
                    print(f"  [结果{flag}] {txt[:150]}")
        elif isinstance(msg, ResultMessage):
            turns = getattr(msg, "num_turns", 0)
            print(f"  [结束] turns={turns} "
                  f"cost=${getattr(msg,'total_cost_usd',0):.4f} "
                  f"is_error={getattr(msg,'is_error',None)} "
                  f"subtype={getattr(msg,'subtype',None)}")
    return blocked


print("=" * 68)
blocked = asyncio.run(main())
print("=" * 68)
print("handler 实际被调到的:", trace or "（一次都没有）")
print("sink:", sink or "（空）")
print("hook 拦下的:", blocked or "（无）")
print()
if sink:
    print("结论：链路正常，问题在完整版的其他地方")
elif any("report_verdict" in t for t in trace):
    print("结论：handler 跑了但 sink 没传出 —— 闭包/作用域问题")
else:
    print("结论：handler 没跑到 —— 调用被 schema 校验或 hook 挡住了")
