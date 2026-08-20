"""负载生成器。

没有活负载，指标不会动，故障就"看不见" —— 这是 DBA-Bench 说的 production
fidelity 的来源：活跃负载 + 持久状态 + 多源观测。

跑法（长驻进程）：
    python3 -m sandbox.workload --scenario sandbox/scenarios/xxx.yaml

它把滚动窗口的延迟分位数写到 traces/workload_metrics.json，
沙箱 env 和判分器读那个文件拿 KPI。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import threading
import time
from collections import deque
from pathlib import Path

import yaml

from sandbox import db

ROOT = Path(__file__).resolve().parent.parent
METRICS_PATH = ROOT / "traces" / "workload_metrics.json"

_stop = threading.Event()
_lock = threading.Lock()
# 每类查询各自一个滚动窗口：hot 是受故障影响的，canary 用于回归检查
_samples: dict[str, deque] = {}
WINDOW = 50000   # 滚动样本上限。太小会让高负载下的 qps 被缓冲区截断而低报


def _record(kind: str, ms: float, ok: bool) -> None:
    with _lock:
        buf = _samples.setdefault(kind, deque(maxlen=WINDOW))
        buf.append((time.time(), ms, ok))


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(round((p / 100.0) * (len(values) - 1))))
    return values[idx]


def snapshot(window_s: float = 30.0) -> dict:
    """最近 window_s 秒的指标快照。"""
    now = time.time()
    out: dict[str, dict] = {}
    with _lock:
        for kind, buf in _samples.items():
            recent = [(ms, ok) for (ts, ms, ok) in buf if now - ts <= window_s]
            lat = [ms for ms, ok in recent]
            errs = sum(1 for _, ok in recent if not ok)
            out[kind] = {
                "n": len(recent),
                "qps": round(len(recent) / window_s, 2),
                "p50_ms": round(_pct(lat, 50), 2),
                "p95_ms": round(_pct(lat, 95), 2),
                "p99_ms": round(_pct(lat, 99), 2),
                "max_ms": round(max(lat), 2) if lat else 0.0,
                "errors": errs,
            }
    return {"ts": now, "window_s": window_s, "by_query": out}


def _worker(hot_sql: str, canaries: list[str], n_users: int) -> None:
    """一个工作线程：主跑热查询，间歇跑金丝雀与写入，构成真实混合负载。"""
    while not _stop.is_set():
        try:
            with db.connect(role="super", autocommit=True) as conn:
                with conn.cursor() as cur:
                    # 没有超时的话，被锁阻塞的查询会永远挂着，一个延迟样本
                    # 都记不上，指标反而显示"正常"。真实应用也不会无限等。
                    cur.execute("SET statement_timeout = '5s'")
                    while not _stop.is_set():
                        # 1) 热查询 —— 故障直接作用于它
                        t0 = time.perf_counter()
                        ok = True
                        try:
                            cur.execute(hot_sql, {"uid": random.randint(1, n_users)})
                            cur.fetchall()
                        except Exception:
                            ok = False
                        _record("hot", (time.perf_counter() - t0) * 1000, ok)

                        # 2) 金丝雀 —— 不应受本故障影响，Safe Pass 的回归依据
                        for i, sql in enumerate(canaries):
                            t0 = time.perf_counter()
                            ok = True
                            try:
                                cur.execute(sql, {"uid": random.randint(1, n_users)})
                                cur.fetchall()
                            except Exception:
                                ok = False
                            _record(f"canary_{i}", (time.perf_counter() - t0) * 1000, ok)

                        # 3) 周期性新建连接 —— 常驻连接感知不到连接池打满，
                        #    只有新请求会被拒，这是该故障唯一的可观测面
                        if random.random() < 0.08:
                            t0 = time.perf_counter()
                            ok = True
                            try:
                                # 探针模拟业务应用发起的新连接，
                                # 用 app_user（无保留位）才感知得到池子打满
                                with db.connect(role="app") as probe:
                                    with probe.cursor() as pc:
                                        pc.execute("SELECT 1")
                                        pc.fetchall()
                            except Exception:
                                ok = False
                            _record("newconn", (time.perf_counter() - t0) * 1000, ok)

                        # 4) 少量写入 —— 让 autovacuum / 统计信息保持活跃
                        if random.random() < 0.05:
                            t0 = time.perf_counter()
                            ok = True
                            try:
                                cur.execute(
                                    "INSERT INTO orders (user_id, status, total, created_at) "
                                    "VALUES (%s, 'PENDING', %s, now())",
                                    (random.randint(1, n_users), round(random.random() * 500, 2)),
                                )
                            except Exception:
                                ok = False
                            _record("write", (time.perf_counter() - t0) * 1000, ok)
        except Exception:
            if _stop.is_set():
                return
            time.sleep(1.0)  # 连接断了就重连


def _metrics_writer() -> None:
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    while not _stop.wait(1.0):
        snap = snapshot()
        tmp = METRICS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        tmp.replace(METRICS_PATH)  # 原子替换，读者不会看到半截文件


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--duration", type=float, default=0.0, help="0 = 一直跑")
    args = ap.parse_args()

    spec = yaml.safe_load(Path(args.scenario).read_text(encoding="utf-8"))
    wl = spec["workload"]
    hot_sql = " ".join(wl["hot_query"].split())
    canaries = wl.get("canary_queries", [])
    concurrency = int(wl.get("concurrency", 8))
    n_users = int(os.getenv("SEED_USERS", "100000"))

    if not db.wait_ready():
        raise SystemExit("database not reachable")

    print(f"[workload] concurrency={concurrency} hot={hot_sql[:60]}...")
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())

    threads = [threading.Thread(target=_metrics_writer, daemon=True)]
    threads += [
        threading.Thread(target=_worker, args=(hot_sql, canaries, n_users), daemon=True)
        for _ in range(concurrency)
    ]
    for t in threads:
        t.start()

    deadline = time.time() + args.duration if args.duration else None
    try:
        while not _stop.is_set():
            time.sleep(0.5)
            if deadline and time.time() > deadline:
                break
    except KeyboardInterrupt:
        pass
    _stop.set()
    print("[workload] stopped;", json.dumps(snapshot(), indent=2))


if __name__ == "__main__":
    main()
