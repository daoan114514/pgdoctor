#!/usr/bin/env python3
"""安全门的字符串判定全部换成 AST / 单一归一化之后，把每条漏洞钉成断言。

2026-09-23 模型边界审计（5 分面、25 条确认）里安全门这一面的 8 条：
  vacuum_analyze 的回滚放行任意顶层 SELECT（数据修改 CTE 以 agent_rw 真跑）；
  `IRREVERSIBLE;` 带分号在三处归一化不一致，create_index 可跳过回滚配对；
  pg_terminate_backend 只验"某个字面量 pid 被观测过"，多目标 SELECT 全过；
  `VACUUM (FULL)` 括号写法绕过子串 DENY；正则抓表名对带引号的表抓空，DENY 降 CONFIRM；
  explain_query 对任意 SQL 真跑 EXPLAIN ANALYZE；fetch_raw 不校验 episode 段；
  MAIN 角色的取证入参不与热查询对照。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from safety import gate, shield, undo_journal  # noqa: E402
from safety.gate import RemediationProposal  # noqa: E402
from knowledge.causal_graph import graph as causal_graph  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))


def fix_id(root_cause: str, action_type: str) -> str:
    for f in causal_graph.fixes_for(root_cause):
        if f.get("action_type") == action_type and f.get("execution") != "escalate_only":
            return f["fix"]
    return ""


print("[1] 标记归一化只有一份（undo_journal.normalize_rollback）")
for text in ("IRREVERSIBLE;", " irreversible ;;", "NO_ROLLBACK_NEEDED;"):
    check(undo_journal.is_marker(text), f"{text!r} 是标记")
check(not undo_journal.is_marker("IRREVERSIBLE -- ok"), "带注释的不算标记（会落到 AST 配对被拒，保守方向）")
check(not undo_journal.is_marker("IRREVERSIBLE; DROP TABLE x"), "标记后夹带语句不算标记")
check(undo_journal.make_idempotent("no_rollback_needed;") == "NO_ROLLBACK_NEEDED", "journal 落盘的是归一化后的标记")
gsrc = inspect.getsource(gate)
ssrc = inspect.getsource(shield)
check('.upper() == "IRREVERSIBLE"' not in gsrc and '.upper() == "NO_ROLLBACK_NEEDED"' not in gsrc,
      "gate 里没有自己的标记比较")
check('("IRREVERSIBLE", "NO_ROLLBACK_NEEDED")' not in ssrc and "undo_journal.is_marker" in ssrc,
      "shield.inspect_rollback 走 undo_journal.is_marker")

print("[2] gate.assess：AST 事实决定档位（表规模用桩）")
gate._SIZE_CACHE.clear()
gate._table_rows = lambda table, schema="": 12_000_000 if table == "orders" else 100


def assess(sql, rb, rc, at):
    fid = fix_id(rc, at)
    return gate.assess(RemediationProposal(action_type=at, sql=sql, rollback=rb,
                                           root_cause=rc, fix_id=fid))


d = assess('CREATE INDEX idx ON "orders" (customer_id)', "DROP INDEX idx", "missing_index", "create_index")
check(d.tier == "DENY" and not d.approved, "带引号的大表非并发建索引 -> DENY（原正则抓空降成 CONFIRM）", d.reasons)
d = assess("CREATE INDEX idx ON public.orders (customer_id)", "DROP INDEX idx", "missing_index", "create_index")
check(d.tier == "DENY", "带 schema 的大表非并发建索引 -> DENY", d.reasons)
d = assess("VACUUM (FULL) orders", "NO_ROLLBACK_NEEDED", "table_bloat", "vacuum_analyze")
check(d.tier == "DENY" and not d.approved, "VACUUM (FULL) 括号写法 -> DENY（原子串判不到）", d.reasons)
d = assess("VACUUM (FULL, ANALYZE) orders", "NO_ROLLBACK_NEEDED", "table_bloat", "vacuum_analyze")
check(d.tier == "DENY", "VACUUM (FULL, ANALYZE) -> DENY", d.reasons)
d = assess("VACUUM  FULL orders", "NO_ROLLBACK_NEEDED", "table_bloat", "vacuum_analyze")
check(d.tier == "DENY", "两个空格的 VACUUM  FULL -> DENY", d.reasons)
d = assess("ANALYZE orders", "NO_ROLLBACK_NEEDED", "stale_statistics", "vacuum_analyze")
check(d.approved and d.tier == "AUTO", "ANALYZE 仍是 AUTO", d.reasons)
d = assess("CREATE INDEX CONCURRENTLY idx ON orders (customer_id)", "NO_ROLLBACK_NEEDED;", "missing_index", "create_index")
check(not d.approved and d.reason_code == "ROLLBACK_INVALID", "`NO_ROLLBACK_NEEDED;` 不能让建索引跳过回滚配对", d.reasons)
d = assess("CREATE INDEX CONCURRENTLY idx ON orders (customer_id)", "IRREVERSIBLE;", "missing_index", "create_index")
check(not d.approved and d.reason_code == "ROLLBACK_INVALID", "`IRREVERSIBLE;` 同样不能", d.reasons)
d = assess("SELECT pg_terminate_backend(4242), pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'app'",
           "IRREVERSIBLE", "lock_contention", "session_control")
check(not d.approved and d.reason_code == "SQL_INVALID", "多目标 / 带 FROM 的 terminate -> 拒绝", d.reasons)
d = assess("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'idle'", "IRREVERSIBLE", "lock_contention", "session_control")
check(not d.approved, "pid 来自子查询列 -> 拒绝", d.reasons)
d = assess("SELECT pg_terminate_backend(4242)", "IRREVERSIBLE", "lock_contention", "session_control")
check(d.approved and d.tier == "CONFIRM", "单常量 pid 的 terminate 仍是 CONFIRM", d.reasons)
d = assess("SELECT pg_cancel_backend(4242)", "IRREVERSIBLE", "lock_contention", "session_control")
check(d.approved, "pg_cancel_backend 同样接受", d.reasons)
check("re.findall" not in inspect.getsource(gate._blast_radius) and "re.search" not in inspect.getsource(gate._preflight),
      "gate 不再用正则从 SQL 文本抓表名")

print("[3] 只读探测白名单 shield.inspect_readonly（explain_query 入口）")
HOT = "SELECT * FROM orders WHERE created_at > now() - interval '1 day' AND user_id = %(uid)s"
for sql, ok, why in (
    (HOT, True, "热查询原文（带 %(uid)s 占位符）允许 —— 2026-09-23 冒烟实测被拒过"),
    ("SELECT * FROM orders WHERE id = %s AND note LIKE '100%%'", True, "位置占位符与 %% 也允许"),
    ("UPDATE orders SET status = 'X' WHERE id = 1", True, "带 WHERE 的 UPDATE 允许（走 SELECT 代理）"),
    ("SELECT pg_sleep(600)", False, "pg_sleep 拒绝"),
    ("SELECT pg_advisory_lock(1)", False, "advisory lock 拒绝"),
    ("SELECT pg_terminate_backend(1)", False, "同角色 terminate 拒绝"),
    ("WITH d AS (DELETE FROM orders WHERE id = 1 RETURNING 1) SELECT count(*) FROM d", False, "数据修改 CTE 拒绝"),
    ("SELECT * INTO orders_bak FROM orders", False, "SELECT INTO 拒绝"),
    ("SELECT * FROM orders WHERE id = 1 FOR UPDATE", False, "FOR UPDATE 拒绝"),
    ("SELECT 1; SELECT 2", False, "多语句拒绝"),
    ("CREATE INDEX i ON orders (id)", False, "DDL 拒绝"),
    ("SELECT pg_catalog.pg_terminate_backend /* x */ (1)", False, "带注释/schema 的副作用函数按 AST 仍能认出"),
):
    v = shield.inspect_readonly(sql)
    check(v.allowed is ok, f"{'允许' if ok else '拒绝'}: {why}", v.reasons)

print("[4] inspect_sql 的嵌套规则")
check(not shield.inspect_sql("WITH d AS (DELETE FROM orders WHERE id = 1 RETURNING 1) SELECT count(*) FROM d").allowed, "SELECT 套 DELETE 拒绝")
check(shield.inspect_sql("UPDATE orders SET status = 'X' WHERE id IN (SELECT id FROM orders WHERE status = 'Y')").allowed, "UPDATE 套子查询允许")
check(not shield.inspect_sql("WITH u AS (UPDATE orders SET status = 'X' WHERE id = 1 RETURNING id) UPDATE orders SET status = 'Y' WHERE id IN (SELECT id FROM u)").allowed, "UPDATE 套 UPDATE（CTE）拒绝")
check(shield.classify("SELECT pg_catalog.pg_terminate_backend /* x */ (1)") == "session_control", "classify 按 AST 认副作用函数")

print("[5] _sql_facts 的 pid 只在形态合规时绑定")
from agent import explanation_runtime as xr  # noqa: E402
check(xr._sql_facts("SELECT pg_terminate_backend(4242)")["pid"] == 4242, "单常量 -> 绑定")
check(xr._sql_facts("SELECT pg_cancel_backend(7)")["pid"] == 7, "cancel 也绑定")
check(xr._sql_facts("SELECT pg_terminate_backend(4242), pg_terminate_backend(pid) FROM pg_stat_activity")["pid"] is None, "夹带非常量目标 -> 不绑定")
check(xr._sql_facts("SELECT pg_terminate_backend(4242), pg_terminate_backend(4343)")["pids"] == [4242, 4343], "多个常量目标 -> 逐个绑定")
check(xr._sql_facts("SELECT pg_terminate_backend(4242) WHERE true")["pid"] is None, "带 WHERE -> 不绑定")
check("inspect_session_control" in inspect.getsource(xr._sql_facts), "_sql_facts 与 gate 共用 shield.inspect_session_control")

print("[6] AST 取表名 / 索引事实")
check(shield.target_tables('CREATE INDEX i ON "orders" (a)') == [("", "orders")], "带引号")
check(shield.target_tables("VACUUM public.orders") == [("public", "orders")], "带 schema")
check(shield.target_tables("ALTER TABLE orders SET (autovacuum_enabled = true)") == [("", "orders")], "ALTER TABLE")
check(shield.index_facts('CREATE INDEX CONCURRENTLY i ON "orders" (customer_id, created_at)')["columns"] == ["customer_id", "created_at"], "索引列")

print("[7] fetch_raw 只认本 episode 的 trace://<episode>/step_NNN")
from sandbox.traces import TraceStore  # noqa: E402
import tempfile  # noqa: E402
fake = SimpleNamespace(episode_id="ep_self", dir=Path(tempfile.mkdtemp()))
(fake.dir / "step_003.json").write_text('{"raw": "R3"}', encoding="utf-8")
check(TraceStore.fetch_raw(fake, "trace://ep_self/step_003") == "R3", "合法引用回取")
for bad in ("trace://ep_other/step_003", "step_003", "trace://ep_self/step_3", "trace://ep_self/../step_003"):
    try:
        TraceStore.fetch_raw(fake, bad)
        check(False, f"{bad!r} 应被拒")
    except KeyError:
        check(True, f"{bad!r} 被拒")

print("[8] MAIN 角色的取证入参也与目标对照（toolbox._enter）")
from agent.toolbox import Toolbox, PhaseViolation  # noqa: E402
from agent.state_machine import Phase  # noqa: E402
from agent.permissions import Role  # noqa: E402


def make_tb(target_context):
    tb = Toolbox.__new__(Toolbox)
    tb.task_context = None
    tb.target_context = dict(target_context) if target_context else None
    tb.role = Role.MAIN
    tb.hypothesis = None
    tb.environment_tools = None
    tb.sm = SimpleNamespace(phase=Phase.INVESTIGATE)
    tb.calls = []
    tb.st = SimpleNamespace(spend=lambda: True)
    return tb


def violates(tb, tool, target):
    try:
        tb._enter(tool, target)
        return False
    except PhaseViolation:
        return True


tb = make_tb({"hot_query": HOT, "table": "orders"})
check(not violates(tb, "explain_query", {"hot_query": HOT + " ;"}), "热查询原文（含尾分号）允许")
check(violates(tb, "explain_query", {"hot_query": "SELECT * FROM orders ORDER BY created_at"}), "别的 SQL 拒绝")
check(violates(tb, "get_indexes", {"table": "customers"}), "别的表拒绝")
check(not violates(tb, "get_indexes", {"table": "orders"}), "目标表允许")
check(not violates(tb, "fetch_raw", {"raw_ref": "trace://x/step_001"}), "raw_ref 不参与对照（由 traces 层校验）")
tb2 = make_tb({"hot_query": HOT})
check(violates(tb2, "get_indexes", {"table": "orders"}), "目标里没有 table 时拒绝而不是跳过对照")
tb3 = make_tb(None)
check(not violates(tb3, "get_indexes", {"table": "anything"}), "没有目标上下文（离线夹具）不对照")

print("[9] 表级修复的目标表必须是证据里的表（_evaluate_preconditions）")
import json  # noqa: E402
import shutil  # noqa: E402
from agent.episode_state import EpisodeState  # noqa: E402
from agent.explanation import EvidenceBinding, ExplanationScope  # noqa: E402
from knowledge.causal_graph import graph as G  # noqa: E402
from sandbox.traces import TRACE_DIR  # noqa: E402

eid = "ep_safety_ast_check_fixture"
store = TraceStore(eid)
paths = G.enumerate_causal_paths(["latency_p99_up"], use_learned=False)
path = next(p for p in paths if p.node_ids == ["missing_index", "latency_p99_up"])
explanation = G.merge_paths([path], episode_id=eid, observed_symptoms=["latency_p99_up"])


def binding(evidence_type, predicate_id, value, nodes, edges=None):
    ref = store.record("fixture", {"evidence_type": evidence_type}, json.dumps(value), value)
    return EvidenceBinding.create(
        episode_id=eid, raw_ref=ref, evidence_type=evidence_type, status="OBSERVED",
        observed_at=1_800_000_000, predicate_id=predicate_id, predicate_result="SUPPORTS",
        structured_value=value, target_node_ids=nodes, target_edge_ids=edges or [],
        fresh_until=1_900_000_000)


explanation.add_evidence_binding(binding(
    "explain_seq_scan", "explain_seq_scan_v2",
    {"scan_types": ["Seq Scan on orders"], "rows_removed_by_filter": 100_000},
    ["missing_index"], [path.edge_ids[0]]))
explanation.add_evidence_binding(binding(
    "index_existence", "index_existence_v2",
    {"table": "orders", "inventory_collected": True, "indexes": []}, ["missing_index"]))
explanation.select_paths([path.path_id], unexplained_symptoms=[], scope=ExplanationScope.FULL)
st = EpisodeState(eid, "safety_ast_fixture")
st.explanation_graph = explanation
option = {"path_id": path.path_id, "target_node_id": "missing_index",
          "fix": "create_covering_index",
          "preconditions": [{"id": "concrete_table_bound", "required": True},
                            {"id": "table_is_vacuumable", "required": True}]}


def evaluate(sql):
    return {r["condition_id"]: r for r in xr._evaluate_preconditions(st, option=option, sql=sql)}


res = evaluate("ANALYZE customers")
check(not res["concrete_table_bound"]["satisfied"] and "customers" in res["concrete_table_bound"]["reason"],
      "ANALYZE customers 在 orders 的证据上不满足 concrete_table_bound", res["concrete_table_bound"]["reason"])
check(not res["table_is_vacuumable"]["satisfied"], "table_is_vacuumable 同样不满足")
res = evaluate("ANALYZE orders")
check(res["concrete_table_bound"]["satisfied"] and res["concrete_table_bound"]["evidence_refs"],
      "ANALYZE orders 满足并带证据引用", res["concrete_table_bound"]["reason"])
res = evaluate("ALTER TABLE orders SET (autovacuum_enabled = true)")
check(res["concrete_table_bound"]["satisfied"] and not res["table_is_vacuumable"]["satisfied"],
      "ALTER TABLE 满足表绑定但不算 VACUUM")
shutil.rmtree(TRACE_DIR / eid, ignore_errors=True)

print("[10] 读取点接线")
from sandbox import db, observe  # noqa: E402
from agent import loop  # noqa: E402
from agent import toolbox as tbmod  # noqa: E402
check("_explainable(sql)" in inspect.getsource(observe.Observer.explain_query) and
      "shield.inspect_readonly" in inspect.getsource(observe._explainable),
      "observe.explain_query 先过只读白名单（经共用入口 _explainable）")
check("to_regclass" in inspect.getsource(observe.Observer.get_indexes), "get_indexes 对未知表 KeyError")
check("statement_timeout" in inspect.getsource(db.connect) and db.RO_STATEMENT_TIMEOUT_MS > 0, "只读连接带 statement_timeout")
check("Toolbox(env.observe(), st, sm, target_context=" in inspect.getsource(loop), "loop 给主 agent 注入 target_context")
check("_explain_failure_is_database_side(exc)" in inspect.getsource(tbmod.Toolbox.explain_query), "explain 失败按异常类型分流")
check("vacuum_facts" in inspect.getsource(gate.assess) and '"VACUUM FULL" in' not in inspect.getsource(gate.assess), "gate 的 VACUUM FULL 判定走 AST")

print()
if fails:
    print(f"SAFETY AST: FAIL（{len(fails)}/{checks}）")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print(f"SAFETY AST: PASS（{checks} 项）")
