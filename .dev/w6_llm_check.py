"""W6 补充验收：ESC 会不会放行一个合格的真实诊断。

消融实验已经证明 ESC 能拦住偷懒策略。但如果它连正常诊断也拦，那就是
过度保守 —— 一个永远说"证据不足"的检查和一个永远说"够了"的检查
一样没用。这一步就是验这件事。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.llm_policy import LLMPolicy
from agent.loop import run_episode
from sandbox.env import DBAScenarioEnv

SCEN = "sandbox/scenarios/missing_index_orders_user_status_v1.yaml"


def confirm(p, d):
    print(f"    [确认通道] {d.tier} 档 -> 批准: {p.sql[:56]}")
    return True


print("=" * 74)
print("W6 LLM —— ESC 是否放行合格诊断")
print("=" * 74)

ok = True
with DBAScenarioEnv(SCEN, warmup_s=15.0, degrade_timeout_s=90.0) as env:
    obs = env.reset()
    print(f"[env] p99 {obs.healthy_kpi['p99_ms']}ms -> {obs.current_kpi['p99_ms']}ms")

    policy = LLMPolicy(verbose=True, use_subagents=True, batch_size=2)
    print("\n--- agent 开始 ---")
    res, st = run_episode(env, obs, policy, max_steps=80,
                          allow_repair=True, confirm_cb=confirm, use_esc=True)

    print(f"\n最终阶段: {res.final_phase}")
    print(f"根因: {res.claimed_fault_class}")
    print(f"步数 {res.steps} | 用时 {res.elapsed_s}s | ESC 退回次数 {st.esc_retries}")
    if res.error:
        print(f"错误: {res.error}")

    print("\nESC 裁决历史:")
    for i, r in enumerate(res.esc_reports, 1):
        print(f"  第{i}次: {r.summary()}")
        for d in r.dims:
            print(f"      {d.name} {'PASS' if d.passed else 'FAIL'}"
                  f"{'(必需)' if d.mandatory else '      '} {d.detail}")
        if r.directives:
            for dv in r.directives[:3]:
                print(f"      补证: {dv}")

    total = sum((u.get("cost_usd") or 0.0) for u in policy.usage)
    print(f"\n成本合计 ${total:.4f}")

    score = env.score(res.claimed_fault_class, audit=res.audit,
                      kpi=res.final_kpi, regression=res.final_regression)
    print(f"判分: {score.summary()}")

    Path("traces/w6_llm_result.json").write_text(json.dumps({
        "final_phase": res.final_phase, "steps": res.steps,
        "esc_retries": st.esc_retries, "cost_usd": round(total, 4),
        "esc_verdicts": [r.verdict for r in res.esc_reports],
        "applied_sql": res.applied_sql,
        "diagnosis": score.diagnosis, "outcome": score.outcome,
        "safe_pass": score.safe_pass,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    final_esc = res.esc_reports[-1].verdict if res.esc_reports else None
    checks = [
        ("ESC 最终放行", final_esc == "SUFFICIENT"),
        ("退回次数不过多（未过度保守）", st.esc_retries <= 1),
        ("实际执行了修复", len(res.applied_sql) >= 1),
        ("终止在 REPORT", res.final_phase == "REPORT"),
        ("三率全 PASS", score.diagnosis and score.outcome and score.safe_pass),
        ("无阶段违规", not res.violations),
    ]
    print()
    for name, cond in checks:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok &= cond

print("=" * 74)
print("W6 LLM ACCEPTANCE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
