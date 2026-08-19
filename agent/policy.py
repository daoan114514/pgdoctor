"""策略层：在给定阶段里决定做什么。

刻意把"决策"从"流程"里剥出来，于是：
  - 状态机与工具面完全不依赖模型，没有 API 认证也能端到端测试
  - 脚本化策略成为一条诚实的基线 —— 后面接上 LLM 时，
    "模型到底带来多少增益"是可测的，而不是全凭感觉

ScriptedPolicy 把人的鉴别诊断逻辑写死：先取证，再逐条排除，最后用
反事实模拟验证。它会赢得很轻松，因为答案本来就编码在它的 if 分支里；
它的价值在于验证管道，以及给 LLM 提供对照，而不是它自己有多聪明。
"""
from __future__ import annotations

import abc

from agent.episode_state import EpisodeState, Verdict
from agent.state_machine import Phase
from agent.toolbox import Toolbox


class Policy(abc.ABC):
    name: str = "policy"

    @abc.abstractmethod
    def run_phase(self, phase: Phase, tb: Toolbox, st: EpisodeState,
                  ctx: dict) -> Phase:
        """在当前阶段行动，返回下一个目标阶段。"""


class ScriptedPolicy(Policy):
    """确定性基线。领域知识由人写死，不涉及任何模型调用。"""

    name = "scripted"

    # 该症状组合下的候选根因。W6 起这个集合改由故障因果图多跳遍历给出，
    # 以保证覆盖率，而不是靠谁凭印象列举。
    CANDIDATES = ["missing_index", "stale_statistics", "lock_contention"]

    def run_phase(self, phase: Phase, tb: Toolbox, st: EpisodeState,
                  ctx: dict) -> Phase:
        hot = ctx["hot_query"]
        uid = {"uid": 4242}

        if phase is Phase.MONITOR:
            sess = tb.get_active_sessions()
            st.symptoms = ctx.get("symptoms", [])
            waits = {s["wait_event"] for s in sess if s["wait_event"]}
            st.note("scripted", "monitor_snapshot",
                    f"{len(sess)} 个异常会话，等待事件={waits or '无'}")
            return Phase.OBSERVE

        if phase is Phase.OBSERVE:
            top = tb.get_top_queries(5)
            if top:
                st.note("scripted", "slow_query",
                        f"最耗时查询 mean={top[0]['mean_ms']}ms")
            return Phase.HYPOTHESIZE

        if phase is Phase.HYPOTHESIZE:
            st.ensure_hypotheses(self.CANDIDATES)
            return Phase.INVESTIGATE

        if phase is Phase.INVESTIGATE:
            # H1 缺索引：EXPLAIN 见全表扫 + 索引里确实没有可用的
            plan = tb.explain_query(hot, uid)
            seq = any("Seq Scan" in s for s in plan["scan_types"])
            removed = plan["rows_removed_by_filter"]
            idx = tb.get_indexes("orders")
            names = [i["name"] for i in idx]
            covering = [n for n in names if "user_status" in n or "status_user" in n]
            if seq and removed > 1_000_000 and not covering:
                tb.set_hypothesis(
                    "missing_index", Verdict.CONFIRMED.value,
                    f"Seq Scan 过滤掉 {removed:,} 行，且 orders 上无覆盖该谓词的索引")
            else:
                tb.set_hypothesis("missing_index", Verdict.REFUTED.value,
                                  f"计划={plan['scan_types']}, 索引={names}")

            # H2 统计信息过期：last_analyze 新鲜就能干净排除
            stats = tb.get_table_stats("orders")
            if stats["last_analyze"]:
                tb.set_hypothesis(
                    "stale_statistics", Verdict.REFUTED.value,
                    f"last_analyze={stats['last_analyze'][:19]}，统计信息新鲜")
            else:
                tb.set_hypothesis("stale_statistics", Verdict.INCONCLUSIVE.value,
                                  "拿不到 last_analyze")

            # H3 锁竞争：无阻塞链即排除
            chain = tb.get_blocking_chain()
            if not chain:
                tb.set_hypothesis("lock_contention", Verdict.REFUTED.value,
                                  "pg_locks 无阻塞链，会话也未等锁")
            else:
                tb.set_hypothesis("lock_contention", Verdict.CONFIRMED.value,
                                  f"存在 {len(chain)} 条阻塞链")
            return Phase.DIAGNOSE

        if phase is Phase.DIAGNOSE:
            confirmed = st.confirmed()
            if len(confirmed) != 1:
                st.outcome_note = f"未能收敛到唯一根因: {confirmed}"
                return Phase.ESCALATE
            rc = confirmed[0]
            if rc == "missing_index":
                # 反事实：动手之前先证明这个判断成立
                sim = tb.simulate_index(
                    "CREATE INDEX ON orders(user_id, status)", hot, uid)
                if not sim["would_be_used"]:
                    tb.set_hypothesis("missing_index", Verdict.REFUTED.value,
                                      "hypopg 模拟显示优化器不会采用该索引")
                    st.outcome_note = "反事实模拟证伪了缺索引假设"
                    return Phase.ESCALATE
                tb.declare_root_cause(
                    "missing_index", "orders(user_id, status) 上缺少可用索引")
            else:
                tb.declare_root_cause(rc, f"确认的根因: {rc}")
            return Phase.REPORT

        raise RuntimeError(f"ScriptedPolicy 未实现阶段 {phase}")
