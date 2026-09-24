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

from safety import undo_journal


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


# 只读探测（explain_query）里也不许出现的函数：不改数据，但会睡眠、占锁、读文件、
# 改会话或掐别人的连接 —— 只读连接挡的是写权限，挡不住这些。
READONLY_DENY_FUNCS = frozenset(SIDE_EFFECT_FUNCS) | frozenset({
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_advisory_lock", "pg_advisory_lock_shared", "pg_advisory_xact_lock",
    "pg_advisory_xact_lock_shared", "pg_try_advisory_lock",
    "pg_try_advisory_lock_shared", "pg_try_advisory_xact_lock",
    "pg_try_advisory_xact_lock_shared",
    "set_config", "pg_notify", "nextval", "setval",
    "lo_import", "lo_export", "lo_unlink", "dblink", "dblink_exec",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "pg_backup_start", "pg_backup_stop", "pg_start_backup", "pg_stop_backup",
})
# 一条语句内部允许再嵌套的语句类型：只有子查询。数据修改 CTE
# （WITH d AS (DELETE ...) SELECT ...）在 pglast 里是 SelectStmt 套 DeleteStmt，
# 顶层类型看不出来 —— 2026-09-23 审计发现 vacuum_analyze 的回滚白名单曾因此放行
# 一条会以 agent_rw 执行的 DELETE。
NESTED_ALLOW = frozenset({"SelectStmt"})
SESSION_CONTROL_FUNCS = frozenset({"pg_terminate_backend", "pg_cancel_backend"})
READONLY_STMT = frozenset({"SelectStmt", "UpdateStmt", "DeleteStmt"})


def _scan(node, kinds: list[str], funcs: list[str]) -> None:
    """递归收集 AST 里全部语句节点类型与被调用的函数名（小写、去 schema）。"""
    if node is None:
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            _scan(x, kinds, funcs)
        return
    name = _node_name(node)
    if name.endswith("Stmt"):
        kinds.append(name)
    if name == "FuncCall":
        parts = [str(getattr(p, "sval", "") or "") for p in (node.funcname or ())]
        if parts:
            funcs.append(parts[-1].lower())
    for attr in getattr(node, "__slots__", ()) or ():
        try:
            _scan(getattr(node, attr, None), kinds, funcs)
        except Exception:
            pass


def called_functions(sql: str) -> list[str]:
    try:
        tree = parse_sql(sql)
    except Exception:
        return []
    kinds: list[str] = []
    funcs: list[str] = []
    _scan([raw.stmt for raw in tree], kinds, funcs)
    return funcs


def _side_effect_func(sql: str) -> str | None:
    """SQL 里是否调用了有副作用的函数；返回它对应的动作类型。

    按 AST 的 FuncCall 判，不再用正则：`pg_terminate_backend /*x*/ (1)`、
    `"pg_terminate_backend"(1)` 这类写法正则抓不到，会被当成只读 SELECT。"""
    for fn in called_functions(sql):
        if fn in SIDE_EFFECT_FUNCS:
            return SIDE_EFFECT_FUNCS[fn]
    return None


def _nested_problems(stmt, kind: str) -> list[str]:
    """语句内部不许藏别的语句，SELECT 不许 INTO / FOR UPDATE。"""
    out: list[str] = []
    kinds: list[str] = []
    funcs: list[str] = []
    _scan(stmt, kinds, funcs)
    for n in sorted(set(kinds)):
        if n == kind:
            if kind not in NESTED_ALLOW and kinds.count(kind) > 1:
                out.append(f"{kind} 内又嵌套了 {kind}（CTE），不允许")
            continue
        if n in NESTED_ALLOW:
            continue
        if n in FORBIDDEN_STMT:
            out.append(f"嵌套结构中发现灾难动作: {FORBIDDEN_STMT[n]}")
        else:
            out.append(f"{kind} 内嵌套了 {n}，不允许")
    if kind == "SelectStmt":
        if getattr(stmt, "intoClause", None) is not None:
            out.append("SELECT INTO 会创建表，不允许")
        if getattr(stmt, "lockingClause", None):
            out.append("SELECT ... FOR UPDATE/SHARE 会锁行，不允许")
    return out


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
                subtype = getattr(cmd, "subtype", "")
                # pglast 8.x 的 IntEnum.__str__ 只返回数值（例如 "34"），
                # 不能再靠 str(enum) 取得 AT_SetRelOptions。
                sub = getattr(subtype, "name", str(subtype))
                if sub not in ALTER_SUBTYPE_ALLOW:
                    v.allowed = False
                    v.reasons.append(f"ALTER TABLE 子类型不被允许: {sub}")

        if kind in ("UpdateStmt", "DeleteStmt") and stmt.whereClause is None:
            v.allowed = False
            v.reasons.append(f"{kind} 缺少 WHERE 子句，将影响全表")

        # 嵌套结构里藏的灾难动作 / 数据修改 CTE / SELECT INTO / FOR UPDATE
        problems = _nested_problems(stmt, kind)
        if problems:
            v.allowed = False
            v.reasons.extend(problems)

    # 多语句本身可疑：提案应当是单一动作，便于回滚与审计
    if len(tree) > 1:
        v.allowed = False
        v.reasons.append(f"提案含 {len(tree)} 条语句；一个提案只能有一个动作")

    return v


