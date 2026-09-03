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
from knowledge.evidence_predicates import PredicateContext, evaluate


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
    refutes = (
        bool(refuter) and
        str(refuter.get("predicate_id") or "") == binding.predicate_id and
        str(refuter.get("scope") or "") == scope
    )
    return confirms, refutes


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

    for binding in explanation.evidence_bindings.values():
        for node_id in binding.target_node_ids:
            if graph.nodes.get(node_id, {}).get("kind") == "Fix":
                continue
            confirms, refutes = _causal_relation(
                graph, node_id, binding, scope=EvidenceTargetKind.NODE.value)
            if not (confirms or refutes):
                continue
            node_attempts.add(node_id)
            if binding.is_trusted(now=current) and (
                    (binding.predicate_result == PredicateResult.SUPPORTS.value and
                     confirms) or
                    (binding.predicate_result == PredicateResult.REFUTES.value and
                     refutes) or
                    binding.predicate_result in {
                        PredicateResult.NEUTRAL.value,
                        PredicateResult.NOT_APPLICABLE.value,
                    }):
                node_results.setdefault(node_id, {}).setdefault(
                    binding.raw_ref, set()).add(binding.predicate_result)
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
            if binding.is_trusted(now=current) and (
                    (binding.predicate_result == PredicateResult.SUPPORTS.value and
                     confirms) or
                    (binding.predicate_result == PredicateResult.REFUTES.value and
                     refutes) or
                    binding.predicate_result in {
                        PredicateResult.NEUTRAL.value,
                        PredicateResult.NOT_APPLICABLE.value,
                    }):
                edge_results.setdefault(edge_id, {}).setdefault(
                    binding.raw_ref, set()).add(binding.predicate_result)

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
            binding.is_trusted(now=current)
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


def intervention_options(st: EpisodeState, *, executable_only: bool = False
                         ) -> list[dict]:
    explanation = st.explanation_graph
    if explanation is None:
        return []
    options: list[dict] = []
    for path_id in explanation.selected_path_ids:
        for option in G.intervention_options(path_id, explanation):
            downstream = G.downstream_on_path(
                path_id, option["target_node_id"], explanation)
            bounded_effects = [node_id for node_id in
                               option.get("expected_effect_nodes", [])
                               if node_id in downstream]
            item = {**option, "expected_effect_nodes": bounded_effects}
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
            options.append(item)
    return options


def _sql_facts(sql: str) -> dict[str, Any]:
    facts: dict[str, Any] = {"statement": None, "table": "", "pid": None,
                             "index_signature": None, "parameter": ""}
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
        for target in statement.targetList or ():
            call = getattr(target, "val", None)
            if not isinstance(call, ast.FuncCall):
                continue
            function = ".".join(
                str(getattr(part, "sval", "") or "")
                for part in call.funcname or ())
            if function != "pg_terminate_backend" or not call.args:
                continue
            value = getattr(call.args[0], "val", None)
            pid = getattr(value, "ival", None)
            if isinstance(pid, int) and pid > 0:
                facts["pid"] = pid
    return facts


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
    pid = facts["pid"]
    pid_rows: list[tuple[EvidenceBinding, dict]] = []
    for binding, value in values:
        for row in _walk_dicts(value):
            row_pid = row.get("pid", row.get("blocked_by"))
            if pid is not None and str(row_pid) == str(pid):
                pid_rows.append((binding, row))

    def structural(condition_id: str) -> tuple[bool, str, list[str]]:
        statement = facts["statement"]
        if condition_id == "concrete_index_definition_bound":
            signature = facts["index_signature"]
            matching_refs = []
            for binding, value in values:
                if binding.predicate_id != "counterfactual_index_v2":
                    continue
                simulated = _sql_facts(str((value or {}).get("create_sql", "")))
                if signature and simulated["index_signature"] == signature:
                    matching_refs.append(binding.raw_ref)
            return (bool(signature and matching_refs),
                    "proposed index table/columns match the counterfactual trace",
                    matching_refs)
        if condition_id == "concrete_table_bound":
            ok = bool(facts["table"])
            return ok, "SQL AST binds one concrete table", []
        if condition_id in {"table_is_vacuumable",
                            "target_database_or_table_is_vacuumable"}:
            ok = isinstance(statement, ast.VacuumStmt) and bool(facts["table"])
            return ok, "VACUUM AST binds a concrete relation", []
        if condition_id == "concrete_pid_bound":
            return pid is not None, "SQL AST binds one positive backend PID", []
        if condition_id == "session_or_transaction_scope_only":
            ok = isinstance(statement, ast.VariableSetStmt)
            return ok, "SET is scoped to the executing session/transaction", []

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
            "blocking_or_xmin_impact_bound": lambda row: bool(
                row.get("blocking_impact") or row.get("blocked_session_count") or
                row.get("backend_xmin") or row.get("xmin_age")),
            "pid_identity_rechecked_fresh": lambda row: bool(
                row.get("identity_rechecked", True)),
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
        refs = [binding.raw_ref for binding, row in pid_rows if check(row)]
        return bool(refs), f"trace-bound PID facts satisfy {condition_id}", refs

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
            satisfied = bool(matched)
            reason = (f"{predicate_id} has a fresh {wanted} binding" if satisfied
                      else f"{predicate_id} lacks a fresh scoped {wanted} binding")
            refs = [binding.raw_ref for binding in matched]
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
        raise ValueError(
            f"intervention preconditions are not satisfied: {', '.join(unmet)}")
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
