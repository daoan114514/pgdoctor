"""排除一个假设的证据，取值不能反过来支持它。

跨轮对账（5×100）暴露的：D2 只数排除的数量，不看取值方向。在"落进
陷阱"的样本里竞争假设集恰好包含真根因，策略全排时真根因也在其中 ——
D2 把这算作鉴别诊断的进展，等于在奖励"把对的答案排掉"。

修完之后还得防两头：既要拦住"拿支持它的证据去排除它"，也不能误伤
"拿真能反证的证据去排除"。第一版修复就误伤了 —— _supports 对没实现
检查的组合返回 True 表示"这条不查"，被我当成了"这条支持"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import esc
from agent.esc import _CHECKED_TYPES, _supports, _value_checked
from agent.episode_state import EpisodeState, Verdict
from knowledge.causal_graph import graph as G
from knowledge.evidence_predicates import registered_predicates

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}   {detail}")
    if not cond:
        fails.append(name)


OBS = ("连接 92/100 (92.0%), 逼近上限=True, "
       "idle in transaction=86, 按角色={'app_user': 86}")


def mk(refuted: dict):
    st = EpisodeState(episode_id="rt", scenario_id="misleading_idle_txn_eval_v1")
    st.symptoms = ["错误 139"]
    st.claimed_fault_class = "connection_exhaustion"
    st.note("agent", "connection_count", OBS, "", [])
    st.note("agent", "idle_in_transaction", OBS, "", [])
    st.note("agent", "session_wait_profile", "等待事件=无", "", [])
    st.set_verdict("connection_exhaustion", Verdict.CONFIRMED,
                   note="连接 92/100 逼近上限，判定连接池打满")
    for name, note in refuted.items():
        st.set_verdict(name, Verdict.REFUTED, note=note)
    return st


print("[1] 拿支持它的证据去『排除』它 —— 不能算数")
st = mk({"long_idle_transaction": "依据 idle_in_transaction 排除该假设"})
rep = esc.check(st)
d2 = next(d for d in rep.dims if d.name == "D2")
print(f"      裁决 {rep.verdict}   {d2.detail}")
check("D2 不放行", d2.passed is False)
check("整体判负", rep.verdict != esc.ESCVerdict.SUFFICIENT.value, rep.verdict)
check("点名了这次排除没依据",
      "long_idle_transaction" in d2.detail and "无证据支撑" in d2.detail)
print(f"      对照 _supports(idle_in_transaction, 观测, long_idle_transaction)"
      f" = {_supports('idle_in_transaction', OBS, 'long_idle_transaction')}"
      f"  ← 同一条证据其实支持它")

print("\n[2] 只有显式 REFUTED_BY predicate 才能支撑排除")
st = mk({"lock_contention": "等待事件为空，无阻塞链，排除锁竞争"})
rep = esc.check(st)
d2 = next(d for d in rep.dims if d.name == "D2")
print(f"      {d2.detail}")
check("普通 supporting evidence 不能冒充反证",
      "lock_contention" in d2.detail.split("无证据支撑：")[-1],
      "session_wait_profile 没有 REFUTED_BY 关系")
st.note("agent", "lock_blocking_chain", "阻塞链 0 条（无锁等待）", "", [])
rep = esc.check(st)
d2 = next(d for d in rep.dims if d.name == "D2")
check("锁链 predicate 的 PATH 反证可以计入",
      "已排除 1 个" in d2.detail and
      "无证据支撑" not in d2.detail, d2.detail)

print("\n[3] 图与 _supports 必须对得上")
# 方向判据现在从图的 REFUTED_BY 边推导，手工表没了。该查的变成两件事：
# 图上声明能反证的组合，_supports 里有没有真写检查；以及 _supports 里
# 写了检查的类型，图上有没有对应的反证边（没有的话那段代码永远不会被
# 方向判断用到，是死逻辑）。
g = G.load()
causes = [n for n, d in g.nodes(data=True) if d.get("kind") == "RootCause"]
graph_refuters = {r["evidence"] for c in causes
                  for r in G.refuting_evidence(c)}
unknown_predicates = sorted(
    r["predicate_id"] for c in causes for r in G.refuting_evidence(c)
    if r["predicate_id"] not in registered_predicates())
check("图上声明的反证 predicate 都已注册",
      not unknown_predicates, unknown_predicates)

not_declared = sorted(graph_refuters - _CHECKED_TYPES)
check("图上声明能反证的证据，都在 _CHECKED_TYPES 里", not not_declared,
      not_declared)

dead = sorted(_CHECKED_TYPES - graph_refuters)
print(f"      _supports 有检查但图上无反证边（不参与方向判断）: {dead}")

pairs = [(r["evidence"], c) for c in causes for r in G.refuting_evidence(c)]
live = [(e, c) for e, c in pairs if _value_checked(e, c)]
check("推导出的方向判据非空", len(live) >= 10, f"{len(live)} 组")

print("\n[4] 未登记的组合不做方向判断")
check("session_wait_profile 不参与方向判断（图上无反证边）",
      not _value_checked("session_wait_profile", "lock_contention"))
check("idle_in_transaction 对长事务参与", _value_checked(
    "idle_in_transaction", "long_idle_transaction"))
check("idle_in_transaction 对别的根因不参与", not _value_checked(
    "idle_in_transaction", "missing_index"))
check("explain_seq_scan 不参与（存在性判断，图上无反证边）",
      not _value_checked("explain_seq_scan", "missing_index"))
check("row_estimate_deviation 对统计过期参与（这才是真判别特征）",
      _value_checked("row_estimate_deviation", "stale_statistics"))

print("\n[5] missing_index 只能由显式路径反证排除")
# 实测踩过：把 explain_seq_scan / index_existence 也拿来判方向，导致
# "声称锁竞争、排除缺索引"被判成无依据 —— 500 例里多拦了 45 个完全
# 正确的诊断，正确诊断放行率从 45% 掉到 22%。
#
# 根子是那两条在 _supports 里是存在性判断而非双向取值判断：
# index_existence 的注释自己写着"拿到了清单即算取证"；而统计过期场景下
# 计划本来就是 Seq Scan 且过滤大量行，那确实"像"缺索引。
st2 = EpisodeState(episode_id="rt5", scenario_id="lock_contention_eval_v1")
st2.symptoms = ["错误 5285"]
st2.claimed_fault_class = "lock_contention"
st2.note("a", "lock_blocking_chain", "阻塞链 3 条，最久等待 8 分钟", "", [])
st2.note("a", "session_wait_profile", "等待事件={'Lock'}", "", [])
st2.note("a", "explain_seq_scan",
         "4143ms, Seq Scan, Rows Removed by Filter=12,000,606, 用到索引=无",
         "", [])
st2.note("a", "index_existence",
         "orders 上的索引: ['idx_orders_user_status', 'orders_pkey']", "", [])
st2.set_verdict("lock_contention", Verdict.CONFIRMED,
                note="阻塞链 3 条，会话卡在 Lock 等待")
st2.set_verdict("missing_index", Verdict.REFUTED,
                note="覆盖索引存在，计划变慢另有原因")
rep = esc.check(st2)
d2 = next(d for d in rep.dims if d.name == "D2")
print(f"      {d2.detail}")
check("存在性证据不能支撑 missing_index 反证",
      "missing_index" in d2.detail.split("无证据支撑：")[-1],
      "explain_seq_scan / index_existence 不是 REFUTED_BY")
st2.note("a", "explain_plan",
         "2.4ms, Index Scan, Rows Removed by Filter=0, 用到索引=idx_orders_user_status",
         "", [])
rep = esc.check(st2)
d2 = next(d for d in rep.dims if d.name == "D2")
check("当前计划已走索引可以反证当前 missing-index 路径",
      "已排除 1 个" in d2.detail and "无证据支撑" not in d2.detail,
      d2.detail)

print()
print("=" * 66)
print("REFUTE DIRECTION: PASS" if not fails
      else f"REFUTE DIRECTION: FAIL {fails}")
sys.exit(1 if fails else 0)
