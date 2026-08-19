"""最小 SDK 冒烟：验证认证、代理透传、以及自定义工具能被调用。
故意只问一个极简问题，把额度消耗压到最低。"""
import asyncio
import os
import sys

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                              TextBlock, ToolUseBlock, create_sdk_mcp_server,
                              query, tool)

PROXY = {k: os.environ[k] for k in
         ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "no_proxy")
         if k in os.environ}
print("传给 CLI 的代理变量:", list(PROXY) or "(无)")

calls = []


@tool("ping_db", "返回一个固定的假数据库指标，用于验证工具链路", {"what": str})
async def ping_db(args):
    calls.append(args)
    return {"content": [{"type": "text",
                         "text": '{"p99_ms": 1234, "scan": "Seq Scan"}'}]}


async def main():
    srv = create_sdk_mcp_server("smoke", "1.0.0", [ping_db])
    opts = ClaudeAgentOptions(
        model=os.getenv("PGDOCTOR_MODEL", "claude-sonnet-4-5"),
        system_prompt="你是测试助手。必须调用工具，不要凭空回答。",
        mcp_servers={"smoke": srv},
        allowed_tools=["mcp__smoke__ping_db"],
        max_turns=4,
        permission_mode="bypassPermissions",
        setting_sources=None,
        env=PROXY,
    )
    text = []
    async for msg in query(
            prompt="调用 ping_db(what='latency')，然后只回复它返回的 p99_ms 数值。",
            options=opts):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    text.append(b.text)
                elif isinstance(b, ToolUseBlock):
                    print("  模型调用工具:", b.name, b.input)
        elif isinstance(msg, ResultMessage):
            print("  turns:", getattr(msg, "num_turns", "?"),
                  "| cost_usd:", getattr(msg, "total_cost_usd", "?"))
            u = getattr(msg, "usage", None)
            if u:
                print("  usage:", u)
    out = " ".join(text).strip()
    print("  模型回复:", out[:200])
    return out, calls


out, calls = asyncio.run(main())
ok = bool(calls) and "1234" in out
print()
print("工具被真实调用:", bool(calls))
print("回复含正确数值:", "1234" in out)
print("SDK SMOKE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
