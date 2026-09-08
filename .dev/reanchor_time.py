"""把 golden 快照里 orders.created_at 的时间锚重新对齐到 now()。

为什么需要：种子把 created_at 铺在"播种时刻往前 365 天"这个区间上，锚点
是播种那一刻。沙箱一放几天，锚点就随真实时间往后漂，而场景的热查询用的是
`created_at > now() - interval '1 day'` 这种滑动窗口 —— 窗口滑过数据末端之
后命中行数一路掉到零。实测漂了 22 小时时，最近 1 天只剩 2,096 行 / 1200 万，
最近 1 小时是 0 行。

这是静默失效：命中零行的查询很快，健康基线看着很漂亮，故障注入后告警也
可能照样响（丢索引后仍是全表扫），但测的已经不是"索引带来的收益"，而是
"在空结果集上扫不扫全表"。跑批不会报错，只会慢慢失去意义。

为什么不在查询里锚：试过 `created_at > (SELECT max(created_at) FROM orders)
- interval '1 day'`，子查询在计划期是未知的运行时参数，规划器查不了直方图、
只能按默认选择性猜，凭空造出 258 倍的估计偏差，永久误确认 stale_statistics。
时间漂移换成了更糟的假信号。锚要重设在数据上，不是在查询里。

为什么是 UPDATE + CLUSTER 而不是直接 UPDATE：整表 UPDATE 会把每一行写成新
版本，物理顺序被打乱，created_at 的相关性从 1.0 掉下来。而相关性正是这套
基准的命门 —— 实测相关性 0.0158 时，按索引定位 4,150 行再回表要 13.8 秒，
索引在与不在差别不大，missing_index 的 Outcome 永远判不出来。CLUSTER 按主键
重排，created_at 随 id 单调，相关性因此回到 1.0。

用法：python3 .dev/reanchor_time.py [--force] [--dry-run]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox import db
from sandbox.snapshot import GOLDEN

# 超过这个漂移就该重锚：最紧的场景窗口是 1 天，漂 6 小时已经吃掉 1/4。
DRIFT_LIMIT_H = 6.0

force = "--force" in sys.argv
dry = "--dry-run" in sys.argv


def snap(label: str) -> None:
    row = db.query(
        "SELECT max(created_at), extract(epoch FROM now() - max(created_at)) / 3600.0, "
        "count(*) FILTER (WHERE created_at > now() - interval '1 day'), "
        "count(*) FILTER (WHERE created_at > now() - interval '1 hour'), "
        "count(*) FROM orders", dbname=GOLDEN)[0]
    corr = db.query(
        "SELECT correlation FROM pg_stats "
        "WHERE tablename = 'orders' AND attname = 'created_at'",
        dbname=GOLDEN)
    print(f"  {label}: max={row[0]}  漂移={row[1]:.1f}h  "
          f"近1天={row[2]:,}  近1小时={row[3]:,}  总行={row[4]:,}  "
          f"相关性={corr[0][0] if corr else '?'}")
    return row[1]


print(f"[reanchor] 目标库 {GOLDEN}")
drift_h = snap("重锚前")

if drift_h < DRIFT_LIMIT_H and not force:
    print(f"[reanchor] 漂移 {drift_h:.1f}h < {DRIFT_LIMIT_H}h，无需重锚"
          f"（--force 可强制）")
    raise SystemExit(0)
if dry:
    print("[reanchor] --dry-run，不做改动")
    raise SystemExit(0)

t0 = time.time()
# 用固定的偏移量，不要在 UPDATE 里写 now() —— 那样每行取到的时刻不同，
# 而且会把单调性破坏在毫秒级上。
shift = db.query(
    "SELECT (now() - max(created_at))::text FROM orders", dbname=GOLDEN)[0][0]
print(f"[reanchor] 平移 {shift}")

print("[reanchor] UPDATE ...")
db.execute("UPDATE orders SET created_at = created_at + %s::interval",
           (shift,), dbname=GOLDEN)
print(f"[reanchor]   {time.time() - t0:.0f}s")

print("[reanchor] CLUSTER 按主键重排（恢复物理相关性）...")
db.execute("CLUSTER orders USING orders_pkey", dbname=GOLDEN)
print(f"[reanchor]   {time.time() - t0:.0f}s")

print("[reanchor] ANALYZE ...")
db.execute("ANALYZE orders", dbname=GOLDEN)

print(f"[reanchor] 完成，用时 {time.time() - t0:.0f}s")
after = snap("重锚后")

corr = db.query(
    "SELECT correlation FROM pg_stats "
    "WHERE tablename = 'orders' AND attname = 'created_at'",
    dbname=GOLDEN)[0][0]
ok = after < 0.5 and corr is not None and corr > 0.99
print()
print("REANCHOR:", "PASS" if ok else
      f"FAIL（漂移 {after:.2f}h 相关性 {corr}）")
raise SystemExit(0 if ok else 1)
