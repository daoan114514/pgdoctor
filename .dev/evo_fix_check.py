"""L1-L4 修复验收。六个缺陷各一组断言，全程在临时目录里跑，不碰真实学习产物。"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agent.episode_state import EpisodeState, RemediationAttempt
from knowledge import evolution as ev
from knowledge.causal_graph import graph as G

ok = True


def check(label, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{(' | ' + extra) if extra else ''}")


class Score:
    def __init__(self, d, o, s):
        self.diagnosis, self.outcome, self.safe_pass = d, o, s


class Sandbox:
    """把 evolution 的落盘目录换到临时位置。"""
    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.orig = ev.LEARNED
        ev.LEARNED = self.tmp
        return self.tmp

    def __exit__(self, *a):
        ev.LEARNED = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)


def mk(rc, symptoms):
    st = EpisodeState(episode_id="t", scenario_id="s")
    st.symptoms = symptoms
    st.claimed_fault_class = rc
    st.budget["steps"] = 12
    return st


print("\n[1] L3 症状键归一 —— 同类事故必须收敛到同一个键")
with Sandbox():
    ev.learn_truth("stale_statistics", "lock_contention",
                   ["错误 5086", "p99 上升 40x"])
    ev.learn_truth("stale_statistics", "lock_contention",
                   ["错误 7213", "p99 上升 58x"])
    keys = sorted(ev.load_delta().likelihood_adj)
    check("两次同类事故只产生 2 个键（症状种类数），不是 4 个",
          len(keys) == 2, f"keys={keys}")
    check("键的症状侧是图节点 id",
          all(k.split("->", 1)[1] in {"latency_p99_up", "throughput_down",
                                      "cpu_saturated", "queries_blocked",
                                      "disk_growing", "conn_near_limit"}
              for k in keys))
    check("同一个键会累加而不是各自为政",
          ev.load_delta().likelihood_adj[keys[0]] > ev.LR,
          f"{keys[0]}={ev.load_delta().likelihood_adj[keys[0]]}")


print("\n[2] L3 likelihood_adj 真的被读了（原来写完就扔）")
with Sandbox():
    base = {c["root_cause"]: c["score"]
            for c in G.candidate_causes(["latency_p99_up"], top_k=9)}
    d = ev.load_delta()
    d.likelihood_adj["lock_contention->latency_p99_up"] = 0.2
    ev.save_delta(d)
    after = {c["root_cause"]: c["score"]
             for c in G.candidate_causes(["latency_p99_up"], top_k=9)}
    check("调高一条边的权重，该根因排序分数上升",
          after.get("lock_contention", 0) > base.get("lock_contention", 0),
          f"{base.get('lock_contention')} -> {after.get('lock_contention')}")
    check("没被调整的根因分数不变",
          abs(after.get("missing_index", 0) - base.get("missing_index", 0)) < 1e-9)


print("\n[3] L3 误诊只扣一次（原来 learn_from_episode + learn_truth 各扣一次）")
with Sandbox():
    st = mk("stale_statistics", ["p99 上升 40x"])
    ev.learn(st, Score(False, False, True), [], st.symptoms,
             truth="lock_contention")
    adj = ev.load_delta().prior_adj
    got = adj.get("stale_statistics", 0.0)
    check("被错认的根因扣罚恰为 -LR",
          abs(got + ev.LR) < 1e-9, f"实得 {got}，期望 {-ev.LR}")
    check("真凶升 +LR，与扣罚对称",
          abs(adj.get("lock_contention", 0.0) - ev.LR) < 1e-9,
          f"实得 {adj.get('lock_contention')}")


print("\n[4] L3 不可归因的修复失败不压先验")
with Sandbox():
    st = mk("missing_index", ["p99 上升 40x"])
    st.record_attempt(RemediationAttempt(
        root_cause="missing_index", sql="CREATE INDEX i ON orders(x)",
        predicted={}, actual={}, verdict="FAILED_NO_IMPROVEMENT",
        rolled_back=True, inference="另一个故障仍在",
        counts_against_root_cause=False))
    ev.learn_from_episode(st, Score(True, False, True), st.symptoms)
    check("诊断命中 +LR，不可归因的失败不再扣",
          abs(ev.load_delta().prior_adj.get("missing_index", 0) - ev.LR) < 1e-9,
          f"实得 {ev.load_delta().prior_adj.get('missing_index')}")

with Sandbox():
    st = mk("missing_index", ["p99 上升 40x"])
    st.record_attempt(RemediationAttempt(
        root_cause="missing_index", sql="X", predicted={}, actual={},
        verdict="FAILED_NO_IMPROVEMENT", rolled_back=True,
        inference="症状已被完整解释，确属误判"))
    ev.learn_from_episode(st, Score(True, False, True), st.symptoms)
    check("对照：可归因的失败仍然扣（保护不能太宽）",
          ev.load_delta().prior_adj.get("missing_index", 0) < ev.LR - 1e-9,
          f"实得 {ev.load_delta().prior_adj.get('missing_index')}")


print("\n[5] L2 median_steps 是真中位数")
with Sandbox():
    for s in (10, 20, 30, 40):
        st = mk("missing_index", ["p99 上升 40x"])
        st.budget["steps"] = s
        ev.sediment_playbook(st, Score(True, True, True), ["CREATE INDEX i"])
    pb = ev.load_playbooks()["missing_index"]
    check("喂入 [10,20,30,40] 得 25（旧实现是滑动平均，得 31）",
          pb.median_steps == 25, f"实得 {pb.median_steps}")
    check("样本被保留下来", len(pb.steps_samples) == 4,
          f"samples={pb.steps_samples}")


print("\n[6] 历史脏键在载入时自愈")
with Sandbox() as tmp:
    (tmp / "graph_delta.yaml").write_text(
        "likelihood_adj:\n"
        "  lock_contention->错误 5086: 0.05\n"
        "  lock_contention->latency_p99_up: 0.1\n"
        "prior_adj: {}\nobserved: {}\nupdated_at: 0\n",
        encoding="utf-8")
    la = ev.load_delta().likelihood_adj
    check("人话串脏键被丢弃", "lock_contention->错误 5086" not in la)
    check("合法键保留", la.get("lock_contention->latency_p99_up") == 0.1,
          f"la={la}")


print("\n[7] audit_l14.py 能跑完（原来 IsADirectoryError 崩在 L4）")
r = subprocess.run([sys.executable, ".dev/audit_l14.py"],
                   capture_output=True, text=True, cwd=str(REPO), timeout=120)
check("退出码为 0", r.returncode == 0, f"rc={r.returncode}")
check("没有 traceback", "Traceback" not in r.stderr,
      r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "")
# 断言结构而不是某个检查项的名字 —— 之前绑死在 "查询库存储" 这个
# 字符串上，审计项一改名测试就假失败。L4 之后还有段落，能打印出来
# 就说明 L4 整段跑完了。
check("L4 段落完整跑完（原来崩在这里）",
      "L4" in r.stdout and "当前案例库实际内容" in r.stdout)

print(f"\nEVO FIX: {'PASS' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
