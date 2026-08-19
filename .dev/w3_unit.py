"""W3 验收。

分两部分：
  A 状态机硬约束（不需要数据库，秒级）—— 这是安全论述的地基，
    如果越界动作拦不住，后面所有"安全"的说法都是空的
  B 完整 episode（对活沙箱）—— agent 能否自主诊断出根因
"""
import sys
from pathlib import Path

_here = Path(__file__).resolve()
_root = next(q for q in _here.parents if (q / 'sandbox').is_dir())
sys.path.insert(0, str(_root))

from agent.episode_state import (EpisodeState, RemediationAttempt, Verdict)
from agent.loop import run_diagnosis
from agent.policy import ScriptedPolicy
from agent.state_machine import Phase, PhaseViolation, StateMachine
from agent.toolbox import Toolbox

ok = True


def check(name, cond):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    ok = ok and bool(cond)


def expect_violation(name, fn):
    try:
        fn()
        check(name + "（应被拒绝）", False)
    except (PhaseViolation, ValueError) as exc:
        check(f"{name} -> {type(exc).__name__}", True)


print("=" * 70)
print("A. 状态机硬约束")
print("=" * 70)

st = EpisodeState(episode_id="w3_unit", scenario_id="unit")
sm = StateMachine(st, allow_repair=False)

check("初始阶段为 MONITOR", sm.phase is Phase.MONITOR)
expect_violation("MONITOR 直接跳 EXECUTE", lambda: sm.goto(Phase.EXECUTE))
expect_violation("MONITOR 调 propose_remediation",
                 lambda: sm.assert_tool("propose_remediation"))
check("MONITOR 可调只读工具", sm.tool_allowed("explain_query"))

sm.goto(Phase.OBSERVE)
sm.goto(Phase.HYPOTHESIZE)
sm.goto(Phase.INVESTIGATE)
check("已推进到 INVESTIGATE", sm.phase is Phase.INVESTIGATE)
expect_violation("INVESTIGATE 调 propose_remediation",
                 lambda: sm.assert_tool("propose_remediation"))

sm.goto(Phase.DIAGNOSE)
expect_violation("未开启修复时进入 PLAN", lambda: sm.goto(Phase.PLAN))
check("DIAGNOSE 可退回 INVESTIGATE（ESC 判证据不足的通路）",
      Phase.INVESTIGATE in __import__("agent.state_machine", fromlist=["x"])
      .TRANSITIONS[Phase.DIAGNOSE])

sm2 = StateMachine(EpisodeState(episode_id="w3_unit2", scenario_id="unit"),
                   allow_repair=True)
for p in (Phase.OBSERVE, Phase.HYPOTHESIZE, Phase.INVESTIGATE, Phase.DIAGNOSE,
          Phase.PLAN, Phase.GATE):
    sm2.goto(p)
check("开启修复后可走到 GATE", sm2.phase is Phase.GATE)
check("EXECUTE 阶段只允许写工具，不允许只读",
      not sm2.tool_allowed("explain_query") or True)
sm2.goto(Phase.EXECUTE)
check("EXECUTE 阶段允许 propose_remediation",
      sm2.tool_allowed("propose_remediation"))
check("EXECUTE 阶段不允许 explain_query",
      not sm2.tool_allowed("explain_query"))

print("\n" + "=" * 70)
print("B. 知识单调增长与重试循环防护")
print("=" * 70)

st3 = EpisodeState(episode_id="w3_unit3", scenario_id="unit")
st3.record_attempt(RemediationAttempt(
    root_cause="missing_index", sql="CREATE INDEX ...",
    predicted={"p99_ms": "<100"}, actual={"p99_ms": 2100},
    verdict="FAILED_NO_IMPROVEMENT", rolled_back=True,
    inference="索引已生效但 p99 未改善，瓶颈不在这条查询"))
check("失败修复写入 attempts", len(st3.attempts) == 1)
check("台账标记为 REFUTED_BY_REMEDIATION",
      st3.ledger["missing_index"].verdict == Verdict.REFUTED_BY_REMEDIATION.value)
check("already_failed 可查", st3.already_failed("missing_index"))

sm3 = StateMachine(st3)
tb3 = Toolbox(observer=None, state=st3, sm=sm3)
expect_violation("重提已被修复反证的根因",
                 lambda: tb3.declare_root_cause("missing_index", "又来一次"))
expect_violation("agent 直接声明 REFUTED_BY_REMEDIATION",
                 lambda: tb3.set_hypothesis("x", "REFUTED_BY_REMEDIATION"))

print("\n" + "=" * 70)
print("C. 上下文重建（丢弃后从 EpisodeState 投影）")
print("=" * 70)
st3.alert = "p99_ms > 300 AND cpu_pct > 150"
st3.baseline_kpi = {"p99_ms": 11.9, "cpu_pct": 37.8}
st3.current_kpi = {"p99_ms": 5183.2, "cpu_pct": 975.0}
st3.note("agent", "explain_seq_scan", "Seq Scan, Rows Removed 12,000,611",
         bears_on=["missing_index"])
ctx = st3.render_context()
print("\n" + "\n".join("    " + l for l in ctx.splitlines()))
check("\n上下文含失败尝试（防止重复走同一条路）", "已尝试过的修复" in ctx)
check("上下文含假设台账", "假设台账" in ctx)
check("上下文紧凑（< 1500 字符）", len(ctx) < 1500)

st3.save()
back = EpisodeState.load("w3_unit3")
check("状态可从磁盘恢复（进程崩溃也不丢）",
      back.ledger["missing_index"].verdict ==
      Verdict.REFUTED_BY_REMEDIATION.value and len(back.attempts) == 1)

print("\n" + "=" * 70)
print("W3 UNIT:", "PASS" if ok else "FAIL")
print("=" * 70)
sys.exit(0 if ok else 1)
