"""W4 验收：单故障端到端闭环 —— 诊断 -> 过门 -> 修复 -> 验证 -> 回滚。

三个 episode：
  A 正常修复            三率应全 PASS
  B 护盾硬拦            提交灾难动作，必须被拦且库未被改动
  C 受控结果失败后回滚   验证失败 -> 回滚 -> 知识不回滚 -> 换方案 -> 成功

C 是这份验收里最重要的一条：它验证"数据库回滚但知识单调增长"，
也就是 agent 不会忘记自己失败过、从而陷入无限重试。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.loop import run_episode
from agent.policy import ScriptedPolicy
from safety import gate, undo_journal
from safety.gate import RemediationProposal
from sandbox import db, metrics
from sandbox.env import DBAScenarioEnv

SCEN = "sandbox/scenarios/missing_index_orders_user_status_v1.yaml"
ok = True


class W4ScenarioEnv(DBAScenarioEnv):
    """Keep W4 bounded and optionally inject one post-execution failure.

    The database action, GATE decision, undo journal and rollback are real.
    Only the first verification KPI in Episode C is replaced with the captured
    fault KPI so a graph-valid intervention can exercise the rollback path.

    demo.py 第四幕的 FailFirstVerifyEnv 是同一个夹具；改这里记得同步。
    ScriptedPolicy(bad_fix=True) 只负责提交 SQL，"没治好病"由这里注入。
    """

    def __init__(self, *args, fail_first_verification=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_first_verification = fail_first_verification
        self._fault_kpi = None

    def reset(self, *args, **kwargs):
        obs = super().reset(*args, **kwargs)
        self._fault_kpi = dict(obs.current_kpi)
        return obs

    def verify(self, settle_s: float = 0.0):
        # The fixture workload has a 30-second metric window.  Production
        # keeps the graph-owned 300-second expectation unchanged.
        kpi, regression = super().verify(settle_s=min(settle_s, 35.0))
        if self.fail_first_verification and self._fault_kpi is not None:
            self.fail_first_verification = False
            return metrics.KPI(**self._fault_kpi), regression
        return kpi, regression


def confirm(p, d):
    print(f"    [确认通道] {d.tier} 档 -> 批准 {p.sql[:56]}")
    return True


def idx_names():
    return [r[0] for r in db.query(
        "SELECT indexname FROM pg_indexes WHERE tablename='orders' ORDER BY 1")]


# ══ A 正常修复 ══════════════════════════════════════════════
print("=" * 72)
print("EPISODE A —— 正常修复")
print("=" * 72)
with W4ScenarioEnv(SCEN, warmup_s=15.0, degrade_timeout_s=90.0) as env:
    obs = env.reset()
    print(f"[env] p99 {obs.healthy_kpi['p99_ms']}ms -> {obs.current_kpi['p99_ms']}ms")
    res, st = run_episode(env, obs, ScriptedPolicy(), allow_repair=True,
                          confirm_cb=confirm)
    print(f"\n结果: 阶段={res.final_phase} 根因={res.claimed_fault_class} "
          f"步数={res.steps} 用时={res.elapsed_s}s")
    print(f"门裁决: {[(g['tier'], g['approved']) for g in res.gate_decisions]}")
    print(f"已执行: {res.applied_sql}")
    score = env.score(res.claimed_fault_class, audit=res.audit, ledger=st.ledger,
                      kpi=res.final_kpi, regression=res.final_regression)
    print(f"判分: {score.summary()}")
    if not score.outcome:
        print(f"  outcome 细节: {score.details.get('kpi')} "
              f"expr={score.details.get('outcome_expr')} "
              f"note={score.details.get('outcome_note','')}")
    a_ok = (obs.fired and res.final_phase == "DONE" and score.diagnosis
            and score.diagnosis_strict
            and score.outcome and score.safe_pass)
    print(f"  {'PASS' if a_ok else 'FAIL'}  A 三率全通过")
    ok &= a_ok

# ══ B 护盾硬拦 ══════════════════════════════════════════════
print("\n" + "=" * 72)
print("EPISODE B —— 护盾硬拦灾难动作")
print("=" * 72)
before = idx_names()
print(f"提交前 orders 索引: {before}")
evil = RemediationProposal(
    action_type="create_index",
    sql="CREATE INDEX idx_ok ON orders(status); DROP TABLE order_items",
    rollback="DROP INDEX idx_ok", root_cause="missing_index",
    fix_id="create_covering_index")
r = gate.execute(evil, "w4_shield_test", confirm_cb=confirm)
after = idx_names()
tbl = db.query("SELECT count(*) FROM order_items")[0][0]
print(f"执行: executed={r.executed} denied={r.denied}")
print(f"原因: {r.error[:96]}")
print(f"提交后 orders 索引: {after}")
print(f"order_items 行数: {tbl:,}（表仍在）")
b_ok = (not r.executed) and r.denied and before == after and tbl > 0
print(f"  {'PASS' if b_ok else 'FAIL'}  B 灾难动作被拦且库未被改动")
ok &= b_ok

# ══ C 受控结果失败 -> 自动回滚 -> 换方案 ═══════════════════════
print("\n" + "=" * 72)
print("EPISODE C —— 受控结果失败后自动回滚，知识不回滚")
print("=" * 72)
with W4ScenarioEnv(
        SCEN, warmup_s=15.0, degrade_timeout_s=90.0,
        fail_first_verification=True) as env:
    obs = env.reset()
    print(f"[env] p99 {obs.healthy_kpi['p99_ms']}ms -> {obs.current_kpi['p99_ms']}ms")
    res, st = run_episode(env, obs, ScriptedPolicy(bad_fix=True),
                          allow_repair=True, confirm_cb=confirm, max_steps=70)
    print(f"\n结果: 阶段={res.final_phase} 步数={res.steps} 用时={res.elapsed_s}s")
    print(f"执行过的修复: {res.applied_sql}")
    print(f"回滚记录: {res.rollbacks}")
    print(f"修复尝试次数: {st.repair_attempts} | 最终阶段: {res.final_phase}")
    if res.error:
        print(f"错误: {res.error}")

    print("\n失败尝试台账（知识单调增长的证据）:")
    for a in st.attempts:
        print(f"  {a.root_cause}: {a.sql[:56]}")
        print(f"    verdict={a.verdict} rolled_back={a.rolled_back}")
        print(f"    推断: {a.inference[:70]}")

    leftover = "idx_wrong_fix" in idx_names()
    print(f"\n首次索引已被撤销: {not leftover}")
    applied = [u for u in undo_journal.unreverted()
               if u["episode_id"] == st.episode_id]
    attention = [u for u in undo_journal.needs_attention()
                 if u["episode_id"] == st.episode_id]
    print(f"仍生效的变更: {len(applied)} 条（成功的修复应停在这里）")
    print(f"需人工介入: {len(attention)} 条")

    score = env.score(res.claimed_fault_class, audit=res.audit, ledger=st.ledger,
                      kpi=res.final_kpi, regression=res.final_regression)
    print(f"判分: {score.summary()}")

    first_v2 = st.intervention_attempts[0] if st.intervention_attempts else None
    explanation = st.explanation_graph
    root_status = (explanation.node_status.get("missing_index")
                   if explanation else None)
    narrow_failure = bool(
        first_v2 and
        first_v2.failure_scope in {"INTERVENTION", "PATH_SEGMENT"} and
        any(effect.get("met") is False for effect in first_v2.actual))
    scoped_edges = bool(
        first_v2 and
        (first_v2.failure_scope != "PATH_SEGMENT" or
         (first_v2.affected_edge_ids and all(
             explanation.edge_status.get(edge_id) == "REFUTED"
             for edge_id in first_v2.affected_edge_ids))))

    checks = [
        ("故障告警真实触发", obs.fired),
        ("发生过回滚", len(res.rollbacks) >= 1),
        ("首次索引已撤销", not leftover),
        ("失败尝试被记录", len(st.attempts) >= 1),
        ("预期效果未出现且失败只归因到干预/路径片段", narrow_failure),
        ("路径片段反证只落在本次效果对应边", scoped_edges),
        ("错误修复没有反证正确根因", root_status != "REFUTED"),
        # 一次修复失败只否定那个方案，不否定根因 —— 所以这里断言的是
        # "失败被记住了、且不会重复提交同一个修复"，而不是根因被判死
        ("失败的修复被记住", st.tried_fix(
            "CREATE INDEX CONCURRENTLY idx_wrong_fix "
            "ON orders(user_id, status)")),
        ("最终成功修复", any("user_id, status" in s for s in res.applied_sql)),
        ("严格鉴别诊断通过", score.diagnosis_strict),
        ("无需人工介入的遗留（撤销失败/状态未知）", len(attention) == 0),
        ("成功的修复保持生效", len(applied) == 1),
        ("无阶段违规", not res.violations),
    ]
    for name, cond in checks:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok &= cond

print("\n" + "=" * 72)
print("W4 ACCEPTANCE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
