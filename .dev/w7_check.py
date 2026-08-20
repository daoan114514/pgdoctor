"""W7 验收：案例记忆库（离线，零模型成本）。

验四件事：
  1. 写入策略：只有被验证过的知识才进库；eval 场景永不入库
  2. 混合检索：指纹相似度能把同类事故排在前面
  3. 负例：失败的修复被记住并注入先验
  4. 记忆治理：连续帮倒忙的案例被隔离
"""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState, RemediationAttempt, Verdict
from knowledge import case_store as cs

ok = True
# 用独立目录，不污染真实案例库
REAL = cs.CASES_DIR
TMP = REAL.parent / "cases_w7test"
if TMP.exists():
    shutil.rmtree(TMP)
cs.CASES_DIR = TMP


class FakeScore:
    def __init__(self, d=True, o=True, s=True):
        self.diagnosis, self.outcome, self.safe_pass = d, o, s


def mk_state(rc="missing_index", p99_ratio=40.0, waits=None, with_fail=False):
    st = EpisodeState(episode_id=f"w7_{int(time.time()*1000)}", scenario_id="s")
    st.baseline_kpi = {"p99_ms": 20.0, "p50_ms": 3.0, "cpu_pct": 40.0}
    st.current_kpi = {"p99_ms": 20.0 * p99_ratio, "p50_ms": 300.0,
                      "cpu_pct": 800.0}
    st.symptoms = ["p99 上升 40x", "CPU 上升 20x"]
    st.claimed_fault_class = rc
    st.claimed_root_cause = f"{rc} 的详细描述：orders 表缺少复合索引导致全表扫"
    st.note("a", "explain_seq_scan",
            "4143ms, Seq Scan, Rows Removed by Filter=12,000,606")
    st.note("a", "index_existence", "orders 上的索引: ['orders_pkey']")
    st.note("a", "session_wait_profile",
            f"3 个异常会话，等待事件={waits or '无'}")
    st.set_verdict(rc, Verdict.CONFIRMED)
    st.set_verdict("stale_statistics", Verdict.REFUTED, note="last_analyze 新鲜")
    if with_fail:
        st.record_attempt(RemediationAttempt(
            root_cause=rc, sql="CREATE INDEX CONCURRENTLY idx_wrong ON orders(total)",
            predicted={"p99_ms": "<50"}, actual={"p99_ms": 4222},
            verdict="FAILED_NO_IMPROVEMENT", rolled_back=True,
            inference="索引已生效但 p99 无改善"))
    return st


print("=" * 74)
print("[1] 写入策略")
cases = []
for label, st, score, spec, expect in [
    ("成功解决 -> 入库", mk_state(), FakeScore(), {"id": "s1", "split": "train"}, True),
    ("含失败尝试 -> 入库（负例价值高）",
     mk_state(with_fail=True), FakeScore(), {"id": "s2", "split": "train"}, True),
    ("eval 场景 -> 永不入库（防污染）",
     mk_state(), FakeScore(), {"id": "s3", "split": "eval"}, False),
    ("诊断未命中 -> 不入库",
     mk_state(), FakeScore(d=False), {"id": "s4", "split": "train"}, False),
]:
    c = cs.write_case(st, score, spec, ["CREATE INDEX CONCURRENTLY i ON orders(a,b)"])
    got = c is not None
    print(f"  {'PASS' if got == expect else 'FAIL'}  {label:<36} -> "
          f"{'已入库' if got else '未入库'}")
    ok &= (got == expect)
    if c:
        cases.append(c)
    time.sleep(0.01)

st_lock = mk_state(rc="lock_contention", p99_ratio=3.0, waits="['Lock:transactionid']")
c = cs.write_case(st_lock, FakeScore(), {"id": "s5", "split": "train"},
                  ["SELECT pg_terminate_backend(123)"])
cases.append(c)

print(f"\n  库状态: {cs.library_stats()}")

print("\n[2] 混合检索：指纹相似度")
q = cs.fingerprint_from_state(mk_state())          # 与缺索引案例同型
hits = cs.search(q, split="train", top_k=4,
                 query_text="orders 缺少复合索引 全表扫")
for h in hits:
    print(f"  {h['case'].root_cause:<18} 总分={h['score']:.3f} "
          f"指纹={h['fp_sim']:.2f} 文本={h['txt_sim']:.2f}")
top_is_right = hits and hits[0]["case"].root_cause == "missing_index"
lock_ranked_lower = all(h["fp_sim"] <= hits[0]["fp_sim"] for h in hits)
print(f"  {'PASS' if top_is_right else 'FAIL'}  同型事故排在最前")
print(f"  {'PASS' if lock_ranked_lower else 'FAIL'}  异型事故指纹相似度更低")
ok &= top_is_right and lock_ranked_lower

print("\n[3] 防污染：eval 检索不到 train 之外的东西")
eval_hits = cs.search(q, split="eval", top_k=4)
print(f"  split=eval 时命中: {len(eval_hits)} 例（库里 eval 案例数 "
      f"{cs.library_stats()['eval']}）")
no_leak = all(h["case"].split == "eval" for h in eval_hits)
print(f"  {'PASS' if no_leak else 'FAIL'}  检索结果不跨 split")
ok &= no_leak

print("\n[4] 负例进入先验")
prior = cs.render_prior(hits)
print("  " + "\n  ".join(prior.splitlines()))
has_neg = "负例" in prior
has_guard = "不能替代取证" in prior
print(f"  {'PASS' if has_neg else 'FAIL'}  先验里带负例警示")
print(f"  {'PASS' if has_guard else 'FAIL'}  先验里带'不可替代取证'的红线")
print(f"  先验长度 {len(prior)} 字符（渐进式披露，详情按需 fetch_case）")
ok &= has_neg and has_guard

print("\n[5] 记忆治理：连续帮倒忙 -> 隔离")
target = cases[0].case_id
for i in range(4):
    cs.record_reuse(target, helped=False)
after = cs.fetch_case(target)
print(f"  {target[:40]} 复用 {after['reuse_count']} 次后: "
      f"utility={after['utility_score']} status={after['status']}")
quarantined = after["status"] == "quarantined"
still_found = any(h["case"].case_id == target
                  for h in cs.search(q, split="train", top_k=5))
print(f"  {'PASS' if quarantined else 'FAIL'}  已隔离")
print(f"  {'PASS' if not still_found else 'FAIL'}  隔离后不再被召回")
ok &= quarantined and not still_found

shutil.rmtree(TMP, ignore_errors=True)
cs.CASES_DIR = REAL
print("\n" + "=" * 74)
print("W7 ACCEPTANCE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
