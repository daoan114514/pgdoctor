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
        out = [{"blocked_pid": r[0], "blocked_by": r[1], "wait": r[2], "query": r[3]}
               for r in rows]
        self.trace.record("get_blocking_chain", {},
                          json.dumps(out, ensure_ascii=False), {"n": len(out)})
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
        res = {"hypothetical_index": hypo,
               "cost_before": round(cb, 2), "cost_after": round(ca, 2),
               "scans_before": ab["scans"][:3], "scans_after": aa["scans"][:3],
               "would_be_used": bool(used),
               "cost_reduction_pct": round((1 - ca / cb) * 100, 1) if cb else 0.0}
        self.trace.record("simulate_index", {"create": create_sql},
                          json.dumps(res, ensure_ascii=False), res)
        return res

    def fetch_raw(self, ref: str) -> str:
        return self.trace.fetch_raw(ref)
