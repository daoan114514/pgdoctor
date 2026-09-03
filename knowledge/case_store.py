"""案例记忆库 —— 非参数自进化的载体。

红线：**案例只影响假设的生成与排序，绝不替代取证。**
即使案例斩钉截铁说"就是缺索引"，ESC 的 D1 仍要求实际跑 EXPLAIN、
查 pg_indexes 才能通过。ESC 是案例记忆的安全带 —— 没有它，案例库
会把 agent 变成一个抄答案的机器，而抄错答案时没有任何机制能发现。

检索的关键在于：数据库事故的"相似"不是文本相似，而是**指标异常的
模式**相似。所以主力信号是结构化的症状指纹，不是向量。

自进化在这里是可审计的：案例以 YAML 落盘并进 git，这周学到了什么、
哪条被隔离了，都能 diff 出来 —— 比"模型好像变聪明了"可解释得多。
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from agent.episode_state import evidence_is_observed
from agent.explanation import stable_id

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "knowledge" / "cases"
LEARNED_V2 = ROOT / "knowledge" / "learned" / "v2"
CASES_V2 = LEARNED_V2 / "cases.yaml"


# ── 症状指纹 ──────────────────────────────────────────────────

@dataclass
class Fingerprint:
    """结构化症状指纹。

    onset 与 wait_profile 判别力最强：
      突发 + 无等待  -> 指向计划/索引类
      渐进 + 无等待  -> 指向膨胀类
      有 Lock 等待   -> 指向并发类
    这是把领域知识编码进检索键，而不是指望向量相似度自己悟出来。
    """
    metric_deltas: dict = field(default_factory=dict)   # p99/cpu 的倍数变化
    wait_profile: dict = field(default_factory=dict)    # 等待事件分布
    query_scope: str = "unknown"      # single_query_dominant / broad
    onset: str = "unknown"            # sudden / gradual
    object_scope: str = "unknown"     # single_table / multi

    def as_dict(self) -> dict:
        return asdict(self)


def _bucket(ratio: float) -> str:
    if ratio >= 20:
        return "up_20x+"
    if ratio >= 5:
        return "up_5x"
    if ratio >= 2:
        return "up_2x"
    if ratio <= 0.5:
        return "down"
    return "normal"


def fingerprint_from_state(st) -> Fingerprint:
    b, c = st.baseline_kpi or {}, st.current_kpi or {}
    deltas = {}
    for k in ("p99_ms", "p50_ms", "cpu_pct"):
        bv, cv = float(b.get(k, 0) or 0), float(c.get(k, 0) or 0)
        if bv > 0:
            deltas[k] = _bucket(cv / bv)

    waits: dict[str, int] = {}
    for e in st.scratchpad:
        if not evidence_is_observed(e):
            continue
        if e["evidence_type"] != "session_wait_profile":
            continue
        for w in re.findall(r"'([A-Za-z]+:[A-Za-z]+)'", e["observation"]):
            waits[w.split(":")[0]] = waits.get(w.split(":")[0], 0) + 1
        if "等待事件=无" in e["observation"] or "等待事件=set()" in e["observation"]:
            waits["none"] = waits.get("none", 0) + 1

    has_lock = any("阻塞链" in e["observation"] and "0 条" not in e["observation"]
                   for e in st.scratchpad
                   if e["evidence_type"] == "lock_blocking_chain"
                   and evidence_is_observed(e))

    # 本项目的场景都是注入后立刻显现，故为 sudden；
    # 膨胀类故障接入后这里要按指标变化速率判定。
    onset = "sudden"
    return Fingerprint(
        metric_deltas=deltas,
        wait_profile=waits or ({"Lock": 1} if has_lock else {"none": 1}),
        query_scope="single_query_dominant",
        onset=onset,
        object_scope="single_table",
    )


# ── 案例 ──────────────────────────────────────────────────────

@dataclass
class Case:
    case_id: str
    scenario_id: str
    provenance: str                  # sandbox / production / human_labeled
    split: str                       # train / eval
    fingerprint: dict
    env: dict
    root_cause: str
    root_cause_desc: str = ""
    decisive_evidence: list = field(default_factory=list)
    refuted_hypotheses: list = field(default_factory=list)
    fix_sql: str = ""
    fix_action_type: str = ""
    effect: dict = field(default_factory=dict)
    investigation_path: list = field(default_factory=list)
    failed_attempts: list = field(default_factory=list)   # ★ 负例
    created_at: float = field(default_factory=time.time)
    reuse_count: int = 0
    utility_score: float = 0.5
    status: str = "active"           # active / quarantined / stale

    def path(self) -> Path:
        return CASES_DIR / f"{self.case_id}.yaml"

    def save(self) -> None:
        CASES_DIR.mkdir(parents=True, exist_ok=True)
        self.path().write_text(
            yaml.safe_dump(asdict(self), allow_unicode=True, sort_keys=False),
            encoding="utf-8")


# ── 写入策略：只有被验证过的知识才进库 ─────────────────────────

def should_persist(st, score, split: str) -> tuple[bool, str]:
    """防脏记忆的第一道关。

    不知道对错的东西进库就是污染 —— 一条错案例会在之后每一次相似
    告警里把 agent 往错误方向带，而且很难追溯。
    """
    if split == "eval":
        return False, "eval 场景的案例永不入库（防污染）"
    if not st.claimed_fault_class:
        return False, "根因未知"
    if score.diagnosis and score.outcome and score.safe_pass:
        return True, "成功解决"
    if st.attempts and st.claimed_fault_class:
        return True, "含失败尝试的完整案例（负例价值高）"
    if not score.diagnosis:
        return False, "诊断未命中且无可靠结论"
    return False, "结果不足以确认"


def write_case(st, score, spec: dict, applied_sql: list[str]) -> Case | None:
    split = spec.get("split", "train")
    okay, why = should_persist(st, score, split)
    if not okay:
        return None

    fp = fingerprint_from_state(st)
    decisive = [{"type": e["evidence_type"], "obs": e["observation"][:180]}
                for e in st.scratchpad
                if evidence_is_observed(e)
                and e["evidence_type"] in ("explain_seq_scan", "index_existence",
                                          "stats_freshness", "lock_blocking_chain",
                                          "counterfactual_index", "dead_tuple_ratio")]
    refuted = [{"h": k, "why": v.note[:140]} for k, v in st.ledger.items()
               if v.verdict.startswith("REFUTED")]
    failed = [{"sql": a.sql, "verdict": a.verdict, "inference": a.inference[:160]}
              for a in st.attempts]

    c = Case(
        case_id=f"case_{int(time.time())}_{st.claimed_fault_class}",
        scenario_id=spec.get("id", "?"),
        provenance="sandbox",
        split=split,
        fingerprint=fp.as_dict(),
        env={"pg_major": "16", "table_scale": "1e7"},
        root_cause=st.claimed_fault_class,
        root_cause_desc=(st.claimed_root_cause or "")[:220],
        decisive_evidence=decisive[:6],
        refuted_hypotheses=refuted,
        fix_sql=(applied_sql[-1] if applied_sql else ""),
        fix_action_type=st.proposal.get("action_type", ""),
        effect={"p99_before": st.baseline_kpi.get("p99_ms"),
                "p99_after": st.current_kpi.get("p99_ms")},
        investigation_path=[e["evidence_type"] for e in st.scratchpad
                            if evidence_is_observed(e)][:12],
        failed_attempts=failed,
    )
    c.save()
    return c


# ── 检索：结构化过滤 + 指纹相似（主力）+ 词元重叠（兜底）──────

def _load_all() -> list[Case]:
    if not CASES_DIR.exists():
        return []
    out = []
    for p in sorted(CASES_DIR.glob("case_*.yaml")):
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8"))
            out.append(Case(**d))
        except Exception:
            continue
    return out


def _fp_sim(a: dict, b: dict) -> float:
    """指纹相似度。onset 与 wait_profile 权重最高 —— 它们判别力最强。"""
    w = {"onset": 0.30, "wait_profile": 0.28, "metric_deltas": 0.24,
         "query_scope": 0.10, "object_scope": 0.08}
    score = 0.0
    for k, weight in w.items():
        av, bv = a.get(k), b.get(k)
        if isinstance(av, dict) and isinstance(bv, dict):
            keys = set(av) | set(bv)
            if not keys:
                continue
            hit = sum(1 for x in keys if av.get(x) == bv.get(x))
            score += weight * (hit / len(keys))
        elif av is not None and av == bv:
            score += weight
    return round(score, 4)


def _tok(s: str) -> set[str]:
    return set(re.findall(r"[a-z_]{3,}|[一-鿿]{2,}", (s or "").lower()))


def _text_sim(a: str, b: str) -> float:
    ta, tb = _tok(a), _tok(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / math.sqrt(len(ta) * len(tb))


def search(fp: Fingerprint, env: dict | None = None, split: str = "train",
           top_k: int = 3, query_text: str = "") -> list[dict]:
    """混合检索。

    split 过滤是防污染的硬闸：跑 eval 时只能检索 train 案例，
    否则效果曲线就是在背答案，一问就穿。
    """
    now = time.time()
    hits = []
    for c in _load_all():
        if c.status != "active":
            continue
        if c.split != split:
            continue
        if env and c.env.get("pg_major") and env.get("pg_major") \
                and c.env["pg_major"] != env["pg_major"]:
            continue
        s_fp = _fp_sim(fp.as_dict(), c.fingerprint)
        s_txt = _text_sim(query_text, c.root_cause_desc) if query_text else 0.0
        age_days = (now - c.created_at) / 86400
        decay = 1.0 / (1.0 + age_days / 60)      # 两个月半衰，环境会变
        total = (0.75 * s_fp + 0.25 * s_txt) * (0.5 + c.utility_score) * decay
        hits.append({"case": c, "score": round(total, 4),
                     "fp_sim": s_fp, "txt_sim": round(s_txt, 3)})
    hits.sort(key=lambda x: -x["score"])
    return hits[:top_k]


def render_prior(hits: list[dict], budget_chars: int = 700) -> str:
    """压缩成先验注入 —— 渐进式披露，详情按需用 fetch_case 取。

    绝不把案例全文塞进上下文：那是 context bloat 的经典错误，而且
    会诱导 agent 直接抄结论。
    """
    if not hits:
        return ""
    lines = ["[案例先验] 相似历史事故:"]
    counts: dict[str, int] = {}
    for h in hits:
        counts[h["case"].root_cause] = counts.get(h["case"].root_cause, 0) + 1
    for rc, n in sorted(counts.items(), key=lambda x: -x[1]):
        lines.append(f"  · {n} 例根因 = {rc}")
    for h in hits:
        c = h["case"]
        if c.decisive_evidence:
            lines.append(f"  · 决定性证据类型: "
                         f"{[e['type'] for e in c.decisive_evidence][:3]}")
            break
    for h in hits:
        for fa in h["case"].failed_attempts:
            lines.append(f"  ⚠ 负例: 曾试过 {fa['sql'][:56]} -> {fa['verdict']}")
            break
        else:
            continue
        break
    path = hits[0]["case"].investigation_path[:5]
    if path:
        lines.append(f"  · 有效取证顺序: {' -> '.join(path)}")
    lines.append("  （案例只是先验，不能替代取证；结论仍需 ESC 的直接证据）")
    return "\n".join(lines)[:budget_chars]


def fetch_case(case_id: str) -> dict | None:
    p = CASES_DIR / f"{case_id}.yaml"
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ── 记忆治理：效用追踪与隔离 ───────────────────────────────────

def record_reuse(case_id: str, helped: bool) -> None:
    """案例被采纳后，按后续结局更新效用；连续帮倒忙就隔离。"""
    d = fetch_case(case_id)
    if not d:
        return
    c = Case(**d)
    c.reuse_count += 1
    c.utility_score = round(
        max(0.0, min(1.0, c.utility_score + (0.1 if helped else -0.2))), 3)
    if c.utility_score <= 0.1 and c.reuse_count >= 3:
        c.status = "quarantined"     # 留档待审，不再召回
    c.save()


def library_stats() -> dict:
    cases = _load_all()
    by_rc: dict[str, int] = {}
    for c in cases:
        by_rc[c.root_cause] = by_rc.get(c.root_cause, 0) + 1
    return {
        "total": len(cases),
        "active": sum(1 for c in cases if c.status == "active"),
        "quarantined": sum(1 for c in cases if c.status == "quarantined"),
        "train": sum(1 for c in cases if c.split == "train"),
        "eval": sum(1 for c in cases if c.split == "eval"),
        "with_negatives": sum(1 for c in cases if c.failed_attempts),
        "by_root_cause": by_rc,
    }


# ---------------------------------------------------------------------------
# v2 case memory.  The v1 directory above is an immutable compatibility and
# audit surface for the explanation runtime; v2 never imports it implicitly.

@dataclass
class CaseV2:
    case_id: str
    scenario_id: str
    provenance: str
    split: str
    fingerprint: dict
    env: dict
    graph_version: str
    observed_symptoms: list[str] = field(default_factory=list)
    candidate_paths: list[dict] = field(default_factory=list)
    selected_path_ids: list[str] = field(default_factory=list)
    decisive_evidence_bindings: list[dict] = field(default_factory=list)
    excluded_branches: list[dict] = field(default_factory=list)
    p0_obligations: dict[str, dict] = field(default_factory=dict)
    intervention_plan: dict = field(default_factory=dict)
    intervention_attempts: list[dict] = field(default_factory=list)
    expected_actual_effects: list[dict] = field(default_factory=list)
    outcome: str = "UNKNOWN"
    negative_examples: list[dict] = field(default_factory=list)
    # Quality and provenance are part of the retrieval contract.  Historical
    # two-line fixtures remain loadable for audit, but cannot influence online
    # recall unless an explicit review marks them eligible.
    source_refs: list[dict] = field(default_factory=list)
    trace_ref: str = ""
    evidence_quality: str = "unreviewed"
    review_status: str = "unreviewed"
    training_eligible: bool = False
    created_at: float = field(default_factory=time.time)
    reuse_count: int = 0
    reuse_episode_ids: list[str] = field(default_factory=list)
    recall_help_count: int = 0
    tool_calls_saved: int = 0
    avoided_failure_count: int = 0
    utility_score: float = 0.5
    status: str = "active"

    def to_dict(self) -> dict:
        return asdict(self)


def _load_v2_document() -> dict:
    if not CASES_V2.exists():
        return {"schema_version": 2, "cases": []}
    raw = yaml.safe_load(CASES_V2.read_text(encoding="utf-8")) or {}
    if int(raw.get("schema_version", 0)) != 2:
        return {"schema_version": 2, "cases": []}
    return raw


def load_cases_v2() -> list[CaseV2]:
    """Load only the isolated v2 store; v1 cases are never a fallback."""
    out: list[CaseV2] = []
    for item in _load_v2_document().get("cases", []) or []:
        try:
            case = CaseV2(**item)
        except (TypeError, ValueError):
            continue
        if case.split == "eval":
            continue
        out.append(case)
    return out


def save_cases_v2(cases: list[CaseV2]) -> None:
    LEARNED_V2.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": 2,
        "v1_imported": False,
        "cases": [case.to_dict() for case in sorted(
            cases, key=lambda item: item.case_id)],
    }
    CASES_V2.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8")


@lru_cache(maxsize=4096)
def _template_nodes_are_live(graph_version: str,
                             nodes: tuple[str, ...]) -> bool:
    try:
        from knowledge.causal_graph import graph as causal_graph
        graph = causal_graph.load()
        if len(nodes) < 2 or any(node not in graph for node in nodes):
            return False
        return all(graph.has_edge(src, dst, key="CAUSES")
                   for src, dst in zip(nodes, nodes[1:]))
    except Exception:
        return False


def _template_is_live(template: dict) -> bool:
    """Revalidate an old template against the promoted graph without state."""
    try:
        from knowledge.causal_graph import graph as causal_graph
        version = causal_graph.graph_version()
    except Exception:
        return False
    return _template_nodes_are_live(
        version, tuple(str(node) for node in template.get("node_ids") or []))


def search_v2(fp: Fingerprint, env: dict | None = None,
              split: str = "train", top_k: int = 3,
              query_text: str = "",
              observed_symptoms: list[str] | None = None) -> list[dict]:
    """Retrieve path templates and branch negatives, never evidence verdicts."""
    now = time.time()
    observed = set(observed_symptoms or [])
    hits: list[dict] = []
    try:
        from knowledge.causal_graph import graph as causal_graph
        live_graph_version = causal_graph.graph_version()
    except Exception:
        live_graph_version = ""
    for case in load_cases_v2():
        if (case.status != "active" or case.split != split or
                not case.training_eligible or
                case.review_status not in {"approved", "automated_trust_gate"} or
                not case.source_refs):
            continue
        if env and case.env.get("pg_major") and env.get("pg_major") and \
                case.env["pg_major"] != env["pg_major"]:
            continue
        # Candidate branches are retained for audit, but only the path selected
        # by the trusted historical outcome is a positive recall template.
        # Boosting every historical candidate would teach L1 to amplify the
        # ambiguity it was supposed to help resolve.
        templates = [dict(item) for item in case.candidate_paths
                     if item.get("selected", False) and
                     item.get("path_id") in set(case.selected_path_ids) and
                     _template_nodes_are_live(
                         live_graph_version,
                         tuple(str(node) for node in
                               item.get("node_ids") or []))]
        if observed:
            templates = [item for item in templates
                         if not item.get("observed_symptom_id") or
                         item.get("observed_symptom_id") in observed]
        if not templates:
            continue
        fp_score = _fp_sim(fp.as_dict(), case.fingerprint)
        text_haystack = " ".join(
            " ".join(item.get("node_ids") or []) for item in templates)
        text_score = _text_sim(query_text, text_haystack) if query_text else 0.0
        age_days = max(0.0, (now - case.created_at) / 86400)
        decay = 1.0 / (1.0 + age_days / 180)
        # 现算而不是读落盘字段：历史行里存的还是旧累加公式的值（库里
        # 有三条并列 1.0），拿来排序会把旧口径的偏差带进新排序。
        total = (0.85 * fp_score + 0.15 * text_score) * \
            (0.5 + utility_of_v2(case)) * decay
        hits.append({
            "case": case,
            "score": round(total, 6),
            "fp_sim": fp_score,
            "txt_sim": round(text_score, 4),
            "path_templates": templates,
            "negative_examples": list(case.negative_examples),
            "graph_revalidated": case.graph_version != "" and all(
                _template_nodes_are_live(
                    live_graph_version,
                    tuple(str(node) for node in item.get("node_ids") or []))
                for item in templates),
        })
    hits.sort(key=lambda item: (-item["score"], item["case"].case_id))
    if hits:
        # A weakly similar case can saturate the bounded graph adjustment and
        # erase the very wait/onset distinction L1 is meant to preserve.
        # Keep several genuinely close templates, but drop distant profiles.
        best_fp = float(hits[0]["fp_sim"])
        floor = max(0.5, best_fp * 0.8)
        hits = [item for item in hits if float(item["fp_sim"]) >= floor]
    return hits[:max(0, top_k)]


def render_prior_v2(hits: list[dict], budget_chars: int = 700) -> str:
    """Render path-level recall hints without projecting causal state."""
    if not hits:
        return ""
    lines = ["[v2 case path priors]"]
    for hit in hits[:3]:
        case = hit["case"]
        paths = [" -> ".join(item.get("node_ids") or [])
                 for item in hit.get("path_templates", [])]
        lines.append(f"  case={case.case_id} similarity={hit['fp_sim']}: " +
                     "; ".join(paths[:2]))
        for negative in hit.get("negative_examples", [])[:1]:
            lines.append(
                "  scoped negative: " +
                str(negative.get("fix_id") or negative.get("scope") or ""))
    lines.append("  Path templates affect recall only; current evidence is re-collected.")
    return "\n".join(lines)[:budget_chars]


def _latest_sufficient_esc(st) -> dict:
    for report in reversed(getattr(st, "esc_reports", []) or []):
        if report.get("verdict") == "SUFFICIENT":
            return report
    return {}


def should_persist_v2(st, score, split: str) -> tuple[bool, str]:
    if split == "eval":
        return False, "eval episodes never enter v2 memory"
    explanation = getattr(st, "explanation_graph", None)
    if explanation is None or not explanation.selected_path_ids:
        return False, "no selected explanation paths"
    esc_report = _latest_sufficient_esc(st)
    paths = explanation.path_map()
    selected_supported = all(
        paths[path_id].status == "SUPPORTED"
        for path_id in explanation.selected_path_ids if path_id in paths)
    positive = bool(
        esc_report and not explanation.unresolved_p0_paths() and
        selected_supported and score.diagnosis and score.outcome and
        score.safe_pass and
        any(attempt.outcome == "VERIFIED" and attempt.learnable
            for attempt in getattr(st, "intervention_attempts", [])))
    scoped_negative = any(
        attempt.learnable and attempt.failure_scope not in {"", "NONE", "EXECUTION"}
        for attempt in getattr(st, "intervention_attempts", []))
    if positive:
        return True, "verified positive explanation"
    if scoped_negative and esc_report:
        return True, "scoped intervention/path negative"
    return False, "outcome is not trustworthy learning input"


def write_case_v2(st, score, spec: dict,
                  provenance: str = "sandbox") -> CaseV2 | None:
    """Persist one path-level case after deterministic trust checks."""
    split = str(spec.get("split", "train"))
    okay, _reason = should_persist_v2(st, score, split)
    if not okay:
        return None
    if provenance not in {"sandbox", "production", "human_labeled"}:
        raise ValueError("unsupported v2 case provenance")
    explanation = st.explanation_graph
    assert explanation is not None
    path_map = explanation.path_map()
    selected = [path_map[path_id] for path_id in explanation.selected_path_ids
                if path_id in path_map]
    selected_nodes = {node for path in selected for node in path.node_ids}
    selected_edges = {edge for path in selected for edge in path.edge_ids}
    bindings = [binding.to_dict()
                for binding in explanation.evidence_bindings.values()
                if binding.is_trusted() and
                binding.predicate_result in {"SUPPORTS", "REFUTES"} and
                (set(binding.target_node_ids) & selected_nodes or
                 set(binding.target_edge_ids) & selected_edges)]
    excluded = [{"path_id": path.path_id, "status": path.status}
                for path in explanation.candidate_paths
                if path.status == "REFUTED"]
    attempts = [asdict(attempt) for attempt in st.intervention_attempts]
    negatives = [{
        "plan_id": attempt.plan_id,
        "path_id": attempt.selected_path_id,
        "fix_id": attempt.fix_id,
        "scope": attempt.failure_scope,
        "affected_edge_ids": list(attempt.affected_edge_ids),
    } for attempt in st.intervention_attempts
        if attempt.learnable and attempt.failure_scope not in {"", "NONE"}]
    case_id = stable_id("case_v2", {
        "episode_id": st.episode_id,
        "selected_path_ids": explanation.selected_path_ids,
        "outcomes": [attempt.outcome for attempt in st.intervention_attempts],
    })
    case = CaseV2(
        case_id=case_id,
        scenario_id=spec.get("id", st.scenario_id),
        provenance=provenance,
        split=split,
        fingerprint=fingerprint_from_state(st).as_dict(),
        env={"pg_major": str(spec.get("pg_major", "16")),
             "scenario_revision": int(spec.get("revision", 1))},
        graph_version=explanation.graph_version,
        observed_symptoms=list(explanation.observed_symptoms),
        candidate_paths=[{
            "path_id": path.path_id,
            "node_ids": list(path.node_ids),
            "edge_ids": list(path.edge_ids),
            "observed_symptom_id": path.observed_symptom_id,
            "selected": path.path_id in explanation.selected_path_ids,
        } for path in explanation.candidate_paths],
        selected_path_ids=list(explanation.selected_path_ids),
        decisive_evidence_bindings=bindings,
        excluded_branches=excluded,
        p0_obligations={key: value.to_dict() for key, value in
                        explanation.p0_obligations.items()},
        intervention_plan=(st.intervention_plan.to_dict()
                           if st.intervention_plan else {}),
        intervention_attempts=attempts,
        expected_actual_effects=[{
            "attempt_id": attempt.attempt_id,
            "expected": list(attempt.expected),
            "actual": list(attempt.actual),
        } for attempt in st.intervention_attempts],
        outcome=("SUCCESS" if any(a.outcome == "VERIFIED"
                                  for a in st.intervention_attempts)
                 else "SCOPED_FAILURE"),
        negative_examples=negatives,
        source_refs=list(spec.get("source_refs") or []),
        trace_ref=f"trace://{st.episode_id}",
        evidence_quality=("episode_verified" if any(
            attempt.outcome == "VERIFIED" and attempt.learnable
            for attempt in st.intervention_attempts) else
            "episode_scoped_negative"),
        review_status=("automated_trust_gate" if spec.get("source_refs")
                       else "quarantined_missing_source"),
        training_eligible=bool(spec.get("source_refs")),
    )
    cases = {item.case_id: item for item in load_cases_v2()}
    cases[case.case_id] = case
    save_cases_v2(list(cases.values()))
    return case


def fetch_case_v2(case_id: str) -> CaseV2 | None:
    return next((case for case in load_cases_v2()
                 if case.case_id == case_id), None)


# L1 效用分的先验与权重。抽成常量而不是散在公式里：这几个数直接决定
# 案例排序，改动必须看得见。
UTILITY_PRIOR_MEAN = 0.55        # 冷启动分，也是无数据时的先验均值
UTILITY_PRIOR_WEIGHT = 4.0       # 先验相当于几次虚拟观测：头几次真实结果
                                 # 不至于把分数甩来甩去
UTILITY_MISS_WEIGHT = 1.5        # 帮倒忙比帮上忙权重更高（沿用旧的不对称）
UTILITY_BONUS_SHARE = 0.3        # 省调用/避坑最多吃掉剩余空间的三成
UTILITY_QUARANTINE_MAX = 0.25    # 低于此且复用满 3 次 -> 隔离


def utility_of_v2(case: "CaseV2") -> float:
    """从计数器算平滑帮助率，而不是累加一个会顶满的分数。

    旧写法是 utility += ±delta 再钳进 [0, 1]：冷启动 0.55、帮上一次 +0.08，
    6 次就撞上限，之后帮 6 次和帮 600 次完全一样 —— 而最需要分辨力的恰恰
    是这批被反复用到的案例。实测库里 6/6、7/7、10/10 三条并列 1.0，谁更
    可信完全看不出来。累加还带路径依赖：同样的战绩换个先后顺序算出的分
    不一样。

    改成 Beta 后验均值：无数据时恰好等于先验 0.55，样本越多越贴近真实
    帮助率，分子恒小于分母所以永远取不到 1.0，再好的案例之间也还有区分度。

        6/6 -> 0.82   7/7 -> 0.836   10/10 -> 0.871   100/100 -> 0.983
        0/1 -> 0.40   0/3 -> 0.259   0/4   -> 0.22

    省下的工具调用和避开的坏修复走"剩余空间的一个比例"，所以加成再多也
    推不到上限，帮助率始终是主信号。
    """
    prior = UTILITY_PRIOR_MEAN * UTILITY_PRIOR_WEIGHT
    helps = max(0, case.recall_help_count)
    misses = max(0, case.reuse_count - helps)
    weighted = helps + UTILITY_MISS_WEIGHT * misses
    base = (helps + prior) / (weighted + UTILITY_PRIOR_WEIGHT)
    bonus = min(1.0, max(0, case.tool_calls_saved) * 0.02 +
                max(0, case.avoided_failure_count) * 0.08)
    return round(base + (1.0 - base) * UTILITY_BONUS_SHARE * bonus, 4)


def record_reuse_v2(case_id: str, *, recalled_path_ids: list[str],
                    selected_path_ids: list[str], tool_calls_saved: int = 0,
                    avoided_failure_fix_ids: list[str] | None = None,
                    episode_id: str = "") -> None:
    """Update utility using path recall, investigation cost, and bad-fix avoidance."""
    cases = {case.case_id: case for case in load_cases_v2()}
    case = cases.get(case_id)
    if case is None:
        return
    if episode_id and episode_id in case.reuse_episode_ids:
        return
    selected = set(selected_path_ids)
    helped = bool(selected.intersection(recalled_path_ids))
    case.reuse_count += 1
    if episode_id:
        case.reuse_episode_ids.append(episode_id)
    case.recall_help_count += int(helped)
    case.tool_calls_saved += max(0, int(tool_calls_saved))
    avoided = set(avoided_failure_fix_ids or [])
    known_bad = {item.get("fix_id") for item in case.negative_examples}
    case.avoided_failure_count += len(avoided & known_bad)
    # 效用分是计数器的纯函数，不再累加 —— 累加会顶满，也会让同样的
    # 战绩因先后顺序不同而算出不同的分。这里只把算好的值落盘做镜像。
    case.utility_score = utility_of_v2(case)
    if case.reuse_count >= 3 and case.utility_score <= UTILITY_QUARANTINE_MAX:
        # 阈值 0.25 是为了复刻旧行为：连续 4 次帮倒忙才隔离
        # （0/4 = 0.22 进，0/3 = 0.259 不进）。
        case.status = "quarantined"
    cases[case_id] = case
    save_cases_v2(list(cases.values()))
