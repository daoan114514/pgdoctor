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
from dataclasses import asdict, dataclass, field

from agent import esc as esc_mod
from agent import explanation_runtime as xr
from agent import verification as verify_mod
from agent.episode_state import EpisodeState, RemediationAttempt
from agent.explanation import CausalStatus
from agent.policy import Policy
from agent.state_machine import Phase, PhaseViolation, StateMachine
from agent.toolbox import Toolbox
from knowledge.causal_graph import graph as G
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
    benchmark_score: dict = field(default_factory=dict)
    learning_result: dict = field(default_factory=dict)


def _post_episode_learning(env, st: EpisodeState, res: RunResult, *,
                           active_layers: set[str],
                           provenance: str = "sandbox") -> None:
    """Score once, then write enabled v2 learning layers exactly once.

    The episode split is the contamination boundary.  ``eval`` episodes are
    still scored and persisted, but the L1-L4 writers receive the eval split
    and therefore cannot mutate learned state.  Learning failures are recorded
    in the trace instead of changing an already completed operational outcome.
    """
    result = {
        "enabled_layers": sorted(active_layers),
        "split": str(getattr(env, "spec", {}).get("split", "train")),
        "provenance": provenance,
        "score_status": "SKIPPED",
        "l1": {"written": False, "case_id": "", "reuse_updates": 0},
        "l2": 0,
        "l3": 0,
        "l4": 0,
        "written": False,
        "reason": "learning disabled",
    }
    if not active_layers:
        st.learning_result = result
        res.learning_result = dict(result)
        return

    scorer = getattr(env, "score", None)
    if not callable(scorer):
        result["reason"] = "environment has no score() contract"
        st.learning_result = result
        res.learning_result = dict(result)
        return

    try:
        score = scorer(
            res.claimed_fault_class,
            audit=res.audit,
            ledger=st.ledger,
            kpi=res.final_kpi,
            regression=res.final_regression,
        )
        score_dict = asdict(score) if hasattr(score, "__dataclass_fields__") \
            else {
                key: getattr(score, key) for key in (
                    "diagnosis", "outcome", "safe_pass", "non_destructive",
                    "diagnosis_strict", "details") if hasattr(score, key)
            }
        st.benchmark_score = score_dict
        res.benchmark_score = dict(score_dict)
        result["score_status"] = "SCORED"
    except Exception as exc:
        result["reason"] = f"scoring failed: {type(exc).__name__}: {exc}"
        st.learning_result = result
        res.learning_result = dict(result)
        return

    split = result["split"]
    try:
        if "l1" in active_layers:
            from knowledge import case_store as cs

            selected_path_ids = (
                list(st.explanation_graph.selected_path_ids)
                if st.explanation_graph and score.diagnosis else [])
            reuse_updates = 0
            if split != "eval":
                for recall in st.evidence_task_audit:
                    if recall.get("event") != "l1_case_recall":
                        continue
                    cs.record_reuse_v2(
                        recall["case_id"],
                        recalled_path_ids=recall.get("recalled_path_ids", []),
                        selected_path_ids=selected_path_ids,
                        episode_id=st.episode_id,
                    )
                    reuse_updates += 1
            case = cs.write_case_v2(
                st, score, getattr(env, "spec", {}), provenance=provenance)
            result["l1"] = {
                "written": case is not None,
                "case_id": case.case_id if case is not None else "",
                "reuse_updates": reuse_updates,
            }

        v234 = active_layers.intersection({"l2", "l3", "l4"})
        if v234:
            from knowledge import evolution as ev

            learned = ev.learn_v2(
                st, score, split=split, provenance=provenance,
                enabled_layers=v234)
            for layer in ("l2", "l3", "l4"):
                result[layer] = int(learned.get(layer, 0))
        result["written"] = bool(
            result["l1"]["written"] or
            any(result[layer] for layer in ("l2", "l3", "l4")))
        result["reason"] = ("learning updates persisted" if result["written"]
                            else "episode produced no admissible updates")
    except Exception as exc:
        result["reason"] = f"learning failed: {type(exc).__name__}: {exc}"

    st.learning_result = result
    res.learning_result = dict(result)


