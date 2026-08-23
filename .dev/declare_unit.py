"""离线单测：declare_root_cause 的证据门槛与 simulate_index 的平凡基线。

复现的是 lock_contention 那次误诊：子 agent 以置信 1.00 确认了锁竞争，
主 agent 却无依据地把 missing_index 也声明成根因。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState, Verdict
from agent.state_machine import Phase, StateMachine
from agent.toolbox import Toolbox

ok = True


class FakeObs:
    """不碰数据库，只测门槛逻辑。"""
    def __init__(self):
        self.trace = type("T", (), {"record": lambda *a, **k: "ref"})()


def mk():
    st = EpisodeState(episode_id="unit_two", scenario_id="x")
    st.symptoms = ["p99 上升 40x"]
    st.budget["max_steps"] = 40
    sm = StateMachine(st, allow_repair=True)
    sm.goto(Phase.OBSERVE); sm.goto(Phase.HYPOTHESIZE)
    sm.goto(Phase.INVESTIGATE); sm.goto(Phase.DIAGNOSE)
    return st, Toolbox(FakeObs(), st, sm)


print("=" * 74)
print("[1] 无证据声明根因 -> 应被拒（复现 lock_contention 误诊）")
st, tb = mk()
st.set_verdict("lock_contention", Verdict.CONFIRMED,
               note="阻塞链非空16条，会话17906持有行锁18.9秒，6个UPDATE被阻塞")
try:
    tb.declare_root_cause("missing_index", "缺索引")
    print("  FAIL  竟然允许了")
    ok = False
except ValueError as e:
    print(f"  PASS  {str(e)[:110]}")

print("\n[2] 证据齐备时声明 -> 应放行")
st, tb = mk()
st.note("a", "explain_seq_scan", "Seq Scan, Rows Removed by Filter=12,000,606")
st.note("a", "index_existence", "orders 上的索引: ['orders_pkey']")
try:
    r = tb.declare_root_cause(
        "missing_index",
        "orders 缺少覆盖 (user_id, status) 的复合索引，导致全表扫 1200 万行")
    print(f"  PASS  {r}")
    print(f"        台账 note 已自动填入: "
          f"{st.ledger['missing_index'].note[:60]}")
    ok &= bool(st.ledger["missing_index"].note.strip())
except ValueError as e:
    print(f"  FAIL  被误拒: {e}")
    ok = False

print("\n[3] 已有带依据的确认时，短理由改声明 -> 应被拒")
st, tb = mk()
st.note("a", "explain_seq_scan", "Seq Scan, Rows Removed=12,000,606")
st.note("a", "index_existence", "索引: ['orders_pkey']")
st.set_verdict("lock_contention", Verdict.CONFIRMED,
               note="阻塞链非空16条，多个会话卡在 Lock:transactionid")
try:
    tb.declare_root_cause("missing_index", "缺索引")
    print("  FAIL  竟然允许了")
    ok = False
except ValueError as e:
    print(f"  PASS  {str(e)[:110]}")

print("\n[4] 平凡基线上的反事实结果不算数")
from agent import esc
cases = [
    ("hypopg: cost 1 -> 0 (降 87.5%), 优化器会采用=False；"
     "原查询成本仅 1.0，本来就很快，加索引的收益没有意义；"
     "该结果不足以支持'缺索引'的判断", False, "平凡基线"),
    ("hypopg: cost 180,975 -> 52 (降 100.0%), 优化器会采用=True", True, "真实收益"),
]
for obs, want, label in cases:
    got = esc._supports("counterfactual_index", obs, "missing_index")
    good = got == want
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label:<10} -> D5 认可={got}")

print("\n" + "=" * 74)
print("UNIT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
