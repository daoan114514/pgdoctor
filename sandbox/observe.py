"""只读观测工具层 —— agent 的眼睛。

两条铁律：
  1. 全部走 agent_ro 连接，物理上无写权限
  2. 工具内就地萃取，返回结构化摘要 + raw_ref，绝不把原文丢回上下文

经验法则：如果一个工具的返回值需要模型读一遍才知道重点在哪，
那这个工具就没写好。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sandbox import db
from sandbox.traces import TraceStore


_PLACEHOLDER = re.compile(r"%\((\w+)\)s")


def _readonly_proxy(sql: str) -> tuple[str, str]:
    """UPDATE/DELETE → 同表同 WHERE 的 SELECT 1；其它语句原样返回。

    返回 (sql, mode)。只读角色对 DML 连纯 EXPLAIN 都会 InsufficientPrivilege
    （2026-09-22 实测：lock_contention 的热查询是 UPDATE，子 agent 三个 need 因此
    不可得，ESC 直接 EXHAUSTED）。访问路径判别要的是扫描类型/索引/过滤行数，
    这些由 FROM+WHERE 决定，SELECT 代理给出的和原语句一致。psycopg 的 %(name)s
    占位符不是合法 SQL，先换成 $n 让 pglast 解析，输出时换回去。
    解析失败一律原样返回：宁可让原语句去撞权限错误，也不猜。
    """
    names: list[str] = []

    def _to_param(m):
        names.append(m.group(1))
        return f"${len(names)}"
    parsable = _PLACEHOLDER.sub(_to_param, sql)
    try:
        from pglast import ast, parse_sql
        from pglast.stream import RawStream
        stmts = parse_sql(parsable)
    except Exception:
        return sql, "analyze"
    if len(stmts) != 1:
        return sql, "analyze"
    node = stmts[0].stmt
    if not isinstance(node, (ast.UpdateStmt, ast.DeleteStmt)):
        return sql, "analyze"
    try:
        rel = RawStream()(node.relation)
        where = RawStream()(node.whereClause) if node.whereClause is not None else ""
    except Exception:
        return sql, "analyze"
    proxy = f"SELECT 1 FROM {rel}" + (f" WHERE {where}" if where else "")
    for i, name in enumerate(names, 1):
        proxy = proxy.replace(f"${i}", f"%({name})s")
    return proxy, "select_proxy"


def _explainable(sql: str) -> tuple[str, str]:
    """任何要被只读角色 EXPLAIN 的语句都先经这里：AST 白名单（拒 pg_sleep、advisory lock、
    数据修改 CTE……），再把 UPDATE/DELETE 换成同表同 WHERE 的 SELECT 代理。

    原来这两步只长在 explain_query 里，simulate_index 直接对原语句 EXPLAIN：lock_contention
    的热查询是 UPDATE，只读角色连纯 EXPLAIN UPDATE 都被拒，8 条反事实需求全部不可得
    （2026-09-23 跑批；CLAUDE.md 规则 4 —— 同一条规则只修了一个读取点）。"""
    from safety import shield
    verdict = shield.inspect_readonly(sql)
    if not verdict.allowed:
        raise ValueError("拒绝 EXPLAIN: " + "; ".join(verdict.reasons))
    return _readonly_proxy(sql)


def keep_session(state: str | None, duration_s: float, min_duration_s: float,
                 include_idle: bool) -> bool:
    """会话观测的过滤规则，抽成纯函数是为了让离线检查能钉住它。

    idle 默认不要（不是异常）；短于 min_duration_s 的 active 也不要（正常查询）。
    include_idle=True 时 idle 也要 —— 这是终止空闲会话这条修复唯一的 pid 来源。
    """
    if state == "idle":
        return include_idle
    if state == "active" and duration_s < min_duration_s:
        return False
    return True


@dataclass
class ExplainDigest:
    total_time_ms: float
    scan_types: list[str]                 # 关键：Seq Scan 还是 Index Scan
    rows_removed_by_filter: int           # 关键：缺索引故障的冒烟证据
    rows_est_vs_actual: list[tuple]       # 关键：估计偏差 -> 指向统计信息过期
    indexes_used: list[str]
    parallel_workers: int
    top_nodes: list[str]
    raw_ref: str = ""
    # "analyze"：对原语句 EXPLAIN ANALYZE；"select_proxy"：原语句是 UPDATE/DELETE，
    # 只读角色连纯 EXPLAIN 都会被拒（权限检查在 ExecutorStart，2026-09-22 实测），
    # 改为对同表同 WHERE 的 SELECT 取访问路径 —— 扫描类型/索引/过滤行数与原语句一致，
    # 但不含 ModifyTable 的写代价，消费方要按 limitation 对待。
    mode: str = "analyze"


@dataclass
class SessionDigest:
    pid: int
    state: str
    wait_event: str | None
    duration_s: float
    query_fingerprint: str                # 指纹化截断，不带完整 SQL 文本
    role: str = ""
    transaction_age_seconds: float | None = None
    backend_type: str = ""
    backend_xmin: str = ""
    is_current_diagnostic_connection: bool = False
    is_system_or_diagnostic: bool = False
    identity_rechecked: bool = True


@dataclass
class TableStats:
    table: str
    n_live_tup: int
    n_dead_tup: int
    dead_ratio: float
    last_analyze: str
    last_autovacuum: str
    total_size: str
    autovacuum_enabled: bool
    autovacuum_running: bool
    autovacuum_trigger: int
    # 统计信息的已知值域是否还盖得住实际数据。见 _stats_range_drift。
    stats_range_drift_rows: int = 0
    stats_range_drift_pct: float = 0.0
    stats_range_columns: list = field(default_factory=list)
    # 测不到的列。非空时占比只是下界，不许据此做否定裁决。
    stats_range_incomplete: list = field(default_factory=list)
    # 累计扫描计数器（原始值，窗口增量由 Toolbox 差分）。它记的是实际
    # 执行了什么，不经过规划器，所以过期统计污染不到它。
    # 已扣除本 episode 自家取证扫描（_stats_range_drift、explain_query 的 ANALYZE）的净值；
    # 原始值与扣除量另记，便于回查。
    seq_scan: int = 0
    seq_tup_read: int = 0
    idx_scan: int = 0
    seq_scan_raw: int = 0
    seq_tup_read_raw: int = 0
    idx_scan_raw: int = 0
    own_seq_scan: int = 0
    own_seq_tup_read: int = 0
    own_idx_scan: int = 0
    reltuples: int = 0
    stats_reset: str = ""
    raw_ref: str = ""


def _walk_plan(node: dict, out: dict) -> None:
    ntype = node.get("Node Type", "")
    if "Scan" in ntype:
        rel = node.get("Relation Name", "")
        out["scans"].append(ntype + (" on " + rel if rel else ""))
    if node.get("Index Name"):
        out["indexes"].append(node["Index Name"])
    out["removed"] += int(node.get("Rows Removed by Filter", 0) or 0)
    if "Plan Rows" in node and "Actual Rows" in node:
        out["est_act"].append((int(node["Plan Rows"]), int(node["Actual Rows"])))
    out["workers"] = max(out["workers"], int(node.get("Workers Launched", 0) or 0))
    at = node.get("Actual Total Time")
    if at is not None:
        out["nodes"].append((float(at), ntype))
    for child in node.get("Plans", []) or []:
        _walk_plan(child, out)


def _acc() -> dict:
    return {"scans": [], "indexes": [], "removed": 0,
            "est_act": [], "workers": 0, "nodes": []}


_SEQ_SCAN_NODES = frozenset({"Seq Scan", "Parallel Seq Scan"})
_INDEX_SCAN_NODES = frozenset({"Index Scan", "Index Only Scan", "Bitmap Index Scan"})


def _plan_own_counts(plan: dict, parent_rel: str = "",
                     out: dict | None = None) -> dict[str, dict[str, int]]:
    """EXPLAIN ANALYZE 真的执行了一遍查询，它给 pg_stat_user_tables 记的账（CLAUDE.md 规则 6）。

    从计划 JSON 按节点还原，而不是读 pg_stat_xact_user_tables：并行 worker 各自计数，
    事务级视图只看得到 leader 自己（_stats_range_drift 能关并行，热查询的 EXPLAIN 不能 ——
    关了就不是在诊断那个计划了）。计划里的 Actual Rows / Rows Removed by Filter 是
    按 loop 平均的（并行节点的 loop 数就是参与者数），乘回 loops 才是总数。

    seq_scan：每次 (重)扫描计一次 = loops；seq_tup_read：扫描交给执行器的可见元组 =
    (输出 + 被过滤) × loops；idx_scan：索引扫描每次 (重)扫描计一次。Bitmap Index Scan 节点
    不带表名，记到父节点 Bitmap Heap Scan 的表上。
    """
    out = {} if out is None else out
    ntype = plan.get("Node Type", "")
    rel = plan.get("Relation Name", "") or parent_rel
    loops = int(plan.get("Actual Loops", 0) or 0)
    if rel and loops and (ntype in _SEQ_SCAN_NODES or ntype in _INDEX_SCAN_NODES):
        acc = out.setdefault(rel, {"seq_scan": 0, "seq_tup_read": 0, "idx_scan": 0})
        if ntype in _SEQ_SCAN_NODES:
            per_loop = (float(plan.get("Actual Rows", 0) or 0)
                        + float(plan.get("Rows Removed by Filter", 0) or 0))
            acc["seq_scan"] += loops
            acc["seq_tup_read"] += int(round(per_loop * loops))
        else:
            acc["idx_scan"] += loops
    child_rel = rel if ntype in {"Bitmap Heap Scan", "BitmapAnd", "BitmapOr"} else ""
    for child in plan.get("Plans", []) or []:
        _plan_own_counts(child, child_rel, out)
    return out


def _merge_own_counts(plan_counts: dict, leader: dict, parallel: bool) -> dict[str, dict[str, int]]:
    """EXPLAIN ANALYZE 自家扫描记账：计划 JSON 与事务级视图两个来源合并。

    两个来源各缺一块：计划 JSON 含并行 worker 但不含规划期的索引端点探测（范围谓词、连接估算
    会真去索引里取最值），事务级视图（pg_stat_xact_user_tables）含规划期但只有 leader。

      顺序扫描：规划器不做顺序扫描，计划按 loop 计的是全体参与者 -> 取大即取计划，精确。
      索引扫描：非并行计划 leader 就是全部，事务级视图精确（>= 计划）-> 取大。
                并行计划两者相加：leader 的执行期扫描算了两次，是**上界**。上界是安全的方向 ——
                净 idx_scan 只会偏低，seq_scan_volume 用它只为区分"窗口里没有活动"（NEUTRAL）与
                "只有索引扫描"（REFUTES），偏低只会多给 NEUTRAL，不会凭自家的规划期探测把一个
                空窗口判成反证（规则 1）。取大在并行计划上是下界、方向反了：2026-09-24 活库实测
                统计过期热查询走并行计划，orders 原始 6 次、只记 4 次（.dev/tool_perturbation_live.py）。
    """
    zero = {"seq_scan": 0, "seq_tup_read": 0, "idx_scan": 0}
    out: dict[str, dict[str, int]] = {}
    for rel in set(plan_counts) | set(leader):
        plan = plan_counts.get(rel, zero)
        lead = leader.get(rel, zero)
        out[rel] = {
            "seq_scan": max(plan["seq_scan"], lead["seq_scan"]),
            "seq_tup_read": max(plan["seq_tup_read"], lead["seq_tup_read"]),
            "idx_scan": (plan["idx_scan"] + lead["idx_scan"] if parallel
                         else max(plan["idx_scan"], lead["idx_scan"])),
        }
    return out


def _flush_own_stats(conn) -> bool:
    """把本连接挂起的统计计数立即刷进共享统计（PG15+ pg_stat_force_next_flush）。

    自家扫描 / 外溢记账后要从原始计数器里扣掉，前提是原始计数器**已经**含有它们。
    不刷的话要等后端退出时才刷，与下一次读计数器之间是毫秒级竞态：先扣后到账，
    这一窗口的净值被低估（可能低到 0，反证了真根因），下一窗口又被高估。
    强制标志在语句结束、回到空闲时（发 ReadyForQuery 之前）生效，所以 execute
    返回时已经刷完。只能在自动提交模式下调：事务块里空闲不刷。
    """
    try:
        if not conn.autocommit:
            conn.rollback()
            conn.autocommit = True
        conn.execute("SELECT pg_stat_force_next_flush()")
        return True
    except Exception:                                  # noqa: BLE001
        return False


# 累计统计的周期（stats_reset，没 reset 过取实例启动时刻）。表扫描计数器的窗口差分与 ESC 的
# 统计周期核对都认它（esc._CUMULATIVE_EPOCH_KEYS：seq_scan_volume -> pg_stat_database），
# get_table_stats 与 get_table_scan_counters 必须同一个表达式、同一个字符串（规则 4）。
_STATS_EPOCH_SQL = ("(SELECT COALESCE(stats_reset, pg_postmaster_start_time())"
                    " FROM pg_stat_database WHERE datname = current_database())")

# "持续" idle in transaction 的门槛。事务型应用在语句之间会短暂处于这个状态，只有持续这么久的
# 才算长事务占着连接（connection_residual 判据；偏高只会少反证）。
LONG_IDLE_TX_S = 30.0

# 刷不掉时（旧版本 / 权限），等后端退出刷账的上限。宁可慢：见 CLAUDE.md 首要原则。
OWN_STATS_SETTLE_S = 2.0


class Observer:
    """所有只读诊断工具。每次调用都落轨迹。"""

    def __init__(self, trace: TraceStore | None = None):
        self.trace = trace or TraceStore()
        self.last_raw_refs: dict[str, str] = {}
        self._extension_cache: dict[str, bool] = {}
        # 本 episode 自家取证扫描（_stats_range_drift、explain_query 的 ANALYZE）累计给各表
        # 记上的扫描计数，get_table_stats 从原始计数器里扣掉它，seq_scan_volume 才量的是
        # 负载而不是自己。
        self._own_scans: dict[str, dict[str, int]] = {}
        # explain_query 执行时外溢的临时文件字节数上界（Temp Written Blocks × 块大小：每次
        # 写至多一块，文件大小不会超过它）。get_database_stats 从 temp_bytes 里扣掉。
        self._own_temp_bytes: int = 0
        # 刷账失败时，读累计计数器前要等到这个时刻（time.monotonic）。
        self._own_settle_until: float = 0.0

    def extension_available(self, extension: str) -> bool:
        """Read-only capability probe used by the v2 tool planner."""
        if extension in self._extension_cache:
            return self._extension_cache[extension]
        rows = db.query(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = %s)",
            (extension,), role="ro")
        available = bool(rows and rows[0][0])
        self._extension_cache[extension] = available
        return available

    def raw_ref_for(self, tool: str) -> str:
        return self.last_raw_refs.get(tool, "")

    def _note_own_flush(self, flushed: bool) -> None:
        if not flushed:
            self._own_settle_until = max(self._own_settle_until,
                                         time.monotonic() + OWN_STATS_SETTLE_S)

    def _await_own_stats(self) -> None:
        """读累计计数器之前：自家记账若没能强制刷出，等它随后端退出到账。"""
        wait = self._own_settle_until - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _add_own_scans(self, table: str, counts: dict) -> None:
        own = self._own_scans.setdefault(
            table, {"seq_scan": 0, "seq_tup_read": 0, "idx_scan": 0})
        for key in own:
            own[key] += int(counts.get(key, 0) or 0)

    def explain_query(self, sql: str, params: dict | None = None) -> ExplainDigest:
        # 只读连接挡写权限，挡不住 pg_sleep / advisory lock / 数据修改 CTE / 无 WHERE
        # 大排序污染 temp 计数器（CLAUDE.md 规则 6）。语句先过 AST 白名单，不合规直接拒，
        # 由调用方回给模型，不落盘为证据。
        run_sql, mode = _explainable(sql)
        # 放进一个事务：跑完读 pg_stat_xact_user_tables 拿 leader 自己的精确计数。
        with db.connect(role="ro", autocommit=False) as conn, conn.cursor() as cur:
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + run_sql, params or {})
            plan_json = cur.fetchone()[0]
            cur.execute("SELECT current_setting('block_size')::int")
            block_size = int(cur.fetchone()[0])
            cur.execute("SELECT relname, coalesce(seq_scan, 0), coalesce(seq_tup_read, 0),"
                        " coalesce(idx_scan, 0) FROM pg_stat_xact_user_tables"
                        " WHERE coalesce(seq_scan, 0) > 0 OR coalesce(idx_scan, 0) > 0")
            leader = {rel: {"seq_scan": int(a), "seq_tup_read": int(b), "idx_scan": int(c)}
                      for rel, a, b, c in cur.fetchall()}
            conn.rollback()
            flushed = _flush_own_stats(conn)
        acc = _acc()
        _walk_plan(plan_json[0]["Plan"], acc)
        acc["nodes"].sort(reverse=True)
        # ANALYZE 真执行了查询：它的扫描与外溢记进了负载读的累计计数器，记账后由
        # get_table_stats / get_database_stats 扣掉（CLAUDE.md 规则 6）。合并规则与方向见
        # _merge_own_counts。
        own_counts = _merge_own_counts(_plan_own_counts(plan_json[0]["Plan"]), leader,
                                       parallel=acc["workers"] > 0)
        for rel, counts in own_counts.items():
            self._add_own_scans(rel, counts)
        own_temp = int(plan_json[0]["Plan"].get("Temp Written Blocks", 0) or 0) * block_size
        self._own_temp_bytes += own_temp
        self._note_own_flush(flushed)
        digest = ExplainDigest(
            total_time_ms=round(plan_json[0].get("Execution Time", 0.0), 2),
            scan_types=acc["scans"][:5],
            rows_removed_by_filter=acc["removed"],
            rows_est_vs_actual=acc["est_act"][:3],
            indexes_used=sorted(set(acc["indexes"])),
            parallel_workers=acc["workers"],
            top_nodes=[str(round(t, 1)) + "ms " + n for t, n in acc["nodes"][:3]],
        )
        digest.mode = mode
        trace_digest = asdict(digest)
        trace_digest.pop("raw_ref", None)
        trace_digest["own_counts"] = own_counts
        trace_digest["own_temp_bytes"] = own_temp
        trace_digest["own_stats_flushed"] = flushed
        digest.raw_ref = self.trace.record(
            "explain_query", {"sql": sql[:200], "executed_sql": run_sql[:200], "mode": mode},
            json.dumps(plan_json, indent=2), trace_digest)
        return digest

    def get_active_sessions(self, min_duration_s: float = 1.0,
                            include_idle: bool = False,
                            idle_limit: int = 64) -> list[SessionDigest]:
        """异常会话；include_idle=True 时另外带上最久的 idle 客户端会话。

        为什么要能带 idle：connection_exhaustion 的修复 terminate_idle_backend 的前置条件
        要求 pid 来自**已观测到的 idle 行**（explanation_runtime 的 pid_is_client_backend_and_
        state_idle），而这里默认把 idle 全过滤掉 —— 2026-09-22 实测主策略三次提交都被
        "concrete_pid_bound" 拒掉，get_active_sessions 返回 []，get_connection_stats 只给计数，
        模型拿不到任何 pid，12 轮全空转。默认仍不带 idle（连接打满时 idle 有几十个，
        3500 字的工具输出会被截断），要终止空闲会话时显式开。
        """
        rows = db.query(
            "SELECT pid, state, wait_event_type, wait_event,"
            " EXTRACT(EPOCH FROM (now() - query_start)), query,"
            " coalesce(usename,''),"
            " EXTRACT(EPOCH FROM (now() - xact_start)),"
            " coalesce(backend_type,''), coalesce(backend_xmin::text,'')"
            " FROM pg_stat_activity"
            " WHERE state IS NOT NULL AND pid <> pg_backend_pid()"
            "   AND datname = current_database()"
            # 上限要盖住 max_connections：多 pid 终止的每个 pid 都必须来自这里的观测行
            # （shield.MAX_SESSION_TARGETS = 64），原来 LIMIT 50 / idle 15 行根本凑不够。
            " ORDER BY 5 DESC NULLS LAST LIMIT 200",
            role="ro")
        out = []
        for (pid, state, wtype, wevent, dur, q, role, xact_age,
             backend_type, backend_xmin) in rows:
            dur = float(dur or 0)
            if not keep_session(state, dur, min_duration_s, include_idle):
                continue
            if state == "idle" and sum(1 for x in out if x.state == "idle") >= idle_limit:
                continue
            we = (wtype + ":" + str(wevent)) if wtype else None
            system_or_diagnostic = (
                backend_type != "client backend" or
                role in {"postgres", "agent_ro", "agent_rw"})
            out.append(SessionDigest(
                pid, state, we, round(dur, 2),
                re.sub(r"\s+", " ", q or "")[:80],
                role=role,
                transaction_age_seconds=(
                    round(float(xact_age), 2) if xact_age is not None else None),
                backend_type=backend_type,
                backend_xmin=backend_xmin,
                is_current_diagnostic_connection=False,
                is_system_or_diagnostic=system_or_diagnostic,
                identity_rechecked=True,
            ))
        self.trace.record("get_active_sessions", {},
                          json.dumps([asdict(s) for s in out], ensure_ascii=False),
                          {"n": len(out)})
        return out

    def get_top_queries(self, n: int = 5) -> list[dict]:
        rows = db.query(
            "SELECT queryid, calls, round(mean_exec_time::numeric,2),"
            " round(total_exec_time::numeric,2), rows, query"
            " FROM pg_stat_statements"
            " WHERE query NOT ILIKE %s"
            # 诊断连接与安全门自己的语句不是负载：track=all 时 EXPLAIN ANALYZE 执行的
            # 热查询、值域漂移的 count(*) 全表扫都会进来，按用户单独成条（规则 6）。
            "   AND userid NOT IN (SELECT oid FROM pg_roles"
            "                      WHERE rolname IN ('agent_ro', 'agent_rw'))"
            " ORDER BY total_exec_time DESC LIMIT %s",
            ("%pg_stat_statements%", n), role="ro")
        out = [{"queryid": str(r[0]), "calls": r[1], "mean_ms": float(r[2]),
                "total_ms": float(r[3]), "rows": r[4],
                "query": re.sub(r"\s+", " ", r[5])[:120]} for r in rows]
        self.trace.record("get_top_queries", {"n": n},
                          json.dumps(out, ensure_ascii=False), {"n": len(out)})
        return out

    def get_blocking_chain(self) -> list[dict]:
        """工具内直接算出阻塞链（谁挡谁），而不是把锁矩阵丢回去让模型自己拼。"""
        rows = db.query(
            "SELECT blocked.pid, blocking.pid,"
            " coalesce(blocked.wait_event_type,'') || ':' || coalesce(blocked.wait_event,''),"
            " left(regexp_replace(blocked.query, %s, ' ', 'g'), 80),"
            " coalesce(blocking.usename,''), coalesce(blocking.state,''),"
            " round(extract(epoch FROM now() - blocking.xact_start))::int,"
            " coalesce(blocking.backend_type,''),"
            " coalesce(blocking.backend_xmin::text,''),"
            " cardinality(pg_blocking_pids(blocking.pid)) = 0,"
            " count(*) OVER (PARTITION BY blocking.pid)"
            " FROM pg_stat_activity blocked"
            " JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS bp(pid) ON true"
            " JOIN pg_stat_activity blocking ON blocking.pid = bp.pid",
            ("[[:space:]]+",), role="ro")
        out = [{"blocked_pid": r[0], "blocked_by": r[1], "pid": r[1],
                "wait": r[2], "query": r[3], "role": r[4],
                "state": r[5], "transaction_age_seconds": r[6],
                "backend_type": r[7], "backend_xmin": r[8],
                "is_topmost_blocker": bool(r[9]),
                "blocking_impact": int(r[10] or 0),
                "is_current_diagnostic_connection": False,
                "is_system_or_diagnostic": (
                    r[7] != "client backend" or
                    r[4] in {"postgres", "agent_ro", "agent_rw"}),
                "identity_rechecked": True,
                "evidence": "currently_waiting"}
               for r in rows]

        # 上面那个查询只看得见"此刻正在等待"的会话。等待者一旦被
        # statement_timeout 掐掉，阻塞链立刻变空 —— 只报这一种信号，
        # agent 拿到的 blocked_by 是瞬时的，等它提出终止提案时可能已失效。
        # 稳定信号是"持有锁且事务挂着不动"，这也是真实 DBA 排查锁问题时
        # 真正依据的东西：连接不在跑任何语句，却攥着锁不放。
        idle = db.query(
            "SELECT a.pid, a.usename,"
            " round(extract(epoch FROM now() - a.xact_start))::int,"
            " count(DISTINCT l.relation) FILTER (WHERE l.relation IS NOT NULL),"
            " left(regexp_replace(coalesce(a.query,''), %s, ' ', 'g'), 80)"
            " FROM pg_stat_activity a"
            " JOIN pg_locks l ON l.pid = a.pid AND l.granted"
            " WHERE a.datname = current_database()"
            "   AND a.state = 'idle in transaction'"
            "   AND a.pid <> pg_backend_pid()"
            " GROUP BY a.pid, a.usename, a.xact_start, a.query"
            " ORDER BY 3 DESC NULLS LAST",
            ("[[:space:]]+",), role="ro")
        for r in idle:
            out.append({"blocked_pid": None, "blocked_by": r[0], "pid": r[0],
                        "wait": f"idle_in_transaction:{r[2]}s",
                        "query": f"[持锁 {r[3]} 个对象，最后语句] {r[4]}",
                        "role": r[1], "state": "idle in transaction",
                        "transaction_age_seconds": r[2],
                        "backend_type": "client backend",
                        "is_topmost_blocker": True,
                        "blocking_impact": int(r[3] or 0),
                        "is_current_diagnostic_connection": False,
                        "is_system_or_diagnostic": (
                            r[1] in {"postgres", "agent_ro", "agent_rw"}),
                        "identity_rechecked": True,
                        "evidence": "idle_in_transaction_holding_locks"})

        ref = self.trace.record("get_blocking_chain", {},
                                json.dumps(out, ensure_ascii=False),
                                {"chains": out})
        self.last_raw_refs["get_blocking_chain"] = ref
        return out

    def _stats_range_drift(self, table: str) -> tuple[int, list, list]:
        """统计信息的已知值域，还盖不盖得住实际数据。

        为什么需要它：`stale_statistics` 的判别特征是估计与实际行数的偏差
        倍数，而那个倍数只有 EXPLAIN 给得出来。可 EXPLAIN 在写负载上取不到
        （只读角色无权 EXPLAIN 一条 UPDATE，架构不变式），于是这条竞争路径
        在锁竞争场景里结构上关不掉，ESC 空转到预算耗尽。

        这里换一个只读、且**不经过规划器**的量：直方图记录了每一列的已知
        取值范围，落在范围之外的行数就是"统计没见过的数据有多少"。它不是
        偏差倍数的代理，它是偏差倍数的成因 —— 实测统计过期场景里规划器对
        `created_at > now() - 1 hour` 估 1,185 行、实际 400,000 行（337 倍），
        正因为那 40 万行的 created_at 全部落在旧直方图上界之外。

        实测分离度（orders 表，1200 万行）：
            健康态 golden        118 行超范围   (0.001%)
            统计过期注入态   400,288 行超范围   (3.2%)

        注意这不构成循环论证：直方图是**被检验的对象**，不是判据的依据 ——
        用真实行数去检验统计声称的值域，而不是拿统计去证明统计。

        更直觉的几个量实测都不行，别再试了：reltuples 对 n_live_tup 只有
        1.033 倍；n_mod_since_analyze 是 40 万，而 PostgreSQL 自己的
        autoanalyze 阈值是 50 + 0.1 x 1200 万 = 120 万 —— 按它自己的标准
        这份统计根本不算过期。这个故障是倾斜不是增量。

        **不筛选列**，凡是有直方图的都测。第一版为了省钱只测"有 btree 索引
        做前导列"的列，那是个错误：它把测量的**覆盖范围**变成了索引存在性
        的函数。missing_index 场景丢掉 idx_orders_created_at 之后，承载信号
        的 created_at 列直接从测量里消失（实测：测到的列从
        ['id','user_id','created_at'] 变成 ['id','user_id']）。于是

            stale_statistics ⇝ missing_index    经 explain 一族
            missing_index    ⇝ stale_statistics 经这里的索引限制

        形成 2-环。有环就不能靠调整判别顺序解决 —— 无论先判哪个，用到的
        证据都已经被另一个污染了，只能改证据本身。

        代价其实微不足道：实测走索引 2.5ms，全表并行扫 253ms。为省 250 毫秒
        引入一条污染边，是笔亏本买卖。
        """
        cols = db.query(
            "SELECT s.attname, format_type(a.atttypid, a.atttypmod),"
            "       (s.histogram_bounds::text::text[])[1],"
            "       (s.histogram_bounds::text::text[])["
            "         array_length(s.histogram_bounds::text::text[], 1)]"
            " FROM pg_stats s"
            " JOIN pg_class c ON c.relname = s.tablename"
            " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = s.attname"
            " WHERE s.tablename = %s AND s.histogram_bounds IS NOT NULL",
            (table,), role="ro")
        worst, detail, incomplete = 0, [], []
        # 全表扫会给 pg_stat_user_tables 记 seq_scan / seq_tup_read，而 seq_scan_volume 读的
        # 正是这个计数器的窗口增量 —— 自家的取证扫描把它污染成"窗口内有大规模顺序扫描"，
        # missing_index 因此被 SUPPORTS（2026-09-23 跑批：lock_contention 里"3 次顺序扫描各读
        # 400 万行"全是这里扫出来的；CLAUDE.md 规则 6 的只读版）。所以：一个事务、禁并行
        # （并行 worker 各自计数，事务级视图看不到它们）、扫完从 pg_stat_xact_user_tables
        # 读出本事务自己的精确计数，累加进 _own_scans，由 get_table_stats 从原始计数器扣掉。
        with db.connect(role="ro", autocommit=False) as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            for name, coltype, lo, hi in cols:
                if lo is None or hi is None:
                    continue
                cur.execute("SAVEPOINT drift_col")
                try:
                    cur.execute(
                        f'SELECT count(*) FROM "{table}"'
                        f' WHERE "{name}" < %s::{coltype} OR "{name}" > %s::{coltype}',
                        (lo, hi))
                    beyond = int((cur.fetchone() or [0])[0] or 0)
                except Exception as exc:
                    # 跳过一列会让 max() 只在剩下的列上取，结果**只可能偏低** ——
                    # 偏低的占比会让判据返回 REFUTES，把真的统计过期排除掉。
                    # 所以测不到必须如实上报，由判据决定不能据此否定。
                    cur.execute("ROLLBACK TO SAVEPOINT drift_col")
                    incomplete.append({"column": name, "error": str(exc)[:120]})
                    continue
                detail.append({"column": name, "known_min": str(lo),
                               "known_max": str(hi), "rows_outside": beyond})
                worst = max(worst, beyond)
            cur.execute(
                "SELECT coalesce(seq_scan, 0), coalesce(seq_tup_read, 0), coalesce(idx_scan, 0)"
                " FROM pg_stat_xact_user_tables WHERE relname = %s", (table,))
            row = cur.fetchone() or (0, 0, 0)
            conn.rollback()
            flushed = _flush_own_stats(conn)
        self._add_own_scans(table, {"seq_scan": row[0], "seq_tup_read": row[1],
                                    "idx_scan": row[2]})
        self._note_own_flush(flushed)
        return worst, detail, incomplete

    def get_table_stats(self, table: str) -> TableStats:
        self._await_own_stats()
        r = db.query(
            "SELECT n_live_tup, n_dead_tup, last_analyze, last_autoanalyze,"
            " last_autovacuum, pg_size_pretty(pg_total_relation_size(s.relid)),"
            " current_setting('autovacuum')::boolean AND coalesce(("
            "   SELECT option_value::boolean FROM pg_options_to_table(c.reloptions)"
            "   WHERE option_name = 'autovacuum_enabled'), true),"
            " EXISTS (SELECT FROM pg_stat_progress_vacuum p WHERE p.relid = s.relid),"
            " ceil(coalesce((SELECT option_value::numeric"
            "                FROM pg_options_to_table(c.reloptions)"
            "                WHERE option_name = 'autovacuum_vacuum_threshold'),"
            "               current_setting('autovacuum_vacuum_threshold')::numeric)"
            "      + coalesce((SELECT option_value::numeric"
            "                  FROM pg_options_to_table(c.reloptions)"
            "                  WHERE option_name = 'autovacuum_vacuum_scale_factor'),"
            "                 current_setting('autovacuum_vacuum_scale_factor')::numeric)"
            "        * greatest(s.n_live_tup, 0))::bigint"
            " , s.seq_scan, s.seq_tup_read, s.idx_scan, c.reltuples::bigint"
            " , " + _STATS_EPOCH_SQL +
            " FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid"
            " WHERE s.relname = %s", (table,), role="ro")
        if not r:
            raise KeyError(table)
        (live, dead, la, laa, lav, size, av_enabled, av_running, av_trigger,
         seq_scan, seq_tup_read, idx_scan, reltuples, stats_reset) = r[0]
        # 原始计数器在上面读、自家扫描在下面做：扣的是**此前**各次调用累计的自家计数，
        # 本次扫描的计数会出现在下一次的原始值里，那时再扣。
        own_before = dict(self._own_scans.get(
            table) or {"seq_scan": 0, "seq_tup_read": 0, "idx_scan": 0})
        drift_rows, drift_cols, drift_gaps = self._stats_range_drift(table)
        st = TableStats(table, live or 0, dead or 0,
                        round((dead or 0) / max(live or 1, 1), 4),
                        str(la or laa or ""), str(lav or ""), size,
                        bool(av_enabled), bool(av_running), int(av_trigger or 0),
                        stats_range_drift_rows=drift_rows,
                        stats_range_drift_pct=round(
                            100.0 * drift_rows / max(live or 1, 1), 4),
                        stats_range_columns=drift_cols,
                        stats_range_incomplete=drift_gaps,
                        seq_scan=max(0, int(seq_scan or 0) - own_before["seq_scan"]),
                        seq_tup_read=max(0, int(seq_tup_read or 0) - own_before["seq_tup_read"]),
                        idx_scan=max(0, int(idx_scan or 0) - own_before["idx_scan"]),
                        seq_scan_raw=int(seq_scan or 0),
                        seq_tup_read_raw=int(seq_tup_read or 0),
                        idx_scan_raw=int(idx_scan or 0),
                        own_seq_scan=own_before["seq_scan"],
                        own_seq_tup_read=own_before["seq_tup_read"],
                        own_idx_scan=own_before["idx_scan"],
                        reltuples=int(reltuples or 0),
                        stats_reset=str(stats_reset or ""))
        raw = asdict(st)
        raw.pop("raw_ref", None)
        ref = self.trace.record("get_table_stats", {"table": table},
                                json.dumps(raw, ensure_ascii=False), raw)
        st.raw_ref = ref
        return st

    def get_table_scan_counters(self, table: str) -> dict:
        """只读表扫描累计计数器与统计周期：不做值域扫描、不落任何证据。

        MONITOR 在告警时刻用它给热表建扫描基线（系统动作，发生在 agent 的任何动作之前，规则 6
        干净）。原来第一次 get_table_stats 在 INVESTIGATE 里，只能建基线、seq_scan_volume 拿到
        UNKNOWN，5/5 局都白读一次（2026-09-24 缺陷报告 5cc2c25 P2-1）。原始值、自家扣除量、统计
        周期与 get_table_stats 同源同口径，Toolbox 按同一组计数器差分。
        """
        self._await_own_stats()
        rows = db.query(
            "SELECT s.seq_scan, s.seq_tup_read, s.idx_scan, " + _STATS_EPOCH_SQL +
            " FROM pg_stat_user_tables s WHERE s.relname = %s", (table,), role="ro")
        if not rows:
            raise KeyError(table)
        seq_scan, seq_tup_read, idx_scan, epoch = rows[0]
        own = dict(self._own_scans.get(table) or {"seq_scan": 0, "seq_tup_read": 0, "idx_scan": 0})
        out = {"table": table,
               "seq_scan_raw": int(seq_scan or 0), "seq_tup_read_raw": int(seq_tup_read or 0),
               "idx_scan_raw": int(idx_scan or 0),
               "own_seq_scan": int(own["seq_scan"]), "own_seq_tup_read": int(own["seq_tup_read"]),
               "own_idx_scan": int(own["idx_scan"]),
               "stats_reset": str(epoch or "")}
        out["raw_ref"] = self.trace.record("get_table_scan_counters", {"table": table},
                                           json.dumps(out, ensure_ascii=False), dict(out))
        return out

    def get_physical_bloat(self, table: str) -> dict:
        """Measure physical reclaimable space with pgstattuple_approx.

        The algorithm is explicit and versioned: dead tuple bytes plus
        approximate free bytes, divided by the physical table length.  Missing
        extension/function access is reported as unavailable, never inferred
        from pg_stat_user_tables dead-tuple estimates.
        """
        algorithm = "pgstattuple_approx_reclaimable_pct_v1"
        out: dict = {"table": table, "algorithm": algorithm}
        try:
            available = db.query(
                "SELECT to_regprocedure('pgstattuple_approx(regclass)') IS NOT NULL",
                role="ro")[0][0]
            if not available:
                out.update({
                    "availability": "UNAVAILABLE",
                    "reason": "pgstattuple_approx(regclass) is not installed",
                })
            else:
                row = db.query(
                    "SELECT table_len, dead_tuple_percent, approx_free_percent "
                    "FROM pgstattuple_approx(%s::regclass)",
                    (table,), role="ro")[0]
                dead_pct = float(row[1] or 0.0)
                free_pct = float(row[2] or 0.0)
                out.update({
                    "availability": "AVAILABLE",
                    "table_bytes": int(row[0] or 0),
                    "dead_tuple_percent": round(dead_pct, 4),
                    "free_percent": round(free_pct, 4),
                    "reclaimable_pct": round(min(100.0, dead_pct + free_pct), 4),
                })
        except Exception as exc:
            unavailable = type(exc).__name__ in {
                "InsufficientPrivilege", "UndefinedFunction",
                "FeatureNotSupported",
            }
            out.update({"availability": ("UNAVAILABLE" if unavailable else "ERROR"),
                        "reason": f"{type(exc).__name__}: {exc}"[:200]})
        ref = self.trace.record(
            "get_physical_bloat", {"table": table},
            json.dumps(out, ensure_ascii=False, default=str), out)
        out["raw_ref"] = ref
        return out

    def get_indexes(self, table: str) -> list[dict]:
        # 表不存在与"表上没索引"原来都是 []，而 [] 会被当成 missing_index 的必需证据
        # index_existence 已满足（2026-09-23 审计）。先按 to_regclass 解析成规范名，
        # 解析不到就像 get_table_stats 一样 KeyError；"Orders" / public.orders 归一到 orders。
        resolved = db.query(
            "SELECT c.relname FROM pg_class c WHERE c.oid = to_regclass(%s)",
            (table,), role="ro")
        if not resolved:
            raise KeyError(table)
        table = str(resolved[0][0])
        rows = db.query(
            "SELECT i.indexname, i.indexdef,"
            " pg_size_pretty(pg_relation_size(i.indexname::regclass)), s.idx_scan"
            " FROM pg_indexes i"
            " LEFT JOIN pg_stat_user_indexes s ON s.indexrelname = i.indexname"
            " WHERE i.tablename = %s ORDER BY 1", (table,), role="ro")
        out = [{"name": r[0], "definition": r[1], "size": r[2], "scans": r[3] or 0}
               for r in rows]
        self.trace.record("get_indexes", {"table": table},
                          json.dumps(out, ensure_ascii=False), {"n": len(out)})
        return out

    def get_connection_stats(self) -> dict:
        """连接数与上限、按状态与角色的分布。

        连接打满时会话大多是 idle，用 get_active_sessions 看不出问题 ——
        必须直接看总数与上限的关系，以及谁占着这些连接。
        """
        from knowledge.evidence_predicates import CONNECTION_NEAR_LIMIT_RATIO
        maxc = int(db.query("SHOW max_connections", role="ro")[0][0])
        # 持续 idle in transaction 的会话单独计数（同一条查询、同一个快照）：
        # connection_residual 用它判"打满是不是由长事务解释掉了"。
        rows = db.query(
            "SELECT coalesce(usename,'?'), coalesce(state,'?'), count(*),"
            " count(*) FILTER (WHERE state = 'idle in transaction'"
            "   AND now() - state_change >= make_interval(secs => %s)) "
            "FROM pg_stat_activity GROUP BY 1,2 ORDER BY 3 DESC",
            (LONG_IDLE_TX_S,), role="ro")
        total = sum(r[2] for r in rows)
        by_user: dict[str, int] = {}
        by_state: dict[str, int] = {}
        for u, st_, n, _long in rows:
            by_user[u] = by_user.get(u, 0) + n
            by_state[st_] = by_state.get(st_, 0) + n
        idle_in_tx = by_state.get("idle in transaction", 0)
        out = {"used": total, "max_connections": maxc,
               "pct": round(total / max(maxc, 1) * 100, 1),
               "by_user": by_user, "by_state": by_state,
               "idle_in_transaction": idle_in_tx,
               "idle_in_transaction_long": int(sum(r[3] for r in rows)),
               "long_idle_threshold_s": LONG_IDLE_TX_S,
               "near_limit": total >= maxc * CONNECTION_NEAR_LIMIT_RATIO}
        ref = self.trace.record("get_connection_stats", {},
                                json.dumps(out, ensure_ascii=False), out)
        self.last_raw_refs["get_connection_stats"] = ref
        out["raw_ref"] = ref
        return out

    def get_vacuum_horizon(self) -> dict:
        """谁挡着 xmin 前进 —— 一次问清膨胀与回卷这一整族根因。

        PostgreSQL 手册 "Routine Vacuuming" 把"死元组回收不掉"归到同一
        个机制上：只要还有事务可能看见旧版本，VACUUM 就不能删。手册列出
        四个持有者，本方法逐个查：

          长事务        pg_stat_activity.backend_xmin
          复制槽        pg_replication_slots.xmin / catalog_xmin
          预备事务      pg_prepared_xacts.transactionid
          数据库年龄    pg_database.datfrozenxid（回卷风险的直接判据）

        手册原文对应的排查步骤："Drop any old replication slots. Use
        pg_replication_slots to find slots where age(xmin) or
        age(catalog_xmin) is large." / "Resolve old prepared transactions.
        You can find these by checking pg_prepared_xacts for rows where
        age(transactionid) is large."

        做成一个工具而不是四个：它们是同一个 xmin 视界的四个持有者，
        分开查会让 agent 查到第一个就收工 —— 而真正的根因常常是另一个。
        """
        out: dict = {}
        try:
            r = db.query("SELECT datname, age(datfrozenxid) FROM pg_database "
                         "WHERE datname = current_database()", role="ro")
            out["db_xid_age"] = int(r[0][1]) if r else 0
        except Exception:
            out["db_xid_age"] = 0
        try:
            r = db.query(
                "SELECT c.oid::regclass::text, "
                "greatest(age(c.relfrozenxid), coalesce(age(t.relfrozenxid),0)) a "
                "FROM pg_class c LEFT JOIN pg_class t ON c.reltoastrelid = t.oid "
                "WHERE c.relkind IN ('r','m') ORDER BY a DESC LIMIT 1", role="ro")
            out["oldest_table"] = r[0][0] if r else None
            out["oldest_table_xid_age"] = int(r[0][1]) if r else 0
        except Exception:
            out["oldest_table"], out["oldest_table_xid_age"] = None, 0
        try:
            out["freeze_max_age"] = int(
                db.query("SHOW autovacuum_freeze_max_age", role="ro")[0][0])
        except Exception:
            out["freeze_max_age"] = 200_000_000
        try:
            out["slots"] = [
                {"name": n, "xmin_age": int(x or 0),
                 "catalog_xmin_age": int(cx or 0), "active": bool(a),
                 "retained_wal_bytes": int(wal or 0)}
                for n, x, cx, a, wal in db.query(
                    "SELECT slot_name, age(xmin), age(catalog_xmin), active, "
                    "greatest(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn), 0) "
                    "FROM pg_replication_slots", role="ro")]
        except Exception:
            out["slots"] = []
        try:
            out["prepared_xacts"] = [
                {"gid": g, "xid_age": int(a or 0),
                 "prepared_age_s": int(seconds or 0)}
                for g, a, seconds in db.query(
                    "SELECT gid, age(transactionid), "
                    "extract(epoch FROM now() - prepared)::bigint "
                    "FROM pg_prepared_xacts",
                    role="ro")]
        except Exception:
            out["prepared_xacts"] = []
        try:
            r = db.query(
                "SELECT pid, age(backend_xmin) a FROM pg_stat_activity "
                "WHERE backend_xmin IS NOT NULL ORDER BY a DESC LIMIT 1",
                role="ro")
            out["oldest_backend_pid"] = int(r[0][0]) if r else None
            out["oldest_backend_xmin_age"] = int(r[0][1]) if r else 0
        except Exception:
            out["oldest_backend_pid"], out["oldest_backend_xmin_age"] = None, 0

        # 回卷风险按手册的阈值判：autovacuum_freeze_max_age 是"再不 vacuum
        # 就该强制 vacuum 了"的线，越过它说明 autovacuum 已经跟不上。
        out["wraparound_pct"] = round(
            out["db_xid_age"] / max(out["freeze_max_age"], 1) * 100, 1)
        out["at_risk"] = out["wraparound_pct"] >= 100
        holders = []
        if out["oldest_backend_xmin_age"] > 1_000_000:
            holders.append("long_transaction")
        if any(s["xmin_age"] > 1_000_000 or s["catalog_xmin_age"] > 1_000_000
               for s in out["slots"]):
            holders.append("replication_slot")
        if any(x["xid_age"] > 1_000_000 for x in out["prepared_xacts"]):
            holders.append("prepared_transaction")
        out["xmin_holders"] = holders
        ref = self.trace.record("get_vacuum_horizon", {},
                                json.dumps(out, ensure_ascii=False, default=str), out)
        out["raw_ref"] = ref
        return out

    def get_database_stats(self) -> dict:
        """库级累计计数器：死锁、临时文件外溢、检查点压力、I/O 等待。

        这些都是 pg_stat_database / 检查点视图里的**累计值**，单次读数说明
        不了问题。这里返回原始计数和 reset 时刻，由 Toolbox 持久化基线并
        计算相邻两次观测之间的窗口增量。

        检查点那组列在 PG17 拆去了 pg_stat_checkpointer，PG16 及更早在
        pg_stat_bgwriter 里叫另一套名字。沙箱是 16.15，官方 current 文档
        是 18 —— 照文档抄会直接报列不存在，所以两套都试。
        """
        out: dict = {"errors": {}}
        self._await_own_stats()
        try:
            r = db.query(
                "SELECT deadlocks, temp_files, temp_bytes, "
                "blk_read_time, blk_write_time, xact_commit, xact_rollback, "
                "COALESCE(stats_reset, pg_postmaster_start_time()) "
                "FROM pg_stat_database WHERE datname = current_database()",
                role="ro")[0]
            # own_temp_bytes：本 episode 自家 EXPLAIN ANALYZE 外溢的累计上界。窗口净值由
            # Toolbox 用两者各自的增量相减 —— 扣的是上界，净值只可能偏低：为正说明负载
            # 确实外溢了，为 0 而自家有外溢时不能据此否定（判据看 own_temp_bytes）。
            # temp_files 计划里给不出，保持原始值。
            out.update({"deadlocks": int(r[0]), "temp_files": int(r[1]),
                        "temp_bytes": int(r[2]),
                        "own_temp_bytes": self._own_temp_bytes,
                        "blk_read_time_ms": float(r[3] or 0),
                        "blk_write_time_ms": float(r[4] or 0),
                        "xact_commit": int(r[5]), "xact_rollback": int(r[6]),
                        "db_stats_reset": str(r[7] or "")})
        except Exception as exc:
            out["errors"]["pg_stat_database"] = str(exc)[:120]
        try:                                  # PG17+
            r = db.query("SELECT num_timed, num_requested, write_time, "
                         "sync_time, COALESCE(stats_reset, "
                         "pg_postmaster_start_time()) FROM pg_stat_checkpointer",
                         role="ro")[0]
            out.update({"ckpt_timed": int(r[0]), "ckpt_requested": int(r[1]),
                        "ckpt_write_time_ms": float(r[2] or 0),
                        "ckpt_sync_time_ms": float(r[3] or 0),
                        "ckpt_stats_reset": str(r[4] or ""),
                        "ckpt_source": "pg_stat_checkpointer"})
        except Exception as pg17_exc:
            try:                              # PG16 及更早
                r = db.query(
                    "SELECT checkpoints_timed, checkpoints_req, "
                    "checkpoint_write_time, checkpoint_sync_time, "
                    "COALESCE(stats_reset, pg_postmaster_start_time()) "
                    "FROM pg_stat_bgwriter", role="ro")[0]
                out.update({"ckpt_timed": int(r[0]),
                            "ckpt_requested": int(r[1]),
                            "ckpt_write_time_ms": float(r[2] or 0),
                            "ckpt_sync_time_ms": float(r[3] or 0),
                            "ckpt_stats_reset": str(r[4] or ""),
                            "ckpt_source": "pg_stat_bgwriter"})
            except Exception as pg16_exc:
                out["errors"]["checkpoint_stats"] = (
                    f"PG17+: {pg17_exc}; PG16-: {pg16_exc}")[:240]
        try:
            if db.PG_HOST not in {"localhost", "127.0.0.1", "::1"} and not os.getenv(
                    "PGDOCTOR_DATA_PATH"):
                raise OSError("远程数据库未配置 PGDOCTOR_DATA_PATH")
            configured = os.getenv("PGDOCTOR_DATA_PATH")
            data_path = Path(configured or db.query(
                "SHOW data_directory", role="ro")[0][0])
            probe = data_path
            while True:
                try:
                    usage = shutil.disk_usage(probe)
                    break
                except PermissionError:
                    parent = probe.parent
                    if parent == probe:
                        raise
                    probe = parent
            out["disk_usage"] = {
                "path": str(probe),
                "total_bytes": int(usage.total),
                "used_bytes": int(usage.used),
                "free_bytes": int(usage.free),
                "used_pct": round(usage.used / max(usage.total, 1) * 100, 1),
            }
        except Exception as exc:
            out["errors"]["disk_usage"] = str(exc)[:160]
        if "temp_bytes" in out:
            out["temp_mb"] = round(out["temp_bytes"] / 1048576, 1)
        if "ckpt_timed" in out and "ckpt_requested" in out:
            # 这个仍只是累计占比，Toolbox 会基于两次快照重算窗口占比。
            t, q = out["ckpt_timed"], out["ckpt_requested"]
            out["ckpt_requested_pct"] = round(q / max(t + q, 1) * 100, 1)
        if not out["errors"]:
            out.pop("errors")
        ref = self.trace.record("get_database_stats", {},
                                json.dumps(out, ensure_ascii=False), out)
        out["raw_ref"] = ref
        return out

    def simulate_index(self, create_sql: str, test_sql: str,
                       params: dict | None = None) -> dict:
        """hypopg 假设索引：不改生产就能预先证伪一个缺索引的判断。ESC 的 D5。"""
        from safety import shield
        if shield.classify(create_sql) != "create_index":
            raise ValueError("simulate_index 的 create_sql 必须是一条 CREATE INDEX")
        run_sql, mode = _explainable(test_sql)
        with db.connect(role="ro") as conn, conn.cursor() as cur:
            cur.execute("SELECT hypopg_reset()")
            cur.execute("EXPLAIN (FORMAT JSON) " + run_sql, params or {})
            before = cur.fetchone()[0][0]["Plan"]
            cur.execute("SELECT indexname FROM hypopg_create_index(%s)", (create_sql,))
            hypo = cur.fetchone()[0]
            cur.execute("EXPLAIN (FORMAT JSON) " + run_sql, params or {})
            after = cur.fetchone()[0][0]["Plan"]
            cur.execute("SELECT hypopg_reset()")
        ab, aa = _acc(), _acc()
        _walk_plan(before, ab)
        _walk_plan(after, aa)
        cb = float(before.get("Total Cost", 0.0))
        ca = float(after.get("Total Cost", 0.0))
        used = any(hypo in i or i.startswith("<") for i in aa["indexes"])
        # 绝对成本太小时，相对降幅没有意义。实测出现过 cost 1 -> 0
        # 报"降 87.5%、会采用"，模型据此把锁竞争误诊成了缺索引。
        MEANINGFUL_COST = 100.0
        trivial = cb < MEANINGFUL_COST
        res = {"hypothetical_index": hypo,
               "cost_before": round(cb, 2), "cost_after": round(ca, 2),
               "scans_before": ab["scans"][:3], "scans_after": aa["scans"][:3],
               "would_be_used": bool(used) and not trivial,
               "cost_reduction_pct": round((1 - ca / cb) * 100, 1) if cb else 0.0,
               "trivial_baseline": trivial,
               # DML 热查询走的是同表同 WHERE 的 SELECT 代理：访问路径可比，写代价不在其中
               "mode": mode}
        if trivial:
            res["note"] = (
                f"原查询成本仅 {cb:.1f}，本来就很快，加索引的收益没有意义；"
                f"该结果不足以支持'缺索引'的判断")
        ref = self.trace.record("simulate_index", {"create": create_sql},
                                json.dumps(res, ensure_ascii=False), res)
        res["raw_ref"] = ref
        return res

    def fetch_raw(self, ref: str) -> str:
        return self.trace.fetch_raw(ref)
