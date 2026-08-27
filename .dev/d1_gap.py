"""验证 D2 存在的那个理由，在误导性告警场景里是否真的成立。

D2 抓的是"直接证据齐备、结论仍错"。现有 44 个样本里这种案例是 0 个 ——
4 个误诊恰好都缺直接证据，全被 D1 拦了，所以 D2 一次都没派上用场。

误导性告警按设计应该能造出这种案例：agent 落进陷阱声称
connection_exhaustion 时，它的必需证据 connection_count **取值是真的
支持它的**（连接数确实逼近上限），D1 会放行。这时候只有 D2 拦得住。

如果这条成立，那"D2 没用"就是数据问题而不是设计问题，而补数据的方向
也就明确了：要的不是更多同类 episode，是更多这类场景。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import esc
from agent.episode_state import EpisodeState, Verdict

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}   {detail}")
    if not cond:
        fails.append(name)


# 误导性告警场景下的真实观测：连接确实满了（92/100），
# 其中 86 个处于 idle in transaction —— 这是 smoke 测里实测的数字。
CONN_OBS = ("连接 92/100 (92.0%), 逼近上限=True, "
            "idle in transaction=86, 按角色={'app_user': 86}")


def mk(claimed, refuted=(), with_idle=True):
    st = EpisodeState(episode_id="d1gap", scenario_id="misleading_idle_txn_eval_v1")
    st.symptoms = ["错误 139"]
    st.claimed_fault_class = claimed
    st.note("agent", "connection_count", CONN_OBS, "", [])
    st.note("agent", "session_wait_profile", "等待事件=无", "", [])
    if with_idle:
        st.note("agent", "idle_in_transaction", CONN_OBS, "", [])
    st.set_verdict(claimed, Verdict.CONFIRMED, note="连接数逼近上限")
    for r in refuted:
        st.set_verdict(r, Verdict.REFUTED, note="已排除")
    return st


print("[1] 落进陷阱：声称 connection_exhaustion，零排除")
st = mk("connection_exhaustion", refuted=(), with_idle=False)
rep = esc.check(st)
dims = {d.name: d for d in rep.dims}
print(f"      裁决 {rep.verdict}   "
      + " ".join(f"{d.name}{'+' if d.passed else '-'}" for d in rep.dims))
print(f"      D1: {dims['D1'].detail}")
print(f"      D2: {dims['D2'].detail}")
check("D1 为错误的根因放行了（这正是 D2 存在的理由）",
      dims["D1"].passed is True,
      "connection_count 取值确实支持它 —— 连接是真的满了")
check("D2 拦住了它", dims["D2"].passed is False)
check("整体判负", rep.verdict != esc.ESCVerdict.SUFFICIENT.value, rep.verdict)

print("\n[2] 同样落进陷阱，但把竞争假设排干净（D2 也会放行）")
st = mk("connection_exhaustion",
        refuted=("long_idle_transaction", "lock_contention",
                 "missing_index", "deadlock"), with_idle=False)
rep = esc.check(st)
dims = {d.name: d for d in rep.dims}
print(f"      裁决 {rep.verdict}   "
      + " ".join(f"{d.name}{'+' if d.passed else '-'}" for d in rep.dims))
check("D2 此时放行", dims["D2"].passed is True, dims["D2"].detail)
print("      → 说明 D2 拦的是「没做鉴别诊断」，不是「结论错」。"
      "结论错但排查扎实的情况，得靠 idle_in_transaction 那条判别证据。")

print("\n[3] 正解：声称 long_idle_transaction 并排掉陷阱")
st = mk("long_idle_transaction",
        refuted=("connection_exhaustion", "lock_contention"))
rep = esc.check(st)
dims = {d.name: d for d in rep.dims}
print(f"      裁决 {rep.verdict}   "
      + " ".join(f"{d.name}{'+' if d.passed else '-'}" for d in rep.dims))
check("正解放行", rep.verdict == esc.ESCVerdict.SUFFICIENT.value, rep.verdict)

print()
print("=" * 66)
if not fails:
    print("D1 GAP: PASS —— 存在 D1 放行而 D2 拦下的案例，D2 有独立价值")
else:
    print(f"D1 GAP: FAIL {fails}")
sys.exit(1 if fails else 0)