def _stmt_name_parts(node) -> list[str]:
    """DropStmt.objects 里的名字是 String 列表；取小写的最后一段（不带 schema）。"""
    out = []
    for obj in (getattr(node, "objects", None) or []):
        parts = obj if isinstance(obj, (list, tuple)) else [obj]
        vals = [str(getattr(x, "sval", getattr(x, "val", x))) for x in parts]
        out.append(vals[-1].lower() if vals else "")
    return out


def inspect_rollback(forward_sql: str, rollback_sql: str) -> ShieldVerdict:
    """回滚语句的 AST 白名单：按前向动作类别配对，不看字符串。

    原来的规则是"回滚过不了护盾但含 DROP 就放行"（本意只是放行建索引的逆操作
    DROP INDEX），于是 DROP TABLE orders CASCADE、`DELETE FROM orders -- drop` 都能过，
    而 gate.rollback() 以 agent_rw 直接执行日志里的 undo_sql（2026-09-23 架构评审
    发现，与 CLAUDE.md 硬规则 3 直接冲突）。这里改成：

      create_index         ↔ 恰好一条 DROP INDEX（可带 CONCURRENTLY / IF EXISTS），
                             同名索引，不带 CASCADE
      dml_update/dml_delete ↔ 一条过护盾的 UPDATE/DELETE（必须带 WHERE）
      set_parameter        ↔ 同名的 SET / RESET
      alter_table_options  ↔ 同一张表、受控子类型的 ALTER TABLE
      标记（IRREVERSIBLE / NO_ROLLBACK_NEEDED）由 gate 按类别单独校验，这里放行——
      它们永远不会被执行。
      其它组合一律拒绝。
    """
    rb = undo_journal.normalize_rollback(rollback_sql)
    if undo_journal.is_marker(rb):
        return ShieldVerdict(True, [], [rb.upper()], [rb.upper()])
    fwd = classify(forward_sql)
    try:
        tree = parse_sql(rb)
    except Exception as exc:
        return ShieldVerdict(False, [f"回滚语句无法解析: {exc}"])
    if len(tree) != 1:
        return ShieldVerdict(False, [f"回滚必须恰好一条语句，实际 {len(tree)} 条"])
    stmt = tree[0].stmt
    kind = _node_name(stmt)
    nested: list[str] = []
    _walk(stmt, nested)
    for n in set(nested):
        if n != kind:
            return ShieldVerdict(False, [f"回滚语句嵌套了灾难动作: {FORBIDDEN_STMT[n]}"])

    def deny(msg: str) -> ShieldVerdict:
        return ShieldVerdict(False, [msg], [kind])

    if fwd == "create_index":
        if kind != "DropStmt":
            return deny(f"建索引的回滚只能是 DROP INDEX，实际 {kind}")
        remove = getattr(getattr(stmt, "removeType", None), "name", str(getattr(stmt, "removeType", "")))
        if remove != "OBJECT_INDEX":
            return deny(f"回滚只允许 DROP INDEX，实际 DROP {remove}")
        behavior = getattr(getattr(stmt, "behavior", None), "name", "")
        if behavior == "DROP_CASCADE":
            return deny("回滚的 DROP INDEX 不允许 CASCADE")
        names = _stmt_name_parts(stmt)
        if len(names) != 1:
            return deny(f"回滚必须只删一个索引，实际 {len(names)} 个")
        try:
            fwd_stmt = parse_sql(forward_sql)[0].stmt
            created = str(getattr(fwd_stmt, "idxname", "") or "").lower()
        except Exception:
            created = ""
        if not created:
            return deny("前向 CREATE INDEX 没有显式索引名，无法配对回滚")
        if names[0] != created:
            return deny(f"回滚删的索引 {names[0]} 与前向创建的 {created} 不一致")
        return ShieldVerdict(True, [], [kind], [RawStream()(stmt)])

    if fwd in ("dml_update", "dml_delete"):
        if kind not in ("UpdateStmt", "DeleteStmt"):
            return deny(f"DML 的回滚只能是 UPDATE/DELETE，实际 {kind}")
        v = inspect_sql(rb)
        return v if not v.allowed else ShieldVerdict(True, [], [kind], v.statements)

    if fwd == "set_parameter":
        if kind != "VariableSetStmt":
            return deny(f"SET 的回滚只能是 SET/RESET，实际 {kind}")
        try:
            fwd_name = str(parse_sql(forward_sql)[0].stmt.name or "").lower()
        except Exception:
            fwd_name = ""
        if str(getattr(stmt, "name", "") or "").lower() != fwd_name:
            return deny(f"回滚的参数名 {getattr(stmt, 'name', '')} 与前向 {fwd_name} 不一致")
        v = inspect_sql(rb)
        return v if not v.allowed else ShieldVerdict(True, [], [kind], v.statements)

    if fwd == "alter_table_options":
        if kind != "AlterTableStmt":
            return deny(f"ALTER TABLE 的回滚只能是 ALTER TABLE，实际 {kind}")
        try:
            fwd_rel = str(parse_sql(forward_sql)[0].stmt.relation.relname or "").lower()
        except Exception:
            fwd_rel = ""
        rel = str(getattr(getattr(stmt, "relation", None), "relname", "") or "").lower()
        if rel != fwd_rel:
            return deny(f"回滚的表 {rel} 与前向 {fwd_rel} 不一致")
        v = inspect_sql(rb)
        return v if not v.allowed else ShieldVerdict(True, [], [kind], v.statements)

    if fwd == "vacuum_analyze":
        # 自愈类动作本无需回滚；允许的唯一 SQL 形态是再做一次 VACUUM/ANALYZE（不含 FULL）。
        # 原来还放行"只读 SELECT"，但只读是靠顶层类型判的：数据修改 CTE、SELECT INTO 都是
        # 顶层 SelectStmt，会以 agent_rw 真跑（2026-09-23 审计）。回滚 ANALYZE 用 SELECT
        # 本来就没有意义，干脆不放行。
        if kind != "VacuumStmt":
            return deny("VACUUM/ANALYZE 的回滚只能是 NO_ROLLBACK_NEEDED 或再一次 "
                        f"VACUUM/ANALYZE，实际 {kind}")
        if vacuum_facts(rb)["full"]:
            return deny("回滚不允许 VACUUM FULL")
        v = inspect_sql(rb)
        return v if not v.allowed else ShieldVerdict(True, [], [kind], v.statements)
    if fwd == "session_control":
        return deny("会话控制不可撤销，rollback 请写 IRREVERSIBLE")
    return deny(f"{fwd} 没有允许的回滚形态")


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


