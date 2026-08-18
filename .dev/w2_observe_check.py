"""W2 验收（观测层）：健康态 vs 故障态，各工具是否给出可判定的证据；
并量化"工具内就地萃取"相对于把原文丢回上下文省了多少。"""
import json
import random
import sys
from dataclasses import asdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import snapshot
from sandbox.observe import Observer
from sandbox.injectors.missing_index import MissingIndexInjector
from sandbox.traces import TraceStore

ROOT = Path(__file__).resolve().parent
spec = yaml.safe_load(
    (ROOT / "sandbox/scenarios/missing_index_orders_user_status_v1.yaml").read_text(
        encoding="utf-8"))
HOT = " ".join(spec["workload"]["hot_query"].split())

ok = True
obs = Observer(TraceStore("w2_check"))
inj = MissingIndexInjector(spec)
params = inj.params(random.Random(0))


def show(tag):
    d = obs.explain_query(HOT, {"uid": 4242})
    print(f"  [{tag}] {d.total_time_ms}ms | scans={d.scan_types}")
    print(f"        rows_removed={d.rows_removed_by_filter:,} "
          f"| idx={d.indexes_used} | workers={d.parallel_workers}")
    print(f"        est_vs_actual={d.rows_est_vs_actual}")
    return d


print("=" * 68)
print("[1] 健康态观测")
h = show("healthy")
ok &= any("Index Scan" in s for s in h.scan_types)

print("\n[2] 其余只读工具（健康态）")
idx = obs.get_indexes("orders")
print(f"  get_indexes      -> {[i['name'] for i in idx]}")
ts = obs.get_table_stats("orders")
print(f"  get_table_stats  -> live={ts.n_live_tup:,} dead={ts.n_dead_tup:,} "
      f"dead_ratio={ts.dead_ratio} size={ts.total_size}")
print(f"                      last_analyze={ts.last_analyze[:19]}")
top = obs.get_top_queries(3)
print(f"  get_top_queries  -> {len(top)} 条, 最慢 mean={top[0]['mean_ms']}ms"
      if top else "  get_top_queries  -> (空)")
sess = obs.get_active_sessions()
print(f"  get_active_sessions -> {len(sess)} 个异常会话")
blk = obs.get_blocking_chain()
print(f"  get_blocking_chain  -> {len(blk)} 条阻塞链 (健康态应为 0)")
ok &= (len(blk) == 0)

print("\n[3] 注入故障")
rec = inj.inject(params)
print(f"  {rec.notes}")

print("\n[4] 故障态观测")
f = show("faulty")
ok &= any("Seq Scan" in s for s in f.scan_types)
ok &= f.rows_removed_by_filter > 1_000_000
print(f"  -> D1 直接证据齐备: Seq Scan={any('Seq Scan' in s for s in f.scan_types)}, "
      f"Rows Removed={f.rows_removed_by_filter:,}")

idx2 = obs.get_indexes("orders")
gone = "idx_orders_user_status" not in [i["name"] for i in idx2]
print(f"  -> D1 索引存在性: 目标索引已不在 = {gone}")
ok &= gone

ts2 = obs.get_table_stats("orders")
fresh = bool(ts2.last_analyze)
print(f"  -> D2 排除统计信息过期: last_analyze={ts2.last_analyze[:19]} (新鲜={fresh})")
ok &= fresh

blk2 = obs.get_blocking_chain()
print(f"  -> D2 排除锁竞争: 阻塞链={len(blk2)} 条")
ok &= (len(blk2) == 0)

print("\n[5] D5 反事实模拟（不改生产）")
sim = obs.simulate_index(
    "CREATE INDEX ON orders(user_id, status)", HOT, {"uid": 4242})
print(f"  cost {sim['cost_before']:,.0f} -> {sim['cost_after']:,.0f} "
      f"(降 {sim['cost_reduction_pct']}%)")
print(f"  优化器会采用该假设索引: {sim['would_be_used']}")
print(f"  scans: {sim['scans_before']} -> {sim['scans_after']}")
ok &= sim["would_be_used"]
still_gone = "idx_orders_user_status" not in [
    i["name"] for i in obs.get_indexes("orders")]
print(f"  模拟未真建索引（生产未被改动）: {still_gone}")
ok &= still_gone

print("\n[6] 上下文效率：就地萃取 vs 原文直接回灌")
steps = obs.trace.all_steps()
raw_total = sum(len(s["raw"]) for s in steps)
dig_total = sum(len(json.dumps(s["digest"], ensure_ascii=False)) for s in steps)
print(f"  {len(steps)} 次工具调用")
print(f"  原文合计   {raw_total:>8,} 字符")
print(f"  摘要合计   {dig_total:>8,} 字符")
print(f"  节省       {(1 - dig_total / raw_total) * 100:>7.1f}%  "
      f"(约 {raw_total // 4:,} -> {dig_total // 4:,} token)")

print("\n[7] raw_ref 可回取原文")
ref = h.raw_ref
back = obs.fetch_raw(ref)
print(f"  {ref} -> {len(back):,} 字符, 可解析={bool(json.loads(back))}")

print("\n[8] 回滚复原")
snapshot.reset()
back_ok = "idx_orders_user_status" in [i["name"] for i in obs.get_indexes("orders")]
print(f"  基线索引已恢复: {back_ok}")
ok &= back_ok

print("=" * 68)
print("W2 OBSERVE ACCEPTANCE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
