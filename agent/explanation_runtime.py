"""Deterministic runtime for episode-level causal explanations.

Policies collect evidence and propose intervention intent.  This module is the
only place that turns structured tool output into causal state, path selection,
ESC decisions, and graph-bound intervention metadata.
"""
from __future__ import annotations

import itertools
import time
from typing import Any

from pglast import ast, parse_sql
from pglast.stream import RawStream

from agent.episode_state import EpisodeState, EvidenceStatus, Verdict
from agent.explanation import (
    DEFAULT_FRESHNESS_S,
    CausalGateContext,
    CausalStatus,
    EvidenceBinding,
    EvidenceNeed,
    EvidenceTargetKind,
    ExplanationScope,
    InterventionPlan,
    ObligationStatus,
    PredicateResult,
)
from knowledge.causal_graph import graph as G
from knowledge.evidence_predicates import PredicateContext, evaluate, is_gate_predicate


class CausalGateError(ValueError):
    """A deterministic causal denial with an explicit retry destination."""

    def __init__(self, message: str, *, reason_code: str,
                 retry_phase: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retry_phase = retry_phase


def map_observed_symptoms(st: EpisodeState) -> tuple[list[str], list[str]]:
    mapped: list[str] = []
    unmapped: list[str] = []
    for symptom in st.symptoms:
        ids = G.map_symptoms([symptom], fallback=False)
        if ids:
            mapped.extend(ids)
        else:
            unmapped.append(symptom)
    st.observed_symptom_ids = sorted(set(mapped))
    st.unmapped_symptoms = list(dict.fromkeys(unmapped))
    return st.observed_symptom_ids, st.unmapped_symptoms


def _case_path_scores(paths, case_hits: list[dict] | None) -> dict[str, float]:
    hits = case_hits or []
    scores: dict[str, float] = {}
    for path in paths:
        score = 0.0
        for hit in hits:
            for template in hit.get("path_templates", []) or []:
                if list(template.get("node_ids") or []) == path.node_ids:
                    score += float(hit.get("score", 0.0))
                    break
        if score:
            scores[path.path_id] = score
    return scores


def recall_explanation(st: EpisodeState, *, case_hits: list[dict] | None = None,
                       use_learned: bool = True, use_l1: bool = True,
                       use_l3_edges: bool = True,
                       use_l3_paths: bool = True):
    mapped, unmapped = map_observed_symptoms(st)
    observed = mapped + unmapped
    seed_paths = G.enumerate_causal_paths(observed, use_learned=False)
    case_scores = _case_path_scores(seed_paths, case_hits) if use_l1 else {}
    previous = st.explanation_graph
    explanation = G.recall_explanation(
        observed, episode_id=st.episode_id, use_learned=use_learned,
        case_path_scores=case_scores,
        use_l3_edges=use_l3_edges, use_l3_paths=use_l3_paths)

    for symptom_id in mapped:
        explanation.set_node_status(symptom_id, CausalStatus.SUPPORTED)

    # Re-hypothesizing may expand coverage, but it must not discard fresh,
    # verifiable evidence for structural paths that still exist.
    if previous is not None:
        live_nodes = {node for path in explanation.candidate_paths
                      for node in path.node_ids}
        live_edges = {edge for path in explanation.candidate_paths
                      for edge in path.edge_ids}
        for binding in previous.evidence_bindings.values():
            if (set(binding.target_node_ids).intersection(live_nodes) or
                    set(binding.target_edge_ids).intersection(live_edges)):
                try:
                    explanation.add_evidence_binding(binding)
                except ValueError:
                    continue

    st.explanation_graph = explanation
    recompute_statuses(st)
    sync_v1_projection(st)
    return explanation


def _target_causes(explanation, need: EvidenceNeed) -> set[str]:
    causes: set[str] = set()
    paths = explanation.path_map()
    if need.target_kind in {EvidenceTargetKind.NODE.value,
                            EvidenceTargetKind.P0.value}:
        causes.update(need.target_ids)
    for path_id in need.path_ids:
        path = paths.get(path_id)
        if path is None:
            continue
        for target_id in need.target_ids:
            if target_id in path.edge_ids:
                causes.add(path.node_ids[path.edge_ids.index(target_id)])
            elif target_id in path.node_ids:
                causes.add(target_id)
    return causes


def _entry_matches(explanation, need: EvidenceNeed, entry: dict) -> bool:
    if entry.get("evidence_type") != need.evidence_type:
        return False
    if not entry.get("raw_ref"):
        return False
    if need.target_kind == EvidenceTargetKind.INTERVENTION.value:
        return bool(set(need.target_ids).intersection(entry.get("target_ids") or []))
    bears_on = set(entry.get("bears_on") or [])
    targets = set(entry.get("target_ids") or [])
    return bool(_target_causes(explanation, need).intersection(bears_on | targets))


def _binding_targets(need: EvidenceNeed) -> tuple[list[str], list[str]]:
    if need.target_kind in {EvidenceTargetKind.EDGE.value,
                            EvidenceTargetKind.BRANCH.value}:
        return [], list(need.target_ids)
    return list(need.target_ids), []


def _current_esc_needs(st: EpisodeState, explanation) -> list[EvidenceNeed]:
    """Return typed gaps from the current INSUFFICIENT ESC revision."""
    for report in reversed(st.esc_reports):
        try:
            current_revision = (
                int(report.get("explanation_revision", -1)) ==
                explanation.revision)
        except (TypeError, ValueError):
            current_revision = False
        if (report.get("verdict") != "INSUFFICIENT" or
                report.get("requires_rehypothesize") or
                report.get("explanation_id") != explanation.explanation_id or
                not current_revision):
            continue
        directed = []
        for value in report.get("evidence_needs", []):
            try:
                directed.append(EvidenceNeed.from_dict(value))
            except (KeyError, TypeError, ValueError):
                continue
        return directed
    return []


def bind_evidence(st: EpisodeState, *, max_rounds: int = 12,
                  evidence_task_ids: set[str] | None = None,
                  raw_refs: set[str] | None = None,
                  base_revision: int | None = None,
                  explicit_needs: list[EvidenceNeed] | None = None) -> list[str]:
    explanation = st.explanation_graph
    if explanation is None:
        return []
    added: list[str] = []
    planned_revision = (explanation.revision if base_revision is None
                        else base_revision)

    for _ in range(max_rounds):
        round_added = False
        needs = list(explicit_needs or [])
        if not needs:
            needs = _current_esc_needs(st, explanation)
        if not needs:
            needs = G.evidence_needs(explanation)
        for need in needs:
            target_nodes, target_edges = _binding_targets(need)
            for entry in reversed(st.scratchpad):
                task_id = str(entry.get("evidence_task_id") or "")
                task_explanation = str(entry.get("explanation_id") or "")
                if evidence_task_ids is not None and task_id not in evidence_task_ids:
                    continue
                if raw_refs is not None and str(entry.get("raw_ref") or "") not in raw_refs:
                    continue
                if task_explanation:
                    # Evidence collected for an older explanation revision is
                    # retained as a candidate, but never mutates the current
                    # graph until an explicit revalidation retags it.
                    if task_explanation != explanation.explanation_id:
                        continue
                    if entry.get("explanation_revision") != planned_revision:
                        continue
                    assigned_needs = set(entry.get("evidence_need_ids") or [])
                    if assigned_needs and need.need_id not in assigned_needs:
                        continue
                if not _entry_matches(explanation, need, entry):
                    continue
                observed_at = float(entry.get("ts", time.time()))
                status = str(entry.get("status", EvidenceStatus.OBSERVED.value))
                decision = evaluate(
                    need.predicate_id,
                    entry.get("structured_value"),
                    context=PredicateContext(
                        target_kind=need.target_kind,
                        target_ids=tuple(need.target_ids),
                        collection_status=status,
                        window_start=entry.get("window_start"),
                        window_end=entry.get("window_end"),
                        source_epoch=str(entry.get("source_epoch") or ""),
                    ),
                )
                binding = EvidenceBinding.create(
                    episode_id=st.episode_id,
                    raw_ref=str(entry["raw_ref"]),
                    evidence_type=need.evidence_type,
                    status=status,
                    observed_at=observed_at,
                    predicate_id=need.predicate_id,
                    predicate_result=decision.result,
                    structured_value=entry.get("structured_value"),
                    target_node_ids=target_nodes,
                    target_edge_ids=target_edges,
                    summary=decision.reason,
                    window_start=entry.get("window_start"),
                    window_end=entry.get("window_end"),
                    source_epoch=str(entry.get("source_epoch") or ""),
                    fresh_until=observed_at + need.freshness_seconds,
                )
                try:
                    changed = explanation.add_evidence_binding(binding)
                except ValueError:
                    continue
                if changed:
                    added.append(binding.binding_id)
                    round_added = True
                break
        recompute_statuses(st)
        if not round_added:
            break
    sync_v1_projection(st)
    return added


def _decision_status(results: list[str], *, attempted: bool) -> str:
    decisive = set(results).intersection({PredicateResult.SUPPORTS.value,
                                          PredicateResult.REFUTES.value})
    if decisive == {PredicateResult.SUPPORTS.value}:
        return CausalStatus.SUPPORTED.value
    if decisive == {PredicateResult.REFUTES.value}:
        return CausalStatus.REFUTED.value
    if decisive or attempted:
        return CausalStatus.INCONCLUSIVE.value
    return CausalStatus.UNTESTED.value


def _causal_relation(graph, cause_id: str, binding: EvidenceBinding,
                     *, scope: str) -> tuple[bool, bool]:
    """Return whether a binding may support/refute this causal segment.

    DISCRIMINATES is a collection-priority relation, not a truth relation.  A
    discriminator observation therefore cannot support or close a node merely
    because the evidence task was targeted at that candidate.  Direction is
    granted only by CONFIRMED_BY or by a predicate/scope-matched REFUTED_BY.
    """
    if cause_id not in graph or binding.evidence_type not in graph:
        return False, False
    relations = graph.get_edge_data(cause_id, binding.evidence_type) or {}
    confirms = "CONFIRMED_BY" in relations
    refuter = relations.get("REFUTED_BY") or {}
    refuter_scope = str(refuter.get("scope") or "")
    # 根因节点被反证，它发出的因果边自然也被反证：边一级同时接受 NODE 范围的
    # REFUTED_BY。原来边只认 PATH、节点只认 NODE，需求落在哪一级由 frontier 决定，
    # 一条反证边总有一级是死的（2026-09-23：explain_plan 对 missing_index 从未生效）。
    refutes = (
        bool(refuter) and
        str(refuter.get("predicate_id") or "") == binding.predicate_id and
        (refuter_scope == scope or (scope == "PATH" and refuter_scope == "NODE"))
    )
    return confirms, refutes


def window_spans_own_write(st: EpisodeState, binding) -> bool:
    """这条窗口证据的观测窗，是否跨越了本 episode 自己执行的写操作。

    累计计数器分不清是谁写的。实测 missing_index 场景：agent 建了一个
    1200 万行的索引（CREATE INDEX CONCURRENTLY，67.2 秒），排序外溢
    495.4 MB 临时文件，而 temp_file_volume 读的是 pg_stat_database 的库级
    计数器 —— 于是 work_mem_spill 被确认。同一个 episode 里确认了两个根因，
    严格诊断的 F1 掉到 0.67。**agent 自己的修复动作，制造出了确认另一个根因
    的证据。**

    这是"动作污染证据"，与 provenance 规则管的"根因污染证据"是两个类别：
    前者取决于本 episode 做过什么，后者取决于图的结构。

    放在这里而不是只放在 ESC 里：路径状态和由它投影出的假设台账由
    recompute_statuses 算，它直接读绑定、不经过 ESC 的可信过滤。只在 ESC
    那层拦，台账照样会确认错的根因（实测过一次，白改）。两处共用这一个
    函数，别各写一份 —— 同一条规则分两份实现，迟早会漂。

    没有观测窗的绑定天然不受影响，所以不必先筛出窗口类判据。
    只看真正执行成功的干预：被门拦下或没跑的提案不写库，影响不到计数器。
    """
    start, end = binding.window_start, binding.window_end
    if start is None or end is None:
        return False
    for attempt in getattr(st, "intervention_attempts", []) or []:
        get = (attempt.get if isinstance(attempt, dict)
               else lambda name, default=None: getattr(attempt, name, default))
        if str(get("execution_status") or "") != "SUCCEEDED":
            continue
        began = float(get("created_at") or 0.0)
        if not began:
            continue
        finished = began + float(get("execution_duration_s") or 0.0)
        if began <= end and finished >= start:
            return True
    return False


def live_invalidators(explanation) -> set[str]:
    """尚未被反证的候选根因：它们为真时会让别的证据关于其它根因的裁决失真。"""
    return {path.root_node_id for path in explanation.candidate_paths
            if path.status != CausalStatus.REFUTED.value}


def contaminated_by(binding: EvidenceBinding, live: set[str],
                    edge_sources: dict[str, set[str]] | None = None) -> list[str]:
    """这条绑定的来源，是否正被一条尚未反证的候选路径怀疑失真（provenance_rules 推出）。

    自身豁免：证据判它自己的来源是否失真是检验不是污染，所以绑定目标节点、以及目标边的
    起点根因都不算污染源。ESC 的 EVIDENCE_TRUST 与这里的方向裁决共用这一个函数（规则 4）。
    """
    own_targets = set(binding.target_node_ids)
    for edge_id in binding.target_edge_ids:
        own_targets |= set((edge_sources or {}).get(edge_id, set()))
    return sorted((G.invalidators_of(binding.evidence_type) - own_targets) & live)


def _direction_result(binding: EvidenceBinding, live: set[str],
                      edge_sources: dict[str, set[str]] | None = None) -> str:
    """绑定对节点/边状态的方向贡献。

    存在性门（index_existence、slow_query_ranking）只算 NEUTRAL：它们的 SUPPORTS 仍留在
    绑定上，供 ESC 的 ROOT_REQUIRED_EVIDENCE 与路径的 required_supported 读取；只是不再把
    "数据到手"当成"支持该根因"。被污染的 REFUTES 也只算 NEUTRAL：拿被污染的证据去关掉
    竞争路径正是静默选错的形状（污染只压 REFUTES 不压 SUPPORTS，与 ESC 同一条规则）。
    原来这条规则只在 ESC 里有，路径状态由这里直接算，explain_plan 的反证范围一改成 NODE，
    stale_statistics 未排除时它就能把 missing_index 判成 REFUTED。"""
    if is_gate_predicate(binding.predicate_id):
        return PredicateResult.NEUTRAL.value
    if (binding.predicate_result == PredicateResult.REFUTES.value and
            contaminated_by(binding, live, edge_sources)):
        return PredicateResult.NEUTRAL.value
    return binding.predicate_result


def recompute_statuses(st: EpisodeState, *, now: float | None = None) -> None:
    explanation = st.explanation_graph
    if explanation is None:
        return
    current = time.time() if now is None else now
    node_results: dict[str, dict[str, set[str]]] = {}
    edge_results: dict[str, dict[str, set[str]]] = {}
    node_attempts: set[str] = set()
    edge_attempts: set[str] = set()
    graph = G.load()
    edge_sources: dict[str, set[str]] = {}
    for path in explanation.candidate_paths:
        for index, edge_id in enumerate(path.edge_ids):
            edge_sources.setdefault(edge_id, set()).add(path.node_ids[index])
    # 用进入时的路径状态算"尚未反证的污染源"；bind_evidence 会反复调用这里，
    # 一条路径被干净证据反证后，下一轮它就不再污染别的证据。
    live = live_invalidators(explanation)

    for binding in explanation.evidence_bindings.values():
        for node_id in binding.target_node_ids:
            if graph.nodes.get(node_id, {}).get("kind") == "Fix":
                continue
            confirms, refutes = _causal_relation(
                graph, node_id, binding, scope=EvidenceTargetKind.NODE.value)
            if not (confirms or refutes):
                continue
            node_attempts.add(node_id)
            if binding.is_trusted(now=current) and not window_spans_own_write(st, binding) and (
                    (binding.predicate_result == PredicateResult.SUPPORTS.value and
                     confirms) or
                    (binding.predicate_result == PredicateResult.REFUTES.value and
                     refutes) or
                    binding.predicate_result in {
                        PredicateResult.NEUTRAL.value,
                        PredicateResult.NOT_APPLICABLE.value,
                    }):
                node_results.setdefault(node_id, {}).setdefault(
                    binding.raw_ref, set()).add(_direction_result(binding, live, edge_sources))
        for edge_id in binding.target_edge_ids:
            directions = [
                _causal_relation(
                    graph, cause_id, binding,
                    scope="PATH")
                for cause_id in edge_sources.get(edge_id, set())
            ]
            confirms = any(item[0] for item in directions)
            refutes = any(item[1] for item in directions)
            if not (confirms or refutes):
                continue
            edge_attempts.add(edge_id)
            if binding.is_trusted(now=current) and not window_spans_own_write(st, binding) and (
                    (binding.predicate_result == PredicateResult.SUPPORTS.value and
                     confirms) or
                    (binding.predicate_result == PredicateResult.REFUTES.value and
                     refutes) or
                    binding.predicate_result in {
                        PredicateResult.NEUTRAL.value,
                        PredicateResult.NOT_APPLICABLE.value,
                    }):
                edge_results.setdefault(edge_id, {}).setdefault(
                    binding.raw_ref, set()).add(_direction_result(binding, live, edge_sources))

    for symptom_id in st.observed_symptom_ids:
        explanation.set_node_status(symptom_id, CausalStatus.SUPPORTED)
    for node_id in ({node for path in explanation.candidate_paths
                     for node in path.node_ids} - set(st.observed_symptom_ids)):
        results = [result for by_ref in node_results.get(node_id, {}).values()
                   for result in by_ref]
        explanation.set_node_status(
            node_id, _decision_status(results, attempted=node_id in node_attempts))
    for edge_id in {edge for path in explanation.candidate_paths
                    for edge in path.edge_ids}:
        results = [result for by_ref in edge_results.get(edge_id, {}).values()
                   for result in by_ref]
        explanation.set_edge_status(
            edge_id, _decision_status(results, attempted=edge_id in edge_attempts))

    for path in explanation.candidate_paths:
        segment_states = ([explanation.node_status.get(
            node_id, CausalStatus.UNTESTED.value) for node_id in path.node_ids[:-1]] +
            [explanation.edge_status.get(
                edge_id, CausalStatus.UNTESTED.value) for edge_id in path.edge_ids])
        required_supported = all(any(
            binding.evidence_type == evidence_type and
            binding.predicate_result == PredicateResult.SUPPORTS.value and
            binding.is_trusted(now=current) and not window_spans_own_write(st, binding)
            for binding_id in path.evidence_binding_ids
            if (binding := explanation.evidence_bindings.get(binding_id)) is not None)
            for evidence_type in path.required_evidence_types)
        if CausalStatus.REFUTED.value in segment_states:
            status = CausalStatus.REFUTED.value
        elif (segment_states and all(value == CausalStatus.SUPPORTED.value
                                     for value in segment_states) and
              required_supported):
            status = CausalStatus.SUPPORTED.value
        elif CausalStatus.INCONCLUSIVE.value in segment_states:
            status = CausalStatus.INCONCLUSIVE.value
        else:
            status = CausalStatus.UNTESTED.value
        explanation.set_path_status(path.path_id, status)

    _recompute_p0(explanation, current)


def _recompute_p0(explanation, now: float) -> None:
    graph = G.load()
    for cause_id, obligation in explanation.p0_obligations.items():
        bindings = []
        for binding in explanation.evidence_bindings.values():
            if cause_id not in binding.target_node_ids:
                continue
            _confirms, refutes = _causal_relation(
                graph, cause_id, binding,
                scope=EvidenceTargetKind.NODE.value)
            if (binding.evidence_type in obligation.required_evidence_types or
                    refutes):
                bindings.append(binding)
        binding_ids = [binding.binding_id for binding in bindings]
        by_type: dict[str, list[EvidenceBinding]] = {}
        for binding in bindings:
            by_type.setdefault(binding.evidence_type, []).append(binding)
        refuted = any(
            binding.is_trusted(now=now) and
            binding.predicate_result == PredicateResult.REFUTES.value and
            _causal_relation(
                graph, cause_id, binding,
                scope=EvidenceTargetKind.NODE.value)[1]
            for binding in bindings)
        supported = all(any(
            binding.is_trusted(now=now) and
            binding.predicate_result == PredicateResult.SUPPORTS.value
            for binding in by_type.get(evidence_type, []))
            for evidence_type in obligation.required_evidence_types)
        unavailable = any(binding.status != EvidenceStatus.OBSERVED.value
                          for binding in bindings)
        attempted = bool(bindings)
        if refuted:
            status, reason = ObligationStatus.REFUTED, "required predicate refuted P0"
        elif supported:
            status, reason = ObligationStatus.SUPPORTED, "all required P0 evidence supported"
        elif unavailable:
            status, reason = ObligationStatus.UNAVAILABLE, "required P0 evidence unavailable"
        elif attempted:
            status, reason = ObligationStatus.INCONCLUSIVE, "P0 evidence was inconclusive"
        elif obligation.status == ObligationStatus.UNAVAILABLE.value:
            # Planner/runtime capability failures may have no raw_ref.  Keep
            # the conservative obligation state until a later collection
            # actually supplies evidence; never silently reopen it as though
            # the availability failure had not happened.
            status = ObligationStatus.UNAVAILABLE
            reason = obligation.resolution_reason or "required P0 evidence unavailable"
        else:
            status, reason = ObligationStatus.OPEN, "P0 evidence has not been collected"
        explanation.resolve_p0(cause_id, status, reason=reason,
                               binding_ids=binding_ids)


def _path_score(path) -> float:
    return float(path.score_components.get("total", 0.0))


def select_minimal_explanation(st: EpisodeState) -> list[str]:
    explanation = st.explanation_graph
    if explanation is None:
        return []
    recompute_statuses(st)
    groups: list[list[Any]] = []
    for symptom_id in st.observed_symptom_ids:
        options = [path for path in explanation.candidate_paths
                   if path.observed_symptom_id == symptom_id and
                   path.status == CausalStatus.SUPPORTED.value]
        # A fully supported upstream extension subsumes its shorter suffix.
        # Keeping both would turn the downstream mechanism into an artificial
        # root merely because it uses fewer edges.
        options = [path for path in options if not any(
            len(other.node_ids) > len(path.node_ids) and
            other.node_ids[-len(path.node_ids):] == path.node_ids and
            other.edge_ids[-len(path.edge_ids):] == path.edge_ids
            for other in options)]
        if options:
            groups.append(sorted(options, key=lambda path: (
                -_path_score(path), len(path.edge_ids), path.path_id))[:4])

    best: tuple | None = None
    best_ids: list[str] = []
    if groups:
        combinations = itertools.product(*groups)
        for index, combo in enumerate(combinations):
            if index >= 4096:
                break
            ids = list(dict.fromkeys(path.path_id for path in combo))
            edge_count = len({edge for path in combo for edge in path.edge_ids})
            node_count = len({node for path in combo for node in path.node_ids})
            score = sum(_path_score(path) for path in combo)
            key = (edge_count, node_count, -score, tuple(ids))
            if best is None or key < best:
                best, best_ids = key, ids

    covered = {explanation.path_map()[path_id].observed_symptom_id
               for path_id in best_ids}
    unexplained = [symptom for symptom in explanation.observed_symptoms
                   if symptom not in covered]
    scope = (ExplanationScope.FULL if not unexplained else
             ExplanationScope.PARTIAL)
    explanation.select_paths(best_ids, unexplained_symptoms=unexplained,
                             scope=scope)
    sync_v1_projection(st)
    return best_ids


def sync_v1_projection(st: EpisodeState) -> None:
    explanation = st.explanation_graph
    if explanation is None:
        return
    roots = list(dict.fromkeys(path.root_node_id
                              for path in explanation.candidate_paths))
    st.hypothesis_candidates = roots
    st.ensure_hypotheses(roots)
    for root in roots:
        status = explanation.node_status.get(root, CausalStatus.UNTESTED.value)
        rooted_paths = [path for path in explanation.candidate_paths
                        if path.root_node_id == root]
        # v1 has no path/edge scope.  The least misleading compatibility
        # projection is root REFUTED only when every recalled path rooted at
        # that node has been explicitly closed.  A single refuted branch is
        # never enough to kill a root with another viable path.
        if (rooted_paths and all(path.status == CausalStatus.REFUTED.value
                                 for path in rooted_paths)):
            status = CausalStatus.REFUTED.value
        verdict = {
            CausalStatus.SUPPORTED.value: Verdict.CONFIRMED.value,
            CausalStatus.REFUTED.value: Verdict.REFUTED.value,
            CausalStatus.INCONCLUSIVE.value: Verdict.INCONCLUSIVE.value,
        }.get(status, Verdict.UNTESTED.value)
        if st.ledger[root].verdict != Verdict.REFUTED_BY_REMEDIATION.value:
            st.set_verdict(root, verdict, note="v2 explanation projection")

    selected_roots = explanation.derive_selected_root_causes()
    if selected_roots:
        st.claimed_fault_class = selected_roots[0]
        selected = [explanation.path_map()[path_id]
                    for path_id in explanation.selected_path_ids]
        st.claimed_root_cause = "; ".join(
            " -> ".join(path.node_ids) for path in selected)
    else:
        st.claimed_fault_class = None
        st.claimed_root_cause = None


def compact_projection(st: EpisodeState) -> dict:
    explanation = st.explanation_graph
    if explanation is None:
        return {"explanation_id": "", "revision": 0,
                "frontier": [], "needs": []}
    frontier = G.path_frontier(explanation)
    needs = _current_esc_needs(st, explanation)
    need_source = "esc" if needs else "frontier"
    # ESC has already reduced the full frontier to the concrete gaps blocking
    # this explanation.  Feed those typed needs into the next INVESTIGATE
    # round while the report still matches this exact revision.  Falling back
    # to the global first page here can starve lower-ranked discriminators as
    # recall grows and repeatedly collect unrelated evidence.
    if not needs:
        needs = G.evidence_needs(explanation)
    return {
        "explanation_id": explanation.explanation_id,
        "revision": explanation.revision,
        "frontier": frontier[:12],
        "needs": [need.to_dict() for need in needs[:20]],
        "need_source": need_source,
        "selected_path_ids": list(explanation.selected_path_ids),
        "scope": explanation.scope,
    }


def assess_explanation(st: EpisodeState) -> dict:
    """Compatibility wrapper for callers introduced during the v2 migration."""
    from agent.esc import check_explanation
    return check_explanation(st, persist=False)


def effective_but_insufficient_fixes(st: EpisodeState) -> set[str]:
    """本 episode 里执行成功、预期效果全部达成、症状却没恢复的修复（fix id）。

    再执行一次同一个修复不会带来新信息：2026-09-23 stale_statistics 第一次 ANALYZE 把
    估计偏差从 440 倍压到 1.0（效果完美），p50 没恢复，回 INVESTIGATE 后模型又提交了
    VACUUM ANALYZE（同一个 fix），偏差 1.25→1.25，判 INTERVENTION 失败，白白多一次写库、
    耗尽尝试次数。效果达成却未恢复说明解释不完整，该换修复或升级，不该重试。

    会话控制除外：终止另一组 pid 是另一次干预，不是重复（完全相同的 SQL 已由
    EpisodeState.tried_fix 挡住）。"""
    fixes: dict[str, dict] = {}
    for root in G.load().nodes:
        for fix in G.fixes_for(root):
            fixes.setdefault(fix["fix"], fix)
    out: set[str] = set()
    for attempt in getattr(st, "intervention_attempts", []) or []:
        get = (attempt.get if isinstance(attempt, dict)
               else lambda name, default=None: getattr(attempt, name, default))
        actual = list(get("actual") or [])
        if (str(get("execution_status") or "") == "SUCCEEDED" and actual and
                all(item.get("met") is True for item in actual) and
                str(get("outcome") or "") != "VERIFIED" and
                fixes.get(str(get("fix_id") or ""), {}).get("action_type") != "session_control"):
            out.add(str(get("fix_id")))
    return out


def intervention_options(st: EpisodeState, *, executable_only: bool = False
                         ) -> list[dict]:
    explanation = st.explanation_graph
    if explanation is None:
        return []
    exhausted = effective_but_insufficient_fixes(st) if executable_only else set()
    options: list[dict] = []
    for path_id in explanation.selected_path_ids:
        for option in G.intervention_options(path_id, explanation):
            downstream = G.downstream_on_path(
                path_id, option["target_node_id"], explanation)
            bounded_effects = [node_id for node_id in
                               option.get("expected_effect_nodes", [])
                               if node_id in downstream]
            # 预期效果标了 node 的，只保留证明本路径上节点（干预目标或其下游）的那几条；
            # 没标 node 的照旧全部保留。
            keep_nodes = set(bounded_effects) | {option["target_node_id"]}
            path_effects = [effect for effect in option.get("expected_effects", []) or []
                            if not effect.get("node") or effect.get("node") in keep_nodes]
            item = {**option, "expected_effect_nodes": bounded_effects,
                    "expected_effects": path_effects}
            manual = (item.get("execution") == "escalate_only" or
                      item.get("manual") or
                      item.get("intervention_kind") == "MANUAL")
            if not manual and (not bounded_effects or
                               not item.get("expected_effects")):
                # A fix may be valid for the node in general but make no
                # defensible prediction for this concrete path's downstream.
                continue
            if executable_only and (item.get("execution") == "escalate_only" or
                                    item.get("risk_tier") == "DENY"):
                continue
            if executable_only and item.get("fix") in exhausted:
                continue
            options.append(item)
    return options


def _sql_facts(sql: str) -> dict[str, Any]:
    facts: dict[str, Any] = {"statement": None, "table": "", "pid": None,
                             "pids": [], "index_signature": None, "parameter": ""}
    try:
        parsed = parse_sql(sql)
    except Exception:
        return facts
    if len(parsed) != 1:
        return facts
    statement = parsed[0].stmt
    facts["statement"] = statement
    if isinstance(statement, ast.IndexStmt):
        table = str(getattr(statement.relation, "relname", "") or "")
        schema = str(getattr(statement.relation, "schemaname", "") or "")
        columns = tuple(RawStream()(item)
                        for item in statement.indexParams or ())
        included = tuple(RawStream()(item)
                         for item in statement.indexIncludingParams or ())
        predicate = (RawStream()(statement.whereClause)
                     if statement.whereClause is not None else "")
        facts.update({"table": table,
                      "index_signature": (
                          schema, table, str(statement.accessMethod or "btree"),
                          bool(statement.unique), columns, included, predicate)})
    elif isinstance(statement, ast.VacuumStmt):
        relations = statement.rels or ()
        if relations:
            facts["table"] = str(
                getattr(relations[0].relation, "relname", "") or "")
    elif isinstance(statement, ast.AlterTableStmt):
        facts["table"] = str(getattr(statement.relation, "relname", "") or "")
    elif isinstance(statement, ast.VariableSetStmt):
        facts["parameter"] = str(statement.name or "")
    elif isinstance(statement, ast.SelectStmt):
        # pid 只在语句形态合规时才算绑定：恰好一条 SELECT pg_terminate_backend(<常量>)。
        # 原来只要目标列表里有一个字面量 pid 就算，
        # `..., pg_terminate_backend(pid) FROM pg_stat_activity` 的第二项会掐掉整库会话
        # （2026-09-23 审计）。判定与 gate.assess 共用 shield 的同一个函数。
        from safety import shield
        shape_ok, _reasons, pids = shield.inspect_session_control(sql)
        if shape_ok and pids:
            facts["pids"] = list(pids)
            facts["pid"] = pids[0]
    return facts


_TABLE_KEYS = ("table", "relname", "relation", "table_name", "tablename")


def _binding_tables(bindings) -> dict[str, set[str]]:
    """每条可信绑定的结构化值里出现过的表名（小写、去 schema）。
    执行计划的 scan_types 形如 "Seq Scan on orders"，也算。"""
    out: dict[str, set[str]] = {}
    for binding in bindings:
        names: set[str] = set()
        for row in _walk_dicts(binding.structured_value()):
            for key in _TABLE_KEYS:
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    names.add(value.strip().split(".")[-1].lower())
            for item in row.get("scan_types") or []:
                if isinstance(item, str) and " on " in item:
                    names.add(item.rsplit(" on ", 1)[-1].strip().lower())
        if names:
            out[binding.raw_ref] = names
    return out


# pid 行的新鲜度：pid 会被回收复用，终止会话前必须是刚观测到的行。模型在 PLAN 里调
# get_active_sessions(include_idle=true) 到 submit_proposal 只隔几秒，300s 足够宽。
PID_ROW_FRESHNESS_S = 300
_PID_EVIDENCE_TYPES = frozenset({"session_wait_profile", "lock_blocking_chain",
                                 "idle_in_transaction"})


def _fresh_observations(st: EpisodeState, evidence_types, freshness_s: float
                        ) -> list[EvidenceBinding]:
    """scratchpad 里新鲜、可信的观测，临时构造成绑定（不写进解释图、不动 revision）。

    绑定会 bump 解释图 revision，而 GATE 要求 ESC 的 SUFFICIENT 报告与当前 revision 一致，
    所以绑定只在 INVESTIGATE/DIAGNOSE 做；模型在 PLAN 阶段取到的观测（idle 会话行、
    simulate_index 的反事实）永远进不了绑定，前置条件永远不满足 —— 2026-09-23 跑批：
    terminate 类修复从未过门，missing_index 场景 simulate_index 三次 would_be_used=True 仍
    四次被 "concrete_index_definition_bound, counterfactual_index_v2" 拒掉。观测是系统产出
    的（toolbox 落盘、trace 有 digest），按同一条信任规则（EvidenceBinding.is_trusted：
    状态、raw_ref、digest、新鲜度）临时构造绑定来查。"""
    now = time.time()
    wanted = set(evidence_types)
    out: list[EvidenceBinding] = []
    for entry in reversed(st.scratchpad):
        if entry.get("evidence_type") not in wanted or not entry.get("raw_ref"):
            continue
        observed_at = float(entry.get("ts", 0.0) or 0.0)
        if now - observed_at > freshness_s:
            continue
        binding = EvidenceBinding.create(
            episode_id=st.episode_id, raw_ref=str(entry["raw_ref"]),
            evidence_type=str(entry.get("evidence_type")),
            status=str(entry.get("status", EvidenceStatus.OBSERVED.value)),
            observed_at=observed_at, predicate_id="", predicate_result="NEUTRAL",
            structured_value=entry.get("structured_value"),
            fresh_until=observed_at + freshness_s)
        if binding.is_trusted():
            out.append(binding)
    return out


def _fresh_pid_observations(st: EpisodeState, pid) -> list[tuple[EvidenceBinding, dict]]:
    """含该 pid 的新鲜（PID_ROW_FRESHNESS_S 内）可信会话观测行。pid 会被回收复用，所以
    这里的新鲜度比一般证据严得多。"""
    if pid is None:
        return []
    out: list[tuple[EvidenceBinding, dict]] = []
    for binding in _fresh_observations(st, _PID_EVIDENCE_TYPES, PID_ROW_FRESHNESS_S):
        for row in _walk_dicts(binding.structured_value()):
            if str(row.get("pid", row.get("blocked_by"))) == str(pid):
                out.append((binding, row))
    return out


def _evidence_type_of_predicate(predicate_id: str) -> str:
    for node_id, data in G.load().nodes(data=True):
        if data.get("kind") == "Evidence" and str(data.get("predicate_id") or "") == predicate_id:
            return str(node_id)
    return ""


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_dicts(child)


def _binding_relevant(binding: EvidenceBinding, *, target_kind: str,
                      target: str, fix_id: str, path) -> bool:
    if target_kind == EvidenceTargetKind.INTERVENTION.value:
        return fix_id in binding.target_node_ids
    if target_kind == "PATH":
        return bool(set(path.node_ids).intersection(binding.target_node_ids) or
                    set(path.edge_ids).intersection(binding.target_edge_ids))
    return target in binding.target_node_ids


def _plan_bindings(st: EpisodeState, path, *, target: str,
                   fix_id: str) -> list[EvidenceBinding]:
    explanation = st.explanation_graph
    if explanation is None:
        return []
    adjacent_index = path.node_ids.index(target)
    adjacent_edges = set(
        path.edge_ids[max(0, adjacent_index - 1):adjacent_index + 1])
    result = []
    for binding in explanation.evidence_bindings.values():
        relevant = bool(
            {target, fix_id}.intersection(binding.target_node_ids) or
            adjacent_edges.intersection(binding.target_edge_ids))
        if relevant and binding.is_trusted():
            result.append(binding)
    return result


def _evaluate_preconditions(st: EpisodeState, *, option: dict,
                            sql: str) -> list[dict]:
    explanation = st.explanation_graph
    if explanation is None:
        return []
    path = explanation.path_map()[option["path_id"]]
    target = option["target_node_id"]
    fix_id = option["fix"]
    bindings = [binding for binding in explanation.evidence_bindings.values()
                if binding.is_trusted()]
    values = [(binding, binding.structured_value()) for binding in bindings]
    facts = _sql_facts(sql) if sql.strip() else _sql_facts("")
    pids = list(facts.get("pids") or [])
    # 每个 pid 各自的观测行（已绑定的 + PLAN 阶段新鲜可信的）。多 pid 提案要求每个 pid
    # 都满足同一条前置条件 —— 任何一个不满足，整条提案不满足。
    rows_by_pid: dict[str, list[tuple[EvidenceBinding, dict]]] = {
        str(item): [] for item in pids}
    for binding, value in values:
        for row in _walk_dicts(value):
            row_pid = str(row.get("pid", row.get("blocked_by")))
            if row_pid in rows_by_pid:
                rows_by_pid[row_pid].append((binding, row))
    for item in pids:
        rows_by_pid[str(item)].extend(_fresh_pid_observations(st, item))

    def per_pid(check_row, label: str) -> tuple[bool, str, list[str]]:
        if not pids:
            return False, "SQL AST binds no backend PID", []
        refs: list[str] = []
        missing: list[str] = []
        for key, rows in rows_by_pid.items():
            hits = [binding.raw_ref for binding, row in rows if check_row(binding, row)]
            if hits:
                refs.extend(hits)
            else:
                missing.append(key)
        if missing:
            return (False, f"{label} fails for pid {', '.join(missing[:10])}"
                    + (f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""), [])
        return True, f"{label} holds for all {len(pids)} pid(s)", refs

    connection_pressure_refs = [
        binding.raw_ref for binding in bindings
        if binding.predicate_id == "connection_count_v2" and
        binding.predicate_result == PredicateResult.SUPPORTS.value]

    fresh_counterfactuals = _fresh_observations(
        st, {"counterfactual_index"},
        float(G.load().nodes.get("counterfactual_index", {}).get(
            "freshness_seconds", DEFAULT_FRESHNESS_S)))

    relevant = _plan_bindings(st, path, target=target, fix_id=fix_id)
    evidence_tables = _binding_tables(relevant) or _binding_tables(bindings)

    def table_matches_evidence(table: str, why: str) -> tuple[bool, str, list[str]]:
        # 目标表必须是证据里出现过的表。原来只要求"绑了某张表"，`ANALYZE customers`
        # 也能在 orders 的统计过期证据上过门（2026-09-23 审计）。证据里没有任何表名
        # （库级证据）时退回只查 AST 绑定，并把这一点写进 reason。
        if not table:
            return False, "SQL AST binds no concrete table", []
        if not evidence_tables:
            return True, f"{why} (evidence exposes no table name; AST-bound only)", []
        wanted = table.split(".")[-1].lower()
        refs = [ref for ref, names in evidence_tables.items() if wanted in names]
        if refs:
            return True, f"{why}; table {table} appears in trusted evidence", refs
        known = sorted(set().union(*evidence_tables.values()))
        return False, f"table {table} is not among evidence tables {known}", []

    def structural(condition_id: str) -> tuple[bool, str, list[str]]:
        statement = facts["statement"]
        if condition_id == "concrete_index_definition_bound":
            signature = facts["index_signature"]
            matching_refs = []
            candidates = [(binding, value) for binding, value in values
                          if binding.predicate_id == "counterfactual_index_v2"]
            # PLAN 阶段刚做的 simulate_index 还没绑定，也算（同一条信任规则）
            candidates += [(binding, binding.structured_value())
                           for binding in fresh_counterfactuals]
            for binding, value in candidates:
                simulated = _sql_facts(str((value or {}).get("create_sql", "")))
                if signature and simulated["index_signature"] == signature:
                    matching_refs.append(binding.raw_ref)
            return (bool(signature and matching_refs),
                    "proposed index table/columns match the counterfactual trace",
                    matching_refs)
        if condition_id == "concrete_table_bound":
            return table_matches_evidence(facts["table"],
                                          "SQL AST binds one concrete table")
        if condition_id in {"table_is_vacuumable",
                            "target_database_or_table_is_vacuumable"}:
            if not isinstance(statement, ast.VacuumStmt):
                return False, "statement is not VACUUM/ANALYZE", []
            return table_matches_evidence(facts["table"],
                                          "VACUUM AST binds a concrete relation")
        if condition_id == "concrete_pid_bound":
            return (bool(pids), f"SQL AST binds {len(pids)} positive backend PID(s)"
                    if pids else "SQL AST binds no backend PID", [])
        if condition_id == "session_or_transaction_scope_only":
            ok = isinstance(statement, ast.VariableSetStmt)
            return ok, "SET is scoped to the executing session/transaction", []
        if condition_id == "pid_identity_rechecked_fresh":
            # 原来只读行上的 identity_rechecked，而观测器恒写 True，这条断言实际是常量
            # （2026-09-23 审计）。现在"新鲜"是真的：该 pid 的行必须在 PID_ROW_FRESHNESS_S
            # 内观测到。
            now = time.time()
            return per_pid(
                lambda binding, row: bool(row.get("identity_rechecked", True)) and
                now - float(binding.observed_at or 0.0) <= PID_ROW_FRESHNESS_S,
                f"PID row observed within {PID_ROW_FRESHNESS_S}s")

        checks = {
            "pid_is_topmost_blocker": lambda row: bool(
                row.get("is_topmost_blocker")),
            "pid_state_idle_in_transaction": lambda row: str(
                row.get("state", "")).lower() == "idle in transaction",
            "transaction_age_bound": lambda row: any(
                row.get(key) is not None for key in
                ("transaction_age_seconds", "xact_age_seconds", "xact_age")),
            "database_role_bound": lambda row: bool(
                row.get("role") or row.get("usename") or row.get("user")),
            # 危害三选一：持锁挡人、持快照挡 vacuum、占连接槽。最后一种要求可信证据证明连接
            # 已逼近上限（connection_count_v2 SUPPORTS），而不是任何空闲事务都能杀。
            "session_impact_bound": lambda row: bool(
                row.get("blocking_impact") or row.get("blocked_session_count") or
                row.get("backend_xmin") or row.get("xmin_age") or
                (connection_pressure_refs and
                 str(row.get("state", "")).lower().startswith("idle in transaction"))),
            "pid_is_client_backend_and_state_idle": lambda row: (
                str(row.get("backend_type", "client backend")).lower() ==
                "client backend" and str(row.get("state", "")).lower() == "idle"),
            "pid_is_not_current_diagnostic_connection": lambda row: not bool(
                row.get("is_current_diagnostic_connection", True)),
            "role_is_not_system_or_diagnostic": lambda row: bool(
                (row.get("role") or row.get("usename")) and
                not row.get("is_system_or_diagnostic", True)),
        }
        check = checks.get(condition_id)
        if check is None:
            return False, f"no deterministic evaluator for {condition_id}", []
        ok, reason, refs = per_pid(lambda _binding, row: check(row),
                                   f"trace-bound PID facts satisfy {condition_id}")
        if ok and condition_id == "session_impact_bound" and connection_pressure_refs:
            refs = list(refs) + connection_pressure_refs
        return ok, reason, refs

    results: list[dict] = []
    for condition in option.get("preconditions", []):
        required = bool(condition.get("required", True))
        predicate_id = str(condition.get("predicate_id") or "")
        if predicate_id:
            wanted = str(condition.get("result") or PredicateResult.SUPPORTS.value)
            target_kind = str(condition.get("target_kind") or
                              EvidenceTargetKind.NODE.value)
            scoped_target = str(condition.get("target_id") or target)
            matched = [
                binding for binding in bindings
                if binding.predicate_id == predicate_id and
                binding.predicate_result == wanted and
                _binding_relevant(binding, target_kind=target_kind,
                                  target=scoped_target, fix_id=fix_id, path=path)
            ]
            refs = [binding.raw_ref for binding in matched]
            if not matched:
                # 没有绑定时看 PLAN 阶段的新鲜观测：按同一判据、同一目标现场求值。
                # 反事实证据还要求模拟的就是本提案这条索引，否则任何一次 simulate_index
                # 都能替所有索引定义背书。
                evidence_type = _evidence_type_of_predicate(predicate_id)
                freshness = float(G.load().nodes.get(evidence_type, {}).get(
                    "freshness_seconds", DEFAULT_FRESHNESS_S)) if evidence_type else 0.0
                for binding in (_fresh_observations(st, {evidence_type}, freshness)
                                if evidence_type else []):
                    value = binding.structured_value()
                    if evidence_type == "counterfactual_index":
                        simulated = _sql_facts(str((value or {}).get("create_sql", "")))
                        if not facts["index_signature"] or                                 simulated["index_signature"] != facts["index_signature"]:
                            continue
                    decision = evaluate(predicate_id, value, context=PredicateContext(
                        target_kind=target_kind,
                        target_ids=((fix_id,) if target_kind ==
                                    EvidenceTargetKind.INTERVENTION.value
                                    else (scoped_target,)),
                        collection_status=binding.status))
                    if decision.result == wanted:
                        refs.append(binding.raw_ref)
            satisfied = bool(refs)
            reason = (f"{predicate_id} has a fresh {wanted} binding" if satisfied
                      else f"{predicate_id} lacks a fresh scoped {wanted} binding")
            condition_id = predicate_id
        else:
            condition_id = str(condition.get("id") or "")
            satisfied, reason, refs = structural(condition_id)
        results.append({
            "condition_id": condition_id,
            "required": required,
            "satisfied": bool(satisfied),
            "reason": reason,
            "evidence_refs": list(dict.fromkeys(refs)),
        })
    return results


def create_intervention_plan(st: EpisodeState, *, action_type: str, sql: str,
                             rollback: str, rationale: str,
                             selected_path_id: str = "", fix_id: str = "",
                             intervention_target: str = "") -> InterventionPlan:
    explanation = st.explanation_graph
    if explanation is None or not explanation.selected_path_ids:
        raise ValueError("an explanation path must be selected before planning")
    options = intervention_options(st, executable_only=True)
    matches = [option for option in options
               if option.get("action_type") == action_type]
    if selected_path_id:
        matches = [option for option in matches
                   if option.get("path_id") == selected_path_id]
    if fix_id:
        matches = [option for option in matches if option.get("fix") == fix_id]
    if intervention_target:
        matches = [option for option in matches
                   if option.get("target_node_id") == intervention_target]
    if len(matches) != 1:
        raise ValueError(f"intervention intent is ambiguous or illegal: {len(matches)} matches")
    option = matches[0]
    precondition_results = _evaluate_preconditions(st, option=option, sql=sql)
    unmet = [result["condition_id"] for result in precondition_results
             if result["required"] and not result["satisfied"]]
    if unmet:
        # 光说哪条不满足不够：2026-09-22 实测模型对 concrete_pid_bound 连试三次都不知道
        # pid 该从哪来（会话工具默认不带 idle）。把"怎么满足"一起告诉它。
        hint = ""
        if any(c in unmet for c in ("concrete_pid_bound",
                                    "pid_is_client_backend_and_state_idle")):
            hint = ("；pid 必须来自已观测到的会话行：先调用 "
                    "get_active_sessions(include_idle=true) 拿到具体 idle 客户端 pid，"
                    "再在 SQL 里写死那个 pid")
        raise ValueError(
            f"intervention preconditions are not satisfied: {', '.join(unmet)}{hint}")
    path = explanation.path_map()[option["path_id"]]
    evidence_refs = [binding.raw_ref for binding in _plan_bindings(
        st, path, target=option["target_node_id"], fix_id=option["fix"])]
    plan = InterventionPlan.create(
        explanation_id=explanation.explanation_id,
        explanation_revision=explanation.revision,
        selected_path_id=option["path_id"],
        intervention_target=option["target_node_id"],
        fix_id=option["fix"],
        intervention_kind=option["intervention_kind"],
        action_type=action_type,
        sql=sql,
        rollback=rollback,
        execution=option.get("execution", "gated"),
        manual=bool(option.get("manual", False)),
        preconditions=option.get("preconditions", []),
        precondition_results=precondition_results,
        evidence_refs=evidence_refs,
        expected_effect_nodes=option.get("expected_effect_nodes", []),
        expected_effects=option.get("expected_effects", []),
        rationale=rationale,
    )
    st.intervention_plan = plan
    st.causal_gate_context = None
    return plan


def create_manual_intervention_plan(st: EpisodeState) -> InterventionPlan:
    """Persist an evidence-bound escalation plan without producing SQL."""
    explanation = st.explanation_graph
    if explanation is None or not explanation.selected_path_ids:
        raise ValueError("an explanation path must be selected before escalation")
    options = [option for option in intervention_options(st)
               if option.get("execution") == "escalate_only" or
               option.get("manual") or
               option.get("intervention_kind") == "MANUAL"]
    if not options:
        raise ValueError("selected explanation has no manual intervention")
    option = sorted(options, key=lambda item: (
        item["path_id"], item["target_node_id"], item["fix"]))[0]
    results = _evaluate_preconditions(st, option=option, sql="")
    path = explanation.path_map()[option["path_id"]]
    evidence_refs = [binding.raw_ref for binding in _plan_bindings(
        st, path, target=option["target_node_id"], fix_id=option["fix"])]
    plan = InterventionPlan.create(
        explanation_id=explanation.explanation_id,
        explanation_revision=explanation.revision,
        selected_path_id=option["path_id"],
        intervention_target=option["target_node_id"],
        fix_id=option["fix"],
        intervention_kind=option.get("intervention_kind", "MANUAL"),
        action_type=option.get("action_type", "manual_procedure"),
        sql="", rollback=option.get("rollback", "IRREVERSIBLE"),
        execution="escalate_only", manual=True,
        preconditions=option.get("preconditions", []),
        precondition_results=results, evidence_refs=evidence_refs,
        expected_effect_nodes=option.get("expected_effect_nodes", []),
        expected_effects=option.get("expected_effects", []),
        rationale=(f"manual escalation for {option['fix']}; unresolved "
                   "preconditions remain explicit"),
    )
    st.intervention_plan = plan
    st.causal_gate_context = None
    return plan


def build_gate_context(st: EpisodeState, *,
                       model_payload: dict | None = None) -> CausalGateContext:
    explanation = st.explanation_graph
    plan = st.intervention_plan
    if explanation is None or plan is None:
        raise CausalGateError(
            "explanation and intervention plan are required",
            reason_code="CAUSAL_BINDING_INVALID", retry_phase="PLAN")
    if explanation.graph_version != G.graph_version():
        raise CausalGateError(
            "explanation graph version is stale",
            reason_code="STALE_EXPLANATION", retry_phase="INVESTIGATE")
    if (plan.explanation_id != explanation.explanation_id or
            plan.explanation_revision != explanation.revision):
        raise CausalGateError(
            "intervention plan is stale for this explanation",
            reason_code="STALE_EXPLANATION", retry_phase="INVESTIGATE")
    reports = [report for report in st.esc_reports
               if report.get("verdict") == "SUFFICIENT" and
               report.get("explanation_id") == explanation.explanation_id and
               report.get("explanation_revision") == explanation.revision and
               report.get("graph_version", explanation.graph_version) ==
               explanation.graph_version]
    if not reports:
        raise CausalGateError(
            "no current sufficient ESC report",
            reason_code="EVIDENCE_MISSING", retry_phase="INVESTIGATE")
    report_id = reports[-1].get("esc_report_id") or reports[-1].get("report_id")
    if not report_id:
        raise CausalGateError(
            "current sufficient ESC report has no esc_report_id",
            reason_code="EVIDENCE_MISSING", retry_phase="INVESTIGATE")
    try:
        context = CausalGateContext.build(
            explanation, plan, report_id, model_payload=model_payload)
    except ValueError as exc:
        message = str(exc)
        stale = "stale" in message
        raise CausalGateError(
            message,
            reason_code=("STALE_EXPLANATION" if stale else
                         "CAUSAL_BINDING_INVALID"),
            retry_phase=("INVESTIGATE" if stale else "PLAN")) from exc
    if context.unresolved_p0_paths:
        raise CausalGateError(
            "unresolved P0 evidence paths",
            reason_code="P0_MANUAL_REQUIRED", retry_phase="ESCALATE")
    if not context.evidence_refs:
        expired = any(
            plan.intervention_target in binding.target_node_ids and
            not binding.is_fresh()
            for binding in explanation.evidence_bindings.values())
        raise CausalGateError(
            "intervention target has no fresh trusted evidence",
            reason_code=("EVIDENCE_EXPIRED" if expired else "EVIDENCE_MISSING"),
            retry_phase="INVESTIGATE")
    options = [option for option in intervention_options(st)
               if option["path_id"] == plan.selected_path_id and
               option["target_node_id"] == plan.intervention_target and
               option["fix"] == plan.fix_id]
    if len(options) != 1:
        raise CausalGateError(
            "selected path, target and fix are no longer bound",
            reason_code="CAUSAL_BINDING_INVALID", retry_phase="PLAN")
    option = options[0]
    graph_owned = {
        "action_type": option.get("action_type"),
        "intervention_kind": option.get("intervention_kind"),
        "expected_effect_nodes": option.get("expected_effect_nodes", []),
        "expected_effects": option.get("expected_effects", []),
    }
    plan_owned = {
        "action_type": plan.action_type,
        "intervention_kind": plan.intervention_kind,
        "expected_effect_nodes": plan.expected_effect_nodes,
        "expected_effects": plan.expected_effects,
    }
    if graph_owned != plan_owned:
        raise CausalGateError(
            "intervention plan conflicts with graph-owned fix semantics",
            reason_code="CAUSAL_BINDING_INVALID", retry_phase="PLAN")
    results = _evaluate_preconditions(st, option=option, sql=plan.sql)
    unmet = [result["condition_id"] for result in results
             if result["required"] and not result["satisfied"]]
    if unmet:
        predicate_ids = {str(item.get("predicate_id") or "")
                         for item in option.get("preconditions", [])}
        evidence_gap = any(condition in predicate_ids for condition in unmet)
        raise CausalGateError(
            f"intervention preconditions are not satisfied: {', '.join(unmet)}",
            reason_code="PRECONDITION_FAILED",
            retry_phase=("INVESTIGATE" if evidence_gap else "PLAN"))
    st.causal_gate_context = context
    return context


def _binding_projection(binding: EvidenceBinding) -> dict:
    return {
        "binding_id": binding.binding_id,
        "evidence_type": binding.evidence_type,
        "status": binding.status,
        "predicate_id": binding.predicate_id,
        "predicate_result": binding.predicate_result,
        "target_node_ids": list(binding.target_node_ids),
        "target_edge_ids": list(binding.target_edge_ids),
        "summary": binding.summary,
        "raw_ref": binding.raw_ref,
        "observed_at": binding.observed_at,
        "fresh_until": binding.fresh_until,
    }


def _path_report(explanation, path) -> dict:
    bindings = [explanation.evidence_bindings[binding_id]
                for binding_id in path.evidence_binding_ids
                if binding_id in explanation.evidence_bindings]
    nodes = []
    for node_id in path.node_ids:
        node_evidence = [_binding_projection(binding) for binding in bindings
                         if node_id in binding.target_node_ids]
        nodes.append({
            "node_id": node_id,
            "role": path.node_roles[node_id],
            "status": explanation.node_status.get(
                node_id, CausalStatus.UNTESTED.value),
            "evidence": node_evidence,
        })
    segments = []
    for index, edge_id in enumerate(path.edge_ids):
        edge_evidence = [_binding_projection(binding) for binding in bindings
                         if edge_id in binding.target_edge_ids]
        segments.append({
            "edge_id": edge_id,
            "from": path.node_ids[index],
            "to": path.node_ids[index + 1],
            "status": explanation.edge_status.get(
                edge_id, CausalStatus.UNTESTED.value),
            "evidence": edge_evidence,
        })
    return {
        "path_id": path.path_id,
        "chain": list(path.node_ids),
        "root_node_id": path.root_node_id,
        "observed_symptom_id": path.observed_symptom_id,
        "status": path.status,
        "nodes": nodes,
        "segments": segments,
    }


def _alternative_report(explanation, path) -> dict:
    relevant = []
    for binding_id in path.evidence_binding_ids:
        binding = explanation.evidence_bindings.get(binding_id)
        if (binding is not None and
                binding.predicate_result in {
                    PredicateResult.SUPPORTS.value,
                    PredicateResult.REFUTES.value,
                }):
            relevant.append(_binding_projection(binding))
    return {
        "path_id": path.path_id,
        "chain": list(path.node_ids),
        "status": path.status,
        "distinguishing_evidence": relevant,
    }


def _manual_report(st: EpisodeState, option: dict) -> dict:
    results = _evaluate_preconditions(st, option=option, sql="")
    unmet = [result for result in results
             if result["required"] and not result["satisfied"]]
    explanation = st.explanation_graph
    path = explanation.path_map()[option["path_id"]] if explanation else None
    refs = ([binding.raw_ref for binding in _plan_bindings(
        st, path, target=option["target_node_id"], fix_id=option["fix"])]
        if path is not None else [])
    return {
        "path_id": option["path_id"],
        "intervention_target": option["target_node_id"],
        "fix_id": option["fix"],
        "intervention_kind": option.get("intervention_kind", "MANUAL"),
        "action_type": option.get("action_type", "manual_procedure"),
        "execution": "escalate_only",
        "sql": "",
        "evidence_refs": list(dict.fromkeys(refs)),
        "unmet_preconditions": unmet,
        "owner_information_required": [
            result["condition_id"] for result in unmet],
        "expected_effect_nodes": option.get("expected_effect_nodes", []),
        "description": option.get("desc", ""),
    }


def _p0_report(explanation=None) -> list[dict]:
    graph = G.load()
    p0_ids = sorted(node_id for node_id, data in graph.nodes(data=True)
                    if data.get("severity") == "P0")
    rows = []
    for cause_id in p0_ids:
        obligation = (explanation.p0_obligations.get(cause_id)
                      if explanation is not None else None)
        binding_ids = (obligation.evidence_binding_ids if obligation else [])
        rows.append({
            "cause_id": cause_id,
            "reachable": obligation is not None,
            "reachable_path_ids": (list(obligation.reachable_path_ids)
                                   if obligation else []),
            "status": (obligation.status if obligation else
                       "NOT_EVALUATED" if explanation is None else
                       "NOT_REACHABLE"),
            "resolution_reason": (obligation.resolution_reason
                                  if obligation else
                                  "explanation recall was not completed"
                                  if explanation is None else
                                  "not reachable from the observed symptoms"),
            "truncated": bool(obligation.truncated) if obligation else False,
            "evidence": [_binding_projection(
                explanation.evidence_bindings[binding_id])
                for binding_id in binding_ids
                if binding_id in explanation.evidence_bindings],
        })
    return rows


def final_report(st: EpisodeState, *, escalated: bool) -> dict:
    explanation = st.explanation_graph
    if explanation is None:
        attempts = [attempt.__dict__.copy()
                    for attempt in st.intervention_attempts]
        return {
            "kind": "ESCALATION" if escalated else "REPORT",
            "reason": st.outcome_note,
            "observed_symptoms": list(st.symptoms),
            "unmapped_observed_symptoms": list(st.unmapped_symptoms),
            "paths": [], "selected_paths": [],
            "selected_root_causes": [], "key_evidence": [],
            "alternative_paths": [], "open_branches": [],
            "unexplained_symptoms": list(st.symptoms),
            "p0_obligations": {}, "p0_matrix": _p0_report(),
            "missing_evidence": [], "manual_options": [],
            "intervention": None,
            "intervention_attempts": attempts,
            "verification": dict(st.verification_result),
            "rollback": dict(st.rollback_decision),
            "answers": {
                "why_this_chain": [], "why_not_alternatives": [],
                "intervention_location": {},
                "effect_proof": [effect for attempt in attempts
                                 for effect in attempt.get("actual", [])],
            },
        }
    paths = explanation.path_map()
    selected = [paths[path_id] for path_id in explanation.selected_path_ids]
    needs = G.evidence_needs(explanation)
    manual_options = [option for option in intervention_options(st)
                      if option.get("execution") == "escalate_only" or
                      option.get("intervention_kind") == "MANUAL"]
    manual = [_manual_report(st, option) for option in manual_options]
    selected_ids = set(explanation.selected_path_ids)
    alternatives = [
        _alternative_report(explanation, path)
        for path in explanation.candidate_paths
        if path.path_id not in selected_ids and
        any(path.observed_symptom_id == chosen.observed_symptom_id
            for chosen in selected)
    ]
    open_branches = [
        _alternative_report(explanation, path)
        for path in explanation.candidate_paths
        if path.path_id not in selected_ids and path.status in {
            CausalStatus.UNTESTED.value,
            CausalStatus.INCONCLUSIVE.value,
        }
    ]
    selected_binding_ids = list(dict.fromkeys(
        binding_id for path in selected
        for binding_id in path.evidence_binding_ids))
    selected_evidence = [
        _binding_projection(explanation.evidence_bindings[binding_id])
        for binding_id in selected_binding_ids
        if binding_id in explanation.evidence_bindings and
        explanation.evidence_bindings[binding_id].predicate_result in {
            PredicateResult.SUPPORTS.value,
            PredicateResult.REFUTES.value,
        }
    ]
    intervention = (
        st.intervention_plan.to_dict() if st.intervention_plan else
        st.rollback_decision.get("intervention_plan") or
        st.last_gate_denial.get("intervention_plan")
    )
    attempts = [attempt.__dict__.copy()
                for attempt in st.intervention_attempts]
    chain_answers = [{
        "path_id": path.path_id,
        "chain": list(path.node_ids),
        "supported_edge_ids": [edge_id for edge_id in path.edge_ids
                               if explanation.edge_status.get(edge_id) ==
                               CausalStatus.SUPPORTED.value],
        "supporting_raw_refs": list(dict.fromkeys(
            binding.raw_ref for binding in
            (explanation.evidence_bindings.get(binding_id)
             for binding_id in path.evidence_binding_ids)
            if binding is not None and
            binding.predicate_result == PredicateResult.SUPPORTS.value)),
    } for path in selected]
    return {
        "kind": "ESCALATION" if escalated else "REPORT",
        "explanation_id": explanation.explanation_id,
        "explanation_revision": explanation.revision,
        "scope": explanation.scope,
        "observed_symptoms": list(explanation.observed_symptoms),
        "unmapped_observed_symptoms": list(st.unmapped_symptoms),
        "paths": [path.to_dict() for path in selected],
        "selected_paths": [_path_report(explanation, path)
                           for path in selected],
        "selected_root_causes": explanation.derive_selected_root_causes(),
        "evidence_binding_ids": selected_binding_ids,
        "key_evidence": selected_evidence,
        "alternative_paths": alternatives,
        "open_branches": open_branches,
        "unexplained_symptoms": list(explanation.unexplained_symptoms),
        "p0_obligations": {key: value.to_dict() for key, value in
                           explanation.p0_obligations.items()},
        "p0_matrix": _p0_report(explanation),
        "missing_evidence": [need.to_dict() for need in needs],
        "manual_options": manual,
        "intervention": intervention,
        "intervention_attempts": attempts,
        "verification": dict(st.verification_result),
        "rollback": dict(st.rollback_decision),
        "answers": {
            "why_this_chain": chain_answers,
            "why_not_alternatives": alternatives,
            "intervention_location": ({
                "selected_path_id": intervention.get("selected_path_id", ""),
                "intervention_target": intervention.get(
                    "intervention_target", ""),
                "fix_id": intervention.get("fix_id", ""),
                "intervention_kind": intervention.get(
                    "intervention_kind", ""),
            } if intervention else {}),
            "effect_proof": [effect for attempt in attempts
                             for effect in attempt.get("actual", [])],
        },
        "reason": st.outcome_note,
    }