def vacuum_facts(sql: str) -> dict:
    """VACUUM/ANALYZE 的 AST 事实。FULL 必须从 options 读：`VACUUM (FULL) t` 不含子串
    "VACUUM FULL"，2026-09-23 审计发现 gate 只靠子串判 DENY，括号写法直接降成 CONFIRM。"""
    out = {"is_vacuum_stmt": False, "is_vacuumcmd": False, "full": False,
           "analyze": False, "tables": []}
    try:
        tree = parse_sql(sql)
    except Exception:
        return out
    if len(tree) != 1 or _node_name(tree[0].stmt) != "VacuumStmt":
        return out
    stmt = tree[0].stmt
    out["is_vacuum_stmt"] = True
    out["is_vacuumcmd"] = bool(getattr(stmt, "is_vacuumcmd", False))
    names = {str(getattr(d, "defname", "") or "").lower() for d in (stmt.options or ())}
    out["full"] = "full" in names
    out["analyze"] = (not out["is_vacuumcmd"]) or "analyze" in names
    out["tables"] = [str(getattr(r.relation, "relname", "") or "")
                     for r in (stmt.rels or ())]
    return out


def target_tables(sql: str) -> list[tuple[str, str]]:
    """语句涉及的全部关系 (schema, name)，从 AST 的 RangeVar 取。

    原来 gate 用正则 `ON (word)` 抓表名，`ON "orders"` 带引号就抓空，大表非并发建索引
    从 DENY 降成 CONFIRM（2026-09-23 审计）。CTE 名也会被当成关系名 —— 它查不到规模，
    gate 按 unknown 保守处理。"""
    try:
        tree = parse_sql(sql)
    except Exception:
        return []
    found: list[tuple[str, str]] = []

    def visit(node):
        if node is None:
            return
        if isinstance(node, (list, tuple)):
            for x in node:
                visit(x)
            return
        if _node_name(node) == "RangeVar":
            pair = (str(getattr(node, "schemaname", "") or ""),
                    str(getattr(node, "relname", "") or ""))
            if pair[1] and pair not in found:
                found.append(pair)
        for attr in getattr(node, "__slots__", ()) or ():
            try:
                visit(getattr(node, attr, None))
            except Exception:
                pass

    visit([raw.stmt for raw in tree])
    return found


