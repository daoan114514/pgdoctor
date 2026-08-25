"""故障因果图的加载与遍历。

用 networkx + YAML 而不是图数据库：几百个节点，内存里跑绰绰有余，
而且 YAML 能进 git —— 图的演化直接体现在 commit 历史里，这本身就是
"可审计的自进化"的实物证据。

给 agent 的接口是若干查询函数，**绝不把整张图塞进上下文**。
"""
from __future__ import annotations

import functools
from pathlib import Path

import networkx as nx
import yaml

HERE = Path(__file__).resolve().parent


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
    if not extra:
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

    for e in edges.get("causes_symptom", []) or []:
        g.add_edge(e["from"], e["to"], key="CAUSES",
                   likelihood=e.get("likelihood", 0.5))
    for e in edges.get("causes_cause", []) or []:
        g.add_edge(e["from"], e["to"], key="CAUSES",
                   likelihood=e.get("likelihood", 0.5), note=e.get("note", ""))
    for e in edges.get("confirmed_by", []) or []:
        g.add_edge(e["cause"], e["evidence"], key="CONFIRMED_BY",
                   necessity=e.get("necessity", "supporting"))
    for e in edges.get("refuted_by", []) or []:
        g.add_edge(e["cause"], e["evidence"], key="REFUTED_BY",
                   when=e.get("when", ""))
    for e in edges.get("discriminates", []) or []:
        g.add_edge(e["evidence"], e["evidence"], key="DISCRIMINATES",
                   separates=e.get("separates", []), power=e.get("power", 0.5))
    for e in edges.get("fixed_by", []) or []:
        g.add_edge(e["cause"], e["fix"], key="FIXED_BY")
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


def _learned_likelihood_adj() -> dict:
    """L3 学到的"某根因导致某症状"的调整量。

    与 prior_adj 的分工：prior_adj 调的是根因自身的基础可能性，这个调
    的是具体一条因果边的权重。同一个根因在不同症状组合下的可能性并不
    相同，这层信息 prior_adj 表达不了 —— 之前这份数据写了从来没人读，
    等于把 L3 学到的一半直接扔掉。
    """
    try:
        from knowledge.evolution import load_delta
        return load_delta().likelihood_adj
    except Exception:
        return {}


def candidate_causes(symptoms: list[str], max_hops: int = 3,
                     top_k: int = 5, use_learned: bool = True) -> list[dict]:
    """从症状反向多跳遍历，得到候选根因并按可能性排序。

    多跳是关键：级联故障里真根因离症状好几跳，单跳等于退化成查找表。
    """
    g = load()
    adj = _learned_adj() if use_learned else {}
    ladj = _learned_likelihood_adj() if use_learned else {}
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
            "desc": g.nodes[c].get("desc", "")}
           for c, sc in sorted(scores.items(), key=lambda x: -x[1])]
    return out[:top_k]


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
    return [{"evidence": v, "when": d.get("when", "")}
            for u, v, k, d in g.out_edges(root_cause, keys=True, data=True)
            if k == "REFUTED_BY"]


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
                        "rollback": d.get("rollback")})
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
