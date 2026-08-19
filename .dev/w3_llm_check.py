"""W3 第二步：让模型真正跑一个 episode，并与脚本化基线对照。

只跑一个 episode —— Pro 额度有限，而这一步要证明的是"模型能在
同一套 harness 里完成诊断"，不是统计意义上的强弱。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.llm_policy import LLMPolicy
from agent.loop import run_diagnosis
from sandbox.env import DBAScenarioEnv

SCEN = "sandbox/scenarios/missing_index_orders_user_status_v1.yaml"

print("=" * 70)
print("W3 LLM EPISODE")
print("=" * 70)

with DBAScenarioEnv(SCEN, warmup_s=15.0, degrade_timeout_s=90.0) as env:
    obs = env.reset()
    print(f"[env] 告警触发={obs.fired} | p99 {obs.healthy_kpi['p99_ms']}ms "
          f"-> {obs.current_kpi['p99_ms']}ms cpu={obs.current_kpi['cpu_pct']}%")

    policy = LLMPolicy(verbose=True)
    print("\n--- agent 开始诊断 ---")
    res, state = run_diagnosis(env, obs, policy, max_steps=45)

    print(f"\n最终阶段: {res.final_phase}")
    print(f"声明根因: {res.claimed_fault_class} — {res.claimed_root_cause}")
    print(f"步数 {res.steps} | 用时 {res.elapsed_s}s")
    if res.error:
        print(f"错误: {res.error}")
    if res.violations:
        print(f"阶段违规: {res.violations}")

    print("\n假设台账:")
    for name, e in state.ledger.items():
        print(f"  {name:<20} {e.verdict:<14} {e.note[:52]}")

    print("\n证据便签（agent 实际取到的证据）:")
    for e in state.scratchpad[-10:]:
        print(f"  [{e['evidence_type']:<22}] {e['observation'][:78]}")

    print("\n模型用量:")
    total = 0.0
    for u in policy.usage:
        c = u.get("cost_usd") or 0.0
        total += c
        print(f"  {u['phase']:<14} turns={u['turns']} cost=${c:.4f}")
    print(f"  合计 ${total:.4f}")

    score = env.score(res.claimed_fault_class)
    print(f"\n判分: {score.summary()}")

    out = {
        "policy": "llm",
        "fault_class": res.claimed_fault_class,
        "steps": res.steps,
        "tool_calls": res.tool_calls,
        "elapsed_s": res.elapsed_s,
        "cost_usd": round(total, 4),
        "diagnosis": score.diagnosis,
        "refuted": state.refuted(),
        "violations": res.violations,
        "error": res.error,
    }
    Path("traces/w3_llm_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    checks = [
        ("诊断命中 missing_index", res.claimed_fault_class == "missing_index"),
        ("终止在 REPORT", res.final_phase == "REPORT"),
        ("无阶段违规", not res.violations),
        ("无异常", not res.error),
        ("至少排除一个竞争假设", len(state.refuted()) >= 1),
    ]
    print()
    ok = True
    for name, cond in checks:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok &= cond

print("=" * 70)
print("W3 LLM ACCEPTANCE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
