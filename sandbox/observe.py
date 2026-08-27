"""只读观测工具层 —— agent 的眼睛。

两条铁律：
  1. 全部走 agent_ro 连接，物理上无写权限
  2. 工具内就地萃取，返回结构化摘要 + raw_ref，绝不把原文丢回上下文

经验法则：如果一个工具的返回值需要模型读一遍才知道重点在哪，
那这个工具就没写好。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from sandbox import db
from sandbox.traces import TraceStore


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


@dataclass
class SessionDigest:
    pid: int
    state: str
    wait_event: str | None
    duration_s: float
    query_fingerprint: str                # 指纹化截断，不带完整 SQL 文本


@dataclass
class TableStats:
    table: str
    n_live_tup: int
    n_dead_tup: int
    dead_ratio: float
    last_analyze: str
    last_autovacuum: str
    total_size: str


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


class Observer:
    """所有只读诊断工具。每次调用都落轨迹。"""

    def __init__(self, trace: TraceStore | None = None):
        self.trace = trace or TraceStore()

    def explain_query(self, sql: str, params: dict | None = None) -> ExplainDigest:
        with db.connect(role="ro") as conn, conn.cursor() as cur:
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params or {})
            plan_json = cur.fetchone()[0]
        acc = _acc()
        _walk_plan(plan_json[0]["Plan"], acc)
        acc["nodes"].sort(reverse=True)
        digest = ExplainDigest(
            total_time_ms=round(plan_json[0].get("Execution Time", 0.0), 2),
            scan_types=acc["scans"][:5],
            rows_removed_by_filter=acc["removed"],
            rows_est_vs_actual=acc["est_act"][:3],
            indexes_used=sorted(set(acc["indexes"])),
            parallel_workers=acc["workers"],
            top_nodes=[str(round(t, 1)) + "ms " + n for t, n in acc["nodes"][:3]],
        )
        digest.raw_ref = self.trace.record(
            "explain_query", {"sql": sql[:200]},
            json.dumps(plan_json, indent=2), asdict(digest))
        return digest

    def get_active_sessions(self, min_duration_s: float = 1.0) -> list[SessionDigest]:
        rows = db.query(
            "SELECT pid, state, wait_event_type, wait_event,"
            " EXTRACT(EPOCH FROM (now() - query_start)), query"
            " FROM pg_stat_activity"
            " WHERE state IS NOT NULL AND pid <> pg_backend_pid()"
            "   AND datname = current_database()"
            " ORDER BY 5 DESC NULLS LAST LIMIT 50",
            role="ro")
        out = []
        for pid, state, wtype, wevent, dur, q in rows:
            dur = float(dur or 0)
            if state == "idle" or (state == "active" and dur < min_duration_s):
                continue
            we = (wtype + ":" + str(wevent)) if wtype else None
            out.append(SessionDigest(pid, state, we, round(dur, 2),
                                     re.sub(r"\s+", " ", q or "")[:80]))
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
            " left(regexp_replace(blocked.query, %s, ' ', 'g'), 80)"
            " FROM pg_stat_activity blocked"
            " JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS bp(pid) ON true"
            " JOIN pg_stat_activity blocking ON blocking.pid = bp.pid",
            ("[[:space:]]+",), role="ro")
        out = [{"blocked_pid": r[0], "blocked_by": r[1], "wait": r[2],
                "query": r[3], "evidence": "currently_waiting"}
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
            out.append({"blocked_pid": None, "blocked_by": r[0],
                        "wait": f"idle_in_transaction:{r[2]}s",
                        "query": f"[持锁 {r[3]} 个对象，最后语句] {r[4]}",
                        "evidence": "idle_in_transaction_holding_locks"})

        self.trace.record("get_blocking_chain", {},
                          json.dumps(out, ensure_ascii=False),
                          {"n": len(out), "n_waiting": len(rows),
                           "n_idle_holders": len(idle)})
        return out

    def get_table_stats(self, table: str) -> TableStats:
        r = db.query(
            "SELECT n_live_tup, n_dead_tup, last_analyze, last_autoanalyze,"
            " last_autovacuum, pg_size_pretty(pg_total_relation_size(relid))"
            " FROM pg_stat_user_tables WHERE relname = %s", (table,), role="ro")
        if not r:
            raise KeyError(table)
        live, dead, la, laa, lav, size = r[0]
        st = TableStats(table, live or 0, dead or 0,
                        round((dead or 0) / max(live or 1, 1), 4),
                        str(la or laa or ""), str(lav or ""), size)
        self.trace.record("get_table_stats", {"table": table},
                          json.dumps(asdict(st), ensure_ascii=False), asdict(st))
        return st

    def get_indexes(self, table: str) -> list[dict]:
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
        maxc = int(db.query("SHOW max_connections", role="ro")[0][0])
        rows = db.query(
            "SELECT coalesce(usename,'?'), coalesce(state,'?'), count(*) "
            "FROM pg_stat_activity GROUP BY 1,2 ORDER BY 3 DESC", role="ro")
        total = sum(r[2] for r in rows)
        by_user: dict[str, int] = {}
        by_state: dict[str, int] = {}
        for u, st_, n in rows:
            by_user[u] = by_user.get(u, 0) + n
            by_state[st_] = by_state.get(st_, 0) + n
        idle_in_tx = by_state.get("idle in transaction", 0)
        out = {"used": total, "max_connections": maxc,
               "pct": round(total / max(maxc, 1) * 100, 1),
               "by_user": by_user, "by_state": by_state,
               "idle_in_transaction": idle_in_tx,
               "near_limit": total >= maxc * 0.85}
        self.trace.record("get_connection_stats", {},
                          json.dumps(out, ensure_ascii=False), out)
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
                 "catalog_xmin_age": int(cx or 0), "active": bool(a)}
                for n, x, cx, a in db.query(
                    "SELECT slot_name, age(xmin), age(catalog_xmin), active "
                    "FROM pg_replication_slots", role="ro")]
        except Exception:
            out["slots"] = []
        try:
            out["prepared_xacts"] = [
                {"gid": g, "xid_age": int(a or 0)}
                for g, a in db.query(
                    "SELECT gid, age(transactionid) FROM pg_prepared_xacts",
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
        self.trace.record("get_vacuum_horizon", {},
                          json.dumps(out, ensure_ascii=False, default=str), out)
        return out

    def get_database_stats(self) -> dict:
        """库级累计计数器：死锁、临时文件外溢、检查点压力、I/O 等待。

        这些都是 pg_stat_database / 检查点视图里的**累计值**，单次读数说明
        不了问题，要看它在故障窗口内涨了多少 —— 所以一并返回，让 agent
        自己对比两次读数。

        检查点那组列在 PG17 拆去了 pg_stat_checkpointer，PG16 及更早在
        pg_stat_bgwriter 里叫另一套名字。沙箱是 16.15，官方 current 文档
        是 18 —— 照文档抄会直接报列不存在，所以两套都试。
        """
        out: dict = {}
        try:
            r = db.query(
                "SELECT deadlocks, temp_files, temp_bytes, "
                "blk_read_time, blk_write_time, xact_commit, xact_rollback "
                "FROM pg_stat_database WHERE datname = current_database()",
                role="ro")[0]
            out.update({"deadlocks": int(r[0]), "temp_files": int(r[1]),
                        "temp_bytes": int(r[2]),
                        "blk_read_time_ms": float(r[3] or 0),
                        "blk_write_time_ms": float(r[4] or 0),
                        "xact_commit": int(r[5]), "xact_rollback": int(r[6])})
        except Exception as exc:
            out["error"] = str(exc)[:120]
        try:                                  # PG17+
            r = db.query("SELECT num_timed, num_requested, write_time, "
                         "sync_time FROM pg_stat_checkpointer", role="ro")[0]
            out.update({"ckpt_timed": int(r[0]), "ckpt_requested": int(r[1]),
                        "ckpt_write_time_ms": float(r[2] or 0),
                        "ckpt_sync_time_ms": float(r[3] or 0)})
        except Exception:
            try:                              # PG16 及更早
                r = db.query(
                    "SELECT checkpoints_timed, checkpoints_req, "
                    "checkpoint_write_time, checkpoint_sync_time "
                    "FROM pg_stat_bgwriter", role="ro")[0]
                out.update({"ckpt_timed": int(r[0]),
                            "ckpt_requested": int(r[1]),
                            "ckpt_write_time_ms": float(r[2] or 0),
                            "ckpt_sync_time_ms": float(r[3] or 0)})
            except Exception:
                pass
        # 请求式检查点占比高 = WAL 涨得比 checkpoint_timeout 快
        t, q = out.get("ckpt_timed", 0), out.get("ckpt_requested", 0)
        out["ckpt_requested_pct"] = round(q / max(t + q, 1) * 100, 1)
        out["temp_mb"] = round(out.get("temp_bytes", 0) / 1048576, 1)
        self.trace.record("get_database_stats", {},
                          json.dumps(out, ensure_ascii=False), out)
        return out

    def simulate_index(self, create_sql: str, test_sql: str,
                       params: dict | None = None) -> dict:
        """hypopg 假设索引：不改生产就能预先证伪一个缺索引的判断。ESC 的 D5。"""
        with db.connect(role="ro") as conn, conn.cursor() as cur:
            cur.execute("SELECT hypopg_reset()")
            cur.execute("EXPLAIN (FORMAT JSON) " + test_sql, params or {})
            before = cur.fetchone()[0][0]["Plan"]
            cur.execute("SELECT indexname FROM hypopg_create_index(%s)", (create_sql,))
            hypo = cur.fetchone()[0]
            cur.execute("EXPLAIN (FORMAT JSON) " + test_sql, params or {})
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
               "trivial_baseline": trivial}
        if trivial:
            res["note"] = (
                f"原查询成本仅 {cb:.1f}，本来就很快，加索引的收益没有意义；"
                f"该结果不足以支持'缺索引'的判断")
        self.trace.record("simulate_index", {"create": create_sql},
                          json.dumps(res, ensure_ascii=False), res)
        return res

    def fetch_raw(self, ref: str) -> str:
        return self.trace.fetch_raw(ref)
