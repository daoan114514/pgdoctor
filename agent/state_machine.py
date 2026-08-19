"""MAPE-K 状态机 —— 架构里那个确定性的部分。

LLM 负责推理，状态机负责保证它不越界。这不是流程图上的装饰：
阶段推进是硬约束，而阶段决定了哪些工具可用。W4 起 PreToolUse hook
会按这张表拦截调用，于是"agent 在证据不足或未过门的情况下动生产库"
在结构上不可能发生，而不是靠提示词祈祷。

W3 阶段只走到 DIAGNOSE -> REPORT（只诊断不修复）。
PLAN/GATE/EXECUTE/VERIFY 已经定义好，等 W4 的安全门就位再接上。
"""
from __future__ import annotations

from enum import Enum


class Phase(str, Enum):
    MONITOR = "MONITOR"           # 抓当下快照
    OBSERVE = "OBSERVE"           # 只读收集证据
    HYPOTHESIZE = "HYPOTHESIZE"   # 生成候选根因
    INVESTIGATE = "INVESTIGATE"   # 逐条取证
    DIAGNOSE = "DIAGNOSE"         # 收敛根因（W4 起这里挂 ESC 硬闸）
    PLAN = "PLAN"                 # 修复方案 + 预测影响
    GATE = "GATE"                 # 护盾 + 分级安全门
    EXECUTE = "EXECUTE"           # 唯一写区
    VERIFY = "VERIFY"             # KPI + 回归套件
    ROLLBACK = "ROLLBACK"         # 系统阶段，非 LLM 阶段
    REPORT = "REPORT"
    ESCALATE = "ESCALATE"         # 升级人工：合法但扣分的出路
    DONE = "DONE"


READ_TOOLS = {
    "explain_query", "get_active_sessions", "get_top_queries",
    "get_blocking_chain", "get_table_stats", "get_indexes",
    "simulate_index", "fetch_raw",
}
REASON_TOOLS = {"note_evidence", "set_hypothesis", "declare_root_cause"}
WRITE_TOOLS = {"propose_remediation"}

# 每个阶段允许的工具。只读区与写区被状态机硬性隔开 ——
# INVESTIGATE 阶段调 propose_remediation 会被直接拒绝。
ALLOWED_TOOLS: dict[Phase, set[str]] = {
    Phase.MONITOR: READ_TOOLS | REASON_TOOLS,
    Phase.OBSERVE: READ_TOOLS | REASON_TOOLS,
    Phase.HYPOTHESIZE: READ_TOOLS | REASON_TOOLS,
    Phase.INVESTIGATE: READ_TOOLS | REASON_TOOLS,
    Phase.DIAGNOSE: READ_TOOLS | REASON_TOOLS,
    Phase.PLAN: READ_TOOLS | REASON_TOOLS,
    Phase.GATE: set(),
    Phase.EXECUTE: WRITE_TOOLS,
    Phase.VERIFY: READ_TOOLS,
    Phase.ROLLBACK: set(),
    Phase.REPORT: set(),
    Phase.ESCALATE: set(),
    Phase.DONE: set(),
}

# 合法转移。不在表里的转移一律拒绝 —— agent 不能自己跳到 EXECUTE。
TRANSITIONS: dict[Phase, set[Phase]] = {
    Phase.MONITOR: {Phase.OBSERVE, Phase.ESCALATE},
    Phase.OBSERVE: {Phase.HYPOTHESIZE, Phase.ESCALATE},
    Phase.HYPOTHESIZE: {Phase.INVESTIGATE, Phase.ESCALATE},
    Phase.INVESTIGATE: {Phase.DIAGNOSE, Phase.HYPOTHESIZE, Phase.ESCALATE},
    # DIAGNOSE 可退回 INVESTIGATE：ESC 判证据不足时走这条
    Phase.DIAGNOSE: {Phase.PLAN, Phase.REPORT, Phase.INVESTIGATE, Phase.ESCALATE},
    Phase.PLAN: {Phase.GATE, Phase.ESCALATE},
    Phase.GATE: {Phase.EXECUTE, Phase.PLAN, Phase.ESCALATE},
    Phase.EXECUTE: {Phase.VERIFY, Phase.ROLLBACK},
    # VERIFY 失败 -> ROLLBACK -> 换假设重来
    Phase.VERIFY: {Phase.REPORT, Phase.ROLLBACK},
    Phase.ROLLBACK: {Phase.HYPOTHESIZE, Phase.ESCALATE},
    Phase.REPORT: {Phase.DONE},
    Phase.ESCALATE: {Phase.DONE},
    Phase.DONE: set(),
}


class PhaseViolation(RuntimeError):
    """非法转移或阶段不允许的工具调用。"""


class StateMachine:
    def __init__(self, state, allow_repair: bool = False):
        self.state = state
        # W3 只诊断不修复。W4 打开这个开关后 DIAGNOSE 才能走向 PLAN。
        self.allow_repair = allow_repair
        self.history: list[tuple[str, str, str]] = []

    @property
    def phase(self) -> Phase:
        return Phase(self.state.phase)

    def tool_allowed(self, tool: str) -> bool:
        return tool in ALLOWED_TOOLS[self.phase]

    def assert_tool(self, tool: str) -> None:
        if not self.tool_allowed(tool):
            raise PhaseViolation(
                f"阶段 {self.phase.value} 不允许调用 {tool}；"
                f"该阶段可用: {sorted(ALLOWED_TOOLS[self.phase]) or '(无)'}")

    def goto(self, target: Phase, reason: str = "") -> None:
        cur = self.phase
        if target not in TRANSITIONS[cur]:
            raise PhaseViolation(f"非法转移 {cur.value} -> {target.value}")
        if target is Phase.PLAN and not self.allow_repair:
            raise PhaseViolation(
                "本次运行未开启修复（allow_repair=False），不能进入 PLAN")
        self.history.append((cur.value, target.value, reason))
        self.state.phase = target.value
        self.state.save()

    def terminal(self) -> bool:
        return self.phase in (Phase.DONE, Phase.REPORT, Phase.ESCALATE)
