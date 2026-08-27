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
    "get_connection_stats",
    # 按官方手册扩图后新增：xmin 视界的四个持有者、库级累计计数器
    "get_vacuum_horizon", "get_database_stats",
    "simulate_index", "fetch_raw",
}
REASON_TOOLS = {"note_evidence", "set_hypothesis", "declare_root_cause"}
# 提交提案不写库，只是把一个类型化对象交给安全门，所以它属于推理动作。
# agent 自始至终没有任何能改数据库的工具 —— 执行是系统阶段，由门用
# 它独占的 agent_rw 凭据完成。
PROPOSE_TOOLS = {"submit_proposal"}
# 子 agent 汇报裁决的唯一通道。忘了把它加进 INVESTIGATE 的允许集，
# 结果 PreToolUse hook 把子 agent 的输出口拦死了：它反复重试直到 turn
# 预算耗尽，三条假设全部返回 INCONCLUSIVE。教训是工具表与工具实现
# 必须一起改 —— 白名单漏掉自己的工具，症状看起来像模型不听话。
SUBAGENT_TOOLS = {"report_verdict"}

# 每个阶段允许的工具。只读区与写区被状态机硬性隔开 ——
# INVESTIGATE 阶段调 propose_remediation 会被直接拒绝。
ALLOWED_TOOLS: dict[Phase, set[str]] = {
    Phase.MONITOR: READ_TOOLS | REASON_TOOLS,
    Phase.OBSERVE: READ_TOOLS | REASON_TOOLS,
    Phase.HYPOTHESIZE: READ_TOOLS | REASON_TOOLS,
    Phase.INVESTIGATE: READ_TOOLS | REASON_TOOLS | SUBAGENT_TOOLS,
    Phase.DIAGNOSE: READ_TOOLS | REASON_TOOLS,
    Phase.PLAN: READ_TOOLS | REASON_TOOLS | PROPOSE_TOOLS,
    Phase.GATE: set(),        # 系统阶段：护盾 + 分级门，无 agent 工具
    Phase.EXECUTE: set(),     # 系统阶段：由门执行，agent 无写权限
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
