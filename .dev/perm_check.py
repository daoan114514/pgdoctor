"""工具权限验收。

分两层：权限表本身对不对，以及 **hook 是否真的把它执行到每一次调用上**。
只测前者没有意义 —— 之前 can_use_tool 被 bypassPermissions 静默架空，
表一直是对的，防线却一次都没生效过。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.hooks import make_phase_hook
from agent.permissions import (INVESTIGATOR_DENIED, INVESTIGATOR_ONLY,
                               INVESTIGATOR_PHASES, SYSTEM_PHASES, Role,
                               allowed_tools, matrix)
from agent.state_machine import Phase
from agent.toolbox import Toolbox

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}   {detail}")
    if not cond:
        fails.append(name)


def guard_says(phase, role, tool, hypothesis=None, builtin=False):
    """真的走一遍 hook，看它放不放行。

    builtin=True 时按裸名传 —— 内建工具在真实调用里就是裸名。加上
    mcp__pgdoctor__ 前缀会走到自有工具那条分支去，结论看着对但理由是
    错的（第一版就这么写的，Bash 那条是碰巧过的）。
    """
    h = make_phase_hook(phase, None, role=role, hypothesis=hypothesis)
    fn = h["PreToolUse"][0].hooks[0]
    name = tool if builtin else f"mcp__pgdoctor__{tool}"
    out = asyncio.run(fn({"tool_name": name}, "t1", None))
    return out == {}          # 空字典 = 放行


print("[1] 系统阶段：没有 LLM，也就没有工具")
for p in SYSTEM_PHASES:
    check(f"{p.value} 主 agent 空集", not allowed_tools(p, Role.MAIN))
    check(f"{p.value} 子 agent 空集", not allowed_tools(p, Role.INVESTIGATOR))
check("SYSTEM 角色任何阶段都是空集",
      all(not allowed_tools(p, Role.SYSTEM) for p in Phase))

print("\n[2] 子 agent 的边界")
for p in Phase:
    got = allowed_tools(p, Role.INVESTIGATOR)
    if p in INVESTIGATOR_PHASES:
        check(f"{p.value} 有工具", bool(got), f"{len(got)} 个")
    else:
        check(f"{p.value} 不存在子 agent", not got, sorted(got))

leaked = {p.value: sorted(allowed_tools(p, Role.INVESTIGATOR)
                          & INVESTIGATOR_DENIED)
          for p in Phase if allowed_tools(p, Role.INVESTIGATOR)
          & INVESTIGATOR_DENIED}
check("子 agent 任何阶段都拿不到下结论/提案的工具", not leaked, leaked)

print("\n[3] 主 agent 的边界")
leaked = {p.value: sorted(allowed_tools(p, Role.MAIN) & INVESTIGATOR_ONLY)
          for p in Phase if allowed_tools(p, Role.MAIN) & INVESTIGATOR_ONLY}
check("主 agent 拿不到 report_verdict（它是收裁决的一方）", not leaked, leaked)

print("\n[4] 声明的工具都要有实现")
impl = {m for m in dir(Toolbox) if not m.startswith("_")} | set(INVESTIGATOR_ONLY)
ghost = set()
for p in Phase:
    for r in (Role.MAIN, Role.INVESTIGATOR):
        ghost |= allowed_tools(p, r) - impl
check("没有声明了却没实现的幽灵工具", not ghost, sorted(ghost))

print("\n[5] 按假设收窄")
narrow = allowed_tools(Phase.INVESTIGATE, Role.INVESTIGATOR, "lock_contention")
full = allowed_tools(Phase.INVESTIGATE, Role.INVESTIGATOR)
check("确实收窄了", 0 < len(narrow) < len(full), f"{len(narrow)} < {len(full)}")
check("收窄后仍保留 report_verdict",
      INVESTIGATOR_ONLY <= narrow,
      "漏了它子 agent 就无法回传，会重试到耗尽预算 —— 踩过这个坑")
check("锁竞争收窄到阻塞链/会话类工具",
      "get_blocking_chain" in narrow and "explain_query" not in narrow,
      sorted(narrow))
unknown = allowed_tools(Phase.INVESTIGATE, Role.INVESTIGATOR, "不存在的假设")
check("未知假设不塌成空集", bool(unknown), f"{len(unknown)} 个")

print("\n[6] hook 真的执行这张表（不是表对就算数）")
check("主 agent 在 PLAN 可提案",
      guard_says(Phase.PLAN, Role.MAIN, "submit_proposal"))
check("主 agent 在 INVESTIGATE 不可提案",
      not guard_says(Phase.INVESTIGATE, Role.MAIN, "submit_proposal"))
check("子 agent 不可声明根因",
      not guard_says(Phase.INVESTIGATE, Role.INVESTIGATOR,
                     "declare_root_cause"))
check("子 agent 可回传裁决",
      guard_says(Phase.INVESTIGATE, Role.INVESTIGATOR, "report_verdict"))
check("系统阶段任何工具都不放行",
      not guard_says(Phase.EXECUTE, Role.MAIN, "explain_query"))
check("内建工具默认拒绝（Bash）",
      not guard_says(Phase.OBSERVE, Role.MAIN, "Bash", builtin=True))
check("子 agent 也拒绝内建工具（实测它真的试过调 Bash）",
      not guard_says(Phase.INVESTIGATE, Role.INVESTIGATOR, "Bash",
                     builtin=True))
check("加载 schema 的那个放行（ToolSearch）",
      guard_says(Phase.OBSERVE, Role.MAIN, "ToolSearch", builtin=True))
check("收窄后不在集合里的工具被拦",
      not guard_says(Phase.INVESTIGATE, Role.INVESTIGATOR,
                     "explain_query", hypothesis="lock_contention"))

print("\n[7] 权限矩阵")
print("      " + "\n      ".join(
    f"{r['phase']:<13} 主 {len(r['main']):>2}  子 {len(r['investigator']):>2}"
    for r in matrix()))

print()
print("=" * 62)
print("PERMISSIONS: PASS" if not fails else f"PERMISSIONS: FAIL {fails}")
sys.exit(1 if fails else 0)
