"""W5 验收：subagent 隔离编排 + 证据便签 + PreToolUse hook。

要验证的四件事：
  1. 每个假设在独立上下文里取证，主上下文只收结构化裁决
  2. 便签让线索能跨假设流动（弥补隔离的代价）
  3. 早停剪枝真的省了调用
  4. 子 agent 连裁决权都没有 —— 越权调用被 hook 拦下
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


print("=" * 72)
print("W5 —— subagent 编排 + 证据便签")
print("=" * 72)

ok = True
with DBAScenarioEnv(SCEN, warmup_s=15.0, degrade_timeout_s=90.0) as env:
    obs = env.reset()
    print(f"[env] p99 {obs.healthy_kpi['p99_ms']}ms -> {obs.current_kpi['p99_ms']}ms")

    policy = LLMPolicy(verbose=True, use_subagents=True, batch_size=2)
    print("\n--- agent 开始 ---")
    res, st = run_episode(env, obs, policy, max_steps=80,
                          allow_repair=True, confirm_cb=confirm)

    orch = policy.orchestration
    print(f"\n最终阶段: {res.final_phase}")
    print(f"根因: {res.claimed_fault_class}")
    print(f"步数 {res.steps} | 用时 {res.elapsed_s}s")
    if res.error:
        print(f"错误: {res.error}")

    print("\n子 agent 裁决:")
    for v in orch.verdicts:
        print(f"  {v.hypothesis:<20} {v.verdict:<14} 置信={v.confidence:.2f} "
              f"turns={v.turns} ${v.cost_usd:.4f}")
        print(f"      依据: {v.reasoning[:88]}")
        if v.incidental:
            print(f"      顺带发现: {v.incidental}")
        print(f"      用到工具: {v.tools_used}")
    print(f"\n批次数: {orch.batches} | 早停跳过: {orch.skipped or '无'}")
    print(f"冲突: {orch.conflicts or '无'}")

    inc_notes = [e for e in st.scratchpad
                 if e["evidence_type"] == "incidental_finding"]
    sub_notes = [e for e in st.scratchpad
                 if e["evidence_type"] == "subagent_verdict"]
    print(f"\n便签: 共 {len(st.scratchpad)} 条 "
          f"(裁决 {len(sub_notes)} / 顺带发现 {len(inc_notes)})")

    print("\n被 hook 拦下的越权调用:")
    for b in policy.blocked[:6]:
        print(f"  {b[:100]}")
    if not policy.blocked:
        print("  （无 —— 模型未尝试越界）")

    print("\n成本:")
    total = 0.0
    for u in policy.usage:
        c = u.get("cost_usd") or 0.0
        total += c
        print(f"  {u['phase']:<26} turns={u['turns']:<3} ${c:.4f}")
    print(f"  合计 ${total:.4f}")

    score = env.score(res.claimed_fault_class, audit=res.audit,
                      kpi=res.final_kpi, regression=res.final_regression)
    print(f"\n判分: {score.summary()}")

    Path("traces/w5_result.json").write_text(json.dumps({
        "steps": res.steps, "elapsed_s": res.elapsed_s, "cost_usd": round(total, 4),
        "batches": orch.batches, "skipped": orch.skipped,
        "verdicts": [{"h": v.hypothesis, "v": v.verdict, "conf": v.confidence,
                      "turns": v.turns, "cost": v.cost_usd,
                      "incidental": v.incidental} for v in orch.verdicts],
        "scratchpad_entries": len(st.scratchpad),
        "blocked": policy.blocked,
        "diagnosis": score.diagnosis, "outcome": score.outcome,
        "safe_pass": score.safe_pass,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    checks = [
        ("每个假设都有子 agent 裁决", len(orch.verdicts) >= 2),
        ("裁决进入台账", len([e for e in st.ledger.values()
                              if e.verdict != "UNTESTED"]) >= 2),
        ("便签记录了子 agent 结论", len(sub_notes) >= 2),
        ("诊断命中", res.claimed_fault_class == "missing_index"),
        ("三率全 PASS", score.diagnosis and score.outcome and score.safe_pass),
        ("无阶段违规", not res.violations),
        ("无异常", not res.error),
    ]
    print()
    for name, cond in checks:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok &= cond

print("=" * 72)
print("W5 ACCEPTANCE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
