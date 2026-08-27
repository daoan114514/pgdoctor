"""主循环 —— 把状态机、工具面、策略、安全门接起来。

循环本身不做领域判断：只负责推进阶段、校验转移合法性、记账与落盘。
"该做什么"在策略里，"能不能做"在状态机里，"安不安全"在安全门里。

阶段分两类：
  策略阶段  MONITOR..PLAN —— 由 policy 决定动作（可能是 LLM）
  系统阶段  GATE/EXECUTE/ROLLBACK —— 确定性代码，模型不参与

这个划分是安全故事的核心：模型能提出什么，和什么会被真正执行，
是两件被结构隔开的事。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent import esc as esc_mod
from agent.episode_state import EpisodeState, RemediationAttempt
from agent.policy import Policy
from agent.state_machine import Phase, PhaseViolation, StateMachine
from agent.toolbox import Toolbox
from safety import gate
from safety.gate import RemediationProposal
from sandbox.env import DBAScenarioEnv

POLICY_PHASES = {Phase.MONITOR, Phase.OBSERVE, Phase.HYPOTHESIZE,
                 Phase.INVESTIGATE, Phase.DIAGNOSE, Phase.PLAN}


@dataclass
class RunResult:
    episode_id: str
    final_phase: str
    claimed_fault_class: str | None
    claimed_root_cause: str | None
    steps: int
    tool_calls: list[str] = field(default_factory=list)
    transitions: list[tuple] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    applied_sql: list[str] = field(default_factory=list)
    gate_decisions: list[dict] = field(default_factory=list)
    rollbacks: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    error: str = ""
    audit: dict = field(default_factory=dict)
    esc_reports: list = field(default_factory=list)
    case_ids_used: list = field(default_factory=list)
    # 循环里 VERIFY 已经量过一次，判分应复用它而不是再测一遍：
    # 重测会拿到另一个时间窗的数据，导致"循环说恢复了、判分说没恢复"。
    final_kpi: object = None
    final_regression: object = None


def _symptoms(obs) -> list[str]:
    s = []
    b, c = obs.healthy_kpi, obs.current_kpi
    if c.get("p99_ms", 0) > b.get("p99_ms", 0) * 3:
        s.append(f"p99 上升 {c['p99_ms'] / max(b.get('p99_ms', 1), 1):.0f}x")
    if c.get("cpu_pct", 0) > b.get("cpu_pct", 0) * 2:
        s.append(f"CPU 上升 {c['cpu_pct'] / max(b.get('cpu_pct', 1), 1):.0f}x")
    if c.get("errors", 0) > 0:
        s.append(f"错误 {c['errors']}")
    return s


def run_episode(env: DBAScenarioEnv, obs, policy: Policy,
                max_steps: int = 45, allow_repair: bool = False,
                confirm_cb=None, quiet: bool = False, use_esc: bool = True,
                use_cases: bool = True, use_cases_split: str = "train"
                ) -> tuple[RunResult, EpisodeState]:
    st = EpisodeState(episode_id=env.episode_id, scenario_id=env.spec["id"])
    st.alert = obs.alert
    st.baseline_kpi = obs.healthy_kpi
    st.current_kpi = obs.current_kpi
    st.budget["max_steps"] = max_steps
    st.symptoms = _symptoms(obs)
    st.save()

    sm = StateMachine(st, allow_repair=allow_repair)
    tb = Toolbox(env.observe(), st, sm)
    # 候选根因由故障因果图多跳遍历给出，而不是谁凭印象列举 ——
    # 这样覆盖率有保证，级联故障里离症状好几跳的真根因也不会被漏掉。
    from knowledge.causal_graph import graph as _G
    from knowledge import case_store as _cs
    graph_symptoms = _map_symptoms(st.symptoms)
    candidates = [c["root_cause"] for c in
                  _G.candidate_causes(graph_symptoms, top_k=4)]
    ctx = {
        # 告警里带上慢查询本身：真实场景里 APM 会指出哪条查询在拖慢，
        # 让 agent 从零猜"哪条查询有问题"不是本项目要解决的问题。
        "hot_query": " ".join(env.spec["workload"]["hot_query"].split()),
        "symptoms": st.symptoms,
        "candidates": candidates,
        "graph_symptoms": graph_symptoms,
    }
    # 案例先验：只影响假设的生成与排序，绝不替代取证。
    # split 过滤是防污染的硬闸 —— 跑 eval 时只能检索 train 案例。
    _fp = _cs.fingerprint_from_state(st)
    _hits = _cs.search(_fp, split=use_cases_split, top_k=3,
                       query_text=" ".join(st.symptoms)) if use_cases else []
    ctx["case_prior"] = _cs.render_prior(_hits) if _hits else ""
    # L2：历史有效的取证顺序。与案例先验的区别在于它是跨 episode
    # 聚合出来的流程，而不是某一次具体事故。
    try:
        from knowledge.evolution import render_playbook_hint
        ctx["playbook_hint"] = render_playbook_hint(candidates)
    except Exception:
        ctx["playbook_hint"] = ""
    ctx["case_ids"] = [h["case"].case_id for h in _hits]
    if not quiet:
        print(f"  [因果图] 症状 {graph_symptoms} -> 候选根因 {candidates}")
        if _hits:
            print(f"  [案例库] 命中 {len(_hits)} 例 "
                  f"(指纹相似度 {[h['fp_sim'] for h in _hits]})")

    t0 = time.time()
    res = RunResult(episode_id=st.episode_id, final_phase=st.phase,
                    claimed_fault_class=None, claimed_root_cause=None, steps=0)
    audit = {"ungated_writes": [], "shield_blocked": [], "shield_breaches": [], "table_locks": [],
             "unreverted_failures": []}

    def log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    try:
        while not sm.terminal():
            if st.exhausted():
                st.outcome_note = "步数预算耗尽"
                sm.goto(Phase.ESCALATE, "budget exhausted")
                break

            cur = sm.phase

            # ── 策略阶段 ──────────────────────────────────
            if cur in POLICY_PHASES:
                nxt = policy.run_phase(cur, tb, st, ctx)

                # ★ 证据充分性检查：DIAGNOSE -> PLAN 之间的硬转移。
                # 不过 ESC 就进不了 PLAN，也就永远到不了 EXECUTE ——
                # 这是防"基于错根因动生产库"的第一道闸（第二道是安全门）。
                if cur is Phase.DIAGNOSE and nxt is Phase.PLAN and use_esc:
                    rep = esc_mod.check(st, candidates=ctx.get("candidates"))
                    res.esc_reports.append(rep)
                    log(f"  ESC          -> {rep.summary()}")
                    for d in rep.dims:
                        log(f"               {d.name} "
                            f"{'PASS' if d.passed else 'FAIL'}"
                            f"{'(必需)' if d.mandatory else ''} {d.detail}")
                    if rep.verdict == esc_mod.ESCVerdict.SUFFICIENT.value:
                        # D3 是非必需维度，所以"存在孤儿症状"也能拿到
                        # SUFFICIENT。把这个事实记下来：单一根因解释不了
                        # 全部症状时，后面修复失败不能把账算到它头上。
                        _d3 = next((d for d in rep.dims if d.name == "D3"),
                                   None)
                        st.partial_fix_suspected = bool(_d3 and _d3.missing)
                        if st.partial_fix_suspected:
                            log(f"               D3 孤儿症状 {_d3.missing}"
                                f" -> 疑似第二个故障，本轮修复失败不反证根因")
                    elif rep.verdict in (esc_mod.ESCVerdict.AMBIGUOUS.value,
                                         esc_mod.ESCVerdict.EXHAUSTED.value):
                        st.outcome_note = f"ESC {rep.verdict}: {rep.directives[:2]}"
                        sm.goto(Phase.ESCALATE, f"esc {rep.verdict}")
                        continue
                    else:
                        # INSUFFICIENT 不只是拒绝，还要指路：把缺什么证据
                        # 变成定向取证指令，退回继续调查
                        st.directives = rep.directives[:5]
                        st.claimed_fault_class = None
                        st.claimed_root_cause = None
                        st.esc_retries = getattr(st, "esc_retries", 0) + 1
                        for dtv in rep.directives[:3]:
                            log(f"               补证: {dtv}")
                        if st.esc_retries > 2:
                            st.outcome_note = "反复取证仍不足，升级人工"
                            sm.goto(Phase.ESCALATE, "esc exhausted")
                        else:
                            sm.goto(Phase.INVESTIGATE, "esc insufficient")
                        continue

                log(f"  {cur.value:<12} -> {nxt.value:<12} "
                    f"(累计 {st.budget['steps']} 步)")
                sm.goto(nxt, f"policy={policy.name}")
                st.save()
                continue

            # ── 系统阶段：安全门裁决 ───────────────────────
            if cur is Phase.GATE:
                if not st.proposal:
                    st.outcome_note = "进入 GATE 但没有提案"
                    sm.goto(Phase.ESCALATE, "no proposal")
                    continue
                p = RemediationProposal(**{k: v for k, v in st.proposal.items()
                                           if k in RemediationProposal.__annotations__})
                d = gate.assess(p)
                res.gate_decisions.append(
                    {"tier": d.tier, "approved": d.approved,
                     "reasons": d.reasons + d.shield_reasons, "sql": p.sql})
                log(f"  GATE         -> {d.tier:<8} "
                    f"{(d.reasons + d.shield_reasons)[0][:46] if (d.reasons or d.shield_reasons) else ''}")
                if not d.approved:
                    if d.shield_reasons and not d.approved and any(
                            "护盾" in r for r in d.reasons):
                        # 记成 blocked 而不是 breach：护盾在这里是生效了，
                        # 不是被击穿了。两者混为一谈的话，模型每次伸手拿
                        # 危险动作都会判掉 Safe Pass —— 那衡量的是"有没有
                        # 起过坏念头"，不是"系统安不安全"。
                        audit["shield_blocked"].append(p.sql[:80])
                    reasons = d.reasons + d.shield_reasons
                    # 理由必须落进 episode 状态：只记在 res 上的话，模型
                    # 退回 PLAN 时读到的上下文和被拒之前一模一样。
                    st.last_gate_denial = {
                        "sql": p.sql,
                        "action_type": p.action_type,
                        "rollback": p.rollback,
                        "tier": d.tier,
                        "reasons": reasons,
                    }
                    st.note("gate", "proposal_denied",
                            f"[{d.tier}] {p.sql[:60]} — "
                            f"{'; '.join(reasons)[:150]}")
                    st.proposal = {}
                    st.repair_attempts += 1
                    if st.repair_attempts >= st.max_repair_attempts:
                        st.outcome_note = (
                            f"提案连续被拒，升级人工；最后一次 [{d.tier}] "
                            f"{'; '.join(reasons)[:160]}")
                        sm.goto(Phase.ESCALATE, "gate denied")
                    else:
                        sm.goto(Phase.PLAN, "gate denied, replan")
                    continue
                sm.goto(Phase.EXECUTE, f"gate {d.tier}")
                continue

            # ── 系统阶段：执行（唯一写区）──────────────────
            if cur is Phase.EXECUTE:
                p = RemediationProposal(**{k: v for k, v in st.proposal.items()
                                           if k in RemediationProposal.__annotations__})
                r = gate.execute(p, st.episode_id, confirm_cb=confirm_cb)
                if not r.executed:
                    log(f"  EXECUTE      -> 未执行: {r.error[:60]}")
                    st.repair_attempts += 1
                    st.proposal = {}
                    if r.undo_id:
                        st.undo_refs.append(r.undo_id)
                    if st.repair_attempts >= st.max_repair_attempts:
                        st.outcome_note = f"执行失败: {r.error[:120]}"
                        sm.goto(Phase.ESCALATE, "execute failed")
                    else:
                        sm.goto(Phase.ROLLBACK, "execute failed")
                    continue
                st.undo_refs.append(r.undo_id)
                res.applied_sql.append(p.sql)
                env.applied_sql.append(p.sql)
                log(f"  EXECUTE      -> 已执行 ({r.duration_s}s) undo={r.undo_id}")
                sm.goto(Phase.VERIFY, "executed")
                continue

            # ── 系统阶段：验证 ────────────────────────────
            if cur is Phase.VERIFY:
                # verify 内部会保证至少等满一个指标窗口，这里不必再指定
                kpi, reg = env.verify()
                st.current_kpi = kpi.as_dict()
                recovered = False
                try:
                    from sandbox import metrics
                    recovered = metrics.eval_expr(
                        env.spec["success"]["outcome"], kpi)
                except Exception:
                    pass
                log(f"  VERIFY       -> p99={kpi.p99_ms}ms cpu={kpi.cpu_pct}% "
                    f"恢复={recovered} 回归={reg.passed}")
                res.audit["verify"] = {"kpi": kpi.as_dict(),
                                       "regression_passed": reg.passed}
                res.final_kpi, res.final_regression = kpi, reg
                if recovered and reg.passed:
                    sm.goto(Phase.REPORT, "verified")
                else:
                    why = ("KPI 未恢复" if not recovered
                           else f"回归失败: {reg.latency_regressions + reg.invariant_violations}")
                    st.outcome_note = why
                    sm.goto(Phase.ROLLBACK, why[:60])
                continue

            # ── 系统阶段：回滚（数据库回滚，知识不回滚）────
            if cur is Phase.ROLLBACK:
                undo_id = st.undo_refs[-1] if st.undo_refs else ""
                okr, msg = (gate.rollback(undo_id) if undo_id
                            else (True, "无可回滚的变更"))
                res.rollbacks.append(f"{undo_id}: {msg[:60]}")
                log(f"  ROLLBACK     -> ok={okr} {msg[:56]}")
                if not okr:
                    # 回滚失败是最危险的情形：冻结并升级，绝不重试
                    audit["unreverted_failures"].append(f"{undo_id}: {msg[:80]}")
                    st.outcome_note = f"回滚失败，需人工介入: {msg[:100]}"
                    sm.goto(Phase.ESCALATE, "undo failed")
                    continue

                rc = st.claimed_fault_class or "unknown"
                # 存在未被解释的症状时，"KPI 没回基线"更可能是第二个故障
                # 还在，而不是这个根因判错了 —— 不能算进反证计数。
                _counts = not st.partial_fix_suspected
                st.record_attempt(RemediationAttempt(
                    root_cause=rc,
                    sql=(res.applied_sql[-1] if res.applied_sql else ""),
                    predicted=st.proposal.get("predicted_impact", {}),
                    actual=st.current_kpi,
                    verdict="FAILED_NO_IMPROVEMENT",
                    rolled_back=True,
                    inference=st.outcome_note or "修复后未达成功判据",
                    counts_against_root_cause=_counts))
                if not _counts:
                    log(f"               失败不计入 {rc} 的反证计数"
                        f"（有未解释的症状，疑似第二个故障）")
                # 知识单调增长：数据库回到原状，但"这条路走过、不通"留下了
                st.proposal = {}
                st.repair_attempts += 1

                # 一次修复失败只说明那个方案不行；同一根因连续失败才反证根因本身
                if st.attempts_for(rc) >= 2:
                    st.refute_by_remediation(
                        rc, f"{st.attempts_for(rc)} 次修复均未改善")
                    st.claimed_fault_class = None
                    st.claimed_root_cause = None
                    log(f"               {rc} 连续 {st.attempts_for(rc)} 次修复失败"
                        f" -> 升级为根因级反证")

                if st.repair_attempts >= st.max_repair_attempts:
                    st.outcome_note = "修复尝试次数用尽"
                    sm.goto(Phase.ESCALATE, "attempts exhausted")
                else:
                    log("               知识不回滚：已记录失败尝试，重新诊断")
                    sm.goto(Phase.HYPOTHESIZE, "retry after failed fix")
                continue

            raise RuntimeError(f"未处理的阶段 {cur}")

    except PhaseViolation as exc:
        # 状态机拦下越界动作。这不是崩溃，是设计生效了。
        res.violations.append(str(exc))
        st.outcome_note = f"阶段违规: {exc}"
        try:
            sm.goto(Phase.ESCALATE, "phase violation")
        except PhaseViolation:
            pass
    except Exception as exc:
        res.error = f"{type(exc).__name__}: {exc}"
        st.outcome_note = res.error

    st.finished = True
    st.save()

    res.final_phase = st.phase
    res.claimed_fault_class = st.claimed_fault_class
    res.claimed_root_cause = st.claimed_root_cause
    res.steps = st.budget["steps"]
    res.tool_calls = tb.calls
    res.transitions = sm.history
    res.elapsed_s = round(time.time() - t0, 1)
    res.audit.update(audit)
    res.case_ids_used = ctx.get("case_ids", [])
    return res, st


# 向后兼容：W3 的只诊断入口
def run_diagnosis(env, obs, policy, max_steps: int = 40,
                  allow_repair: bool = False, quiet: bool = False):
    return run_episode(env, obs, policy, max_steps=max_steps,
                       allow_repair=allow_repair, quiet=quiet)


def _map_symptoms(symptoms: list[str]) -> list[str]:
    """把人话症状描述映射成因果图的节点 id。

    实现搬到 graph.map_symptoms —— ESC 的 D3 也要用同一份映射，
    之前两边各有一份副本，改了一边另一边不会跟着动。
    """
    from knowledge.causal_graph import graph as _G
    return _G.map_symptoms(symptoms, fallback=True)
