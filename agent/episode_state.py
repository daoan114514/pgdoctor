"""EpisodeState —— 持久真相源。

核心不变式：上下文是可丢弃的缓存，这个对象才是真相。
任何系统正确性依赖的东西（当前阶段、假设台账、证据引用、回滚句柄、
基线 KPI）都不许只存在于上下文里，否则一次压缩就可能让 agent 失忆、
重复调查、甚至重复执行已经失败过的修复。

它落盘保存，所以进程崩溃后也能恢复。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any

from agent.explanation import (CausalGateContext, ExplanationGraph,
                               InterventionPlan, json_ready,
                               legacy_readonly_projection, stable_id)

ROOT = Path(__file__).resolve().parent.parent


class Verdict(str, Enum):
    UNTESTED = "UNTESTED"
    CONFIRMED = "CONFIRMED"
    REFUTED = "REFUTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    # 比只读证据强得多的反证：真修了，没用。
    # 它的存在是为了堵死"忘了修复失败过 -> 重新推导出同一个根因 -> 再修一次"
    # 这个无限重试循环。
    REFUTED_BY_REMEDIATION = "REFUTED_BY_REMEDIATION"


class EvidenceStatus(str, Enum):
    """一次观测能否作为事实进入诊断。

    OBSERVED 表示工具成功取得了可解释的值；UNKNOWN 表示尚缺基线、统计
    周期发生变化等导致值不可判；ERROR 表示观测本身失败。后两者会保留在
    轨迹和上下文里，但绝不能喂给 ESC 当确认或反证。
    """
    OBSERVED = "OBSERVED"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


def evidence_is_observed(entry: dict) -> bool:
    """兼容旧轨迹：没有 status 的历史证据视为 OBSERVED。"""
    return entry.get("status", EvidenceStatus.OBSERVED.value) == \
        EvidenceStatus.OBSERVED.value


@dataclass
class EvidenceRef:
    """指向轨迹里一次真实的工具调用。ESC 核验的是这个，不是 agent 的自述。"""
    kind: str                 # 证据类型，来自受控词表
    raw_ref: str              # trace://episode/step_NNN
    summary: str
    bears_on: list[str] = field(default_factory=list)   # 这条线索关系到哪些假设
    status: str = EvidenceStatus.OBSERVED.value


@dataclass
class HypothesisEntry:
    verdict: str = Verdict.UNTESTED.value
    evidence: list[EvidenceRef] = field(default_factory=list)
    note: str = ""


@dataclass
class RemediationAttempt:
    """失败的修复尝试。知识单调增长的载体 —— 数据库回滚，但这条记录不回滚。"""
    root_cause: str
    sql: str
    predicted: dict
    actual: dict
    verdict: str
    rolled_back: bool = False
    inference: str = ""
    # 这次失败该不该算到"根因被反证"的头上。
    # 多根因场景里修一个 -> KPI 回不到基线，失败的原因是另一个故障还在，
    # 不是这个根因判错了。算进去会把正确的根因反证掉，而且是永久的。
    counts_against_root_cause: bool = True


@dataclass
class InterventionAttempt:
    """One version-bound intervention and its observed causal outcome.

    This is append-only episode knowledge.  Rollback may restore database
    state, but it must never remove the execution result or its scoped
    counterfactual evidence.
    """
    attempt_id: str
    episode_id: str
    plan_id: str
    explanation_id: str
    explanation_revision: int
    selected_path_id: str
    intervention_target: str
    fix_id: str
    intervention_kind: str
    sql: str
    ordinal: int = 1
    execution_status: str = "PENDING"
    execution_error: str = ""
    execution_undo_id: str = ""
    execution_duration_s: float = 0.0
    expected: list[dict] = field(default_factory=list)
    actual: list[dict] = field(default_factory=list)
    outcome: str = "PENDING"
    failure_scope: str = "NONE"
    affected_edge_ids: list[str] = field(default_factory=list)
    rollback_attempted: bool = False
    rollback_status: str = "NOT_NEEDED"
    rollback_message: str = ""
    learnable: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.affected_edge_ids = list(dict.fromkeys(self.affected_edge_ids))
        expected_id = self.expected_attempt_id()
        if self.attempt_id and self.attempt_id != expected_id:
            raise ValueError("attempt_id does not match episode/plan/ordinal")
        self.attempt_id = expected_id

    @classmethod
    def create(cls, *, episode_id: str, plan: InterventionPlan,
               ordinal: int) -> "InterventionAttempt":
        return cls(
            attempt_id="",
            episode_id=episode_id,
            plan_id=plan.plan_id,
            explanation_id=plan.explanation_id,
            explanation_revision=plan.explanation_revision,
            selected_path_id=plan.selected_path_id,
            intervention_target=plan.intervention_target,
            fix_id=plan.fix_id,
            intervention_kind=plan.intervention_kind,
            sql=plan.sql,
            ordinal=int(ordinal),
            expected=[dict(item) for item in plan.expected_effects],
        )

    def expected_attempt_id(self) -> str:
        return stable_id("attempt", {
            "episode_id": self.episode_id,
            "plan_id": self.plan_id,
            "ordinal": int(self.ordinal),
        })


@dataclass
class EpisodeState:
    episode_id: str
    scenario_id: str
    # New episodes use the v2 explanation contracts.  A loaded trace with no
    # version is explicitly kept as v1 instead of pretending its edges were
    # validated by the new pipeline.
    schema_version: int = 2
    phase: str = "MONITOR"
    alert: str = ""
    baseline_kpi: dict = field(default_factory=dict)
    current_kpi: dict = field(default_factory=dict)
    symptoms: list[str] = field(default_factory=list)
    # 假设生成阶段产出的正式候选集。ESC 只能消费它，不能在裁决时按一张
    # 可能已经变化的图重新生成另一套候选。旧轨迹没有该字段时由 ESC 的
    # 兼容分支按历史规则重建。
    hypothesis_candidates: list[str] = field(default_factory=list)
    ledger: dict[str, HypothesisEntry] = field(default_factory=dict)
    scratchpad: list[dict] = field(default_factory=list)     # append-only
    # 累计统计必须做窗口差分。基线放在持久状态里，进程恢复后也不能把
    # 下一次读数重新当成“第一次”，更不能用累计总量冒充本次事故增量。
    cumulative_baselines: dict[str, dict] = field(default_factory=dict)
    incident_window: dict = field(default_factory=dict)
    observed_symptom_ids: list[str] = field(default_factory=list)
    unmapped_symptoms: list[str] = field(default_factory=list)
    explanation_graph: ExplanationGraph | None = None
    # v2 subagent reports are persisted separately from causal bindings.  A
    # report is an observation envelope; only the predicate layer may turn it
    # into a trusted binding and update node/edge state.
    evidence_reports: dict[str, dict] = field(default_factory=dict)
    evidence_task_audit: list[dict] = field(default_factory=list)
    esc_reports: list[dict] = field(default_factory=list)
    intervention_plan: InterventionPlan | None = None
    causal_gate_context: CausalGateContext | None = None
    pre_intervention_kpi: dict = field(default_factory=dict)
    pre_intervention_effects: dict = field(default_factory=dict)
    verification_result: dict = field(default_factory=dict)
    rollback_decision: dict = field(default_factory=dict)
    final_report: dict = field(default_factory=dict)
    # Deterministic post-episode outputs.  Keeping these in the trace makes a
    # completed episode self-contained: callers do not need to score it again
    # (which can otherwise collect a different KPI window) or replay learning.
    benchmark_score: dict = field(default_factory=dict)
    learning_result: dict = field(default_factory=dict)
    attempts: list[RemediationAttempt] = field(default_factory=list)
    intervention_attempts: list[InterventionAttempt] = field(default_factory=list)
    undo_refs: list[str] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)      # ESC 给的定向取证指令
    claimed_root_cause: str | None = None
    claimed_fault_class: str | None = None
    proposal: dict = field(default_factory=dict)
    repair_attempts: int = 0
    # 最近一次被安全门拒绝的提案及其理由。不存进状态的话，模型退回
    # PLAN 时读到的上下文和第一次完全一样，只会把同样的错误再提一遍。
    last_gate_denial: dict = field(default_factory=dict)
    esc_retries: int = 0
    # 最近一次针对 claimed_fault_class 的 ESC 裁决。只有状态机能写入，
    # Toolbox 会把它封进提案，GATE 不接受模型自报“证据已经充分”。
    esc_verdict: str = ""
    # ESC 判 SUFFICIENT 但 D3 查出孤儿症状时置位：单一根因解释不了全部
    # 症状，很可能还有第二个故障。此时修复失败不该反证当前根因。
    partial_fix_suspected: bool = False
    max_repair_attempts: int = 2
    budget: dict = field(default_factory=lambda: {"steps": 0, "max_steps": 40})
    started_at: float = field(default_factory=time.time)
    finished: bool = False
    outcome_note: str = ""

    # ── 台账 ──────────────────────────────────────────────
    def ensure_hypotheses(self, names: list[str]) -> None:
        for n in names:
            self.ledger.setdefault(n, HypothesisEntry())

    def set_verdict(self, name: str, verdict: Verdict | str,
                    evidence: list[EvidenceRef] | None = None,
                    note: str = "") -> None:
        e = self.ledger.setdefault(name, HypothesisEntry())
        e.verdict = verdict.value if isinstance(verdict, Verdict) else verdict
        if evidence:
            e.evidence.extend(evidence)
        if note:
            e.note = note

    def confirmed(self) -> list[str]:
        return [k for k, v in self.ledger.items()
                if v.verdict == Verdict.CONFIRMED.value]

    def refuted(self) -> list[str]:
        return [k for k, v in self.ledger.items()
                if v.verdict in (Verdict.REFUTED.value,
                                 Verdict.REFUTED_BY_REMEDIATION.value)]

    def untested(self) -> list[str]:
        return [k for k, v in self.ledger.items()
                if v.verdict == Verdict.UNTESTED.value]

    # ── 便签（append-only）───────────────────────────────
    def note(self, author: str, kind: str, observation: str,
             raw_ref: str = "", bears_on: list[str] | None = None,
             status: EvidenceStatus | str = EvidenceStatus.OBSERVED,
             structured_value: Any = None, predicate_id: str = "",
             target_kind: str = "NODE", target_ids: list[str] | None = None,
             window_start: float | None = None,
             window_end: float | None = None,
             source_epoch: str = "", explanation_id: str = "",
             explanation_revision: int | None = None,
             evidence_task_id: str = "",
             evidence_need_ids: list[str] | None = None,
             collection_tool: str = "") -> None:
        status_value = status.value if isinstance(status, EvidenceStatus) else status
        if status_value not in {s.value for s in EvidenceStatus}:
            raise ValueError(f"未知证据状态: {status_value}")
        self.scratchpad.append({
            "seq": len(self.scratchpad) + 1,
            "ts": time.time(),
            "author": author,
            "evidence_type": kind,
            "observation": observation,
            "raw_ref": raw_ref,
            "bears_on": bears_on or [],
            "status": status_value,
            "structured_value": structured_value,
            "predicate_id": predicate_id,
            "target_kind": target_kind,
            "target_ids": target_ids or list(bears_on or []),
            "window_start": window_start,
            "window_end": window_end,
            "source_epoch": source_epoch,
            "explanation_id": explanation_id,
            "explanation_revision": explanation_revision,
            "evidence_task_id": evidence_task_id,
            "evidence_need_ids": evidence_need_ids or [],
            "collection_tool": collection_tool,
        })

    # ── 失败尝试：知识不回滚 ──────────────────────────────
    def record_attempt(self, attempt: RemediationAttempt) -> None:
        """只登记"这次修复失败了"，不直接否定根因。

        最初把两者等同，结果一次建错列的索引就把正确的根因判死，
        agent 再也无法用正确的修复重试。失败的是具体修复，不是根因；
        只有同一根因下多次修复都失败，才谈得上反证根因本身。
        """
        self.attempts.append(attempt)

    def record_intervention_attempt(self, attempt: InterventionAttempt) -> None:
        """Append once, or persist updates to the same stable attempt."""
        for index, current in enumerate(self.intervention_attempts):
            if current.attempt_id == attempt.attempt_id:
                self.intervention_attempts[index] = attempt
                return
        self.intervention_attempts.append(attempt)

    def intervention_attempt_for(self, plan_id: str) -> InterventionAttempt | None:
        return next((attempt for attempt in reversed(self.intervention_attempts)
                     if attempt.plan_id == plan_id), None)

    def attempts_for(self, root_cause: str) -> int:
        """反证判定用：只数"能算到这个根因头上"的失败。"""
        return sum(1 for a in self.attempts
                   if a.root_cause == root_cause
                   and a.counts_against_root_cause)

    def attempts_logged_for(self, root_cause: str) -> int:
        """日志与报告用：这个根因下一共试过几次（含不计入反证的）。"""
        return sum(1 for a in self.attempts if a.root_cause == root_cause)

    def refute_by_remediation(self, root_cause: str, note: str = "") -> None:
        """同一根因反复修不好，才升级为根因级反证。"""
        self.set_verdict(root_cause, Verdict.REFUTED_BY_REMEDIATION, note=note)

    def already_failed(self, root_cause: str) -> bool:
        """是否已被修复反证（不是"是否尝试过"）。"""
        e = self.ledger.get(root_cause)
        return bool(e and e.verdict == Verdict.REFUTED_BY_REMEDIATION.value)

    def tried_fix(self, sql: str) -> bool:
        norm = " ".join(sql.split()).lower()
        return any(" ".join(a.sql.split()).lower() == norm for a in self.attempts)

    def spend(self, n: int = 1) -> bool:
        self.budget["steps"] += n
        return self.budget["steps"] < self.budget["max_steps"]

    def exhausted(self) -> bool:
        return self.budget["steps"] >= self.budget["max_steps"]

    # ── 持久化 ────────────────────────────────────────────
    def path(self) -> Path:
        d = ROOT / "traces" / self.episode_id
        d.mkdir(parents=True, exist_ok=True)
        return d / "episode_state.json"

    def save(self) -> None:
        self.path().write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2,
                       sort_keys=False),
            encoding="utf-8")

    def to_dict(self) -> dict:
        """Return the explicit JSON contract used by save and round-trip tests."""
        return {f.name: json_ready(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def load(cls, episode_id: str) -> "EpisodeState":
        p = ROOT / "traces" / episode_id / "episode_state.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        # No version was written before the explanation-subgraph migration.
        schema_version = int(d.get("schema_version", 1))
        st = cls(episode_id=d["episode_id"], scenario_id=d["scenario_id"],
                 schema_version=schema_version)
        for k, v in d.items():
            if k == "ledger":
                st.ledger = {
                    name: HypothesisEntry(
                        verdict=e.get("verdict", Verdict.UNTESTED.value),
                        evidence=[EvidenceRef(**r) for r in e.get("evidence", [])],
                        note=e.get("note", ""))
                    for name, e in v.items()}
            elif k == "attempts":
                st.attempts = [RemediationAttempt(**a) for a in v]
            elif k == "intervention_attempts":
                st.intervention_attempts = [InterventionAttempt(**a) for a in v]
            elif k == "explanation_graph":
                st.explanation_graph = (ExplanationGraph.from_dict(v)
                                        if v is not None else None)
            elif k == "intervention_plan":
                st.intervention_plan = (InterventionPlan.from_dict(v)
                                        if v is not None else None)
            elif k == "causal_gate_context":
                st.causal_gate_context = (CausalGateContext.from_dict(v)
                                          if v is not None else None)
            elif hasattr(st, k):
                setattr(st, k, v)
        return st

    def v1_readonly_projection(self) -> ExplanationGraph:
        """Expose old state to v2 readers without fabricating verified edges."""
        if self.schema_version != 1:
            raise ValueError("readonly v1 projection is only valid for v1 traces")
        return legacy_readonly_projection(
            episode_id=self.episode_id,
            symptoms=self.symptoms,
            ledger=self.ledger,
        )

    # ── 上下文投影 ────────────────────────────────────────
    def render_context(self) -> str:
        """把状态投影成一段紧凑的上下文。

        回退时不恢复旧上下文，而是丢弃后从这里重建 —— 重建比恢复更好：
        没有失败尝试的噪声、token 更省，而且重试次数不会让上下文线性膨胀。
        """
        L = []
        L.append(f"# 事故 {self.episode_id}")
        L.append(f"告警: {self.alert}")
        if self.baseline_kpi:
            b, c = self.baseline_kpi, self.current_kpi
            L.append(f"KPI: p99 {b.get('p99_ms')}ms -> {c.get('p99_ms')}ms | "
                     f"cpu {b.get('cpu_pct')}% -> {c.get('cpu_pct')}%")
        if self.symptoms:
            L.append("症状: " + ", ".join(self.symptoms))

        if self.schema_version == 2 and self.explanation_graph is not None:
            explanation = self.explanation_graph
            paths = explanation.path_map()
            L.append(f"\n## 解释子图 {explanation.explanation_id} "
                     f"rev={explanation.revision} scope={explanation.scope}")
            if explanation.selected_path_ids:
                L.append("选中路径:")
                for path_id in explanation.selected_path_ids:
                    path = paths[path_id]
                    segment_states = [
                        f"{edge_id}:{explanation.edge_status.get(edge_id, 'UNTESTED')}"
                        for edge_id in path.edge_ids
                    ]
                    L.append(f"- {' -> '.join(path.node_ids)} "
                             f"[{' | '.join(segment_states)}]")
            else:
                L.append("选中路径: （尚未选择）")

            unresolved = [
                p for p in explanation.candidate_paths
                if p.path_id not in explanation.selected_path_ids and
                p.status in ("UNTESTED", "INCONCLUSIVE")
            ]
            if unresolved:
                L.append("未决分叉:")
                for path in unresolved[:8]:
                    L.append(f"- {' -> '.join(path.node_ids)} ({path.status})")

            if explanation.p0_obligations:
                L.append("P0 义务:")
                for cause_id, obligation in explanation.p0_obligations.items():
                    suffix = " / TRUNCATED" if obligation.truncated else ""
                    L.append(f"- {cause_id}: {obligation.status}{suffix} "
                             f"paths={obligation.reachable_path_ids}")

            missing = []
            for path in explanation.candidate_paths:
                observed_types = {
                    explanation.evidence_bindings[binding_id].evidence_type
                    for binding_id in path.evidence_binding_ids
                    if binding_id in explanation.evidence_bindings and
                    explanation.evidence_bindings[binding_id].status ==
                    EvidenceStatus.OBSERVED.value
                }
                for evidence_type in path.required_evidence_types:
                    if evidence_type not in observed_types:
                        missing.append(f"{path.path_id}: {evidence_type}")
            if missing:
                L.append("缺失证据:")
                for item in list(dict.fromkeys(missing))[:12]:
                    L.append(f"- {item}")
            if explanation.unexplained_symptoms:
                L.append("未解释症状: " + ", ".join(
                    explanation.unexplained_symptoms))

        elif self.ledger:
            L.append("\n## 假设台账")
            for name, e in self.ledger.items():
                ev = f" [{len(e.evidence)} 条证据]" if e.evidence else ""
                nt = f" — {e.note}" if e.note else ""
                L.append(f"- {name}: {e.verdict}{ev}{nt}")

        if self.attempts:
            L.append("\n## 已尝试过的修复（不要重复）")
            for a in self.attempts:
                L.append(f"- {a.root_cause}: {a.sql[:70]} -> {a.verdict}"
                         f"{' (已回滚)' if a.rolled_back else ''}")
                if a.inference:
                    L.append(f"  推断: {a.inference}")

        if self.scratchpad:
            L.append("\n## 证据便签")
            for e in self.scratchpad[-12:]:
                bo = f" ->{','.join(e['bears_on'])}" if e["bears_on"] else ""
                status = e.get("status", EvidenceStatus.OBSERVED.value)
                mark = "" if status == EvidenceStatus.OBSERVED.value else f"/{status}"
                L.append(f"- [{e['evidence_type']}{mark}]{bo} "
                         f"{e['observation'][:110]}")

        if self.directives:
            L.append("\n## 待补取证（ESC 判定证据不足）")
            for d in self.directives:
                L.append(f"- {d}")

        L.append(f"\n阶段: {self.phase} | 步数 {self.budget['steps']}/"
                 f"{self.budget['max_steps']}")
        return "\n".join(L)
