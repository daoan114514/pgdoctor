"""故障因果图的加载与遍历。

用 networkx + YAML 而不是图数据库：几百个节点，内存里跑绰绰有余，
而且 YAML 能进 git —— 图的演化直接体现在 commit 历史里，这本身就是
"可审计的自进化"的实物证据。

给 agent 的接口是若干查询函数，**绝不把整张图塞进上下文**。
"""
from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
import yaml

from agent.explanation import (CausalPath, CausalStatus, EvidenceNeed,
                               EvidenceTargetKind, ExplanationGraph,
                               ObligationStatus, P0Obligation,
                               PredicateResult, stable_id)

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class PathRecallConfig:
    max_hops: int = 4
    ordinary_path_budget: int = 12
    max_paths_per_root_symptom: int = 3
    exploration_path_budget: int = 2
    p0_max_paths_per_cause: int = 20

    def __post_init__(self) -> None:
        for name in ("max_hops", "ordinary_path_budget",
                     "max_paths_per_root_symptom", "exploration_path_budget",
                     "p0_max_paths_per_cause"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.exploration_path_budget > self.ordinary_path_budget:
            raise ValueError("exploration budget cannot exceed ordinary budget")


DEFAULT_PATH_RECALL = PathRecallConfig()
_LEARNED_RELATIVE_CAP = 0.5
_CASE_RELATIVE_CAP = 0.25
_PATH_REGISTRY: dict[str, CausalPath] = {}
_RECALL_OBLIGATION_CACHE: dict[tuple[str, ...], dict[str, P0Obligation]] = {}


def _causes_edge_id(src: str, dst: str) -> str:
    return stable_id("edge", {"kind": "CAUSES", "from": src, "to": dst})


def _promoted_entry_is_live(entry: dict) -> bool:
    # Legacy promoted files predate per-edge status and are trusted by their
    # location.  Once status is present, only an explicit approval is live.
    return entry.get("status") in (None, "approved", "promoted")


def _live_promoted(extra: dict | None) -> dict:
    return {
        key: [dict(entry) for entry in (extra or {}).get(key, []) or []
              if _promoted_entry_is_live(entry)]
        for key in ("causes_symptom", "causes_cause", "confirmed_by")
    }


def graph_version() -> str:
    """Hash the seed graph and only the promoted runtime overlay."""
    try:
        from knowledge.structure import load_promoted
        promoted = _live_promoted(load_promoted())
    except Exception:
        promoted = _live_promoted({})
    return stable_id("graph", {
        "nodes_yaml": (HERE / "nodes.yaml").read_text(encoding="utf-8"),
        "edges_yaml": (HERE / "edges.yaml").read_text(encoding="utf-8"),
        "promoted": promoted,
    })


def _merge_promoted(edges: dict) -> dict:
    """并入人工审批通过的学习边（promoted_edges.yaml）。

    候选提案文件 learned/candidate_edges.yaml **永远不在这条路径上** ——
    机器写候选，人 promote 之后才进这里，和数据库那侧"提案→过门→执行"
    是同一个模式。

    这里再挡一道，是纵深防御：只接受 causes_symptom / causes_cause /
    confirmed_by，且 confirmed_by 一律降级为 supporting。structure.promote()
    已经禁过一次，就算有人手改 promoted_edges.yaml，也塞不进一条 required
    边或 REFUTED_BY 边 —— 前者等于让系统给自己降标准，后者会静默杀掉
    正确假设。
    """
    try:
        from knowledge.structure import load_promoted
        extra = load_promoted()
    except Exception:
        return edges
    extra = _live_promoted(extra)
    if not any(extra.values()):
        return edges
    out = {k: list(v or []) for k, v in edges.items()}
    for key in ("causes_symptom", "causes_cause"):
        for e in extra.get(key, []) or []:
            out.setdefault(key, []).append(dict(e))
    for e in extra.get("confirmed_by", []) or []:
        d = dict(e)
        d["necessity"] = "supporting"      # 学来的永远不能是必需证据
        out.setdefault("confirmed_by", []).append(d)
    return out


@functools.lru_cache(maxsize=1)
def load() -> nx.MultiDiGraph:
    nodes = yaml.safe_load((HERE / "nodes.yaml").read_text(encoding="utf-8"))
    edges = yaml.safe_load((HERE / "edges.yaml").read_text(encoding="utf-8"))
    edges = _merge_promoted(edges)
    g = nx.MultiDiGraph()

    for kind, key in (("symptoms", "Symptom"), ("root_causes", "RootCause"),
                      ("evidence", "Evidence"), ("fixes", "Fix")):
        for n in nodes.get(kind, []) or []:
            g.add_node(n["id"], kind=key, **{k: v for k, v in n.items()
                                             if k != "id"})

    for section in ("causes_symptom", "causes_cause"):
        for e in edges.get(section, []) or []:
            attrs = {name: value for name, value in e.items()
                     if name not in {"from", "to"}}
            attrs.setdefault("likelihood", 0.5)
            attrs["edge_id"] = _causes_edge_id(e["from"], e["to"])
            g.add_edge(e["from"], e["to"], key="CAUSES", **attrs)
    for e in edges.get("confirmed_by", []) or []:
        attrs = {name: value for name, value in e.items()
                 if name not in {"cause", "evidence"}}
        attrs.setdefault("necessity", "supporting")
        g.add_edge(e["cause"], e["evidence"], key="CONFIRMED_BY", **attrs)
    for e in edges.get("refuted_by", []) or []:
        attrs = {name: value for name, value in e.items()
                 if name not in {"cause", "evidence"}}
        g.add_edge(e["cause"], e["evidence"], key="REFUTED_BY", **attrs)
    for e in edges.get("discriminates", []) or []:
        attrs = {name: value for name, value in e.items()
                 if name not in {"evidence"}}
        attrs.setdefault("power", 0.5)
        g.add_edge(e["evidence"], e["evidence"], key="DISCRIMINATES", **attrs)
    for e in edges.get("fixed_by", []) or []:
        attrs = {name: value for name, value in e.items()
                 if name not in {"cause", "fix"}}
        g.add_edge(e["cause"], e["fix"], key="FIXED_BY", **attrs)
    return g


# ── 查询接口（会被包成 MCP 工具）──────────────────────────────

def _learned_adj() -> dict:
    """L3 学到的先验调整。

    单独存成 overlay 而不是改种子图：种子图是手工写的 ground truth，
    混在一起就分不清"人写的"和"学来的"，出问题也没法回滚。
    """
    try:
        from knowledge.evolution import load_delta
        return load_delta().prior_adj
    except Exception:
        return {}


def _learned_likelihood_adj(*, schema_version: int = 2) -> dict:
    """L3 学到的"某根因导致某症状"的调整量。

    与 prior_adj 的分工：prior_adj 调的是根因自身的基础可能性，这个调
    的是具体一条因果边的权重。同一个根因在不同症状组合下的可能性并不
    相同，这层信息 prior_adj 表达不了 —— 之前这份数据写了从来没人读，
    等于把 L3 学到的一半直接扔掉。
    """
    try:
        if schema_version == 1:
            from knowledge.evolution import load_delta
            return load_delta().likelihood_adj
        from knowledge.evolution import load_l3_v2_adjustments
        edge_adjustments, _path_adjustments = load_l3_v2_adjustments(
            graph_version())
        return edge_adjustments
    except Exception:
        return {}


def _learned_path_adj() -> dict:
    try:
        from knowledge.evolution import load_l3_v2_adjustments
        _edge_adjustments, path_adjustments = load_l3_v2_adjustments(
            graph_version())
        return path_adjustments
    except Exception:
        return {}


def candidate_causes(symptoms: list[str], max_hops: int = 3,
                     top_k: int = 5, use_learned: bool = True) -> list[dict]:
    """从症状反向多跳遍历，得到候选根因并按可能性排序。

    多跳是关键：级联故障里真根因离症状好几跳，单跳等于退化成查找表。
    """
    g = load()
    adj = _learned_adj() if use_learned else {}
    ladj = (_learned_likelihood_adj(schema_version=1)
            if use_learned else {})
    scores: dict[str, float] = {}
    paths: dict[str, list[str]] = {}

    for s in symptoms:
        if s not in g:
            continue
        # 直接指向该症状的根因
        frontier = []
        for u, _v, k in g.in_edges(s, keys=True):
            if k != "CAUSES":
                continue
            w = g[u][s][k].get("likelihood", 0.5)
            if use_learned:
                # 与 prior 同样的纪律：只调权重不改变集合，且夹在 (0,1)
                # 内，学习不能让一条因果边彻底消失
                w = min(0.99, max(0.01, w + ladj.get(f"{u}->{s}", 0.0)))
            frontier.append((u, w, [u]))
        seen = set()
        hop = 0
        while frontier and hop < max_hops:
            nxt = []
            for cause, w, path in frontier:
                if cause in seen:
                    continue
                seen.add(cause)
                if g.nodes.get(cause, {}).get("kind") == "RootCause":
                    prior = g.nodes[cause].get("prior", 0.1)
                    if use_learned:
                        # 学到的调整量有上下限，且只影响排序不改变集合 ——
                        # 学习不该让某个根因彻底进不了候选，否则系统会
                        # 因为几次失败而永久丧失识别某类故障的能力
                        prior = max(0.01, prior + adj.get(cause, 0.0))
                    sc = w * (0.5 + prior)
                    if sc > scores.get(cause, 0):
                        scores[cause] = sc
                        paths[cause] = path
                # 再往上游找：谁会导致这个根因
                for u, v, k in g.in_edges(cause, keys=True):
                    if k == "CAUSES" and u not in seen:
                        nxt.append((u, w * g[u][v][k].get("likelihood", 0.5) * 0.9,
                                    [u] + path))
            frontier = nxt
            hop += 1

    out = [{"root_cause": c, "score": round(sc, 4),
            "path": " -> ".join(paths[c]),
            "hops": len(paths[c]) - 1,
            "learned_adj": round(adj.get(c, 0.0), 4),
            "severity": g.nodes[c].get("severity", ""),
            "desc": g.nodes[c].get("desc", "")}
           for c, sc in sorted(scores.items(), key=lambda x: -x[1])]
    return out[:top_k]


def severity_of(root_cause: str) -> str:
    """Return the graph-owned incident severity for a root cause."""
    g = load()
    if root_cause not in g or g.nodes[root_cause].get("kind") != "RootCause":
        return ""
    return str(g.nodes[root_cause].get("severity", ""))


def recall_candidates(symptoms: list[str], max_hops: int = 3,
                      base_top_k: int = 6, use_learned: bool = True,
                      required_severities: tuple[str, ...] = ("P0",)
                      ) -> list[dict]:
    """Risk-aware candidate recall used by the live investigation loop.

    Keep the highest-scoring ``base_top_k`` candidates, then retain every
    reachable root cause whose graph severity is in ``required_severities``.
    A high-risk cause that has no causal path from the observed symptoms is
    never injected into the set.  Output order remains the graph score order;
    ``forced_by_risk`` records which entries survived only because of severity.

    ``candidate_causes`` intentionally keeps its historical fixed-top-k
    semantics for offline experiments and ablations.
    """
    ranked = candidate_causes(
        symptoms, max_hops=max_hops, top_k=max(load().number_of_nodes(), 1),
        use_learned=use_learned)
    base_ids = {c["root_cause"] for c in ranked[:max(base_top_k, 0)]}
    severities = set(required_severities)
    selected = base_ids | {
        c["root_cause"] for c in ranked if c.get("severity") in severities
    }
    return [
        {**c, "forced_by_risk": c["root_cause"] not in base_ids}
        for c in ranked if c["root_cause"] in selected
    ]


# ── v2 path runtime ─────────────────────────────────────────────

def _score_components(*, root: str, manual_weights: list[float],
                      edge_pairs: list[tuple[str, str]], path_id: str,
                      use_learned: bool, case_path_scores: dict[str, float],
                      learned_path_scores: dict[str, float],
                      use_l3_edges: bool,
                      use_l3_paths: bool) -> dict[str, float]:
    g = load()
    manual = math.prod(manual_weights)
    prior = float(g.nodes[root].get("prior", 0.1))
    learned_prior = 0.0
    learned_edges = 0.0
    l1_path = 0.0
    l3_path = 0.0
    if use_learned:
        # v2 deliberately ignores v1 root priors and graph_delta.yaml.  L3
        # operates on stable edge/path IDs; the component remains explicit so
        # old score consumers keep a stable schema.
        learned_prior = 0.0

        if use_l3_edges:
            edge_overlay = _learned_likelihood_adj()
            adjusted_weights = []
            for (src, dst), weight in zip(edge_pairs, manual_weights):
                edge_id = _causes_edge_id(src, dst)
                # The src->dst fallback is an in-memory compatibility hook for
                # old ablation tests.  v2 persistence validates stable IDs.
                raw = float(edge_overlay.get(
                    edge_id, edge_overlay.get(f"{src}->{dst}", 0.0)))
                cap = weight * _LEARNED_RELATIVE_CAP
                adjustment = max(-cap, min(cap, raw))
                adjusted_weights.append(min(
                    0.999, max(0.001, weight + adjustment)))
            learned_edges_raw = math.prod(adjusted_weights) - manual
            edge_path_cap = manual * _LEARNED_RELATIVE_CAP
            learned_edges = max(-edge_path_cap,
                                min(edge_path_cap, learned_edges_raw))

        path_cap = manual * _CASE_RELATIVE_CAP
        l1_path = max(-path_cap, min(path_cap,
                                     float(case_path_scores.get(path_id, 0.0))))
        if use_l3_paths:
            path_overlay = _learned_path_adj()
            raw_path = (float(path_overlay.get(path_id, 0.0)) +
                        float(learned_path_scores.get(path_id, 0.0)))
            l3_path = max(-path_cap, min(path_cap, raw_path))

    components = {
        "manual_causes_likelihood": manual,
        "manual_root_prior": prior * 0.5,
        "learned_root_prior_adjustment": learned_prior * 0.5,
        "l1_path_template_adjustment": l1_path,
        "l3_edge_adjustment": learned_edges,
        "l3_path_adjustment": l3_path,
        "symptom_coverage_reward": 0.1,
        "hop_penalty": -0.03 * max(0, len(manual_weights) - 1),
        "redundancy_penalty": 0.0,
    }
    components["total"] = sum(components.values())
    return {name: round(value, 8) for name, value in components.items()}


def _enumerate_all_paths(symptoms: list[str], *, max_hops: int,
                         use_learned: bool,
                         case_path_scores: dict[str, float],
                         learned_path_scores: dict[str, float],
                         use_l3_edges: bool,
                         use_l3_paths: bool) -> list[CausalPath]:
    if max_hops < 1:
        return []
    g = load()
    version = graph_version()
    found: dict[str, CausalPath] = {}
    for symptom in dict.fromkeys(symptoms):
        if symptom not in g or g.nodes[symptom].get("kind") != "Symptom":
            continue
        # Traversal moves backwards, while every stored path remains in the
        # graph's forward direction: upstream cause -> observed symptom.
        stack: list[tuple[str, list[str], list[str], list[float],
                          list[tuple[str, str]]]] = [
            (symptom, [symptom], [], [], [])
        ]
        while stack:
            current, node_ids, edge_ids, weights, pairs = stack.pop()
            if len(edge_ids) >= max_hops:
                continue
            incoming = sorted(
                ((src, data) for src, _dst, key, data in
                 g.in_edges(current, keys=True, data=True) if key == "CAUSES"),
                key=lambda item: item[0], reverse=True)
            for source, edge_data in incoming:
                if source in node_ids:
                    continue
                if g.nodes[source].get("kind") != "RootCause":
                    continue
                next_nodes = [source] + node_ids
                next_edges = [edge_data.get("edge_id") or
                              _causes_edge_id(source, current)] + edge_ids
                next_weights = [float(edge_data.get("likelihood", 0.5))] + weights
                next_pairs = [(source, current)] + pairs
                path = CausalPath.create(
                    graph_version=version,
                    node_ids=next_nodes,
                    edge_ids=next_edges,
                    observed_symptom_id=symptom,
                    source="graph",
                    required_evidence_types=sorted({
                        evidence_type
                        for cause_id in next_nodes[:-1]
                        for evidence_type in required_evidence(cause_id)
                    }),
                )
                path.score_components = _score_components(
                    root=source,
                    manual_weights=next_weights,
                    edge_pairs=next_pairs,
                    path_id=path.path_id,
                    use_learned=use_learned,
                    case_path_scores=case_path_scores,
                    learned_path_scores=learned_path_scores,
                    use_l3_edges=use_l3_edges,
                    use_l3_paths=use_l3_paths,
                )
                if use_learned and case_path_scores.get(path.path_id):
                    path.source = list(dict.fromkeys(path.source + ["case_template"]))
                found[path.path_id] = path
                stack.append((source, next_nodes, next_edges,
                              next_weights, next_pairs))
    return sorted(found.values(), key=lambda p: (
        -p.score_components.get("total", 0.0), len(p.edge_ids), p.path_id))


def _select_ordinary_paths(paths: list[CausalPath], *,
                           config: PathRecallConfig) -> list[CausalPath]:
    budget = config.ordinary_path_budget
    if budget <= 0:
        return []

    # Limit repeated structural variants for the same upstream root/symptom.
    grouped: dict[tuple[str, str], list[CausalPath]] = {}
    for path in paths:
        grouped.setdefault((path.root_node_id, path.observed_symptom_id), []).append(path)
    eligible = []
    for group in grouped.values():
        eligible.extend(sorted(group, key=lambda p: (
            -p.score_components.get("total", 0.0), p.path_id)
        )[:config.max_paths_per_root_symptom])
    ranked = sorted(eligible, key=lambda p: (
        -p.score_components.get("total", 0.0), p.path_id))

    selected: list[CausalPath] = []
    selected_ids: set[str] = set()

    def add(path: CausalPath) -> bool:
        if path.path_id in selected_ids or len(selected) >= budget:
            return False
        selected.append(path)
        selected_ids.add(path.path_id)
        return True

    # First preserve at least one explanation for every observed graph symptom.
    for symptom in sorted({path.observed_symptom_id for path in ranked}):
        best = next((path for path in ranked
                     if path.observed_symptom_id == symptom), None)
        if best:
            add(best)

    remaining = [path for path in ranked if path.path_id not in selected_ids]
    reserve = min(config.exploration_path_budget, len(remaining),
                  max(0, budget - len(selected)))

    # Then preserve the first causal branch nearest the observed symptom.
    best_by_branch: dict[tuple[str, str], CausalPath] = {}
    for path in remaining:
        branch = (path.observed_symptom_id, path.node_ids[-2])
        best_by_branch.setdefault(branch, path)
    for path in sorted(best_by_branch.values(), key=lambda p: (
            -p.score_components.get("total", 0.0), p.path_id)):
        if len(selected) >= budget - reserve:
            break
        add(path)

    # Reserve replayable exploration for low-prior but structurally distinct paths.
    remaining = [path for path in ranked if path.path_id not in selected_ids]
    existing_shapes = {(path.root_node_id, path.node_ids[-2]) for path in selected}
    exploration = sorted(remaining, key=lambda p: (
        p.score_components.get("total", 0.0), p.path_id))
    novel = [path for path in exploration
             if (path.root_node_id, path.node_ids[-2]) not in existing_shapes]
    for path in novel + [p for p in exploration if p not in novel]:
        if reserve <= 0 or len(selected) >= budget:
            break
        if add(path):
            path.source = list(dict.fromkeys(path.source + ["exploration"]))
            reserve -= 1

    # Finally fill the remaining budget by score and record redundancy cost.
    for path in ranked:
        if len(selected) >= budget:
            break
        if path.path_id in selected_ids:
            continue
        overlap = max((len(set(path.edge_ids).intersection(other.edge_ids)) /
                       max(len(path.edge_ids), 1) for other in selected), default=0.0)
        penalty = round(-0.05 * overlap, 8)
        path.score_components["redundancy_penalty"] = penalty
        path.score_components["total"] = round(
            path.score_components.get("total", 0.0) + penalty, 8)
        add(path)
    return selected


def _reachable_p0_path_counts(symptoms: list[str], *,
                              stop_after: int) -> dict[str, int]:
    """Count P0 paths without applying ordinary depth or score limits."""
    g = load()
    causes = nx.DiGraph()
    causes.add_nodes_from(g.nodes)
    causes.add_edges_from((src, dst) for src, dst, key in g.edges(keys=True)
                          if key == "CAUSES")
    counts: dict[str, int] = {}
    for cause_id, data in g.nodes(data=True):
        if data.get("severity") != "P0":
            continue
        count = 0
        for symptom in symptoms:
            if symptom not in causes or not nx.has_path(causes, cause_id, symptom):
                continue
            for _path in nx.all_simple_paths(causes, cause_id, symptom):
                count += 1
                if count >= stop_after:
                    break
            if count >= stop_after:
                break
        if count:
            counts[cause_id] = count
    return counts


def _recall_paths(symptoms: list[str], *, config: PathRecallConfig,
                  use_learned: bool,
                  case_path_scores: dict[str, float] | None,
                  learned_path_scores: dict[str, float] | None,
                  use_l3_edges: bool,
                  use_l3_paths: bool,
                  ) -> tuple[list[CausalPath], dict[str, P0Obligation]]:
    all_paths = _enumerate_all_paths(
        symptoms,
        max_hops=config.max_hops,
        use_learned=use_learned,
        case_path_scores=case_path_scores or {},
        learned_path_scores=learned_path_scores or {},
        use_l3_edges=use_l3_edges,
        use_l3_paths=use_l3_paths,
    )
    p0_groups: dict[str, list[CausalPath]] = {}
    ordinary = []
    for path in all_paths:
        if severity_of(path.root_node_id) == "P0":
            p0_groups.setdefault(path.root_node_id, []).append(path)
        else:
            ordinary.append(path)

    selected = _select_ordinary_paths(ordinary, config=config)
    reachable_p0 = _reachable_p0_path_counts(
        symptoms, stop_after=max(config.p0_max_paths_per_cause + 1, 1))
    obligations: dict[str, P0Obligation] = {}
    for cause_id in sorted(reachable_p0):
        group = p0_groups.get(cause_id, [])
        ranked = sorted(group, key=lambda p: (
            -p.score_components.get("total", 0.0), p.path_id))
        retained = ranked[:config.p0_max_paths_per_cause]
        selected.extend(retained)
        obligations[cause_id] = P0Obligation(
            cause_id=cause_id,
            reachable_path_ids=[path.path_id for path in retained],
            status=ObligationStatus.OPEN.value,
            required_evidence_types=required_evidence(cause_id),
            truncated=reachable_p0[cause_id] > len(retained),
        )

    unique = {path.path_id: path for path in selected}
    paths = list(unique.values())
    for path in paths:
        _PATH_REGISTRY[path.path_id] = path
    cache_key = tuple(sorted(unique))
    _RECALL_OBLIGATION_CACHE[cache_key] = obligations
    while len(_RECALL_OBLIGATION_CACHE) > 128:
        _RECALL_OBLIGATION_CACHE.pop(next(iter(_RECALL_OBLIGATION_CACHE)))
    return paths, obligations


def enumerate_causal_paths(symptoms: list[str], max_hops: int | None = None, *,
                           config: PathRecallConfig | None = None,
                           use_learned: bool = True,
                           case_path_scores: dict[str, float] | None = None,
                           learned_path_scores: dict[str, float] | None = None,
                           use_l3_edges: bool | None = None,
                           use_l3_paths: bool | None = None,
                           ) -> list[CausalPath]:
    """Recall simple CAUSES paths stored in upstream-to-symptom direction."""
    base = config or DEFAULT_PATH_RECALL
    effective = PathRecallConfig(
        max_hops=base.max_hops if max_hops is None else max_hops,
        ordinary_path_budget=base.ordinary_path_budget,
        max_paths_per_root_symptom=base.max_paths_per_root_symptom,
        exploration_path_budget=base.exploration_path_budget,
        p0_max_paths_per_cause=base.p0_max_paths_per_cause,
    )
    paths, _obligations = _recall_paths(
        list(dict.fromkeys(symptoms)), config=effective,
        use_learned=use_learned,
        case_path_scores=case_path_scores,
        learned_path_scores=learned_path_scores,
        use_l3_edges=(use_learned if use_l3_edges is None else
                      bool(use_l3_edges and use_learned)),
        use_l3_paths=(use_learned if use_l3_paths is None else
                      bool(use_l3_paths and use_learned)),
    )
    return paths


def _copy_obligations(values: dict[str, P0Obligation]) -> dict[str, P0Obligation]:
    return {cause_id: P0Obligation.from_dict(obligation.to_dict())
            for cause_id, obligation in values.items()}


def merge_paths(paths: list[CausalPath], *, episode_id: str = "unbound",
                observed_symptoms: list[str] | None = None,
                p0_obligations: dict[str, P0Obligation] | None = None
                ) -> ExplanationGraph:
    """Merge shared path structure into one candidate ExplanationGraph."""
    path_ids = tuple(sorted({path.path_id for path in paths}))
    obligations = p0_obligations
    if obligations is None:
        obligations = _RECALL_OBLIGATION_CACHE.get(path_ids)
    if obligations is None:
        grouped: dict[str, list[str]] = {}
        for path in paths:
            if severity_of(path.root_node_id) == "P0":
                grouped.setdefault(path.root_node_id, []).append(path.path_id)
        obligations = {
            cause_id: P0Obligation(
                cause_id=cause_id,
                reachable_path_ids=ids,
                required_evidence_types=required_evidence(cause_id),
            )
            for cause_id, ids in grouped.items()
        }
    explanation = ExplanationGraph.create(
        graph_version=graph_version(),
        episode_id=episode_id,
        observed_symptoms=(observed_symptoms if observed_symptoms is not None else
                           list(dict.fromkeys(
                               path.observed_symptom_id for path in paths))),
        candidate_paths=paths,
        p0_obligations=_copy_obligations(obligations),
    )
    for path in explanation.candidate_paths:
        _PATH_REGISTRY[path.path_id] = path
    return explanation


def recall_explanation(symptoms: list[str], *, episode_id: str,
                       config: PathRecallConfig = DEFAULT_PATH_RECALL,
                       use_learned: bool = True,
                       case_path_scores: dict[str, float] | None = None,
                       learned_path_scores: dict[str, float] | None = None,
                       use_l3_edges: bool | None = None,
                       use_l3_paths: bool | None = None,
                       ) -> ExplanationGraph:
    """Atomic v2 recall entry point that preserves P0 truncation metadata."""
    paths, obligations = _recall_paths(
        list(dict.fromkeys(symptoms)), config=config,
        use_learned=use_learned,
        case_path_scores=case_path_scores,
        learned_path_scores=learned_path_scores,
        use_l3_edges=(use_learned if use_l3_edges is None else
                      bool(use_l3_edges and use_learned)),
        use_l3_paths=(use_learned if use_l3_paths is None else
                      bool(use_l3_paths and use_learned)),
    )
    return merge_paths(paths, episode_id=episode_id,
                       observed_symptoms=symptoms,
                       p0_obligations=obligations)


def path_frontier(explanation: ExplanationGraph) -> list[dict]:
    """Return the closest unresolved and path-discriminating nodes/edges."""
    records: dict[tuple[str, str], dict] = {}
    viable = [path for path in explanation.candidate_paths
              if path.status != CausalStatus.REFUTED.value]
    for path in viable:
        for index in range(len(path.edge_ids) - 1, -1, -1):
            distance = len(path.edge_ids) - 1 - index
            edge_id = path.edge_ids[index]
            node_id = path.node_ids[index]
            edge_status = explanation.edge_status.get(
                edge_id, CausalStatus.UNTESTED.value)
            node_status = explanation.node_status.get(
                node_id, CausalStatus.UNTESTED.value)
            unresolved = {CausalStatus.UNTESTED.value,
                          CausalStatus.INCONCLUSIVE.value}
            if edge_status in unresolved:
                record = records.setdefault(("EDGE", edge_id), {
                    "target_kind": "EDGE", "target_id": edge_id,
                    "path_ids": [], "distance_from_observed": distance,
                })
                record["path_ids"].append(path.path_id)
            if node_status in unresolved:
                record = records.setdefault(("NODE", node_id), {
                    "target_kind": "NODE", "target_id": node_id,
                    "path_ids": [], "distance_from_observed": distance,
                })
                record["path_ids"].append(path.path_id)
            if edge_status in unresolved or node_status in unresolved:
                break

    for record in records.values():
        record["path_ids"] = sorted(set(record["path_ids"]))
        target_id = record["target_id"]
        alternatives = 0
        absent = 0
        for path in viable:
            collection = path.edge_ids if record["target_kind"] == "EDGE" else path.node_ids
            if target_id in collection:
                alternatives += 1
            else:
                absent += 1
        structural = absent / max(alternatives + absent, 1)
        power = 0.0
        if record["target_kind"] == "NODE":
            for evidence_type in discriminators_of(target_id):
                for _u, _v, key, data in load().out_edges(
                        evidence_type, keys=True, data=True):
                    if key == "DISCRIMINATES":
                        power = max(power, float(data.get("power", 0.5)))
        record["discrimination_score"] = round(structural + power, 6)
    return sorted(records.values(), key=lambda item: (
        item["distance_from_observed"], -item["discrimination_score"],
        0 if item["target_kind"] == "EDGE" else 1, item["target_id"]))


def _binding_satisfies(explanation: ExplanationGraph, *, evidence_type: str,
                       target_ids: list[str]) -> bool:
    targets = set(target_ids)
    for binding in explanation.evidence_bindings.values():
        if (binding.evidence_type != evidence_type or not binding.is_trusted() or
                binding.predicate_result not in {
                    PredicateResult.SUPPORTS.value,
                    PredicateResult.REFUTES.value,
                }):
            continue
        if targets.intersection(binding.target_node_ids + binding.target_edge_ids):
            return True
    return False


def evidence_needs(explanation: ExplanationGraph) -> list[EvidenceNeed]:
    """Build deterministic evidence tasks from graph relations and frontier."""
    g = load()
    grouped: dict[tuple, dict] = {}

    def add(*, path_ids: list[str], target_kind: str, target_ids: list[str],
            cause_id: str, evidence_type: str, required: bool,
            reason: str, predicate_id: str = "") -> None:
        if evidence_type not in g:
            return
        predicate_id = predicate_id or str(
            g.nodes[evidence_type].get("predicate_id", ""))
        tool = str(g.nodes[evidence_type].get("obtained_by", ""))
        if not predicate_id or not tool:
            return
        if _binding_satisfies(explanation, evidence_type=evidence_type,
                              target_ids=target_ids):
            return
        key = (target_kind, tuple(sorted(target_ids)), evidence_type,
               predicate_id, required)
        entry = grouped.setdefault(key, {
            "path_ids": [], "target_kind": target_kind,
            "target_ids": target_ids, "evidence_type": evidence_type,
            "predicate_id": predicate_id, "required": required,
            "freshness_seconds": int(g.nodes[evidence_type].get(
                "freshness_seconds", 300)),
            "candidate_tools": [], "reasons": [],
        })
        entry["path_ids"].extend(path_ids)
        entry["candidate_tools"].append(tool)
        entry["reasons"].append(f"{cause_id}: {reason}")

    edge_sources = {
        edge_id: path.node_ids[index]
        for path in explanation.candidate_paths
        for index, edge_id in enumerate(path.edge_ids)
    }
    for item in path_frontier(explanation):
        target_kind = item["target_kind"]
        target_id = item["target_id"]
        cause_id = (target_id if target_kind == "NODE"
                    else edge_sources.get(target_id, ""))
        if not cause_id or cause_id not in g:
            continue
        for _u, evidence_type, key, data in g.out_edges(
                cause_id, keys=True, data=True):
            if key == "CONFIRMED_BY":
                add(path_ids=item["path_ids"], target_kind=target_kind,
                    target_ids=[target_id], cause_id=cause_id,
                    evidence_type=evidence_type,
                    required=data.get("necessity") == "required",
                    reason=f"{data.get('necessity', 'supporting')} support")
            elif key == "REFUTED_BY":
                intervention = data.get("scope") == "INTERVENTION"
                refute_kind = (EvidenceTargetKind.INTERVENTION.value
                               if intervention else target_kind)
                refute_targets = ([str(data.get("target_fix"))]
                                   if intervention and data.get("target_fix")
                                   else [target_id])
                add(path_ids=item["path_ids"], target_kind=refute_kind,
                    target_ids=refute_targets, cause_id=cause_id,
                    evidence_type=evidence_type, required=False,
                    reason="scoped refutation",
                    predicate_id=str(data.get("predicate_id", "")))
        for evidence_type in discriminators_of(cause_id):
            add(path_ids=item["path_ids"], target_kind=target_kind,
                target_ids=[target_id], cause_id=cause_id,
                evidence_type=evidence_type, required=False,
                reason="branch discriminator")

    # Frontier state is not equivalent to required-evidence completeness.  A
    # segment can be supported by one discriminator while still missing a
    # second required predicate.  Keep those obligations live for every cause
    # segment until the concrete required evidence is decisive.
    for path in explanation.candidate_paths:
        if path.status == CausalStatus.REFUTED.value:
            continue
        for index, cause_id in enumerate(path.node_ids[:-1]):
            edge_id = path.edge_ids[index]
            for evidence_type in required_evidence(cause_id):
                add(path_ids=[path.path_id], target_kind="NODE",
                    target_ids=[cause_id], cause_id=cause_id,
                    evidence_type=evidence_type, required=True,
                    reason="required path-node evidence")
                add(path_ids=[path.path_id], target_kind="EDGE",
                    target_ids=[edge_id], cause_id=cause_id,
                    evidence_type=evidence_type, required=True,
                    reason="required path-edge evidence")

    # P0 needs are mandatory even when ordinary frontier ranking is crowded.
    for cause_id, obligation in explanation.p0_obligations.items():
        if obligation.resolved:
            continue
        for evidence_type in obligation.required_evidence_types:
            add(path_ids=obligation.reachable_path_ids,
                target_kind=EvidenceTargetKind.P0.value,
                target_ids=[cause_id], cause_id=cause_id,
                evidence_type=evidence_type, required=True,
                reason="open P0 obligation")

    needs = []
    for entry in grouped.values():
        needs.append(EvidenceNeed.create(
            path_ids=sorted(set(entry["path_ids"])),
            target_kind=entry["target_kind"],
            target_ids=entry["target_ids"],
            evidence_type=entry["evidence_type"],
            predicate_id=entry["predicate_id"],
            required=entry["required"],
            freshness_seconds=entry["freshness_seconds"],
            candidate_tools=sorted(set(entry["candidate_tools"])),
            reason="; ".join(sorted(set(entry["reasons"]))),
        ))
    return sorted(needs, key=lambda need: (
        not need.required, need.target_kind != EvidenceTargetKind.P0.value,
        need.evidence_type, need.need_id))


def _resolve_path(path_id: str,
                  explanation: ExplanationGraph | None = None) -> CausalPath:
    if explanation is not None:
        path = explanation.path_map().get(path_id)
        if path:
            return path
    if path_id in _PATH_REGISTRY:
        return _PATH_REGISTRY[path_id]
    raise KeyError(f"unknown causal path: {path_id}")


def alternatives_for(path_id: str,
                     explanation: ExplanationGraph | None = None) -> list[CausalPath]:
    """Find paths sharing an observed symptom or downstream suffix."""
    target = _resolve_path(path_id, explanation)
    candidates = (explanation.candidate_paths if explanation is not None
                  else list(_PATH_REGISTRY.values()))

    def suffix_length(other: CausalPath) -> int:
        count = 0
        for left, right in zip(reversed(target.node_ids), reversed(other.node_ids)):
            if left != right:
                break
            count += 1
        return count

    alternatives = [
        path for path in candidates
        if path.path_id != path_id and
        (path.observed_symptom_id == target.observed_symptom_id or
         suffix_length(path) > 0)
    ]
    return sorted(alternatives, key=lambda path: (
        -suffix_length(path), -path.score_components.get("total", 0.0),
        path.path_id))


def intervention_options(path_id: str,
                         explanation: ExplanationGraph | None = None) -> list[dict]:
    """Return only FIXED_BY interventions attached to nodes on this path."""
    path = _resolve_path(path_id, explanation)
    g = load()
    options = []
    for node_id in path.node_ids[:-1]:
        for _u, fix_id, key, edge_data in g.out_edges(
                node_id, keys=True, data=True):
            if key != "FIXED_BY":
                continue
            fix = g.nodes[fix_id]
            options.append({
                **{name: value for name, value in fix.items() if name != "kind"},
                **{name: value for name, value in edge_data.items()
                   if name not in {"edge_id"}},
                "path_id": path.path_id,
                "target_node_id": node_id,
                "dynamic_role": path.node_roles[node_id],
                "fix": fix_id,
            })
    return options


def downstream_on_path(path_id: str, node_id: str,
                       explanation: ExplanationGraph | None = None) -> list[str]:
    """Return bounded downstream nodes from one concrete causal path."""
    path = _resolve_path(path_id, explanation)
    if node_id not in path.node_ids:
        raise ValueError(f"{node_id} is not on path {path_id}")
    return path.node_ids[path.node_ids.index(node_id) + 1:]


def required_evidence(root_cause: str) -> list[str]:
    """该根因的必需证据类型 —— ESC 的 D1 直接读这里。"""
    g = load()
    if root_cause not in g:
        return []
    return sorted({v for u, v, k, d in g.out_edges(root_cause, keys=True, data=True)
                   if k == "CONFIRMED_BY" and d.get("necessity") == "required"})


def supporting_evidence(root_cause: str) -> list[str]:
    g = load()
    if root_cause not in g:
        return []
    return sorted({v for u, v, k, d in g.out_edges(root_cause, keys=True, data=True)
                   if k == "CONFIRMED_BY" and d.get("necessity") != "required"})


def refuting_evidence(root_cause: str) -> list[dict]:
    g = load()
    if root_cause not in g:
        return []
    return [{"evidence": v,
             "predicate_id": d.get("predicate_id", ""),
             "scope": d.get("scope", ""),
             "window_required": bool(d.get("window_required", False)),
             "target_fix": d.get("target_fix", ""),
             "when": d.get("when", "")}
            for u, v, k, d in g.out_edges(root_cause, keys=True, data=True)
            if k == "REFUTED_BY"]


def discriminators_of(root_cause: str) -> set[str]:
    """哪些证据类型能把这个根因和别的候选分开。

    DISCRIMINATES 边存的是"一条证据分开哪几个候选"，这里做反向索引。
    ESC 的 D2 判"这次排除有没有依据"时要用：拿判别证据排除一个候选，
    是有依据的，哪怕那条证据不在该候选自己的 confirmed_by 边上。
    """
    g = load()
    out: set[str] = set()
    for ev, _v, k, d in g.edges(keys=True, data=True):
        if k != "DISCRIMINATES":
            continue
        if root_cause in (d.get("separates") or []):
            out.add(ev)
    return out


def best_discriminator(candidates: list[str]) -> dict | None:
    """在候选集上找一次能分开最多假设的证据 —— 取证预算有限时最划算的那步。"""
    g = load()
    best, best_n = None, 0
    for n, d in g.nodes(data=True):
        if d.get("kind") != "Evidence":
            continue
        for u, v, k, ed in g.out_edges(n, keys=True, data=True):
            if k != "DISCRIMINATES":
                continue
            hit = [c for c in ed.get("separates", []) if c in candidates]
            score = len(hit) * ed.get("power", 0.5)
            if score > best_n:
                best, best_n = {"evidence": n,
                                "separates": hit,
                                "power": ed.get("power", 0.5),
                                "obtained_by": d.get("obtained_by", "")}, score
    return best


def symptoms_of(root_cause: str) -> list[str]:
    """该根因已知会引发的症状 —— ESC 的 D3 用它检查有无孤儿症状。"""
    g = load()
    if root_cause not in g:
        return []
    out = set()
    frontier = [root_cause]
    seen = set()
    for _ in range(3):
        nxt = []
        for c in frontier:
            if c in seen:
                continue
            seen.add(c)
            for u, v, k in g.out_edges(c, keys=True):
                if k != "CAUSES":
                    continue
                if g.nodes.get(v, {}).get("kind") == "Symptom":
                    out.add(v)
                else:
                    nxt.append(v)
        frontier = nxt
    return sorted(out)


def fixes_for(root_cause: str) -> list[dict]:
    g = load()
    if root_cause not in g:
        return []
    res = []
    for u, v, k in g.out_edges(root_cause, keys=True):
        if k == "FIXED_BY":
            d = g.nodes[v]
            res.append({"fix": v, "action_type": d.get("action_type"),
                        "risk_tier": d.get("risk_tier"),
                        "template": d.get("template"),
                        "rollback": d.get("rollback"),
                        "intervention_kind": d.get("intervention_kind"),
                        "preconditions": d.get("preconditions", []),
                        "expected_effect_nodes": d.get(
                            "expected_effect_nodes", []),
                        "expected_effects": d.get("expected_effects", []),
                        "manual": bool(d.get("manual", False)),
                        "execution": d.get("execution", "gated"),
                        "desc": d.get("desc", "")})
    return res


def upstream_of(root_cause: str) -> list[dict]:
    """谁会导致这个根因 —— 多个假设同时确认时，用它找最上游那个。"""
    g = load()
    if root_cause not in g:
        return []
    return [{"cause": u, "likelihood": d.get("likelihood", 0.5),
             "note": d.get("note", "")}
            for u, v, k, d in g.in_edges(root_cause, keys=True, data=True)
            if k == "CAUSES" and g.nodes.get(u, {}).get("kind") == "RootCause"]


def downstream_of(root_cause: str) -> set[str]:
    """这个根因会导致哪些别的根因 —— 沿 CAUSES 边一路向下。

    ESC 的 D2 用它剔除竞争假设：下游后果不是"竞争解释"，是已经被解释
    掉的结果。声称长事务时连接打满确实在发生，要求 agent 去"排除"它，
    等于要求它否认一个正在发生的事实。
    """
    g = load()
    if root_cause not in g:
        return set()
    out, stack = set(), [root_cause]
    while stack:
        n = stack.pop()
        for _, v, k in g.out_edges(n, keys=True):
            if k != "CAUSES" or g.nodes.get(v, {}).get("kind") != "RootCause":
                continue
            if v not in out and v != root_cause:
                out.add(v)
                stack.append(v)
    return out


def _causes_reachable(g, src: str, dst: str) -> list[str] | None:
    """沿 根因->根因 的 CAUSES 边找 src 到 dst 的路径，找不到返回 None。"""
    stack = [(src, [src])]
    seen = {src}
    while stack:
        n, path = stack.pop()
        for _, v, k in g.out_edges(n, keys=True):
            if k != "CAUSES" or g.nodes.get(v, {}).get("kind") != "RootCause":
                continue
            if v == dst:
                return path + [v]
            if v not in seen:
                seen.add(v)
                stack.append((v, path + [v]))
    return None


def collapse_chain(causes: list[str]) -> dict:
    """多个根因同时被确认时，判断它们是不是同一条因果链。

    级联和真·多根因是完全不同的两件事，处理方式也相反：
      级联   —— 表面两个根因，实际一条链，该改声明最上游那个；
                修下游只治标，根因会复发。
      独立   —— 真的两个不相干的故障同时发生，按单根因修必然修一半。

    返回 kind: single / cascade / independent
    """
    g = load()
    cs = [c for c in dict.fromkeys(causes) if c in g]
    if len(cs) <= 1:
        return {"kind": "single", "upstream": cs[0] if cs else None,
                "explained": [], "independent": [], "path": []}

    for c in cs:
        rest = [o for o in cs if o != c]
        paths = [_causes_reachable(g, c, o) for o in rest]
        if all(p is not None for p in paths):
            longest = max(paths, key=len)
            return {"kind": "cascade", "upstream": c, "explained": rest,
                    "independent": [], "path": longest}

    return {"kind": "independent", "upstream": None, "explained": [],
            "independent": cs, "path": []}


def map_symptoms(symptoms: list[str], fallback: bool = False) -> list[str]:
    """把人话症状描述映射成因果图的节点 id。

    这份映射原本在 loop.py 和 esc.py 各有一份副本，改一边另一边不动。
    合并到这里 —— 它属于"图的词汇表"，本就该由图这边定义。

    fallback: 假设生成需要至少一个种子症状，判孤儿症状则绝不能凭空补
    一个，否则会造出根本没观测到的"孤儿"。
    """
    out = set()
    for s in symptoms:
        low = s.lower()
        if "p99" in low or "延迟" in s or "latency" in low:
            out.add("latency_p99_up")
        if "cpu" in low:
            out.add("cpu_saturated")
        if "错误" in s or "error" in low or "吞吐" in s:
            out.add("throughput_down")
        if "阻塞" in s or "挂起" in s or "blocked" in low:
            out.add("queries_blocked")
        if "磁盘" in s or "disk" in low:
            out.add("disk_growing")
        if "连接" in s or "conn" in low:
            out.add("conn_near_limit")
        if "autovacuum" in low or "自动清理" in s:
            out.add("autovacuum_unhealthy")
    return sorted(out) or (["latency_p99_up"] if fallback else [])


def stats() -> dict:
    g = load()
    kinds: dict[str, int] = {}
    for _, d in g.nodes(data=True):
        kinds[d.get("kind", "?")] = kinds.get(d.get("kind", "?"), 0) + 1
    ekinds: dict[str, int] = {}
    for _, _, k in g.edges(keys=True):
        ekinds[k] = ekinds.get(k, 0) + 1
    return {"nodes": g.number_of_nodes(), "edges": g.number_of_edges(),
            "by_kind": kinds, "by_edge": ekinds}
