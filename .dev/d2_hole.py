"""D2 数的是声明还是依据。

合成策略把这个洞捅了出来：它一句固定文案把竞争假设全标 REFUTED、
一条判别证据都不取，却在 depth=all 时通过了 D2。原因是
set_hypothesis 只对 CONFIRMED 要求依据，而 D2 只看 verdict 字符串。

测试第一版写得不公平：拿来"无脑排除"的假设里，有几个恰好被主假设的
取证顺手覆盖到了（轨迹里 session_wait_profile="等待事件=无"，那确实
能排除锁竞争）。判它们无依据反而是错的。这一版只排除**证据与判别证据
都确实不在轨迹里**的假设，才真正测到规则本身。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import esc
from agent.episode_state import EpisodeState, Verdict
from knowledge.causal_graph import graph as G

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}   {detail}")
    if not cond:
        fails.append(name)


GATHERED = ["session_wait_profile", "idle_in_transaction"]
OBS = ("连接 92/100 (92.0%), 逼近上限=True, "
       "idle in transaction=86, 按角色={'app_user': 86}")


def mk(refuted, note="受控策略：按设定深度排除，未取任何判别证据"):
    st = EpisodeState(episode_id="hole", scenario_id="misleading_idle_txn_eval_v1")
    st.symptoms = ["错误 139"]
    st.claimed_fault_class = "long_idle_transaction"
    st.note("agent", "session_wait_profile", "等待事件=无", "", [])
    st.note("agent", "idle_in_transaction", OBS, "", [])
    st.set_verdict("long_idle_transaction", Verdict.CONFIRMED,
                   note="86 个会话挂在 idle in transaction，占住连接槽")
    for c in refuted:
        st.set_verdict(c, Verdict.REFUTED, note=note)
    return st


def backing(h):
    return (set(G.required_evidence(h)) | set(G.supporting_evidence(h))
            | {r["evidence"] for r in G.refuting_evidence(h)}
            | G.discriminators_of(h))


print("[0] 先确认测试选的假设确实无依据可言")
NAKED = [h for h in ("deadlock", "checkpoint_pressure", "work_mem_spill",
                     "stale_replication_slot")
         if h in G.load() and not (backing(h) & set(GATHERED))]
for h in NAKED:
    print(f"      {h:<26} 需要 {sorted(backing(h))[:3]}  轨迹里没有")
check("找得到确实无依据的排除对象", len(NAKED) >= 2, NAKED)

print("\n[1] 无脑排除这些假设：D2 不该算数")
st = mk(NAKED)
rep = esc.check(st)
d2 = next(d for d in rep.dims if d.name == "D2")
print(f"      {d2.detail}")
check("无依据的排除不计入排除率", d2.passed is False,
      "计入的话，把所有竞争假设标一遍 REFUTED 就能骗过这道闸")

print("\n[2] 对照：有判别证据支撑的排除，该算数")
st = mk(["connection_exhaustion"])
rep = esc.check(st)
d2 = next(d for d in rep.dims if d.name == "D2")
print(f"      {d2.detail}")
check("idle_in_transaction 支撑的排除计入",
      "connection_exhaustion" not in d2.detail.split("无证据支撑：")[-1]
      or "无证据支撑" not in d2.detail,
      "它是分开长事务与连接打满的判别证据，拿它排除是有依据的")

print("\n[3] 指令要指出差在哪，不能只说『尚未排除』")
# 上一版这里排除的是 NAKED 里那几个 —— 它们根本不在竞争假设集里，
# ESC 忽略它们是对的，针对性指令当然不会触发。要测这条，得排除一个
# **真正的竞争假设**且不给依据：去掉 session_wait_profile，
# lock_contention 就失去支撑了。
st = EpisodeState(episode_id="hole3",
                  scenario_id="misleading_idle_txn_eval_v1")
st.symptoms = ["错误 139"]
st.claimed_fault_class = "long_idle_transaction"
st.note("agent", "idle_in_transaction", OBS, "", [])
st.set_verdict("long_idle_transaction", Verdict.CONFIRMED,
               note="86 个会话挂在 idle in transaction，占住连接槽")
st.set_verdict("lock_contention", Verdict.REFUTED,
               note="受控策略：无依据地声称排除")
rep = esc.check(st)
d2 = next(d for d in rep.dims if d.name == "D2")
print(f"      {d2.detail}")
naked_hint = [d for d in rep.directives if "没有任何支撑证据" in d]
check("对无依据的排除给出针对性指令", bool(naked_hint),
      naked_hint[0][:78] if naked_hint else rep.directives[:2])

print("\n[4] 工具层：确认与排除对称，都要给依据")
from agent.state_machine import Phase, StateMachine
from agent.toolbox import Toolbox
st2 = EpisodeState(episode_id="hole2", scenario_id="x")
sm = StateMachine(st2)
sm.goto(Phase.OBSERVE); sm.goto(Phase.HYPOTHESIZE); sm.goto(Phase.INVESTIGATE)
tb = Toolbox(None, st2, sm)
for verdict, label in ((Verdict.CONFIRMED, "确认"), (Verdict.REFUTED, "排除")):
    try:
        tb.set_hypothesis("missing_index", verdict.value, "")
        check(f"{label}空依据被拒", False, "居然放行了")
    except ValueError as exc:
        check(f"{label}空依据被拒", True, str(exc)[:52])

print()
print("=" * 66)
print("D2 HOLE: PASS（排除也要有依据）" if not fails
      else f"D2 HOLE: FAIL {fails}")
sys.exit(1 if fails else 0)
