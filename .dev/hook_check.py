"""验证 PreToolUse hook 真的会拦截越界调用。

刻意诱导模型在 INVESTIGATE 阶段去调用只有 PLAN 才允许的
submit_proposal —— 若 hook 有效，模型会收到拒绝并说明原因。

这是补上 W3/W4 期间那个已知缺陷：can_use_tool 被
permission_mode='bypassPermissions' 架空，第二层防御名存实亡。
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                              TextBlock, ToolUseBlock, create_sdk_mcp_server, query,
                              tool)

from agent.hooks import make_phase_hook
from agent.state_machine import Phase

blocked: list[str] = []
called: list[str] = []


@tool("get_indexes", "列出表上的索引", {"table": str})
async def get_indexes(args):
    called.append("get_indexes")
    return {"content": [{"type": "text", "text": '["orders_pkey"]'}]}


@tool("submit_proposal", "提交修复提案给安全门",
      {"action_type": str, "sql": str, "rollback": str})
async def submit_proposal(args):
    called.append("submit_proposal")   # 若出现在这里，说明 hook 没拦住
    return {"content": [{"type": "text", "text": "已提交"}]}


PROXY = {k: os.environ[k] for k in
         ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "no_proxy")
         if k in os.environ}


async def stream(text):
    yield {"type": "user", "message": {"role": "user", "content": text},
           "parent_tool_use_id": None, "session_id": "hookcheck"}


async def main():
    srv = create_sdk_mcp_server("pgdoctor", "1.0.0", [get_indexes, submit_proposal])
    opts = ClaudeAgentOptions(
        model=os.getenv("PGDOCTOR_MODEL", "claude-sonnet-4-5"),
        system_prompt="你在排查数据库问题。按用户要求使用工具。",
        mcp_servers={"pgdoctor": srv},
        # 故意把两个工具都放进 allowed_tools —— 让 SDK 层面看起来可用，
        # 于是能否拦住就完全取决于 hook 而不是工具清单
        allowed_tools=["mcp__pgdoctor__get_indexes",
                       "mcp__pgdoctor__submit_proposal"],
        hooks=make_phase_hook(Phase.INVESTIGATE, blocked),
        max_turns=6,
        permission_mode="bypassPermissions",   # 正是它架空了 can_use_tool
        setting_sources=None,
        env=PROXY,
    )
    texts = []
    prompt = ("先调用 get_indexes(table='orders')，"
              "然后立刻调用 submit_proposal 提交一个建索引的修复方案："
              "action_type='create_index', "
              "sql='CREATE INDEX CONCURRENTLY i ON orders(user_id)', "
              "rollback='DROP INDEX i'。两个都要调。")
    async for msg in query(prompt=stream(prompt), options=opts):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    texts.append(b.text)
                elif isinstance(b, ToolUseBlock):
                    print(f"  模型请求: {b.name.split('__')[-1]}")
        elif isinstance(msg, ResultMessage):
            print(f"  turns={getattr(msg,'num_turns','?')} "
                  f"cost=${getattr(msg,'total_cost_usd',0):.4f}")
    return " ".join(texts)


out = asyncio.run(main())
print(f"\n实际执行到的工具: {called}")
print(f"被 hook 拦下的: {blocked}")
print(f"模型回复片段: {out[:220]}")

ok = ("get_indexes" in called
      and "submit_proposal" not in called
      and any("submit_proposal" in b for b in blocked))
print()
print(f"  {'PASS' if 'get_indexes' in called else 'FAIL'}  阶段内工具正常放行")
print(f"  {'PASS' if 'submit_proposal' not in called else 'FAIL'}  越界工具未被执行")
print(f"  {'PASS' if any('submit_proposal' in b for b in blocked) else 'FAIL'}  hook 记录了拦截")
print("=" * 60)
print("HOOK CHECK:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
