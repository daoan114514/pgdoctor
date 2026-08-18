"""W1 验收：用真实沙箱模块跑通 基线 -> 注入 -> 验证 -> 回滚 -> 恢复。"""
import random, statistics, sys, time
from pathlib import Path
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sandbox import db, snapshot
from sandbox.injectors.missing_index import MissingIndexInjector

ROOT = Path(__file__).resolve().parent.parent
spec = yaml.safe_load(
    (ROOT / "sandbox/scenarios/missing_index_orders_user_status_v1.yaml").read_text(encoding="utf-8")
)
HOT = " ".join(spec["workload"]["hot_query"].split())


def measure(n=25):
    lat = []
    with db.connect(role="ro") as conn, conn.cursor() as cur:
        for _ in range(n):
            t0 = time.perf_counter()
            cur.execute(HOT, {"uid": random.randint(1, 100000)})
            cur.fetchall()
            lat.append((time.perf_counter() - t0) * 1000)
    lat.sort()
    return {"p50": round(lat[len(lat)//2], 2), "p99": round(lat[-1], 2),
            "mean": round(statistics.mean(lat), 2)}


def plan_summary():
    with db.connect(role="ro") as conn, conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, COSTS OFF) " + HOT, {"uid": 4242})
        txt = "\n".join(r[0] for r in cur.fetchall())
    scan = "Seq Scan" if "Seq Scan" in txt else ("Index Scan" if "Index Scan" in txt else "?")
    removed = [l.strip() for l in txt.splitlines() if "Rows Removed" in l]
    return scan, (removed[0] if removed else "-")

ok = True
inj = MissingIndexInjector(spec)
params = inj.params(random.Random(0))

print("=" * 62)
print("[1] 健康基线")
base = measure(); s, r = plan_summary()
print(f"    {base}  | plan={s} | {r}")
ok &= (s == "Index Scan")

print("[2] 注入故障")
rec = inj.inject(params)
print(f"    {rec.fault_class}: {rec.notes}")

print("[3] verify_injected")
inj_ok = inj.verify_injected(params)
print(f"    索引确已消失: {inj_ok}")
ok &= inj_ok

print("[4] 故障态")
bad = measure(); s2, r2 = plan_summary()
print(f"    {bad}  | plan={s2} | {r2}")
ok &= (s2 == "Seq Scan")
ratio = bad["p50"] / base["p50"] if base["p50"] else 0
print(f"    劣化倍数 p50: {ratio:.0f}x")
ok &= (ratio > 20)

alert = bad["p99"] > 300
print(f"[5] 触发告警阈值 (p99>300ms): {alert}  (p99={bad['p99']}ms)")

print("[6] 快照回滚")
t0 = time.time(); snapshot.reset(); print(f"    耗时 {time.time()-t0:.1f}s")

print("[7] 恢复校验")
back = measure(); s3, r3 = plan_summary()
print(f"    {back}  | plan={s3}")
restored = (not inj.verify_injected(params)) and s3 == "Index Scan"
print(f"    基线已恢复: {restored}")
ok &= restored

print("=" * 62)
print("W1 ACCEPTANCE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