def _symptoms(obs) -> list[str]:
    s = []
    b, c = obs.healthy_kpi, obs.current_kpi
    if c.get("p99_ms", 0) > b.get("p99_ms", 0) * 3:
        s.append(f"p99 上升 {c['p99_ms'] / max(b.get('p99_ms', 1), 1):.0f}x")
    if c.get("cpu_pct", 0) > b.get("cpu_pct", 0) * 2:
        s.append(f"CPU 上升 {c['cpu_pct'] / max(b.get('cpu_pct', 1), 1):.0f}x")
    if c.get("errors", 0) > 0:
        s.append(f"错误 {c['errors']}")
    # KPI 告警之外还可能是容量/连接/阻塞类告警。真实接入里的 alert 通常
    # 是可读规则名或消息；把它纳入词汇映射，否则 disk_growing 这类症状
    # 即使已经告警，也永远到不了候选生成阶段。
    alert = str(getattr(obs, "alert", "") or "")
    low = alert.lower()
    if "磁盘" in alert or "disk" in low:
        s.append(f"磁盘告警 {alert}")
    if "连接" in alert or "conn" in low:
        s.append(f"连接告警 {alert}")
    if "阻塞" in alert or "blocked" in low:
        s.append(f"阻塞告警 {alert}")
    if "autovacuum" in low or "自动清理" in alert:
        s.append(f"autovacuum 告警 {alert}")
    return s


def _symptoms_from_kpis(baseline: dict, current: dict, alert: str = "") -> list[str]:
    class Snapshot:
        healthy_kpi = baseline
        current_kpi = current

    snap = Snapshot()
    snap.alert = alert
    return _symptoms(snap)


def _verify_expected_effects(st: EpisodeState, after: dict) -> dict:
    plan = st.intervention_plan
    before = st.pre_intervention_kpi or st.current_kpi
    aliases = {"latency_p99_ms": "p99_ms", "cpu_usage_pct": "cpu_pct"}
    checks = []
    for expected in (plan.expected_effects if plan else []):
        metric = str(expected.get("metric", ""))
        key = aliases.get(metric, metric)
        old, new = before.get(key), after.get(key)
        result = "INCONCLUSIVE"
        change = None
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            direction = expected.get("direction")
            change = ((old - new) / max(abs(old), 1e-9)
                      if direction == "decrease" else
                      (new - old) / max(abs(old), 1e-9))
            result = ("SUPPORTED" if change >= float(
                expected.get("minimum_change", 0.0)) else "REFUTED")
        checks.append({**expected, "before": old, "after": new,
                       "observed_change": change, "result": result})
    return {
        "plan_id": plan.plan_id if plan else "",
        "expected_effect_nodes": list(plan.expected_effect_nodes) if plan else [],
        "configured_window_seconds": max(
            [int(item.get("window_seconds", 0)) for item in checks] or [0]),
        "effects": checks,
    }


def _rollback_route(st: EpisodeState, plan,
                    current_symptoms: list[str],
                    failure_scope: str = "") -> tuple[Phase, str]:
    """Choose the narrowest retry scope justified by persisted causal state."""
    explanation = st.explanation_graph
    if explanation is not None and plan is not None:
        path = explanation.path_map().get(plan.selected_path_id)
        if path is not None and path.status == CausalStatus.REFUTED.value:
            return Phase.INVESTIGATE, "PATH_SEGMENT"

    from knowledge.causal_graph import graph as causal_graph

    current_mapped: set[str] = set()
    for symptom in current_symptoms:
        current_mapped.update(causal_graph.map_symptoms(
            [symptom], fallback=False))
    if current_mapped - set(st.observed_symptom_ids):
        st.symptoms = list(dict.fromkeys(st.symptoms + current_symptoms))
        return Phase.HYPOTHESIZE, "ROOT_SET"
    if failure_scope:
        target = verify_mod.retry_phase_for_failure(failure_scope)
        return Phase(target), failure_scope
    return Phase.PLAN, "INTERVENTION"


def _gate_denial_target(reasons: list[str]) -> Phase:
    """Evidence/context gaps require recollection; proposal shape requires replan."""
    text = " ".join(reasons).lower()
    evidence_markers = (
        "evidence", "trusted", "fresh", "esc", "证据", "取证",
        "因果上下文", "目标状态",
    )
    return (Phase.INVESTIGATE if any(marker in text
                                     for marker in evidence_markers)
            else Phase.PLAN)


def _retry_phase(value: str) -> Phase:
    return {
        "PLAN": Phase.PLAN,
        "INVESTIGATE": Phase.INVESTIGATE,
        "ESCALATE": Phase.ESCALATE,
    }.get(value, Phase.PLAN)


