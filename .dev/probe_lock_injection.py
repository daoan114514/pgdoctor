"""用真正的注入器跑一遍，量清楚锁到底有没有拿到。"""
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from sandbox import db
from sandbox.injectors.more import LockContentionInjector

ROOT = Path(__file__).resolve().parent.parent
spec = yaml.safe_load(
    (ROOT / "sandbox/scenarios/lock_contention_eval_v1.yaml").read_text(
        encoding="utf-8"))

inj = LockContentionInjector(spec)
params = inj.params(random.Random(0))
print(f"参数: {params}")

t0 = time.time()
rec = inj.inject(params)
print(f"inject() 返回耗时 {time.time() - t0:.1f}s  {rec.notes}\n")

for t in (3, 8, 15, 30, 60):
    while time.time() - t0 < t:
        time.sleep(0.5)
    pid = inj._holder_pid
    rows = db.query(
        "SELECT state, round(extract(epoch FROM now()-xact_start))::int,"
        " coalesce(wait_event_type,'-'), left(coalesce(query,''),40)"
        " FROM pg_stat_activity WHERE pid = %s", (pid,))
    nl = db.query(
        "SELECT count(*) FILTER (WHERE locktype='tuple'),"
        " count(*) FILTER (WHERE locktype='transactionid'),"
        " count(*) FILTER (WHERE locktype='relation')"
        " FROM pg_locks WHERE pid = %s", (pid,))[0]
    st = rows[0] if rows else ("<会话已消失>", 0, "-", "")
    print(f"  t+{t:>2}s state={st[0]:<20} xact={st[1]:>3}s wait={st[2]:<9} "
          f"locks(tuple/txid/rel)={nl[0]}/{nl[1]}/{nl[2]}  {st[3]}")
    print(f"        verify_injected={inj.verify_injected(params)}")

print("\n-- 直接测：更新被锁区间内的一行会不会阻塞 --")
with db.connect(role="super", autocommit=True) as c, c.cursor() as cur:
    cur.execute("SET statement_timeout = '3s'")
    for uid in (1, 500, 50000, 99999):
        t1 = time.perf_counter()
        try:
            cur.execute("UPDATE orders SET status='PAID' WHERE id = %s", (uid,))
            print(f"  id={uid:<6} 未被阻塞，成功 {(time.perf_counter()-t1)*1000:.0f}ms")
        except Exception as e:
            print(f"  id={uid:<6} {(time.perf_counter()-t1)*1000:.0f}ms "
                  f"{type(e).__name__}: {str(e).strip()[:60]}")

r = db.query("SELECT min(id), max(id), count(*) FROM orders WHERE id <= 100000")[0]
print(f"\norders 中 id<=100000 的行: min={r[0]} max={r[1]} count={r[2]}")

inj.cleanup()
print("已清理")