def index_facts(sql: str) -> dict:
    """CREATE INDEX 的目标表与列（AST），给 gate 的预检查用。"""
    out = {"table": "", "schema": "", "columns": [], "name": "", "concurrent": False}
    try:
        stmt = parse_sql(sql)[0].stmt
    except Exception:
        return out
    if _node_name(stmt) != "IndexStmt":
        return out
    out["table"] = str(getattr(stmt.relation, "relname", "") or "")
    out["schema"] = str(getattr(stmt.relation, "schemaname", "") or "")
    out["columns"] = [RawStream()(item) for item in (stmt.indexParams or ())]
    out["name"] = str(getattr(stmt, "idxname", "") or "")
    out["concurrent"] = bool(getattr(stmt, "concurrent", False))
    return out


# 一个会话控制提案最多终止/取消多少个后端。连接打满时注入器持有 85 个 idle 连接，
# 一次只能杀一个在结构上达不到"使用率下降"的预期效果（2026-09-23 跑批）；上限防的是
# 一条提案把整库会话清空。每个 pid 仍要各自满足前置条件，gate 执行前还会逐个复核。
MAX_SESSION_TARGETS = 64


def inspect_session_control(sql: str) -> tuple[bool, list[str], list[int]]:
    """会话控制语句的肯定式形态：`SELECT f(<正整数常量>)[, f(<正整数常量>) ...]`，
    f 是 pg_terminate_backend 或 pg_cancel_backend 且全句同一个；没有 WITH/FROM/WHERE/
    GROUP/ORDER/LIMIT/DISTINCT/INTO/锁子句/集合运算；pid 不重复，个数 1 到
    MAX_SESSION_TARGETS。返回 (合规, 原因, pid 列表)。

    原来的前置条件只验"某个字面量 pid 被观测过"：
    `SELECT pg_terminate_backend(4242), pg_terminate_backend(pid) FROM pg_stat_activity`
    第一项合规，第二项把整库会话掐掉（2026-09-23 审计）。现在每一项都必须是常量，
    调用方对每个 pid 分别求前置条件。gate.assess 与 explanation_runtime._sql_facts
    共用这一个函数。"""
    reasons: list[str] = []
    try:
        tree = parse_sql(sql)
    except Exception as exc:
        return False, [f"SQL 无法解析: {exc}"], []
    if len(tree) != 1:
        return False, [f"必须恰好一条语句，实际 {len(tree)} 条"], []
    stmt = tree[0].stmt
    if _node_name(stmt) != "SelectStmt":
        return False, [f"会话控制必须是 SELECT，实际 {_node_name(stmt)}"], []
    for attr, label in (("withClause", "WITH"), ("fromClause", "FROM"),
                        ("whereClause", "WHERE"), ("groupClause", "GROUP BY"),
                        ("havingClause", "HAVING"), ("windowClause", "WINDOW"),
                        ("sortClause", "ORDER BY"), ("limitCount", "LIMIT"),
                        ("limitOffset", "OFFSET"), ("distinctClause", "DISTINCT"),
                        ("intoClause", "INTO"), ("lockingClause", "FOR UPDATE/SHARE"),
                        ("valuesLists", "VALUES"), ("larg", "集合运算"),
                        ("rarg", "集合运算")):
        if getattr(stmt, attr, None):
            reasons.append(f"会话控制语句不允许 {label}")
    targets = list(stmt.targetList or ())
    if not targets:
        reasons.append("目标列表为空")
    if len(targets) > MAX_SESSION_TARGETS:
        reasons.append(f"一次最多 {MAX_SESSION_TARGETS} 个会话，实际 {len(targets)} 个")
    pids: list[int] = []
    functions: set[str] = set()
    for index, target in enumerate(targets, 1):
        call = getattr(target, "val", None)
        if _node_name(call) != "FuncCall":
            reasons.append(f"第 {index} 项不是函数调用")
            continue
        name = ".".join(str(getattr(part, "sval", "") or "")
                        for part in (call.funcname or ()))
        short = name.split(".")[-1].lower()
        functions.add(short)
        if short not in SESSION_CONTROL_FUNCS:
            reasons.append(f"第 {index} 项的函数 {name} 不是会话控制函数")
        if (getattr(call, "agg_filter", None) or getattr(call, "over", None)
                or getattr(call, "agg_order", None)):
            reasons.append(f"第 {index} 项不允许 FILTER/OVER/ORDER")
        args = list(call.args or ())
        if len(args) != 1:
            reasons.append(f"第 {index} 项参数必须恰好一个，实际 {len(args)} 个")
            continue
        value = getattr(args[0], "val", None)
        ival = getattr(value, "ival", None)
        if (_node_name(args[0]) != "A_Const" or not isinstance(ival, int)
                or isinstance(ival, bool) or ival <= 0):
            reasons.append(f"第 {index} 项的 pid 必须是正整数常量")
            continue
        pids.append(ival)
    if len(functions) > 1:
        reasons.append(f"一条提案只能用一种会话控制函数，实际 {sorted(functions)}")
    if len(set(pids)) != len(pids):
        reasons.append("pid 有重复")
    ok = not reasons
    return ok, reasons, (pids if ok else [])


