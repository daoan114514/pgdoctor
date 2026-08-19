"""PreToolUse hook —— 纵深防御的第二层。

为什么必须是 hook 而不是 can_use_tool：SDK 明确警告
permission_mode='bypassPermissions' 会在 can_use_tool 回调之前自动批准
所有工具调用，所以那个回调根本不会被触发。要拦每一次调用只能用
PreToolUse hook。

两层的分工：
  Toolbox 内的状态机校验   第一层，工具执行前抛异常
  PreToolUse hook          第二层，模型的请求根本发不出去

两层都不依赖提示词。模型即使被诱导、幻觉、或提示注入，也调不动
越界工具 —— 这是结构保证而非约定。
"""
from __future__ import annotations

from claude_agent_sdk import HookMatcher

from agent.state_machine import ALLOWED_TOOLS, Phase

SERVER = "pgdoctor"
# 加载工具 schema 需要它；其余内建工具（Bash/Read/Write…）一律拒绝
BUILTIN_ALLOW = {"ToolSearch"}


def _bare(name: str) -> str:
    return name.split("__")[-1]


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _allow() -> dict:
    return {}


def make_phase_hook(phase: Phase, blocked_log: list | None = None,
                    extra_denied: set[str] | None = None) -> dict:
    """按阶段拦截越界工具调用。

    extra_denied 用于 subagent：调查子 agent 只允许取证，连
    set_hypothesis / declare_root_cause / submit_proposal 都不给 ——
    它的职责是把证据带回来，裁决由主 agent 汇总时做。
    """
    allowed = set(ALLOWED_TOOLS[phase])
    denied = set(extra_denied or ())

    async def guard(input_data, tool_use_id, context):
        name = input_data.get("tool_name", "") if isinstance(input_data, dict) \
            else getattr(input_data, "tool_name", "")
        bare = _bare(name)

        # 内建工具默认拒绝，只放行加载工具 schema 所必需的那个。
        # 上一轮实测里子 agent 调用了 Bash —— 只守 mcp__pgdoctor__* 是不够的，
        # 内建工具集会随 SDK 版本变化，白名单比黑名单可靠。
        if not name.startswith(f"mcp__{SERVER}__"):
            if bare in BUILTIN_ALLOW:
                return _allow()
            msg = f"不允许调用内建工具 {bare}"
            if blocked_log is not None:
                blocked_log.append(f"{bare}: {msg}")
            return _deny(msg)

        if bare in denied:
            msg = f"该子 agent 只负责取证，不允许调用 {bare}"
            if blocked_log is not None:
                blocked_log.append(f"{bare}: {msg}")
            return _deny(msg)

        if bare not in allowed:
            msg = (f"阶段 {phase.value} 不允许调用 {bare}；"
                   f"可用: {sorted(allowed) or '(无)'}")
            if blocked_log is not None:
                blocked_log.append(f"{bare}: {msg}")
            return _deny(msg)

        return _allow()

    # hooks 参数是 {事件名: [HookMatcher]} 字典，不是裸列表
    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]}
