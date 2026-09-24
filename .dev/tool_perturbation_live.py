#!/usr/bin/env python3
"""活库标定：每个取证工具实际给累计计数器记了多少账，观测器的自家记账对不对得上（规则 5、6）。

窗口类证据（seq_scan_volume / temp_file_volume）读的是 pg_stat_user_tables 与
pg_stat_database 的累计计数器，而诊断工具自己也会执行查询：EXPLAIN ANALYZE 真跑一遍热查询，
值域漂移要全表 count(*)。观测器把自家的账记下来、从原始计数器里扣掉；扣除量里值域漂移
来自事务级视图（精确），EXPLAIN 来自计划 JSON（按 loop 平均的行数乘回，外溢取块数上界）。
这里在**空载**的库上逐个工具量：

  原始增量（工具前后各读一次共享统计）  vs  观测器自家记账的增量

要求：顺序扫描次数精确相等；索引扫描次数非并行计划精确相等、并行计划记账是上界（原始 ≤ 记账，见
observe._merge_own_counts）；顺序读行数误差 ≤ 每个 loop 1 行（计划取整）；
外溢字节数原始增量 ≤ 自家上界（上界的方向不能反）；工具返回后立即读与 1.5 秒后再读一致
（强制刷账生效，没有迟到的计数 —— 迟到会让扣除落在错的窗口里）。不记账的工具原始增量
必须是 0。

不进 checkall（要活库）。用法：沙箱静止、无负载、golden 状态下
    python3 .dev/tool_perturbation_live.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sandbox import db  # noqa: E402
from sandbox.observe import Observer  # noqa: E402
from sandbox.traces import TraceStore  # noqa: E402

TABLES = ("orders", "users")
fails: list[str] = []


def counters() -> dict:
    out = {}
    for rel, seq, tup, idx in db.query(
            "SELECT relname, coalesce(seq_scan, 0), coalesce(seq_tup_read, 0), coalesce(idx_scan, 0)"
            " FROM pg_stat_user_tables WHERE relname = ANY(%s)", (list(TABLES),)):
        out[rel] = {"seq_scan": int(seq), "seq_tup_read": int(tup), "idx_scan": int(idx)}
    out["temp_bytes"] = int(db.query(
        "SELECT temp_bytes FROM pg_stat_database WHERE datname = current_database()")[0][0])
    return out


def own_of(o: Observer) -> dict:
    out = {rel: dict(o._own_scans.get(rel) or {"seq_scan": 0, "seq_tup_read": 0, "idx_scan": 0})
           for rel in TABLES}
    out["temp_bytes"] = o._own_temp_bytes
    return out


def diff(a: dict, b: dict) -> dict:
    out = {rel: {k: b[rel][k] - a[rel][k] for k in a[rel]} for rel in TABLES}
    out["temp_bytes"] = b["temp_bytes"] - a["temp_bytes"]
    return out


def measure(label: str, call, *, accounted: bool, loops_hint: int = 64) -> None:
    o = Observer(TraceStore("ep_tool_perturbation_live"))
    before, own_before = counters(), own_of(o)
    started = time.monotonic()
    result = call(o)
    took = time.monotonic() - started
    # 并行计划的索引扫描记账是上界（见 observe._merge_own_counts），这里按上界判
    idx_upper = accounted and int(getattr(result, "parallel_workers", 0) or 0) > 0
    right_after = counters()
    time.sleep(1.5)
    settled = counters()
    raw, late = diff(before, settled), diff(right_after, settled)
    own = diff(own_before, own_of(o))
    print(f"\n== {label}  ({took:.2f}s)")
    for rel in TABLES:
        print(f"   {rel:7s} raw {raw[rel]}  own {own[rel]}")
    print(f"   temp    raw {raw['temp_bytes']:,}  own(upper) {own['temp_bytes']:,}")
    problems = []
    if any(v for rel in TABLES for v in late[rel].values()) or late["temp_bytes"]:
        problems.append(f"工具返回后仍有迟到的计数 {late}")
    for rel in TABLES:
        r, w = raw[rel], (own[rel] if accounted else {"seq_scan": 0, "seq_tup_read": 0, "idx_scan": 0})
        if r["seq_scan"] != w["seq_scan"]:
            problems.append(f"{rel} 顺序扫描次数不符 raw={r} own={w}")
        if (r["idx_scan"] > w["idx_scan"]) if idx_upper else (r["idx_scan"] != w["idx_scan"]):
            problems.append(f"{rel} 索引扫描次数{'超过记账上界' if idx_upper else '不符'} raw={r} own={w}")
        if abs(r["seq_tup_read"] - w["seq_tup_read"]) > max(1, loops_hint):
            problems.append(f"{rel} 顺序读行数误差 {r['seq_tup_read'] - w['seq_tup_read']}")
    own_temp = own["temp_bytes"] if accounted else 0
    if raw["temp_bytes"] > own_temp:
        problems.append(f"外溢原始增量 {raw['temp_bytes']:,} 超过自家上界 {own_temp:,}（上界方向反了）")
    if raw["temp_bytes"] == 0 and own_temp > 0:
        print("   note: 计划记了外溢但库级计数器没涨（上界偏松，只会让净值偏低、不会误支持）")
    for p in problems:
        print("   FAIL", p)
        fails.append(f"{label}: {p}")
    if not problems:
        print("   OK")


active = db.query("SELECT count(*) FROM pg_stat_activity WHERE usename = 'app_user'")[0][0]
if active:
    print(f"有 {active} 个 app_user 连接在跑：空载标定不成立，先停负载（pkill -f sandbox.workload）")
    raise SystemExit(2)

hot_conn = "SELECT id, total, created_at FROM orders WHERE user_id = %(uid)s AND status = 'PENDING'"
hot_missing = ("SELECT id, total, user_id FROM orders WHERE created_at > now() - interval '1 day'"
               " AND status = 'PENDING'")
hot_stale = ("SELECT o.status, count(*), sum(o.total) FROM orders o JOIN users u ON u.id = o.user_id"
             " WHERE o.created_at > now() - interval '1 hour' GROUP BY o.status")
hot_lock = "UPDATE orders SET status = 'PAID' WHERE id = %(uid)s"
seq_filter = "SELECT id FROM orders WHERE total < 0 AND status = 'PENDING'"
spill = "SELECT id, total FROM orders WHERE user_id < 3000 ORDER BY total, id"

measure("explain_query 热查询(连接打满/误导空闲)", lambda o: o.explain_query(hot_conn, {"uid": 4242}), accounted=True)
measure("explain_query 热查询(缺索引，golden 走索引)", lambda o: o.explain_query(hot_missing), accounted=True)
measure("explain_query 热查询(统计过期，嵌套循环)", lambda o: o.explain_query(hot_stale), accounted=True,
        loops_hint=100000)
measure("explain_query 热查询(锁竞争，SELECT 代理)", lambda o: o.explain_query(hot_lock, {"uid": 4242}),
        accounted=True)
measure("explain_query 并行全表扫", lambda o: o.explain_query(seq_filter), accounted=True)
measure("explain_query 位图扫描 + 外溢排序", lambda o: o.explain_query(spill), accounted=True)
measure("get_table_stats（值域漂移全表 count）", lambda o: o.get_table_stats("orders"), accounted=True)
measure("get_physical_bloat", lambda o: o.get_physical_bloat("orders"), accounted=False)
measure("get_indexes", lambda o: o.get_indexes("orders"), accounted=False)
measure("get_top_queries", lambda o: o.get_top_queries(5), accounted=False)
measure("simulate_index（hypopg，不执行）",
        lambda o: o.simulate_index("CREATE INDEX ON orders (total)", seq_filter), accounted=False)
measure("get_database_stats", lambda o: o.get_database_stats(), accounted=False)
measure("get_table_scan_counters（MONITOR 基线，只读计数器）",
        lambda o: o.get_table_scan_counters("orders"), accounted=False)

# MONITOR 的基线读数与 get_table_stats 必须同源同口径：原始值一致、统计周期是同一个字符串
# （ESC 按它核对 seq_scan_volume 的周期，差一个字符就判"周期不符"不可信）。
print()
print("== 基线读数与 get_table_stats 同口径")
_o = Observer(TraceStore("ep_tool_perturbation_live"))
_c = _o.get_table_scan_counters("orders")
_t = _o.get_table_stats("orders")
_same = (_c["stats_reset"] == _t.stats_reset and _c["seq_scan_raw"] == _t.seq_scan_raw
         and _c["seq_tup_read_raw"] == _t.seq_tup_read_raw and _c["idx_scan_raw"] == _t.idx_scan_raw)
print(f"   counters {{{_c['seq_scan_raw']}, {_c['seq_tup_read_raw']}, {_c['idx_scan_raw']}}} epoch {_c['stats_reset']!r}")
print(f"   table    {{{_t.seq_scan_raw}, {_t.seq_tup_read_raw}, {_t.idx_scan_raw}}} epoch {_t.stats_reset!r}")
if _same:
    print("   OK")
else:
    print("   FAIL 基线读数与 get_table_stats 不同口径")
    fails.append("get_table_scan_counters 与 get_table_stats 不同口径")

print()
if fails:
    print(f"TOOL PERTURBATION LIVE: FAIL（{len(fails)}）")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print("TOOL PERTURBATION LIVE: PASS")
