"""W3 第一步：不用模型验证 harness。

先证明状态机、工具面、真相源三者接得对，再让 LLM 进场。
这样 LLM 出问题时能立刻分清是模型的问题还是管道的问题。

同时验证状态机的约束是真的硬约束，而不是摆设。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.episode_state import EpisodeState, Verdict
from agent.loop import run_diagnosis
from agent.policy import ScriptedPolicy
from agent.state_machine import Phase, PhaseViolation, StateMachine
from sandbox.env import DBAScenarioEnv

SCEN = "sandbox/scenarios/missing_index_orders_user_status_v1.yaml"
ok = True

# ── 1. 状态机的约束是不是硬的（纯单元，不碰数据库）──────────
print("=" * 68)
print("[1] 状态机约束（离线单元检查）")
st = EpisodeState(episode_id="unit_test", scenario_id="x")
sm = StateMachine(st, allow_repair=False)

try:
    sm.goto(Phase.EXECUTE)
    print("  FAIL  MONITOR 直接跳 EXECUTE 竟然被允许")
    ok = False
except PhaseViolation as e:
    print(f"  PASS  非法转移被拒: {str(e)[:52]}")

sm.goto(Phase.OBSERVE); sm.goto(Phase.HYPOTHESIZE); sm.goto(Phase.INVESTIGATE)
try:
    sm.assert_tool("propose_remediation")
    print("  FAIL  INVESTIGATE 阶段竟可调用写工具")
    ok = False
except PhaseViolation as e:
    print(f"  PASS  阶段外工具被拒: {str(e)[:52]}")

sm.goto(Phase.DIAGNOSE)
try:
    sm.goto(Phase.PLAN)
    print("  FAIL  allow_repair=False 时竟能进入 PLAN")
    ok = False
except PhaseViolation as e:
    print(f"  PASS  未开修复时进不了 PLAN: {str(e)[:44]}")

# 知识单调增长：修复失败过的根因不能被重新声明
st2 = EpisodeState(episode_id="unit_test2", scenario_id="x")
from agent.episode_state import RemediationAttempt
st2.record_attempt(RemediationAttempt(
    root_cause="missing_index", sql="CREATE INDEX ...",
    predicted={"p99_ms": 50}, actual={"p99_ms": 2000},
    verdict="FAILED_NO_IMPROVEMENT", rolled_back=True,
    inference="索引已生效但 p99 无改善"))
print(f"  PASS  失败尝试写入台账: missing_index = "
      f"{st2.ledger['missing_index'].verdict}")
ok &= (st2.ledger["missing_index"].verdict
       == Verdict.REFUTED_BY_REMEDIATION.value)
ok &= st2.already_failed("missing_index")

# 上下文重建
ctx = st2.render_context()
print(f"  PASS  上下文可从状态重建: {len(ctx)} 字符, "
      f"含已尝试记录={'已尝试过的修复' in ctx}")
ok &= "已尝试过的修复" in ctx

# ── 2. 端到端跑一个 episode（脚本化策略）───────────────────
print("\n[2] 端到端诊断（ScriptedPolicy，不调用模型）")
with DBAScenarioEnv(SCEN, warmup_s=15.0, degrade_timeout_s=90.0) as env:
    obs = env.reset()
    print(f"  告警触发={obs.fired} | p99 {obs.healthy_kpi['p99_ms']}ms "
          f"-> {obs.current_kpi['p99_ms']}ms")

    res, state = run_diagnosis(env, obs, ScriptedPolicy(), max_steps=40)

    print(f"\n  最终阶段: {res.final_phase}")
    print(f"  声明根因: {res.claimed_fault_class} — {res.claimed_root_cause}")
    print(f"  步数 {res.steps} | 用时 {res.elapsed_s}s")
    print(f"  工具调用: {res.tool_calls}")
    if res.error:
        print(f"  错误: {res.error}")
    if res.violations:
        print(f"  违规: {res.violations}")

    print("\n  假设台账:")
    for name, e in state.ledger.items():
        print(f"    {name:<20} {e.verdict:<12} {e.note[:46]}")

    score = env.score(res.claimed_fault_class)
    print(f"\n  判分: {score.summary()}")

    checks = [
        ("诊断命中 missing_index", res.claimed_fault_class == "missing_index"),
        ("终止在 REPORT", res.final_phase == "REPORT"),
        ("无阶段违规", not res.violations),
        ("无异常", not res.error),
        ("两个竞争假设被排除", len(state.refuted()) >= 2),
        ("Diagnosis 判分通过", score.diagnosis),
    ]
    print()
    for name, cond in checks:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok &= cond

print("=" * 68)
print("W3 HARNESS ACCEPTANCE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