def _typed_proposal(st: EpisodeState) -> RemediationProposal:
    """Combine model intent with the separately persisted trusted context."""
    payload = dict(st.proposal)
    if st.schema_version == 2:
        plan = st.intervention_plan
        context = st.causal_gate_context
        if plan is None or context is None:
            raise ValueError("trusted intervention plan and gate context are required")
        payload.update({
            "root_cause": context.intervention_target,
            "fix_id": context.fix_id,
            "esc_verdict": "SUFFICIENT",
            "partial_explanation": st.partial_fix_suspected,
            "evidence_refs": context.evidence_refs,
            "explanation_id": context.explanation_id,
            "explanation_revision": context.explanation_revision,
            "selected_path_id": plan.selected_path_id,
            "intervention_target": context.intervention_target,
            "intervention_kind": context.intervention_kind,
            "expected_effect_nodes": context.expected_effect_nodes,
            "expected_effects": context.expected_effects,
            "esc_report_id": context.esc_report_id,
            "unresolved_p0_paths": context.unresolved_p0_paths,
        })
    return RemediationProposal(**{
        key: value for key, value in payload.items()
        if key in RemediationProposal.__annotations__})


def run_episode(env: DBAScenarioEnv, obs, policy: Policy,
                max_steps: int = 45, allow_repair: bool = False,
                confirm_cb=None, quiet: bool = False, use_esc: bool = True,
                use_cases: bool = True, use_cases_split: str = "train",
                use_learned: bool = True,
                learned_layers: set[str] | None = None,
                ) -> tuple[RunResult, EpisodeState]:
    active_layers = ({"l1", "l2", "l3", "l4"}
                     if learned_layers is None else
                     {str(layer).lower() for layer in learned_layers})
    unknown_layers = active_layers - {"l1", "l2", "l3", "l4"}
    if unknown_layers:
        raise ValueError(f"unknown learned layers: {sorted(unknown_layers)}")
    if not use_learned:
        active_layers.clear()
    if not use_cases:
        active_layers.discard("l1")
    st = EpisodeState(episode_id=env.episode_id, scenario_id=env.spec["id"])
    st.alert = obs.alert
    st.baseline_kpi = obs.healthy_kpi
    st.current_kpi = obs.current_kpi
    st.budget["max_steps"] = max_steps
    st.symptoms = _symptoms(obs)
    st.incident_window = {
        "started_at": st.started_at,
        "baseline_kpi_captured": bool(st.baseline_kpi),
        "metric_window_start": st.started_at,
        "metric_window_end": None,
        "scenario_revision": int(env.spec.get("revision", 1)),
        "learned_layers": sorted(active_layers),
    }
    st.save()

    sm = StateMachine(st, allow_repair=allow_repair)
    tb = Toolbox(env.observe(), st, sm)
    # 候选根因由故障因果图多跳遍历给出，而不是谁凭印象列举 ——
    # 这样覆盖率有保证，级联故障里离症状好几跳的真根因也不会被漏掉。
    from knowledge import case_store as _cs
    ctx = {
        # 告警里带上慢查询本身：真实场景里 APM 会指出哪条查询在拖慢，
        # 让 agent 从零猜"哪条查询有问题"不是本项目要解决的问题。
        "hot_query": " ".join(env.spec["workload"]["hot_query"].split()),
        "allow_repair": allow_repair,
        "explanation": {"explanation_id": "", "revision": 0,
                        "frontier": [], "needs": []},
        "use_learned": bool(active_layers),
        "learned_layers": sorted(active_layers),
    }
    # 案例先验：只影响假设的生成与排序，绝不替代取证。
    # split 过滤是防污染的硬闸 —— 跑 eval 时只能检索 train 案例。
    _fp = _cs.fingerprint_from_state(st)
    from knowledge.causal_graph import graph as _case_graph
    _case_symptoms = _case_graph.map_symptoms(st.symptoms, fallback=False)
    _hits = _cs.search_v2(
        _fp, split=use_cases_split, top_k=3,
        query_text=" ".join(st.symptoms),
        observed_symptoms=_case_symptoms
    ) if use_cases and "l1" in active_layers else []
    ctx["case_prior"] = _cs.render_prior_v2(_hits) if _hits else ""
    ctx["playbook_hint"] = ""
    ctx["case_ids"] = [h["case"].case_id for h in _hits]
    for hit in _hits:
        st.evidence_task_audit.append({
            "event": "l1_case_recall",
            "case_id": hit["case"].case_id,
            "recalled_path_ids": [
                template.get("path_id", "")
                for template in hit.get("path_templates", [])
                if template.get("path_id")
            ],
            "similarity": hit.get("score", 0.0),
            "at": time.time(),
        })
    if _hits:
        st.save()
    if not quiet:
        if _hits:
            print(f"  [案例库] 命中 {len(_hits)} 例 "
                  f"(指纹相似度 {[h['fp_sim'] for h in _hits]})")

    t0 = time.time()
    res = RunResult(episode_id=st.episode_id, final_phase=st.phase,
                    claimed_fault_class=None, claimed_root_cause=None, steps=0)
    audit = {"ungated_writes": [], "shield_blocked": [], "shield_breaches": [], "table_locks": [],
             "unreverted_failures": []}
    unchanged_visits: dict[tuple, int] = {}

    def log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    try:
        while not sm.terminal():
            cur = sm.phase

            explanation_revision = (
                st.explanation_graph.revision if st.explanation_graph else -1)
            progress_key = (
                cur.value, st.budget["steps"], len(st.scratchpad),
                explanation_revision, len(st.esc_reports),
                len(st.intervention_attempts), st.repair_attempts,
            )
            unchanged_visits[progress_key] = unchanged_visits.get(
                progress_key, 0) + 1
            if unchanged_visits[progress_key] >= 4:
                st.outcome_note = (
                    f"阶段 {cur.value} 连续重复且证据/解释/尝试均无变化")
                sm.goto(Phase.ESCALATE, "no causal progress")
                continue

            # REPORT and ESCALATE are deterministic finalization stages, not
            # terminal states.  DONE is the only terminal phase.
            if cur in {Phase.REPORT, Phase.ESCALATE}:
                st.final_report = xr.final_report(
                    st, escalated=cur is Phase.ESCALATE)
                st.finished = True
                st.incident_window["metric_window_end"] = time.time()
                st.save()
                sm.goto(Phase.DONE, "final report persisted")
                continue

            if st.exhausted():
                st.outcome_note = "步数预算耗尽"
                sm.goto(Phase.ESCALATE, "budget exhausted")
                continue

            # ── 策略阶段 ──────────────────────────────────
            if cur in POLICY_PHASES:
                if cur is Phase.HYPOTHESIZE:
                    explanation = xr.recall_explanation(
                        st, case_hits=_hits, use_learned=bool(
                            active_layers & {"l1", "l3"}),
                        use_l1="l1" in active_layers,
                        use_l3_edges="l3" in active_layers,
                        use_l3_paths="l3" in active_layers)
                    st.save()
                    ctx["playbook_hint"] = ""
                    log(f"  [因果图] 解释 {explanation.explanation_id} "
                        f"rev={explanation.revision}: "
                        f"{len(explanation.candidate_paths)} 条路径，"
                        f"P0={list(explanation.p0_obligations)}")

                if cur is Phase.INVESTIGATE:
                    xr.bind_evidence(st)
                if cur is Phase.DIAGNOSE:
                    xr.bind_evidence(st)
                    selected = xr.select_minimal_explanation(st)
                    log(f"  DIAGNOSE     paths={selected or 'none'} "
                        f"scope={st.explanation_graph.scope if st.explanation_graph else 'PARTIAL'}")

                ctx["explanation"] = xr.compact_projection(st)
                if cur is Phase.PLAN:
                    ctx["remediation_options"] = xr.intervention_options(
                        st, executable_only=True)
                nxt = policy.run_phase(cur, tb, st, ctx)

                if cur is Phase.MONITOR:
                    st.incident_window["monitor_completed_at"] = time.time()
                    st.incident_window["source_epochs"] = {
                        key: value.get("stats_reset", "")
                        for key, value in st.cumulative_baselines.items()
                    }
                elif cur is Phase.OBSERVE:
                    mapped, unmapped = xr.map_observed_symptoms(st)
                    st.incident_window["first_snapshot_at"] = time.time()
                    log(f"  OBSERVE      mapped={mapped} "
                        f"unmapped={unmapped or 'none'}")
                elif cur is Phase.INVESTIGATE:
                    added = xr.bind_evidence(st)
                    ctx["explanation"] = xr.compact_projection(st)
                    log(f"  INVESTIGATE  bound={len(added)} "
                        f"frontier={len(ctx['explanation']['frontier'])}")

                # ★ 证据充分性检查：DIAGNOSE 离开分析域前的硬转移。
                # 写流程必须先过 ESC 才能进 PLAN；只读流程也必须先过 ESC
                # 才能发布 REPORT，避免把证据不足的解释包装成最终诊断。
                if (cur is Phase.DIAGNOSE and
                        nxt in {Phase.PLAN, Phase.REPORT} and use_esc):
                    if st.schema_version == 2:
                        rep = esc_mod.check_explanation(st)
                        st.esc_verdict = rep["verdict"]
                        res.esc_reports.append(rep)
                        log(f"  ESC          -> {rep['verdict']} "
                            f"paths={rep['selected_path_ids']} "
                            f"P0_open={len(rep['unresolved_p0_paths'])}")
                        if rep["verdict"] == "SUFFICIENT":
                            st.esc_retries = 0
                        elif rep["verdict"] in {"AMBIGUOUS", "EXHAUSTED"}:
                            st.directives = rep["directives"][:8]
                            st.esc_verdict = ""
                            st.outcome_note = (
                                "解释子图仍有不可安全消解的歧义"
                                if rep["verdict"] == "AMBIGUOUS" else
                                "证据预算耗尽或必需证据长期不可得")
                            sm.goto(Phase.ESCALATE,
                                    f"esc {rep['verdict'].lower()}")
                            st.save()
                            continue
                        else:
                            st.directives = rep["directives"][:8]
                            st.esc_verdict = ""
                            st.esc_retries += 1
                            target = (Phase.HYPOTHESIZE
                                      if rep["requires_rehypothesize"] else
                                      Phase.INVESTIGATE)
                            sm.goto(target, "esc insufficient")
                            st.save()
                            continue
                    else:
                        rep = esc_mod.check(
                            st, candidates=st.hypothesis_candidates)
                        st.esc_verdict = rep.verdict
                        res.esc_reports.append(rep)
                        if rep.verdict != esc_mod.ESCVerdict.SUFFICIENT.value:
                            st.directives = rep.directives[:5]
                            st.esc_verdict = ""
                            st.esc_retries += 1
                            # v1 的 check() 没有 BUDGET_AND_AVAILABILITY 那一维，
                            # 退回取证这条路上没有任何东西喊停，唯一的界是
                            # max_steps —— 而它太大，会把不收敛伪装成还在努力。
                            limit = esc_mod.DEFAULT_EXPLANATION_ESC.max_esc_retries
                            if limit > 0 and st.esc_retries >= limit:
                                st.outcome_note = (
                                    f"ESC 连续 {st.esc_retries} 轮未通过，"
                                    f"证据推不动，升级人工")
                                sm.goto(Phase.ESCALATE, "esc retry budget")
                                st.save()
                                continue
                            sm.goto(Phase.INVESTIGATE, "esc insufficient")
                            continue

                if (cur is Phase.DIAGNOSE and
                        nxt in {Phase.PLAN, Phase.REPORT} and
                        not use_esc and st.schema_version == 2):
                    rep = esc_mod.check_explanation(st, persist=False)
                    rep["actual_verdict"] = rep["verdict"]
                    rep["verdict"] = "SUFFICIENT"
                    rep["bypassed"] = True
                    rep["esc_report_id"] = "bypassed_" + rep["esc_report_id"]
                    rep["report_id"] = rep["esc_report_id"]
                    st.esc_reports.append(rep)
                    st.esc_verdict = "SUFFICIENT"

                # 诊断可以自动完成，不代表处置也能自动完成。没有可执行修复，
                # 或所有修复都标为 escalate_only 时，在 PLAN 之前直接升级，
                # 避免模型反复提交注定会被拒的动作。
                if cur is Phase.DIAGNOSE and nxt is Phase.PLAN:
                    fixes = xr.intervention_options(st)
                    executable = xr.intervention_options(
                        st, executable_only=True)
                    selected_roots = set(
                        st.explanation_graph.derive_selected_root_causes()
                        if st.explanation_graph else [])
                    root_options = [
                        option for option in fixes
                        if option.get("target_node_id") in selected_roots]
                    manual_p0_roots = {
                        option.get("target_node_id") for option in root_options
                        if G.severity_of(option.get("target_node_id", "")) == "P0" and
                        (option.get("execution") == "escalate_only" or
                         option.get("manual") or
                         option.get("intervention_kind") == "MANUAL")
                    }
                    if manual_p0_roots:
                        xr.create_manual_intervention_plan(st)
                        st.outcome_note = (
                            f"选中 P0 根因 {sorted(manual_p0_roots)} 只能升级人工；"
                            "禁止用下游机制修复绕过根因处置")
                        log(f"  REMEDIATION  -> ESCALATE    {st.outcome_note}")
                        sm.goto(Phase.ESCALATE, "manual P0 root selected")
                        st.save()
                        continue
                    if not executable:
                        names = [f["fix"] for f in fixes]
                        manual_options = [
                            option for option in fixes
                            if option.get("execution") == "escalate_only" or
                            option.get("manual") or
                            option.get("intervention_kind") == "MANUAL"
                        ]
                        if manual_options:
                            xr.create_manual_intervention_plan(st)
                        st.outcome_note = (
                            f"已选择解释路径 "
                            f"{st.explanation_graph.selected_path_ids if st.explanation_graph else []}；"
                            f"修复 {names or ['未定义']} 只能升级人工")
                        log(f"  REMEDIATION  -> ESCALATE    {st.outcome_note}")
                        sm.goto(Phase.ESCALATE, "no agent-executable remediation")
                        st.save()
                        continue
                    ctx["remediation_options"] = executable

                log(f"  {cur.value:<12} -> {nxt.value:<12} "
                    f"(累计 {st.budget['steps']} 步)")
                sm.goto(nxt, f"policy={policy.name}")
                st.save()
                continue

            # ── 系统阶段：安全门裁决 ───────────────────────
            if cur is Phase.GATE:
                if not st.proposal:
                    st.outcome_note = "进入 GATE 但没有提案"
                    sm.goto(Phase.PLAN, "no proposal, replan")
                    continue
                if st.schema_version == 2:
                    try:
                        xr.build_gate_context(st, model_payload=st.proposal)
                    except ValueError as exc:
                        reason = str(exc)
                        reason_code = getattr(
                            exc, "reason_code", "CAUSAL_BINDING_INVALID")
                        retry_phase = getattr(exc, "retry_phase", "PLAN")
                        target = _retry_phase(retry_phase)
                        st.last_gate_denial = {
                            "sql": st.proposal.get("sql", ""),
                            "action_type": st.proposal.get("action_type", ""),
                            "rollback": st.proposal.get("rollback", ""),
                            "tier": "CAUSAL",
                            "reasons": [reason],
                            "reason_code": reason_code,
                            "retry_phase": retry_phase,
                            "intervention_plan": (
                                st.intervention_plan.to_dict()
                                if st.intervention_plan else None),
                        }
                        st.proposal = {}
                        st.intervention_plan = None
                        st.causal_gate_context = None
                        log(f"  GATE         -> CAUSAL  {reason[:60]}")
                        sm.goto(target, "causal gate context rejected")
                        continue
                p = _typed_proposal(st)
                d = gate.assess(p)
                res.gate_decisions.append(
                    {"tier": d.tier, "approved": d.approved,
                     "reasons": d.reasons + d.shield_reasons, "sql": p.sql,
                     "reason_code": d.reason_code,
                     "retry_phase": d.retry_phase,
                     "causal_context": (st.causal_gate_context.to_dict()
                                        if st.causal_gate_context else None)})
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
                        "reason_code": d.reason_code,
                        "retry_phase": d.retry_phase,
                        "intervention_plan": (
                            st.intervention_plan.to_dict()
                            if st.intervention_plan else None),
                    }
                    st.note("gate", "proposal_denied",
                            f"[{d.tier}] {p.sql[:60]} — "
                            f"{'; '.join(reasons)[:150]}")
                    st.proposal = {}
                    st.intervention_plan = None
                    st.causal_gate_context = None
                    denial_target = _retry_phase(d.retry_phase)
                    if denial_target is Phase.INVESTIGATE:
                        sm.goto(Phase.INVESTIGATE,
                                "gate evidence/context denied")
                        continue
                    if denial_target is Phase.ESCALATE:
                        st.outcome_note = (
                            f"安全门要求升级人工 [{d.reason_code}] "
                            f"{'; '.join(reasons)[:160]}")
                        sm.goto(Phase.ESCALATE, "gate requires manual escalation")
                        continue
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
                if st.schema_version == 2:
                    explanation = st.explanation_graph
                    plan = st.intervention_plan
                    context = st.causal_gate_context
                    stale = (explanation is None or plan is None or context is None or
                             plan.explanation_id != explanation.explanation_id or
                             plan.explanation_revision != explanation.revision or
                             context.explanation_revision != explanation.revision)
                    if stale:
                        st.outcome_note = "批准后的干预计划已过期，禁止执行"
                        st.proposal = {}
                        st.intervention_plan = None
                        st.causal_gate_context = None
                        sm.goto(Phase.PLAN, "stale approved plan")
                        continue
                p = _typed_proposal(st)
                st.pre_intervention_kpi = dict(st.current_kpi)
                attempt = None
                if st.schema_version == 2 and st.intervention_plan is not None:
                    verify_mod.capture_pre_intervention(
                        st, env.observe(), st.intervention_plan,
                        kpi=st.pre_intervention_kpi,
                        hot_query=str(ctx.get("hot_query") or ""))
                    attempt = verify_mod.start_attempt(st, st.intervention_plan)
                r = gate.execute(p, st.episode_id, confirm_cb=confirm_cb)
                if attempt is not None:
                    verify_mod.mark_execution(attempt, r)
                    st.record_intervention_attempt(attempt)
                    st.save()
                if not r.executed:
                    log(f"  EXECUTE      -> 未执行: {r.error[:60]}")
                    if r.undo_id:
                        st.undo_refs.append(r.undo_id)
                    if not r.undo_id:
                        st.repair_attempts += 1
                        st.rollback_decision = {
                            "scope": "EXECUTION",
                            "target_id": attempt.attempt_id if attempt else "",
                            "reason": r.error,
                            "next_phase": ("ESCALATE" if
                                           st.repair_attempts >=
                                           st.max_repair_attempts else "PLAN"),
                            "intervention_plan": (
                                st.intervention_plan.to_dict()
                                if st.intervention_plan else None),
                        }
                        st.proposal = {}
                        st.intervention_plan = None
                        st.causal_gate_context = None
                    if st.repair_attempts >= st.max_repair_attempts:
                        st.outcome_note = f"执行失败: {r.error[:120]}"
                        sm.goto(Phase.ESCALATE, "execute failed")
                    elif not r.undo_id:
                        sm.goto(Phase.PLAN, "execution not started, replan")
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
                plan = st.intervention_plan
                attempt = (st.intervention_attempt_for(plan.plan_id)
                           if plan else None)
                if (st.schema_version == 2 and
                        not verify_mod.ready_for_causal_verification(attempt)):
                    st.outcome_note = "没有成功执行记录，禁止进入因果效果验证"
                    sm.goto(Phase.ROLLBACK, "execution was not successful")
                    continue
                settle_s = (verify_mod.observation_window(plan)
                            if st.schema_version == 2 else 0)
                kpi, reg = env.verify(settle_s=settle_s)
                st.current_kpi = kpi.as_dict()
                recovered = False
                recovered_error = ""
                try:
                    from sandbox import metrics
                    # 基线取本 episode 注入前实测的那一组：成功判据可以按
                    # 健康态的倍数写，绝对阈值在不同核数的机器上不可移植。
                    recovered = metrics.eval_expr(
                        env.spec["success"]["outcome"], kpi,
                        baseline=getattr(env, "healthy_kpi", None))
                except Exception as exc:
                    # 判据求值失败与"没恢复"长得一模一样，都会触发回滚 ——
                    # 静默吞掉的话，一条正确的修复会被撤销而没人知道为什么。
                    recovered_error = f"{type(exc).__name__}: {exc}"
                expected = (verify_mod.evaluate_expected_effects(
                    st, env.observe(), plan, kpi=kpi.as_dict(),
                    hot_query=str(ctx.get("hot_query") or ""))
                    if st.schema_version == 2 and plan is not None else
                    _verify_expected_effects(st, kpi.as_dict()))
                effects_outcome = expected.get("effects_outcome") or (
                    "REFUTED" if any(item["result"] == "REFUTED"
                                      for item in expected["effects"])
                    else "SUPPORTED")
                effects_ok = effects_outcome == "SUPPORTED"
                st.verification_result = {
                    **expected, "kpi": kpi.as_dict(), "recovered": recovered,
                    "recovered_error": recovered_error,
                    "regression_passed": reg.passed,
                    "regression": {
                        "latency_regressions": list(reg.latency_regressions),
                        "invariant_violations": list(reg.invariant_violations),
                    },
                }
                passed = verify_mod.verification_passed(
                    recovered=recovered,
                    effects_outcome=effects_outcome,
                    regression_passed=reg.passed)
                if attempt is not None:
                    attempt.actual = [dict(item) for item in expected["effects"]]
                    attempt.outcome = "VERIFIED" if passed else "FAILED"
                    attempt.failure_scope = ("NONE" if passed else
                                             verify_mod.classify_failure_scope(
                                                 st, attempt,
                                                 st.verification_result))
                    attempt.affected_edge_ids = (
                        verify_mod.affected_edges_on_path(
                            st, attempt, st.verification_result)
                        if attempt.failure_scope == "PATH_SEGMENT" else [])
                    attempt.learnable = bool(
                        attempt.execution_status == "SUCCEEDED" and
                        effects_outcome != "INCONCLUSIVE")
                    attempt.updated_at = time.time()
                    verify_mod.apply_failure_knowledge(
                        st, attempt, st.verification_result)
                    st.record_intervention_attempt(attempt)
                log(f"  VERIFY       -> p99={kpi.p99_ms}ms cpu={kpi.cpu_pct}% "
                    f"恢复={recovered} 路径预测={effects_outcome} 回归={reg.passed}"
                    + (f" 判据求值失败: {recovered_error}"
                       if recovered_error else ""))
                res.audit["verify"] = dict(st.verification_result)
                res.final_kpi, res.final_regression = kpi, reg
                if passed:
                    sm.goto(Phase.REPORT, "verified")
                else:
                    why = (f"路径预测{effects_outcome}"
                           if not effects_ok else
                           "KPI 未恢复" if not recovered else
                           f"回归失败: {reg.latency_regressions + reg.invariant_violations}")
                    st.outcome_note = why
                    sm.goto(Phase.ROLLBACK, why[:60])
                continue

            # ── 系统阶段：回滚（数据库回滚，知识不回滚）────
            if cur is Phase.ROLLBACK:
                plan = st.intervention_plan
                attempt = (st.intervention_attempt_for(plan.plan_id)
                           if plan else None)
                undo_id = st.undo_refs[-1] if st.undo_refs else ""
                okr, msg = (gate.rollback(undo_id) if undo_id
                            else (True, "无可回滚的变更"))
                if attempt is not None:
                    attempt.rollback_attempted = bool(undo_id)
                    attempt.rollback_status = "SUCCEEDED" if okr else "FAILED"
                    attempt.rollback_message = msg
                    attempt.updated_at = time.time()
                    st.record_intervention_attempt(attempt)
                res.rollbacks.append(f"{undo_id}: {msg[:60]}")
                log(f"  ROLLBACK     -> ok={okr} {msg[:56]}")
                if not okr:
                    # 回滚失败是最危险的情形：冻结并升级，绝不重试
                    audit["unreverted_failures"].append(f"{undo_id}: {msg[:80]}")
                    st.outcome_note = f"回滚失败，需人工介入: {msg[:100]}"
                    st.rollback_decision = {
                        "scope": (attempt.failure_scope if attempt else
                                  "EXECUTION"),
                        "target_id": (attempt.attempt_id if attempt else
                                      plan.plan_id if plan else ""),
                        "reason": st.outcome_note,
                        "next_phase": "ESCALATE",
                        "affected_edge_ids": (list(attempt.affected_edge_ids)
                                              if attempt else []),
                        "rollback_status": "FAILED",
                        "rollback_message": msg,
                        "intervention_plan": plan.to_dict() if plan else None,
                    }
                    sm.goto(Phase.ESCALATE, "undo failed")
                    continue

                rc = (plan.intervention_target if plan else
                      st.claimed_fault_class or "unknown")
                st.record_attempt(RemediationAttempt(
                    root_cause=rc,
                    sql=(plan.sql if plan else
                         (res.applied_sql[-1] if res.applied_sql else "")),
                    predicted=st.proposal.get("predicted_impact", {}),
                    actual=st.current_kpi,
                    verdict=(attempt.outcome if attempt else
                             "FAILED_NO_IMPROVEMENT"),
                    rolled_back=True,
                    inference=st.outcome_note or "修复后未达成功判据",
                    counts_against_root_cause=False))
                st.repair_attempts += 1
                if st.repair_attempts >= st.max_repair_attempts:
                    st.outcome_note = "修复尝试次数用尽"
                    target = Phase.ESCALATE
                    scope = (attempt.failure_scope if attempt else
                             "INTERVENTION")
                else:
                    current_symptoms = _symptoms_from_kpis(
                        st.baseline_kpi, st.current_kpi, st.alert)
                    target, scope = _rollback_route(
                        st, plan, current_symptoms,
                        attempt.failure_scope if attempt else "")
                st.rollback_decision = {
                    "scope": scope,
                    "target_id": (attempt.attempt_id if attempt else
                                  plan.plan_id if plan else rc),
                    "reason": st.outcome_note or "expected downstream effect absent",
                    "next_phase": target.value,
                    "affected_edge_ids": (list(attempt.affected_edge_ids)
                                          if attempt else []),
                    "rollback_status": "SUCCEEDED",
                    "rollback_message": msg,
                    "intervention_plan": plan.to_dict() if plan else None,
                }
                st.proposal = {}
                st.intervention_plan = None
                st.causal_gate_context = None
                if target is Phase.ESCALATE:
                    sm.goto(Phase.ESCALATE, "attempts exhausted")
                else:
                    log(f"               仅反证 {scope}，回到 {target.value}")
                    sm.goto(target, f"rollback scope={scope}")
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

    if sm.phase is not Phase.DONE:
        if sm.phase not in {Phase.REPORT, Phase.ESCALATE}:
            try:
                sm.goto(Phase.ESCALATE, "unhandled episode error")
            except PhaseViolation:
                pass
        if sm.phase in {Phase.REPORT, Phase.ESCALATE}:
            st.final_report = xr.final_report(
                st, escalated=sm.phase is Phase.ESCALATE)
            st.finished = True
            st.incident_window["metric_window_end"] = time.time()
            st.save()
            sm.goto(Phase.DONE, "final report persisted after error")

    st.finished = sm.phase is Phase.DONE
    st.save()

    res.final_phase = st.phase
    res.claimed_fault_class = st.claimed_fault_class
    res.claimed_root_cause = st.claimed_root_cause
    res.steps = st.budget["steps"]
    res.tool_calls = tb.calls
    res.transitions = sm.history
    res.audit.update(audit)
    res.case_ids_used = ctx.get("case_ids", [])
    _post_episode_learning(
        env, st, res, active_layers=active_layers,
        provenance=str(env.spec.get("provenance", "sandbox")))
    st.save()
    res.elapsed_s = round(time.time() - t0, 1)
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
