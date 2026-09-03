"""安全门 —— 类型化提案、四维风险分级、可回滚执行。

与护盾的分工：
  护盾  硬约束，命中黑名单一律拒，不看上下文
  门    在护盾允许的空间内做分级裁决：自动 / 需确认 / 拒绝

关键设计：门持有 agent_rw 凭据，agent 只有 agent_ro。所以 agent 不是
"被要求不要写库"，而是物理上没有写库的手，只能提交提案。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum

from knowledge.causal_graph import graph as causal_graph
from safety import shield, undo_journal
from safety.undo_journal import UndoStatus
from sandbox import db


class Tier(str, Enum):
    AUTO = "AUTO"        # 完全可逆、不锁、单对象
    CONFIRM = "CONFIRM"  # 可逆但重或影响面宽，需一次确认
    DENY = "DENY"        # 不可逆或灾难


class RetryPhase(str, Enum):
    PLAN = "PLAN"
    INVESTIGATE = "INVESTIGATE"
    ESCALATE = "ESCALATE"


@dataclass
class RemediationProposal:
    """类型化提案。门不接受裸 SQL —— 结构化才能做防伪校验。"""
    action_type: str
    sql: str
    rollback: str
    rationale: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    predicted_impact: dict = field(default_factory=dict)
    target: dict = field(default_factory=dict)
    # 由 Toolbox/状态机注入，不暴露为模型可填写参数。
    root_cause: str = ""
    fix_id: str = ""
    esc_verdict: str = ""
    partial_explanation: bool = False
    explanation_id: str = ""
    explanation_revision: int = 0
    selected_path_id: str = ""
    intervention_target: str = ""
    intervention_kind: str = ""
    expected_effect_nodes: list[str] = field(default_factory=list)
    expected_effects: list[dict] = field(default_factory=list)
    esc_report_id: str = ""
    unresolved_p0_paths: list[str] = field(default_factory=list)


@dataclass
class GateDecision:
    tier: str
    approved: bool
    reasons: list[str] = field(default_factory=list)
    risk: dict = field(default_factory=dict)
    shield_reasons: list[str] = field(default_factory=list)
    reason_code: str = "APPROVED"
    retry_phase: str = ""


def denial(reason_code: str, retry_phase: RetryPhase | str,
           reasons: list[str], *, risk: dict | None = None,
           shield_reasons: list[str] | None = None) -> GateDecision:
    phase = retry_phase.value if isinstance(retry_phase, RetryPhase) else retry_phase
    return GateDecision(
        tier=Tier.DENY.value, approved=False, reasons=reasons,
        risk=risk or {}, shield_reasons=shield_reasons or [],
        reason_code=reason_code, retry_phase=phase)


@dataclass
class ExecutionResult:
    executed: bool
    undo_id: str = ""
    error: str = ""
    duration_s: float = 0.0
    denied: bool = False
    decision: GateDecision | None = None


# 这些动作不破坏任何东西，也没有有意义的"撤销"：ANALYZE 只是重算统计，
# 退回失真的旧统计既做不到也没人想要。强求它们给回滚语句只会逼出假的。
# 但仍要求显式写 NO_ROLLBACK_NEEDED —— 留空分不清"想过了不需要"和"忘了写"。
SELF_CORRECTING = frozenset({"vacuum_analyze"})


# 影响面按实际表规模判定，而不是硬编码表名。
# 最初写死一份"核心表"清单，结果 schema 里四张表全在里面，AUTO 档
# 变得不可达、分级形同虚设。用行数判定既客观又能泛化到没见过的表。
LARGE_TABLE_ROWS = 1_000_000
_SIZE_CACHE: dict[str, int] = {}
_TIER_RANK = {Tier.AUTO.value: 0, Tier.CONFIRM.value: 1, Tier.DENY.value: 2}


def _graph_context(p: RemediationProposal) -> tuple[dict, dict, GateDecision | None]:
    """Resolve the graph-owned remediation policy for a typed proposal."""
    if not p.root_cause:
        return {}, {}, denial(
            "CAUSAL_BINDING_INVALID", RetryPhase.PLAN,
            ["提案缺少已确认根因上下文"])
    if not p.fix_id:
        return {}, {}, denial(
            "CAUSAL_BINDING_INVALID", RetryPhase.PLAN,
            ["提案没有绑定因果图修复节点"])

    if p.explanation_id:
        if (not p.explanation_revision or not p.selected_path_id or
                not p.intervention_target or not p.esc_report_id):
            return {}, {}, denial(
                "CAUSAL_BINDING_INVALID", RetryPhase.PLAN,
                ["v2 提案缺少解释 revision、路径、干预目标或 ESC 报告绑定"])
        if p.root_cause != p.intervention_target:
            return {}, {}, denial(
                "CAUSAL_BINDING_INVALID", RetryPhase.PLAN,
                ["提案根因字段与系统干预目标冲突"])
        if p.unresolved_p0_paths:
            return {}, {}, denial(
                "P0_MANUAL_REQUIRED", RetryPhase.ESCALATE,
                ["仍有未解决 P0 路径，禁止执行"])
        if not p.evidence_refs:
            return {}, {}, denial(
                "EVIDENCE_MISSING", RetryPhase.INVESTIGATE,
                ["v2 提案没有路径/目标的可信证据绑定"])
        if not p.expected_effect_nodes or not p.expected_effects:
            return {}, {}, denial(
                "CAUSAL_BINDING_INVALID", RetryPhase.PLAN,
                ["v2 提案缺少路径下游的结构化预期效果"])

    fixes = {f["fix"]: f for f in causal_graph.fixes_for(p.root_cause)}
    fix = fixes.get(p.fix_id)
    if not fix:
        return {}, {}, denial(
            "CAUSAL_BINDING_INVALID", RetryPhase.PLAN,
            [f"修复节点 {p.fix_id} 不属于根因 {p.root_cause}"])

    severity = causal_graph.severity_of(p.root_cause)
    risk = {
        "root_cause": p.root_cause,
        "severity": severity,
        "fix_id": p.fix_id,
        "graph_risk_tier": fix.get("risk_tier", Tier.CONFIRM.value),
        "execution": fix.get("execution", "gated"),
        "intervention_kind": fix.get("intervention_kind", "CORRECTIVE"),
    }
    if fix.get("execution") == "escalate_only":
        code = ("P0_MANUAL_REQUIRED" if severity == "P0" else "MANUAL_ONLY")
        return fix, risk, denial(
            code, RetryPhase.ESCALATE,
            [f"修复 {p.fix_id} 只能升级人工，禁止由 agent 执行"], risk=risk)
    if fix.get("risk_tier") == Tier.DENY.value:
        return fix, risk, denial(
            "MANUAL_ONLY", RetryPhase.ESCALATE,
            [f"修复 {p.fix_id} 的图策略为 DENY"], risk=risk)
    if severity == "P0":
        if p.esc_verdict != "SUFFICIENT":
            return fix, risk, denial(
                "EVIDENCE_MISSING", RetryPhase.INVESTIGATE,
                ["P0 修复必须先通过 ESC 证据充分性检查"], risk=risk)
        if not p.evidence_refs:
            return fix, risk, denial(
                "EVIDENCE_MISSING", RetryPhase.INVESTIGATE,
                ["P0 修复必须携带可审计的原始证据引用"], risk=risk)
    return fix, risk, None


def _table_rows(table: str) -> int:
    """返回估算行数；-1 表示查不到（按保守处理）。"""
    if table in _SIZE_CACHE:
        return _SIZE_CACHE[table]
    try:
        r = db.query("SELECT reltuples::bigint FROM pg_class WHERE relname = %s",
                     (table,), role="ro")
        n = int(r[0][0]) if r else -1
    except Exception:
        n = -1
    _SIZE_CACHE[table] = n
    return n


def _blast_radius(sql: str) -> str:
    tables = set(re.findall(r"\bON\s+(\w+)|\bFROM\s+(\w+)|\bTABLE\s+(\w+)",
                            sql, flags=re.I))
    flat = {t for tup in tables for t in tup if t}
    if not flat:
        return "session"
    sizes = [_table_rows(t) for t in flat]
    if any(s < 0 for s in sizes):
        return "unknown"          # 查不到规模就按大表对待
    if any(s >= LARGE_TABLE_ROWS for s in sizes):
        return "large_table"
    return "small_table"


def assess(p: RemediationProposal) -> GateDecision:
    """四维风险分级：动作类 / 可逆性 / 影响面 / 数据安全。"""
    fix, graph_risk, context_denial = _graph_context(p)
    if context_denial:
        return context_denial

    sv = shield.inspect_sql(p.sql)
    if not sv.allowed:
        return denial("SHIELD_DENIED", RetryPhase.PLAN, ["护盾拦截"],
                      risk=graph_risk, shield_reasons=sv.reasons)

    # 防伪：声明的动作类型必须与 AST 实际解析出来的一致
    actual = shield.classify(p.sql)
    if actual != p.action_type:
        return denial(
            "SQL_INVALID", RetryPhase.PLAN,
            [f"提案声称 {p.action_type}，AST 实际为 {actual}"],
            risk=graph_risk)
    if fix.get("action_type") != actual:
        return denial(
            "SQL_INVALID", RetryPhase.PLAN,
            [f"修复节点 {p.fix_id} 声明 {fix.get('action_type')}，"
             f"提案实际为 {actual}"], risk=graph_risk)

    if not p.rollback or not p.rollback.strip():
        # 终止会话这类动作本质上不可撤销，强求回滚语句只会逼出假的，
        # 反而制造"以为能回滚"的错觉。改为要求显式承认不可逆。
        if actual == "session_control":
            return denial(
                "ROLLBACK_INVALID", RetryPhase.PLAN,
                ["会话控制不可撤销，rollback 请显式写 IRREVERSIBLE 以示知情"])
        if actual in SELF_CORRECTING:
            return denial(
                "ROLLBACK_INVALID", RetryPhase.PLAN,
                [f"{actual} 无需回滚，rollback 请显式写 "
                 f"NO_ROLLBACK_NEEDED 以示知情"])
        return denial("ROLLBACK_INVALID", RetryPhase.PLAN, ["缺少回滚语句"])

    if p.rollback.strip().upper() == "NO_ROLLBACK_NEEDED":
        if actual not in SELF_CORRECTING:
            return denial(
                "ROLLBACK_INVALID", RetryPhase.PLAN,
                [f"{actual} 会改变数据或结构，不能声明无需回滚"])

    if p.rollback.strip().upper() == "IRREVERSIBLE":
        if actual != "session_control":
            return denial("ROLLBACK_INVALID", RetryPhase.PLAN,
                          [f"{actual} 不允许标记为不可逆"])
        return GateDecision(Tier.CONFIRM.value, True,
                            ["终止会话不可撤销，已显式声明并需人工确认"],
                            {**graph_risk,
                             "action_class": actual, "reversible": False,
                             "locks_table": False,
                             "blast_radius": "session",
                             "touches_data": False})

    # 声明标记不是 SQL，不能拿去解析（IRREVERSIBLE 在上面已提前返回，
    # NO_ROLLBACK_NEEDED 会走到这里，两者都得跳过护盾）
    if not undo_journal.is_marker(p.rollback):
        rb = shield.inspect_sql(p.rollback)
        if not rb.allowed and "DROP" not in p.rollback.upper():
            return denial("ROLLBACK_INVALID", RetryPhase.PLAN,
                          ["回滚语句本身不合法"],
                          shield_reasons=rb.reasons)

    concurrent = shield.is_concurrent_index(p.sql)
    radius = _blast_radius(p.sql)
    risk = {**graph_risk,
        "action_class": actual,
        "reversible": True,
        "locks_table": (actual == "create_index" and not concurrent),
        "blast_radius": radius,
        "touches_data": actual in ("dml_update", "dml_delete"),
    }

    reasons: list[str] = []
    tier = Tier.AUTO

    if risk["locks_table"]:
        # 非 CONCURRENTLY 的建索引会持有写锁，大表上等同于停服
        if radius in ("large_table", "unknown"):
            tier = Tier.DENY
            reasons.append(f"{radius} 上的非 CONCURRENTLY 建索引会锁表，拒绝")
        else:
            tier = Tier.CONFIRM
            reasons.append("非 CONCURRENTLY 建索引会持有写锁")
    elif actual == "create_index":
        if radius in ("large_table", "unknown"):
            tier = Tier.CONFIRM
            reasons.append(f"{radius} 上建索引：不锁表但耗 IO 且耗时")
        else:
            reasons.append("小表上并发建索引，可自动执行")
    elif actual == "vacuum_analyze":
        if "VACUUM FULL" in p.sql.upper():
            tier = Tier.DENY
            reasons.append("VACUUM FULL 会重写整表并持排他锁")
        elif p.sql.strip().upper().startswith("ANALYZE"):
            reasons.append("ANALYZE 只更新统计信息，可自动执行")
        else:
            tier = Tier.CONFIRM
            reasons.append("VACUUM 耗 IO")
    elif actual == "set_parameter":
        tier = Tier.CONFIRM
        reasons.append("参数变更需确认")
    elif actual == "alter_table_options":
        tier = Tier.CONFIRM
        reasons.append("表存储参数变更需确认")
    elif actual == "session_control":
        # 终止会话会让对方的事务回滚。这不可撤销 —— 但它恰恰是锁竞争
        # 唯一有效的处置手段，一律拒绝等于让这类故障无解。
        # 归到需确认档，由人来担这个责任。
        tier = Tier.CONFIRM
        risk["reversible"] = False
        reasons.append("终止会话不可撤销，会让对方事务回滚，需人工确认")
    elif actual in ("config_reload", "maintenance", "replication_control"):
        tier = Tier.CONFIRM
        if actual == "replication_control":
            # 丢弃复制槽撤不回来：备库从此断流，必须重做基础备份。
            # 和终止会话同理，标成不可逆由人担责，而不是假装能回滚。
            risk["reversible"] = False
            reasons.append("丢弃复制槽不可撤销，备库需重做基础备份，需人工确认")
        else:
            reasons.append(f"{actual} 类动作影响面较大，需确认")
    elif risk["touches_data"]:
        tier = Tier.CONFIRM
        reasons.append("直接修改数据行")
    else:
        tier = Tier.CONFIRM
        reasons.append("未归类动作，保守起见需确认")

    # 图上的 risk_tier 是最低门槛；SQL 形态和实际影响面只能继续抬高。
    graph_tier = str(fix.get("risk_tier", Tier.CONFIRM.value))
    if graph_tier not in _TIER_RANK:
        graph_tier = Tier.CONFIRM.value
        reasons.append("因果图风险档位非法，保守按 CONFIRM 处理")
    if _TIER_RANK.get(graph_tier, 1) > _TIER_RANK[tier.value]:
        tier = Tier(graph_tier)
        reasons.append(f"因果图将修复 {p.fix_id} 的最低门槛设为 {graph_tier}")
    if graph_risk.get("severity") == "P0" and tier is Tier.AUTO:
        tier = Tier.CONFIRM
        reasons.append("P0 修复禁止自动执行，至少需要人工确认")
    if p.partial_explanation and tier is Tier.AUTO:
        tier = Tier.CONFIRM
        reasons.append("PARTIAL 解释只能处置已选路径，禁止自动执行")

    if (fix.get("intervention_kind") == "CONTAINMENT" and
            tier is Tier.AUTO):
        tier = Tier.CONFIRM
        reasons.append("CONTAINMENT 只能限制影响，禁止自动执行")

    if tier is Tier.DENY:
        return denial("SQL_INVALID", RetryPhase.PLAN, reasons,
                      risk=risk, shield_reasons=sv.reasons)
    return GateDecision(tier.value, True, reasons, risk, sv.reasons,
                        reason_code="APPROVED", retry_phase="")


def _preflight(p: RemediationProposal) -> tuple[bool, str]:
    """执行前检查：批准不等于立刻执行。"""
    if shield.classify(p.sql) == "create_index":
        m = re.search(r"\bON\s+(\w+)\s*\(([^)]+)\)", p.sql, flags=re.I)
        if m:
            table, cols = m.group(1), m.group(2)
            # 磁盘余量：建索引需要额外空间
            try:
                free = db.query(
                    "SELECT pg_size_pretty(pg_database_size(current_database()))",
                    role="rw")[0][0]
                return True, f"目标 {table}({cols})，库大小 {free}"
            except Exception as exc:
                return True, f"预检查部分跳过: {exc}"
    return True, ""


def execute(p: RemediationProposal, episode_id: str,
            confirm_cb=None) -> ExecutionResult:
    """护盾 -> 分级 -> 确认 -> 先写 journal -> 执行。"""
    d = assess(p)
    if not d.approved:
        return ExecutionResult(False, denied=True, decision=d,
                               error="; ".join(d.reasons + d.shield_reasons))

    if d.tier == Tier.CONFIRM.value:
        if confirm_cb is None:
            return ExecutionResult(False, denied=True, decision=d,
                                   error="需要人工确认但未提供确认通道")
        if not confirm_cb(p, d):
            return ExecutionResult(False, denied=True, decision=d,
                                   error="人工确认被拒绝")

    _preflight(p)

    rec = undo_journal.append(episode_id, p.action_type, p.sql, p.rollback)
    t0 = time.time()
    try:
        with db.connect(role="rw", autocommit=True) as conn, conn.cursor() as cur:
            # 拿不到锁就放弃，绝不无限挂住生产库
            cur.execute("SET lock_timeout = '5s'")
            cur.execute("SET statement_timeout = '10min'")
            cur.execute(p.sql)
        undo_journal.mark(rec.undo_id, UndoStatus.APPLIED)
        return ExecutionResult(True, rec.undo_id, duration_s=round(time.time() - t0, 1),
                               decision=d)
    except Exception as exc:
        undo_journal.mark(rec.undo_id, UndoStatus.FAILED, str(exc))
        return ExecutionResult(False, rec.undo_id, error=str(exc),
                               duration_s=round(time.time() - t0, 1), decision=d)


def rollback(undo_id: str) -> tuple[bool, str]:
    """撤销一次已执行的变更。失败即冻结并升级人工，绝不重试。"""
    rec = undo_journal.get(undo_id)
    if not rec:
        return False, f"找不到回滚记录 {undo_id}"
    if rec.get("status") == UndoStatus.REVERTED.value:
        return True, "已经撤销过（幂等）"
    if undo_journal.is_marker(rec.get("undo_sql", "")):
        # 不是失败，是这个动作本来就撤不回来 —— 提案时已显式声明并经人工确认。
        # 当成"回滚失败"会误判成需人工介入的严重情形。
        undo_journal.mark(undo_id, UndoStatus.APPLIED,
                          "该动作不可撤销，提案时已显式声明")
        return True, "该动作不可撤销（提案时已声明 IRREVERSIBLE），无需回滚"
    try:
        with db.connect(role="rw", autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SET lock_timeout = '5s'")
            cur.execute(rec["undo_sql"])
        undo_journal.mark(undo_id, UndoStatus.REVERTED)
        return True, rec["undo_sql"]
    except Exception as exc:
        undo_journal.mark(undo_id, UndoStatus.UNDO_FAILED, str(exc))
        return False, f"撤销失败（需人工介入）: {exc}"
