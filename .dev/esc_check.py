"""ESC 离线验收 —— 构造各种"看起来像但不合格"的证据状态。

最关键的一条是场景 B：结论恰好是对的，但推理过程不合格。ESC 照样必须拦。
因为生产环境里你无法事前知道结论对不对，只能保证过程够扎实 —— 这正是
它区别于"用 LLM 判断答案对不对"的根本。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import esc
from agent.episode_state import EpisodeState, Verdict

CANDS = ["missing_index", "stale_statistics", "lock_contention"]


def mk(name: str) -> EpisodeState:
    st = EpisodeState(episode_id=f"esc_{name}", scenario_id="x")
    st.symptoms = ["p99 上升 40x", "CPU 上升 20x"]
    st.ensure_hypotheses(CANDS)
    st.budget["max_steps"] = 40
    st.budget["steps"] = 12
    return st


def full_evidence(st):
    st.note("a", "explain_seq_scan",
            "4143.08ms, Seq Scan, Rows Removed by Filter=12,000,606, 用到索引=无")
    st.note("a", "index_existence",
            "orders 上的索引: ['idx_orders_created_at', 'orders_pkey']")
    st.note("a", "stats_freshness",
            "orders: live=12,000,773 dead=0 last_analyze=2026-08-19 06:13:21")
    st.note("a", "row_estimate_deviation", "估计与实际行数最大偏差 1 倍")
    st.note("a", "lock_blocking_chain", "阻塞链 0 条（无锁等待）")
    st.note("a", "counterfactual_index",
            "hypopg: cost 180,975 -> 52 (降 100.0%), 优化器会采用=True")
    st.note("a", "slow_query_ranking", "最慢查询 mean=812ms calls=8420")


ok = True
print("=" * 74)

# ── A 证据充分 ────────────────────────────────────────────
st = mk("A")
full_evidence(st)
st.claimed_fault_class = "missing_index"
st.set_verdict("missing_index", Verdict.CONFIRMED)
st.set_verdict("stale_statistics", Verdict.REFUTED)
st.set_verdict("lock_contention", Verdict.REFUTED)
r = esc.check(st, CANDS)
print(f"A 证据充分            -> {r.summary()}")
for d in r.dims:
    print(f"     {d.name} {'PASS' if d.passed else 'FAIL'}"
          f"{'(必需)' if d.mandatory else '      '} {d.detail}")
a_ok = r.verdict == "SUFFICIENT"
print(f"  {'PASS' if a_ok else 'FAIL'}  应放行")
ok &= a_ok

# ── B 静默失败：结论碰巧对，但过程不合格 ────────────────────
print()
st = mk("B")
st.note("a", "slow_query_ranking", "最慢查询 mean=812ms calls=8420")
st.claimed_fault_class = "missing_index"      # 结论其实是对的
st.set_verdict("missing_index", Verdict.CONFIRMED)
r = esc.check(st, CANDS)
print(f"B 静默失败（结论对）   -> {r.summary()}")
print("     定向取证指令:")
for d in r.directives:
    print(f"       - {d}")
b_ok = r.verdict == "INSUFFICIENT" and len(r.directives) >= 2
print(f"  {'PASS' if b_ok else 'FAIL'}  应拦截并给出可执行的补证指令")
ok &= b_ok

# ── C 只取了证但没做鉴别诊断 ───────────────────────────────
print()
st = mk("C")
full_evidence(st)
st.claimed_fault_class = "missing_index"
st.set_verdict("missing_index", Verdict.CONFIRMED)   # 竞争假设一个没排除
r = esc.check(st, CANDS)
d2 = [d for d in r.dims if d.name == "D2"][0]
print(f"C 无鉴别诊断          -> {r.summary()}")
print(f"     D2: {d2.detail} | 未排除: {d2.missing}")
c_ok = r.verdict == "INSUFFICIENT" and not d2.passed
print(f"  {'PASS' if c_ok else 'FAIL'}  D2 必需项不可被其他维度补偿")
ok &= c_ok

# ── D 反事实证伪：只否定当前索引方案，不否定根因 ────────────
print()
st = mk("D")
full_evidence(st)
st.scratchpad = [e for e in st.scratchpad
                 if e["evidence_type"] != "counterfactual_index"]
st.note("a", "counterfactual_index",
        "hypopg: cost 180,975 -> 180,900 (降 0.0%), 优化器会采用=False")
st.claimed_fault_class = "missing_index"
st.set_verdict("missing_index", Verdict.CONFIRMED)
st.set_verdict("stale_statistics", Verdict.REFUTED)
st.set_verdict("lock_contention", Verdict.REFUTED)
r = esc.check(st, CANDS)
d5 = [d for d in r.dims if d.name == "D5"][0]
print(f"D 反事实证伪          -> {r.summary()}")
print(f"     D5: {d5.detail}")
d_ok = ((not d5.passed) and r.verdict == "SUFFICIENT" and
        any("当前具体索引定义" in item for item in r.directives))
print(f"  {'PASS' if d_ok else 'FAIL'}  否定当前方案，但保留 missing-index 路径")
ok &= d_ok

# ── E 证据取值指向反面 ────────────────────────────────────
print()
st = mk("E")
st.note("a", "explain_seq_scan",
        "2.4ms, Index Scan, Rows Removed by Filter=0, 用到索引=idx_orders_user_status")
st.note("a", "index_existence", "orders 上的索引: ['idx_orders_user_status']")
st.claimed_fault_class = "missing_index"
st.set_verdict("missing_index", Verdict.CONFIRMED)
st.set_verdict("stale_statistics", Verdict.REFUTED)
st.set_verdict("lock_contention", Verdict.REFUTED)
r = esc.check(st, CANDS)
d1 = [d for d in r.dims if d.name == "D1"][0]
print(f"E 证据指向反面        -> {r.summary()}")
print(f"     D1: {d1.detail} | 问题项: {d1.missing}")
e_ok = not d1.passed
print(f"  {'PASS' if e_ok else 'FAIL'}  跑了但结果不支持，同样不算数")
ok &= e_ok

# ── F 多个假设同时确认 -> 无法区分 ─────────────────────────
print()
st = mk("F")
full_evidence(st)
st.claimed_fault_class = "missing_index"
st.set_verdict("missing_index", Verdict.CONFIRMED)
st.set_verdict("lock_contention", Verdict.CONFIRMED)
r = esc.check(st, CANDS)
print(f"F 多假设同时确认      -> {r.summary()}")
print(f"     {r.directives[0] if r.directives else ''}")
f_ok = r.verdict in ("AMBIGUOUS", "INSUFFICIENT")
print(f"  {'PASS' if f_ok else 'FAIL'}  不硬下结论")
ok &= f_ok

print()
print("=" * 74)
print("ESC OFFLINE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
