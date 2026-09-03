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

from agent.episode_state import EpisodeState
from agent.policy import Policy
from agent.state_machine import Phase
from agent.toolbox import Toolbox


class DifferentialDepthPolicy(Policy):
    """按指定深度做鉴别诊断，其余照常。

    target      要优先取证的根因
    refute_k    额外调查几个竞争路径的根节点。None 表示全部调查
    """

    def __init__(self, target: str, refute_k: int | None = None,
                 seed: int | None = None):
        self.target = target
        self.refute_k = refute_k
        # 排除哪 k 个是随机选的，不是固定取前 k 个。不同竞争假设的判别
        # 证据难易差很多（lock_blocking_chain 是瞬时的，connection_count
        # 随时可取），固定顺序会把这种差异系统性地藏起来。
        self.seed = seed
        self.name = f"depth[{target},k={refute_k},s={seed}]"

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
                elif by == "get_physical_bloat":
                    tb.get_physical_bloat("orders")
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
            # 建立累计统计的事故窗口基线；症状由 loop 从 Observation 持久化。
            try:
                tb.get_database_stats()
            except Exception:
                pass
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
            return Phase.INVESTIGATE

        if phase is Phase.INVESTIGATE:
            from knowledge.causal_graph import graph as G

            explanation = st.explanation_graph
            competitors = list(dict.fromkeys(
                path.root_node_id for path in
                (explanation.candidate_paths if explanation else [])
                if path.root_node_id != self.target))
            k = len(competitors) if self.refute_k is None else self.refute_k
            k = max(0, min(k, len(competitors)))
            if self.seed is None or k >= len(competitors):
                picked = competitors[:k]
            else:
                import random as _r
                picked = _r.Random(self.seed).sample(competitors, k)

            # 要排除谁，就先去取能排除它的证据。D2 收紧之后只数有依据的
            # 排除，光标 REFUTED 不算 —— 这也正是应该的：排除和确认一样
            # 要有依据。所以这里把被排除者的判别证据一并取回来。
            extra: set[str] = set()
            for c in picked:
                extra |= (set(G.required_evidence(c))
                          | G.discriminators_of(c)
                          | {r["evidence"] for r in G.refuting_evidence(c)})
            self._gather(tb, st, ctx, extra=extra)

            return Phase.DIAGNOSE

        if phase is Phase.DIAGNOSE:
            explanation = st.explanation_graph
            if explanation is None or not explanation.selected_path_ids:
                st.outcome_note = "受控深度调查尚未得到已支持解释路径"
                return Phase.INVESTIGATE
            st.note("depth", "selected_explanation",
                    f"系统选择路径 {explanation.selected_path_ids}")
            return Phase.REPORT

        return Phase.REPORT