def _parseable(sql: str) -> str:
    """把 psycopg 的占位符（%(name)s / %s / %%）换成能解析的形态，只用于 AST 检查。

    热查询原文带 %(uid)s，执行时由 params 代入；pglast 解析不了它。2026-09-23 冒烟：
    inspect_readonly 上线后 14 次 EXPLAIN 因 "SQL 无法解析" 被拒，explain_query 的
    8 个 need 全部 ERROR —— checkall 全绿、活库才炸（CLAUDE.md 规则 5）。"""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        if sql.startswith("%(", i):
            j = sql.find(")s", i)
            if j > i:
                out.append("NULL")
                i = j + 2
                continue
        if sql.startswith("%%", i):
            out.append("%")
            i += 2
            continue
        if sql.startswith("%s", i):
            out.append("NULL")
            i += 2
            continue
        out.append(sql[i])
        i += 1
    return "".join(out)


def inspect_readonly(sql: str) -> ShieldVerdict:
    """只读探测入口（explain_query）的 AST 白名单。

    只读连接挡的是写权限，挡不住 pg_sleep、advisory lock、同角色 pg_terminate_backend、
    数据修改 CTE 这些语义副作用，也挡不住无 WHERE 的大排序把 temp 计数器污染成
    work_mem_spill 的证据（CLAUDE.md 规则 6）。"""
    try:
        tree = parse_sql(_parseable(sql))
    except Exception as exc:
        return ShieldVerdict(False, [f"SQL 无法解析: {exc}"])
    if len(tree) != 1:
        return ShieldVerdict(False, [f"只读探测必须恰好一条语句，实际 {len(tree)} 条"])
    stmt = tree[0].stmt
    kind = _node_name(stmt)
    v = ShieldVerdict(allowed=True, stmt_kinds=[kind])
    if kind not in READONLY_STMT:
        return ShieldVerdict(False, [f"只读探测只接受 SELECT/UPDATE/DELETE，实际 {kind}"],
                             stmt_kinds=[kind])
    v.reasons.extend(_nested_problems(stmt, kind))
    kinds: list[str] = []
    funcs: list[str] = []
    _scan(stmt, kinds, funcs)
    bad = sorted({f for f in funcs if f in READONLY_DENY_FUNCS})
    if bad:
        v.reasons.append(f"只读探测不允许调用 {', '.join(bad)}")
    v.allowed = not v.reasons
    return v
