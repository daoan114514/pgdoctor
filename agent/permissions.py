"""工具权限的单一权威：谁、在哪个阶段、能调哪些工具。

在这之前这套规则散在四个地方 —— state_machine 的阶段白名单、
investigator 的 SUB_DENIED、hooks 的内建工具默认拒绝、toolbox 内部的
阶段校验。散着的问题不是难看，是**没人能一眼说出"子 agent 在取证阶段
到底能调什么"**，而这正是安全评审第一个会问的问题。更早还吃过一次亏：
白名单漏掉 report_verdict，子 agent 的输出口被自己的 hook 拦死，症状
看起来完全像模型不听话。

现在只有这一个模块回答这个问题，hook、子 agent、工具层都从它取。

三个角色：

  MAIN          主 agent。按阶段拿工具，负责推理与下结论。
  INVESTIGATOR  取证子 agent。只在 INVESTIGATE 阶段存在，工具集由因果图
                按假设推导，且**永不具备下结论与提案的能力** —— 它的职责
                是把证据带回来，综合是主 agent 的事，而它只看过全局的一个
                切面，给它综合的能力它就会越权。
  SYSTEM        门 / 执行 / 回滚。没有 LLM，也就没有工具；写操作在这里
                由门用它独占的 agent_rw 凭据完成。
"""
from __future__ import annotations

from enum import Enum

from agent.state_machine import ALLOWED_TOOLS, Phase


class Role(str, Enum):
    MAIN = "main"
    INVESTIGATOR = "investigator"
    SYSTEM = "system"


# 子 agent 永远拿不到的三个：下裁决、声明根因、提交提案。
INVESTIGATOR_DENIED = frozenset(
    {"set_hypothesis", "declare_root_cause", "submit_proposal"})

# 只属于子 agent 的回传通道。主 agent 不该有 —— 它是收裁决的一方。
INVESTIGATOR_ONLY = frozenset({"report_verdict"})

# 子 agent 只在这一个阶段存在。
INVESTIGATOR_PHASES = frozenset({Phase.INVESTIGATE})

# 无 LLM 的系统阶段。它们的工具集为空不是"配置成空"，而是这些阶段
# 根本没有 agent 在跑；写操作由门执行。
SYSTEM_PHASES = frozenset({Phase.GATE, Phase.EXECUTE, Phase.ROLLBACK})

# 内建工具（Bash/Read/Write/WebFetch…）一律默认拒绝，只放行加载工具
# schema 必需的那个。用白名单而非黑名单：内建工具集会随 SDK 版本变化，
# 黑名单永远列不全，而漏掉一个是静默放行 —— 失败方向是错的。
BUILTIN_ALLOW = frozenset({"ToolSearch"})


def allowed_tools(phase: Phase, role: Role = Role.MAIN,
                  hypothesis: str | None = None) -> set[str]:
    """该角色在该阶段实际可调的工具集。这是唯一权威。"""
    if role is Role.SYSTEM:
        return set()

    base = set(ALLOWED_TOOLS.get(phase, set()))

    if role is Role.MAIN:
        # 主 agent 不收 report_verdict —— 它是收裁决的一方，不是汇报的一方
        return base - INVESTIGATOR_ONLY

    if role is Role.INVESTIGATOR:
        if phase not in INVESTIGATOR_PHASES:
            return set()
        tools = (base - INVESTIGATOR_DENIED) | set(INVESTIGATOR_ONLY)
        if hypothesis:
            # 按假设收窄到因果图推导出的取证工具。推导不出来（图上没有
            # 这个假设）时保持全集，而不是收成空集 —— 后者会让子 agent
            # 一个工具都没有，症状又变成"模型不干活"。
            from agent.investigator import toolset_for
            derived = set(toolset_for(hypothesis) or ())
            if derived:
                tools &= (derived | set(INVESTIGATOR_ONLY))
        return tools

    return set()


def matrix() -> list[dict]:
    """完整权限矩阵，给审计与文档用。"""
    out = []
    for p in Phase:
        row = {"phase": p.value,
               "system_phase": p in SYSTEM_PHASES,
               "main": sorted(allowed_tools(p, Role.MAIN)),
               "investigator": sorted(allowed_tools(p, Role.INVESTIGATOR))}
        out.append(row)
    return out


def render_matrix() -> str:
    lines = [f"{'阶段':<14} {'主 agent':>8} {'子 agent':>8}  说明"]
    for r in matrix():
        note = ""
        if r["system_phase"]:
            note = "系统阶段：无 LLM，写操作由门执行"
        elif not r["investigator"]:
            note = "子 agent 不在此阶段存在"
        lines.append(f"{r['phase']:<14} {len(r['main']):>8} "
                     f"{len(r['investigator']):>8}  {note}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(render_matrix())
    print()
    print("子 agent 在 INVESTIGATE 的完整工具集:")
    for t in sorted(allowed_tools(Phase.INVESTIGATE, Role.INVESTIGATOR)):
        print("   ", t)
    print()
    print("按假设收窄（lock_contention）:")
    for t in sorted(allowed_tools(Phase.INVESTIGATE, Role.INVESTIGATOR,
                                  "lock_contention")):
        print("   ", t)
