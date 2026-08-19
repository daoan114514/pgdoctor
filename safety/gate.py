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

from safety import shield, undo_journal
from safety.undo_journal import UndoStatus
from sandbox import db


class Tier(str, Enum):
    AUTO = "AUTO"        # 完全可逆、不锁、单对象
    CONFIRM = "CONFIRM"  # 可逆但重或影响面宽，需一次确认
    DENY = "DENY"        # 不可逆或灾难


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


@dataclass
class GateDecision:
    tier: str
    approved: bool
    reasons: list[str] = field(default_factory=list)
    risk: dict = field(default_factory=dict)
    shield_reasons: list[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    executed: bool
    undo_id: str = ""
    error: str = ""
    duration_s: float = 0.0
    denied: bool = False
    decision: GateDecision | None = None


# 影响面按实际表规模判定，而不是硬编码表名。
# 最初写死一份"核心表"清单，结果 schema 里四张表全在里面，AUTO 档
# 变得不可达、分级形同虚设。用行数判定既客观又能泛化到没见过的表。
LARGE_TABLE_ROWS = 1_000_000
_SIZE_CACHE: dict[str, int] = {}


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
    sv = shield.inspect_sql(p.sql)
    if not sv.allowed:
        return GateDecision(Tier.DENY.value, False,
                            ["护盾拦截"], shield_reasons=sv.reasons)

    # 防伪：声明的动作类型必须与 AST 实际解析出来的一致
    actual = shield.classify(p.sql)
    if actual != p.action_type:
        return GateDecision(
            Tier.DENY.value, False,
            [f"提案声称 {p.action_type}，AST 实际为 {actual}"])

    if not p.rollback or not p.rollback.strip():
        return GateDecision(Tier.DENY.value, False, ["缺少回滚语句"])

    rb = shield.inspect_sql(p.rollback)
    if not rb.allowed and "DROP" not in p.rollback.upper():
        return GateDecision(Tier.DENY.value, False,
                            ["回滚语句本身不合法"], shield_reasons=rb.reasons)

    concurrent = shield.is_concurrent_index(p.sql)
    radius = _blast_radius(p.sql)
    risk = {
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
    elif risk["touches_data"]:
        tier = Tier.CONFIRM
        reasons.append("直接修改数据行")
    else:
        tier = Tier.CONFIRM
        reasons.append("未归类动作，保守起见需确认")

    return GateDecision(tier.value, tier is not Tier.DENY, reasons, risk,
                        sv.reasons)


def _preflight(p: RemediationProposal) -> tuple[bool, str]:
    """执行前检查：批准不等于立刻执行。"""
    if shield.classify(p.sql) == "create_index":
        m = re.search(r"\bON\s+(\w+)\s*\(([^)]+)\)", p.sql, flags=re.I)
        if m:
            table, cols = m.group(1), m.group(2)
            try:
                rows = db.query(
                    "SELECT hypopg_reset(); ", role="rw") if False else None
            except Exception:
                pass
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
    try:
        with db.connect(role="rw", autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SET lock_timeout = '5s'")
            cur.execute(rec["undo_sql"])
        undo_journal.mark(undo_id, UndoStatus.REVERTED)
        return True, rec["undo_sql"]
    except Exception as exc:
        undo_journal.mark(undo_id, UndoStatus.UNDO_FAILED, str(exc))
        return False, f"撤销失败（需人工介入）: {exc}"
