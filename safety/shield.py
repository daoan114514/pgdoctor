"""护盾 —— 硬约束层，不可协商。

不管模型多自信、提示词怎么写、将来奖励函数怎么给，命中黑名单的动作
一律拦下。它保证的是安全下界：越狱或幻觉也炸不了库。

必须基于 AST 而不是正则：正则挡不住
    CREATE INDEX x ON t(c); DROP TABLE orders
这种夹带，而 pglast 会把它解析成两条语句，第二条直接命中黑名单。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pglast import parse_sql
from pglast.stream import RawStream


@dataclass
class ShieldVerdict:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)
    stmt_kinds: list[str] = field(default_factory=list)


# 灾难动作：无论上下文如何都拒绝
FORBIDDEN_STMT = {
    "DropStmt": "DROP 对象",
    "TruncateStmt": "TRUNCATE",
    "DropdbStmt": "DROP DATABASE",
    "DropRoleStmt": "DROP ROLE",
    "DropTableSpaceStmt": "DROP TABLESPACE",
    "AlterSystemStmt": "ALTER SYSTEM（改全局配置且需重载）",
    "GrantStmt": "权限变更",
    "GrantRoleStmt": "角色授予",
    "CreateRoleStmt": "创建角色",
    "AlterRoleStmt": "修改角色",
    "RenameStmt": "重命名对象",
    "CreatedbStmt": "创建数据库",
    "ClusterStmt": "CLUSTER（重写整表并持有排他锁）",
}

# 允许出现在提案里的语句类型（仍需经过分级门）
ALLOWED_STMT = {
    "IndexStmt",        # CREATE INDEX
    "VacuumStmt",       # VACUUM / ANALYZE
    "VariableSetStmt",  # SET
    "AlterTableStmt",   # 仅限受控子类型，见下
    "SelectStmt",       # 只读探测
    "UpdateStmt",
    "DeleteStmt",
}

# AlterTable 里只放行改存储参数一类；加列删列改类型都拒绝
ALTER_SUBTYPE_ALLOW = {"AT_SetRelOptions", "AT_ResetRelOptions"}

# 语法上是 SELECT、语义上却有强副作用的函数。
# 只看语句类型会把它们当成只读放行 —— pg_terminate_backend 会直接
# 掐断别人的连接，这跟"只读查询"是两回事，必须单独归类并过门。
SIDE_EFFECT_FUNCS = {
    "pg_terminate_backend": "session_control",
    "pg_cancel_backend": "session_control",
    "pg_reload_conf": "config_reload",
    "pg_rotate_logfile": "maintenance",
    "pg_switch_wal": "maintenance",
    "pg_promote": "replication_control",
    "pg_drop_replication_slot": "replication_control",
    "pg_create_restore_point": "maintenance",
    "hypopg_reset": "noop",
}


def _side_effect_func(sql: str) -> str | None:
    """SQL 里是否调用了有副作用的函数；返回它对应的动作类型。"""
    low = sql.lower()
    for fn, kind in SIDE_EFFECT_FUNCS.items():
        if re.search(r"\b" + re.escape(fn) + r"\s*\(", low):
            return kind
    return None


def _node_name(node) -> str:
    return type(node).__name__


def _walk(node, hits: list[str]) -> None:
    """递归找嵌套语句 —— CTE、子查询里也可能藏 DDL/DML。"""
    if node is None:
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            _walk(x, hits)
        return
    name = _node_name(node)
    if name in FORBIDDEN_STMT:
        hits.append(name)
    for attr in getattr(node, "__slots__", ()) or ():
        try:
            _walk(getattr(node, attr, None), hits)
        except Exception:
            pass


def inspect_sql(sql: str) -> ShieldVerdict:
    v = ShieldVerdict(allowed=True)
    try:
        tree = parse_sql(sql)
    except Exception as exc:
        return ShieldVerdict(False, [f"SQL 无法解析: {exc}"])

    if not tree:
        return ShieldVerdict(False, ["空语句"])

    for raw in tree:
        stmt = raw.stmt
        kind = _node_name(stmt)
        v.stmt_kinds.append(kind)
        try:
            v.statements.append(RawStream()(stmt))
        except Exception:
            v.statements.append(kind)

        if kind in FORBIDDEN_STMT:
            v.allowed = False
            v.reasons.append(f"灾难动作被护盾拦下: {FORBIDDEN_STMT[kind]}")
            continue

        if kind not in ALLOWED_STMT:
            v.allowed = False
            v.reasons.append(f"不在允许集合内的语句类型: {kind}")
            continue

        if kind == "AlterTableStmt":
            for cmd in (stmt.cmds or []):
                sub = str(getattr(cmd, "subtype", ""))
                if not any(a in sub for a in ALTER_SUBTYPE_ALLOW):
                    v.allowed = False
                    v.reasons.append(f"ALTER TABLE 子类型不被允许: {sub}")

        if kind in ("UpdateStmt", "DeleteStmt") and stmt.whereClause is None:
            v.allowed = False
            v.reasons.append(f"{kind} 缺少 WHERE 子句，将影响全表")

        # 嵌套结构里藏的灾难动作
        nested: list[str] = []
        _walk(stmt, nested)
        for n in set(nested):
            if n != kind:
                v.allowed = False
                v.reasons.append(f"嵌套结构中发现灾难动作: {FORBIDDEN_STMT[n]}")

    # 多语句本身可疑：提案应当是单一动作，便于回滚与审计
    if len(tree) > 1:
        v.allowed = False
        v.reasons.append(f"提案含 {len(tree)} 条语句；一个提案只能有一个动作")

    return v


def classify(sql: str) -> str:
    """给分级门用的动作类型。"""
    try:
        tree = parse_sql(sql)
    except Exception:
        return "unparseable"
    if not tree:
        return "empty"
    kind = _node_name(tree[0].stmt)
    # 先看有没有副作用函数：语法类型在这里会骗人
    se = _side_effect_func(sql)
    if se and kind == "SelectStmt":
        return se
    return {
        "IndexStmt": "create_index",
        "VacuumStmt": "vacuum_analyze",
        "VariableSetStmt": "set_parameter",
        "AlterTableStmt": "alter_table_options",
        "UpdateStmt": "dml_update",
        "DeleteStmt": "dml_delete",
        "SelectStmt": "select",
    }.get(kind, kind)


def is_concurrent_index(sql: str) -> bool:
    """CREATE INDEX CONCURRENTLY 不锁表，是分级的关键依据。"""
    try:
        tree = parse_sql(sql)
        stmt = tree[0].stmt
        return bool(getattr(stmt, "concurrent", False))
    except Exception:
        return False
