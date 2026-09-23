#!/usr/bin/env python3
"""活库标定：get_table_stats 连续两次，净 seq_scan 增量应为 0，原始值应只涨自家扫描（规则 5）。

不进 checkall（要活库）。用法：python3 .dev/own_scan_live_check.py  （沙箱静止、无负载时跑）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sandbox.observe import Observer  # noqa: E402
from sandbox.traces import TraceStore  # noqa: E402

o = Observer(TraceStore("ep_own_scan_live_check"))
a = o.get_table_stats("orders")
b = o.get_table_stats("orders")
c = o.get_table_stats("orders")
own = o._own_scans.get("orders", {})
print(f"raw seq_scan: {a.seq_scan_raw} -> {b.seq_scan_raw} -> {c.seq_scan_raw} | raw tup: {a.seq_tup_read_raw} -> {b.seq_tup_read_raw} -> {c.seq_tup_read_raw}")
print(f"net seq_scan: {a.seq_scan} -> {b.seq_scan} -> {c.seq_scan} | net tup: {a.seq_tup_read} -> {b.seq_tup_read} -> {c.seq_tup_read}")
print(f"own totals after 3 calls: {own} | columns scanned: {[d['column'] for d in c.stats_range_columns]}")
ok = True
raw_delta = c.seq_scan_raw - b.seq_scan_raw
net_delta = c.seq_scan - b.seq_scan
tup_delta = c.seq_tup_read - b.seq_tup_read
print(f"between call 2 and 3: raw +{raw_delta} scans, net +{net_delta} scans, net +{tup_delta} tuples")
if net_delta != 0 or tup_delta != 0:
    ok = False
    print("FAIL: 静止库上净增量应为 0（自家扫描没扣干净，或有别的负载在跑）")
if own.get("seq_scan", 0) <= 0 and own.get("idx_scan", 0) <= 0:
    ok = False
    print("FAIL: 没有记到任何自家扫描计数（pg_stat_xact_user_tables 读取失败？）")
print("OWN SCAN LIVE:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
