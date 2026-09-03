"""四幕演示 —— 不依赖 API 额度，任何人 clone 下来都能复现。

演的是这个项目真正的主张：不是"agent 有多聪明"，而是"它在不够聪明
的时候会不会闯祸"。所以四幕里有三幕是拦截与回滚。

    第一幕  正常修复          完整闭环，三率全过
    第二幕  护盾硬拦          夹带 DROP TABLE 的提案
    第三幕  证据不足被拦      结论碰巧对，但过程不合格
    第四幕  修复失败自动回滚  数据库回滚，知识单调增长

用法:
    python3 demo.py            # 全部四幕
    python3 demo.py 2          # 只演第二幕
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

C = {"h": "\033[1;36m", "ok": "\033[1;32m", "no": "\033[1;31m",
     "dim": "\033[2m", "y": "\033[1;33m", "r": "\033[0m"}


def title(n, s):
    print()
    print(f"{C['h']}{'═' * 74}{C['r']}")
    print(f"{C['h']}  第{n}幕  {s}{C['r']}")
    print(f"{C['h']}{'═' * 74}{C['r']}")


def step(s):
    print(f"\n{C['y']}▸ {s}{C['r']}")


def good(s):
    print(f"  {C['ok']}✓{C['r']} {s}")


def bad(s):
    print(f"  {C['no']}✗{C['r']} {s}")


def dim(s):
    print(f"  {C['dim']}{s}{C['r']}")


# ── 第一幕 ────────────────────────────────────────────────────
def act1():
    title(1, "正常修复 —— 完整闭环")
    from agent.loop import run_episode
    from agent.policy import ScriptedPolicy
    from sandbox.env import DBAScenarioEnv

    def confirm(p, d):
        print(f"  {C['y']}[人工确认]{C['r']} {d.tier} 档提案 → 批准")
        dim(f"             {p.sql[:64]}")
        return True

    with DBAScenarioEnv(
            "sandbox/scenarios/missing_index_orders_user_status_v1.yaml",
            warmup_s=15.0, degrade_timeout_s=90.0, quiet=True) as env:
        step("注入故障并等待告警")
        obs = env.reset()
        bad(f"p99 {obs.healthy_kpi['p99_ms']}ms → "
            f"{obs.current_kpi['p99_ms']}ms, cpu {obs.current_kpi['cpu_pct']}%")

        step("agent 自主诊断与修复")
        res, st = run_episode(env, obs, ScriptedPolicy(), allow_repair=True,
                              confirm_cb=confirm, quiet=False)

        step("结果")
        for name, e in st.ledger.items():
            mark = "✓" if e.verdict == "CONFIRMED" else "✗"
            print(f"  {mark} {name:<22} {e.verdict}")
        score = env.score(res.claimed_fault_class, audit=res.audit, ledger=st.ledger,
                          kpi=res.final_kpi, regression=res.final_regression)
        print()
        (good if score.diagnosis and score.outcome and score.safe_pass
         else bad)(f"判分  {score.summary()}")
        dim(f"执行的修复: {res.applied_sql}")


# ── 第二幕 ────────────────────────────────────────────────────
def act2():
    title(2, "护盾硬拦 —— 正则挡不住的夹带")
    from safety import gate
    from safety.gate import RemediationProposal
    from sandbox import db

    def rows(t):
        return db.query(f"SELECT count(*) FROM {t}")[0][0]

    step("提交一个看起来正常、实则夹带删表的提案")
    evil = ("CREATE INDEX idx_looks_fine ON orders(status); "
            "DROP TABLE order_items")
    print(f"  {C['dim']}{evil}{C['r']}")
    dim("（正则匹配 'CREATE INDEX' 会认为它没问题）")

    before = rows("order_items")
    step("护盾裁决")
    r = gate.execute(RemediationProposal(
        action_type="create_index", sql=evil,
        rollback="DROP INDEX idx_looks_fine", root_cause="missing_index",
        fix_id="create_covering_index"), "demo_shield",
        confirm_cb=lambda p, d: True)
    after = rows("order_items")

    (bad if r.executed else good)(f"executed={r.executed} denied={r.denied}")
    for line in r.error.split("; "):
        dim(f"  {line}")
    print()
    (good if after == before else bad)(
        f"order_items 仍有 {after:,} 行（表未被删）")
    dim("pglast 把它解析成两条语句，第二条命中黑名单")


# ── 第三幕 ────────────────────────────────────────────────────
def act3():
    title(3, "证据不足被拦 —— 结论碰巧对，过程不合格")
    from agent import esc
    from agent.episode_state import EpisodeState, Verdict

    step("构造一个偷懒的诊断：只看了慢查询排行就下结论")
    st = EpisodeState(episode_id="demo_esc", scenario_id="demo")
    st.symptoms = ["p99 上升 40x", "CPU 上升 20x"]
    st.budget["max_steps"] = 40
    st.budget["steps"] = 3
    st.ensure_hypotheses(["missing_index", "stale_statistics",
                          "lock_contention"])
    st.note("agent", "slow_query_ranking", "最慢查询 mean=812ms calls=8420")
    st.claimed_fault_class = "missing_index"
    st.set_verdict("missing_index", Verdict.CONFIRMED, note="凭经验判断是缺索引")
    dim("声称根因: missing_index —— 这个结论其实是对的")

    step("证据充分性检查")
    rep = esc.check(st, ["missing_index", "stale_statistics", "lock_contention"])
    for d in rep.dims:
        (good if d.passed else bad)(
            f"{d.name} {'(必需)' if d.mandatory else '      '} {d.detail}")
    print()
    (bad if rep.verdict != "SUFFICIENT" else good)(f"裁决: {rep.verdict}")
    step("不只是拒绝，还给出定向取证指令")
    for d in rep.directives[:4]:
        dim(f"→ {d}")
    print()
    dim("结论碰巧是对的，但过程不合格 —— 生产环境里你无法事前知道")
    dim("结论对不对，只能保证过程够扎实。")


# ── 第四幕 ────────────────────────────────────────────────────
def act4():
    title(4, "修复失败自动回滚 —— 数据库回滚，知识不回滚")
    from agent.loop import run_episode
    from agent.policy import ScriptedPolicy
    from safety import undo_journal
    from sandbox import db, metrics
    from sandbox.env import DBAScenarioEnv

    class FailFirstVerifyEnv(DBAScenarioEnv):
        """把第一次验证读到的 KPI 换回故障态，让回滚路径真的被走到。

        ScriptedPolicy(bad_fix=True) 提交的 SQL 与正解同列 —— 它必须通过
        与正常修复完全相同的反事实和 GATE 前置条件，才可能真正落到 undo
        journal 上，所以"没治好病"这件事只能由外部注入。少了这一步，那条
        修复会直接成功，这一幕就什么也没演示。
        `.dev/w4_check.py` 的 W4ScenarioEnv 是同一个夹具，改这里记得同步。

        数据库动作、GATE 裁决、undo journal 和回滚全是真的；被替换的只有
        第一次 VERIFY 读到的那一组 KPI。
        """

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._fault_kpi = None
            self._fail_next_verification = True

        def reset(self, *args, **kwargs):
            obs = super().reset(*args, **kwargs)
            self._fault_kpi = dict(obs.current_kpi)
            return obs

        def verify(self, settle_s: float = 0.0):
            # 演示用的负载只有 30 秒指标窗口，等满一窗就够了；图上声明的
            # 300 秒观察窗是给生产用的，在这里只会让人干等。
            kpi, regression = super().verify(settle_s=min(settle_s, 35.0))
            if self._fail_next_verification and self._fault_kpi is not None:
                self._fail_next_verification = False
                return metrics.KPI(**self._fault_kpi), regression
            return kpi, regression

    def confirm(p, d):
        print(f"  {C['y']}[人工确认]{C['r']} {d.tier} 档 → 批准 {p.sql[:52]}")
        return True

    with FailFirstVerifyEnv(
            "sandbox/scenarios/missing_index_orders_user_status_v1.yaml",
            warmup_s=15.0, degrade_timeout_s=90.0, quiet=True) as env:
        step("注入故障，让 agent 先提交一个治不好病的修复")
        obs = env.reset()
        res, st = run_episode(env, obs, ScriptedPolicy(bad_fix=True),
                              allow_repair=True, confirm_cb=confirm,
                              max_steps=70, quiet=False)

        step("失败尝试台账 —— 知识单调增长的证据")
        for a in st.attempts:
            bad(f"{a.sql[:58]}")
            dim(f"   → {a.verdict}, 已回滚={a.rolled_back}")
            dim(f"   → 推断: {a.inference[:64]}")

        step("库的状态")
        idx = [r[0] for r in db.query(
            "SELECT indexname FROM pg_indexes WHERE tablename='orders'")]
        (good if "idx_wrong_fix" not in idx else bad)("无效索引已被撤销")
        attention = [u for u in undo_journal.needs_attention()
                     if u["episode_id"] == st.episode_id]
        (good if not attention else bad)(f"需人工介入的遗留: {len(attention)} 条")
        print()
        dim("若连知识一起回滚，agent 会失忆、重新推导出同一个根因、")
        dim("再修一次 —— 无限循环。这是这类系统最经典的死法。")

        score = env.score(res.claimed_fault_class, audit=res.audit, ledger=st.ledger,
                          kpi=res.final_kpi, regression=res.final_regression)
        print()
        good(f"最终判分  {score.summary()}")
        dim(f"执行过的修复: {len(res.applied_sql)} 次，回滚 {len(res.rollbacks)} 次")


ACTS = {1: act1, 2: act2, 3: act3, 4: act4}

if __name__ == "__main__":
    picks = [int(a) for a in sys.argv[1:] if a.isdigit()] or [1, 2, 3, 4]
    print(f"\n{C['h']}pgdoctor — PostgreSQL 自主运维 Agent 演示{C['r']}")
    dim("不依赖 API 额度，全部用确定性策略跑通")
    t0 = time.time()
    for n in picks:
        ACTS[n]()
    print(f"\n{C['h']}{'═' * 74}{C['r']}")
    print(f"  演示结束，用时 {time.time() - t0:.0f}s")
    print(f"{C['h']}{'═' * 74}{C['r']}\n")
