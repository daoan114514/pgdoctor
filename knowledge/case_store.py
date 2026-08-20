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

import json
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "knowledge" / "cases"


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
        if e["evidence_type"] != "session_wait_profile":
            continue
        for w in re.findall(r"'([A-Za-z]+:[A-Za-z]+)'", e["observation"]):
            waits[w.split(":")[0]] = waits.get(w.split(":")[0], 0) + 1
        if "等待事件=无" in e["observation"] or "等待事件=set()" in e["observation"]:
            waits["none"] = waits.get("none", 0) + 1

    has_lock = any("阻塞链" in e["observation"] and "0 条" not in e["observation"]
                   for e in st.scratchpad
                   if e["evidence_type"] == "lock_blocking_chain")

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
                if e["evidence_type"] in ("explain_seq_scan", "index_existence",
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
        investigation_path=[e["evidence_type"] for e in st.scratchpad][:12],
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
