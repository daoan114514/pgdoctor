"""把"鉴别诊断做到什么程度"变成一个可调的轴。

为什么需要它：ESC 的 D2 一直没有可测的价值，不是因为它没用，是因为
现有数据里**从来没出现过它针对的那种失败**。44 个 episode、4 个误诊，
全都同时缺直接证据，于是 D1 先拦了，D2 永远轮不到。

D2 针对的是"直接证据齐备、却完全没做鉴别诊断"。要测它就得有这种样本，
而 LLM 策略不会主动这么干（提示词和因果图都在推着它做鉴别）。所以造一个
确定性策略，把取证深度和鉴别深度**解耦**：

  取证照做（保证 D1 过） × 只排除 k 个竞争假设（把 D2 压到任意档位）

和既有 ESC 消融用"偷懒策略"是同一套方法论：用一个受控的坏策略去量
防线的价值。证据与 KPI 仍来自真实注入的故障，只有策略是合成的。

零模型调用，所以能跑批。
"""
from __future__ import annotations

from agent.episode_state import EpisodeState, Verdict
from agent.policy import Policy
from agent.state_machine import Phase
from agent.toolbox import Toolbox


class DifferentialDepthPolicy(Policy):
    """按指定深度做鉴别诊断，其余照常。

    target      要声称的根因（可以故意是错的，用来造"落进陷阱"的样本）
    refute_k    排除几个竞争假设。None 表示全排
    """

    def __init__(self, target: str, refute_k: int | None = None):
        self.target = target
        self.refute_k = refute_k
        self.name = f"depth[{target},k={refute_k}]"

    # 证据类型 -> 怎么取。照着因果图的 obtained_by 调，不硬编码一份。
    def _gather(self, tb: Toolbox, st: EpisodeState, ctx: dict,
                extra: set[str] | None = None) -> None:
        from knowledge.causal_graph import graph as G

        g = G.load()
        need = set(G.required_evidence(self.target)) | set(
            G.supporting_evidence(self.target)) | set(extra or ())
        hot, uid = ctx.get("hot_query"), {"uid": 4242}
        called = set()
        for ev in sorted(need):
            by = g.nodes.get(ev, {}).get("obtained_by")
            if not by or by in called:
                continue
            called.add(by)
            try:
                if by == "explain_query" and hot:
                    tb.explain_query(hot, uid)
                elif by == "get_indexes":
                    tb.get_indexes("orders")
                elif by == "get_table_stats":
                    tb.get_table_stats("orders")
                elif by == "get_blocking_chain":
                    tb.get_blocking_chain()
                elif by == "get_active_sessions":
                    tb.get_active_sessions()
                elif by == "get_connection_stats":
                    tb.get_connection_stats()
                elif by == "get_top_queries":
                    tb.get_top_queries(5)
                elif by == "get_vacuum_horizon":
                    tb.get_vacuum_horizon()
                elif by == "get_database_stats":
                    tb.get_database_stats()
            except Exception:
                # 取不到就算了 —— 真实环境里本来就会有取不到的证据，
                # 强行兜住反而让 D1 的判定失真
                pass

    def run_phase(self, phase: Phase, tb: Toolbox, st: EpisodeState,
                  ctx: dict) -> Phase:
        if phase is Phase.MONITOR:
            sess = tb.get_active_sessions()
            st.symptoms = ctx.get("symptoms", [])
            waits = {s["wait_event"] for s in sess if s["wait_event"]}
            st.note("depth", "monitor_snapshot",
                    f"{len(sess)} 个异常会话，等待事件={waits or '无'}")
            return Phase.OBSERVE

        if phase is Phase.OBSERVE:
            top = tb.get_top_queries(5)
            if top:
                st.note("depth", "slow_query",
                        f"最耗时查询 mean={top[0]['mean_ms']}ms")
            return Phase.HYPOTHESIZE

        if phase is Phase.HYPOTHESIZE:
            st.ensure_hypotheses(ctx.get("candidates") or [self.target])
            return Phase.INVESTIGATE

        if phase is Phase.INVESTIGATE:
            from knowledge.causal_graph import graph as G

            competitors = [h for h in st.ledger if h != self.target]
            k = len(competitors) if self.refute_k is None else self.refute_k
            picked = competitors[:max(0, k)]

            # 要排除谁，就先去取能排除它的证据。D2 收紧之后只数有依据的
            # 排除，光标 REFUTED 不算 —— 这也正是应该的：排除和确认一样
            # 要有依据。所以这里把被排除者的判别证据一并取回来。
            extra: set[str] = set()
            for c in picked:
                extra |= (set(G.required_evidence(c))
                          | G.discriminators_of(c)
                          | {r["evidence"] for r in G.refuting_evidence(c)})
            self._gather(tb, st, ctx, extra=extra)

            got = {e["evidence_type"] for e in st.scratchpad}
            for c in picked:
                rel = (set(G.required_evidence(c)) | G.discriminators_of(c)
                       | {r["evidence"] for r in G.refuting_evidence(c)})
                hit = sorted(rel & got)
                if not hit:
                    # 取不到判别证据就别硬排 —— 硬排会被 D2 判成"声称排除
                    # 但无依据"，那反映的是策略在耍赖，不是鉴别深度不够
                    continue
                tb.set_hypothesis(
                    c, Verdict.REFUTED.value,
                    f"受控策略：依据 {hit[:2]} 排除该假设")
            return Phase.DIAGNOSE

        if phase is Phase.DIAGNOSE:
            try:
                tb.declare_root_cause(self.target, ctx.get(
                    "claim_note", "受控策略：按设定目标声称根因"))
            except Exception as exc:
                st.note("depth", "declare_blocked", str(exc)[:180])
            return Phase.REPORT

        return Phase.REPORT
