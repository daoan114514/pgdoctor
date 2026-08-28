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
from agent.esc import _VALUE_CHECKED, _supports, _value_checked
from agent.episode_state import EpisodeState, Verdict
from knowledge.causal_graph import graph as G

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

print("\n[2] 拿真能反证的证据去排除 —— 不能误伤")
st = mk({"lock_contention": "等待事件为空，无阻塞链，排除锁竞争"})
rep = esc.check(st)
d2 = next(d for d in rep.dims if d.name == "D2")
print(f"      {d2.detail}")
check("这次排除计入排除率",
      "已排除 1 个" in d2.detail and "lock_contention" not in
      d2.detail.split("无证据支撑：")[-1],
      "session_wait_profile=等待事件无，是正当的反证")

print("\n[3] _VALUE_CHECKED 与 _supports 必须同步")
# _supports 里每个按根因分支的证据类型，都得在表里登记；否则方向检查
# 会漏掉它，或者把"没查"当成"支持"。这张表和函数分居两处，必然会漂移。
src = (Path(__file__).resolve().parent.parent / "agent/esc.py").read_text(
    encoding="utf-8")
body = src.split("def _supports")[1].split("def _collected")[0]
import re
# 按分支切开再比对。第一版用一条跨行正则，结果匹配到了**下一个**分支的
# root_cause 条件（explain_seq_scan 被配成 stale_statistics），检查照样
# 通过 —— 因为表里那项是 None，短路了。又是一个"过了但理由是错的"测试。
segs = re.split(r'\n    if evidence_type == ', body)
declared, conditional = set(), set()
for seg in segs[1:]:
    m = re.match(r'"([a-z_]+)"', seg)
    if not m:
        continue
    ev = m.group(1)
    declared.add(ev)
    rc = re.search(r'if root_cause == "([a-z_]+)"', seg)
    if rc:
        conditional.add((ev, rc.group(1)))
# 有意不登记的：前两条 _supports 里压根没做取值检查；后两条做的是
# 存在性判断而非双向取值判断，拿来判方向会误伤正当的排除。
EXEMPT = {"dead_tuple_ratio", "connection_count",
          "explain_seq_scan", "index_existence"}
missing = declared - set(_VALUE_CHECKED) - EXEMPT
check("_supports 里的证据类型都在表里（或明确豁免）", not missing,
      f"漏登记: {sorted(missing)}" if missing else
      f"{len(_VALUE_CHECKED)} 项已登记，2 项豁免（无取值检查）")

for ev, rc in conditional:
    if ev in _VALUE_CHECKED:
        want = _VALUE_CHECKED[ev]
        check(f"  {ev} 的登记根因与代码一致", want == rc,
              f"表里 {want}，代码里 {rc}")

print("\n[4] 未登记的组合不做方向判断")
check("session_wait_profile 未登记", not _value_checked(
    "session_wait_profile", "lock_contention"))
check("idle_in_transaction 对长事务已登记", _value_checked(
    "idle_in_transaction", "long_idle_transaction"))
check("idle_in_transaction 对别的根因不登记", not _value_checked(
    "idle_in_transaction", "missing_index"))

print("\n[5] 回归保护：排除 missing_index 不能被方向检查误伤")
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
check("排除 missing_index 计入排除率",
      "missing_index" not in d2.detail.split("无证据支撑：")[-1]
      or "无证据支撑" not in d2.detail,
      "explain_seq_scan / index_existence 是存在性判断，不能拿来判方向")

print()
print("=" * 66)
print("REFUTE DIRECTION: PASS" if not fails
      else f"REFUTE DIRECTION: FAIL {fails}")
sys.exit(1 if fails else 0)
