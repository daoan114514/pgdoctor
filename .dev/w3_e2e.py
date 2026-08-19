"""W3 端到端：agent 能否自主诊断出根因。

对活沙箱跑一个完整 episode。W3 只诊断不修复（allow_repair=False），
所以判据是 Diagnosis 那一率。
"""
import sys
from pathlib import Path

_here = Path(__file__).resolve()
_root = next(q for q in _here.parents if (q / 'sandbox').is_dir())
sys.path.insert(0, str(_root))

from agent.loop import run_diagnosis
from agent.policy import ScriptedPolicy
from sandbox.env import DBAScenarioEnv

SCEN = "sandbox/scenarios/missing_index_orders_user_status_v1.yaml"

print("=" * 70)
print("W3 端到端：ScriptedPolicy 自主诊断")
print("=" * 70)

with DBAScenarioEnv(SCEN, warmup_s=15.0, degrade_timeout_s=90.0) as env:
    obs = env.reset()
    print(f"[obs] 告警触发={obs.fired} | p99 {obs.healthy_kpi['p99_ms']}ms -> "
          f"{obs.current_kpi['p99_ms']}ms | cpu {obs.current_kpi['cpu_pct']}%")

    print("\n[loop] 阶段推进:")
    res, st = run_diagnosis(env, obs, ScriptedPolicy(), max_steps=40,
                            allow_repair=False)

    print(f"\n[result] 终止阶段={res.final_phase} 步数={res.steps} "
          f"耗时={res.elapsed_s}s")
    print(f"[result] 声明根因: {res.claimed_fault_class} — {res.claimed_root_cause}")
    print(f"[result] 工具调用序列: {res.tool_calls}")
    if res.violations:
        print(f"[result] 阶段违规: {res.violations}")
    if res.error:
        print(f"[result] 错误: {res.error}")

    print("\n[ledger] 假设台账:")
    for name, e in st.ledger.items():
        print(f"    {name:<20} {e.verdict:<12} {e.note[:60]}")

    print("\n[evidence] 便签条目:")
    for e in st.scratchpad:
        bo = ("->" + ",".join(e["bears_on"])) if e["bears_on"] else ""
        print(f"    [{e['evidence_type']}]{bo} {e['observation'][:82]}")

    score = env.score(res.claimed_fault_class)
    print(f"\n[score] {score.summary()}")
    print(f"        (W3 只诊断不修复，Outcome/SafePass 不适用)")

ok = True


def check(name, cond):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    ok = ok and bool(cond)


print("\n" + "=" * 70)
print("判据")
print("=" * 70)
check("告警确实触发", obs.fired)
check("诊断命中 missing_index", res.claimed_fault_class == "missing_index")
check("判分器认可诊断", score.diagnosis is True)
check("无阶段违规", not res.violations)
check("无运行错误", not res.error)
check("终止于 REPORT", res.final_phase == "REPORT")
check("排除了统计信息过期", st.ledger["stale_statistics"].verdict == "REFUTED")
check("排除了锁竞争", st.ledger["lock_contention"].verdict == "REFUTED")
check("做了反事实验证", "simulate_index" in res.tool_calls)
check("证据链非空（可供 ESC 核验）", len(st.scratchpad) >= 5)
check("步数在预算内", res.steps < 40)

print("=" * 70)
print("W3 E2E:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
