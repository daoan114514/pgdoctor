"""多根因验收 —— 复现"ESC 放行 -> 修一半 -> 反证掉正确根因"这条链。

修复前的行为（真实代码路径）：
  两个互不相关的根因都被确认，但被声明的那个证据齐备
  -> ESC 判 SUFFICIENT 放行（因为 SUFFICIENT 排在多根因检查之前）
  -> 只修一个 -> KPI 回不到基线 -> 判修复失败 -> 回滚
  -> 两次之后正确的根因被 REFUTED_BY_REMEDIATION 永久封掉

场景 1 和场景 5 是这次修复的核心断言，其余是边界与回归保护。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import esc
from agent.episode_state import EpisodeState, RemediationAttempt, Verdict
from knowledge.causal_graph import graph as G

CANDS = ["missing_index", "stale_statistics", "lock_contention"]
ok = True


def check(label, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{(' | ' + extra) if extra else ''}")


def mk(name):
    st = EpisodeState(episode_id=f"mc_{name}", scenario_id="x")
    st.symptoms = ["p99 上升 40x", "CPU 上升 20x"]
    st.ensure_hypotheses(CANDS)
    st.budget["max_steps"] = 40
    st.budget["steps"] = 12
    return st


def missing_index_evidence(st):
    """让 missing_index 的 D1 必需证据齐备且取值支持。"""
    st.note("a", "explain_seq_scan",
            "4143.08ms, Seq Scan, Rows Removed by Filter=12,000,606, 用到索引=无")
    st.note("a", "index_existence",
            "orders 上的索引: ['idx_orders_created_at', 'orders_pkey']")
    st.note("a", "counterfactual_index",
            "hypopg: cost 180,975 -> 52 (降 100.0%), 优化器会采用=True")


print("\n[collapse_chain] 级联 vs 独立")

r = G.collapse_chain(["missing_index"])
check("单根因 -> single", r["kind"] == "single" and r["upstream"] == "missing_index")

r = G.collapse_chain(["table_bloat", "autovacuum_starvation"])
check("相邻两跳 -> cascade，取上游",
      r["kind"] == "cascade" and r["upstream"] == "autovacuum_starvation",
      f"upstream={r['upstream']}")

r = G.collapse_chain(["long_idle_transaction", "table_bloat"])
check("跨三跳仍识别为 cascade",
      r["kind"] == "cascade" and r["upstream"] == "long_idle_transaction"
      and len(r["path"]) == 3,
      f"path={' -> '.join(r['path'])}")

r = G.collapse_chain(["missing_index", "lock_contention"])
check("互不相关 -> independent",
      r["kind"] == "independent" and set(r["independent"]) ==
      {"missing_index", "lock_contention"})


print("\n[场景 1] ★ 两个独立根因都确认，声明的那个证据齐备")
st = mk("independent")
missing_index_evidence(st)
st.claimed_fault_class = "missing_index"
st.claimed_root_cause = "orders(user_id,status) 无可用索引，全表扫 1200 万行"
st.set_verdict("missing_index", Verdict.CONFIRMED, note="Seq Scan + 无覆盖索引")
st.set_verdict("lock_contention", Verdict.CONFIRMED, note="阻塞链 3 条，等待 8 分钟")
st.set_verdict("stale_statistics", Verdict.REFUTED, note="估计与实际偏差仅 1.1 倍")
rep = esc.check(st, candidates=CANDS)
check("必须判 AMBIGUOUS（修复前这里是 SUFFICIENT）",
      rep.verdict == esc.ESCVerdict.AMBIGUOUS.value, f"实得 {rep.verdict}")
check("指令要点出这是多根因",
      any("互不相关" in d for d in rep.directives),
      rep.directives[0][:52] if rep.directives else "无指令")


print("\n[场景 2] 级联：声明了下游根因")
st = mk("cascade")
st.ensure_hypotheses(["table_bloat", "autovacuum_starvation"])
st.note("a", "dead_tuple_ratio", "orders: live=8,200,000 dead=4,100,000 膨胀率 33%")
st.claimed_fault_class = "table_bloat"
st.claimed_root_cause = "orders 表膨胀 33%，扫描变慢"
st.set_verdict("table_bloat", Verdict.CONFIRMED, note="死元组占比 33%")
st.set_verdict("autovacuum_starvation", Verdict.CONFIRMED,
               note="autovacuum_enabled=false，last_autovacuum 为空")
rep = esc.check(st, candidates=["table_bloat", "autovacuum_starvation"])
check("级联不判歧义，判 INSUFFICIENT 让它改声明上游",
      rep.verdict == esc.ESCVerdict.INSUFFICIENT.value, f"实得 {rep.verdict}")
check("指令要指出上游是谁",
      any("autovacuum_starvation" in d and "上游" in d for d in rep.directives),
      rep.directives[0][:60] if rep.directives else "无指令")


print("\n[场景 3] 单根因证据齐备（回归保护：别把正常路径拦了）")
st = mk("happy")
missing_index_evidence(st)
st.claimed_fault_class = "missing_index"
st.claimed_root_cause = "orders(user_id,status) 无可用索引"
st.set_verdict("missing_index", Verdict.CONFIRMED, note="Seq Scan + 无覆盖索引")
st.set_verdict("stale_statistics", Verdict.REFUTED, note="偏差仅 1.1 倍")
st.set_verdict("lock_contention", Verdict.REFUTED, note="阻塞链为空")
rep = esc.check(st, candidates=CANDS)
check("仍然放行", rep.verdict == esc.ESCVerdict.SUFFICIENT.value,
      f"实得 {rep.verdict}")


print("\n[场景 4] map_symptoms 的 fallback 语义")
check("假设生成需要种子，空输入补默认",
      G.map_symptoms([], fallback=True) == ["latency_p99_up"])
check("判孤儿症状绝不能凭空补",
      G.map_symptoms([], fallback=False) == [])
check("扩后的词汇表认得阻塞",
      "queries_blocked" in G.map_symptoms(["大量查询挂起不返回"]))
check("认得连接",
      "conn_near_limit" in G.map_symptoms(["连接数 98/100"]))


print("\n[场景 5] ★ 台账保护：不可归因的失败不该反证根因")
st = mk("ledger")
st.claimed_fault_class = "missing_index"
for i in range(2):
    st.record_attempt(RemediationAttempt(
        root_cause="missing_index", sql=f"CREATE INDEX i{i} ON orders(x)",
        predicted={}, actual={}, verdict="FAILED_NO_IMPROVEMENT",
        rolled_back=True, inference="KPI 未恢复（另一个故障仍在）",
        counts_against_root_cause=False))
check("两次失败但都不可归因 -> 反证计数为 0",
      st.attempts_for("missing_index") == 0,
      f"attempts_for={st.attempts_for('missing_index')}")
check("失败记录本身不丢（知识单调增长）",
      st.attempts_logged_for("missing_index") == 2)
check("因此不会触发反证",
      st.attempts_for("missing_index") < 2 and
      not st.already_failed("missing_index"))

st2 = mk("ledger2")
st2.claimed_fault_class = "missing_index"
for i in range(2):
    st2.record_attempt(RemediationAttempt(
        root_cause="missing_index", sql=f"CREATE INDEX j{i} ON orders(x)",
        predicted={}, actual={}, verdict="FAILED_NO_IMPROVEMENT",
        rolled_back=True, inference="KPI 未恢复，症状已被完整解释"))
check("对照：可归因的两次失败仍然反证（保护不能太宽）",
      st2.attempts_for("missing_index") == 2)
st2.refute_by_remediation("missing_index", "2 次修复均未改善")
check("对照：反证后 already_failed 生效",
      st2.already_failed("missing_index"))


print(f"\nMULTICAUSE: {'PASS' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
