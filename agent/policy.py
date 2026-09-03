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

from agent.episode_state import EpisodeState
from agent.explanation import EvidenceNeed, EvidenceTargetKind
from agent.explanation_runtime import bind_evidence, intervention_options
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

    def __init__(self, bad_fix: bool = False):
        """bad_fix=True 时先提交一次受控的失败修复。

        用来演示并验证"修复失败 -> 自动回滚 -> 知识不回滚 -> 换假设"
        这条路径。没有它就只能测 happy path，而这条路径恰恰是
        Safe Pass 真正要防的东西。

        注意这里的 SQL 本身是有效的：它与正解同列，必须通过与正常修复
        完全相同的反事实和 GATE 前置条件，才可能真正落到 undo journal
        上 —— 换成一条会被前置条件挡下的 SQL，回滚路径根本走不到。
        代价是"失败"必须由调用方注入（见 `.dev/w4_check.py` 的
        W4ScenarioEnv 与 `demo.py` 第四幕的 FailFirstVerifyEnv）：
        只把 bad_fix 打开而不注入失败，这次修复会直接成功。
        """
        self.bad_fix = bad_fix

    # 该症状组合下的候选根因。W6 起这个集合改由故障因果图多跳遍历给出，
    # 以保证覆盖率，而不是靠谁凭印象列举。
    CANDIDATES = ["missing_index", "stale_statistics", "lock_contention"]

    @staticmethod
    def _collect(tool: str, tb: Toolbox, hot: str, uid: dict) -> None:
        if tool == "explain_query":
            tb.explain_query(hot, uid)
        elif tool == "get_indexes":
            tb.get_indexes("orders")
        elif tool == "get_table_stats":
            tb.get_table_stats("orders")
        elif tool == "get_physical_bloat":
            tb.get_physical_bloat("orders")
        elif tool == "get_top_queries":
            tb.get_top_queries(5)
        elif tool == "get_active_sessions":
            tb.get_active_sessions()
        elif tool == "get_blocking_chain":
            tb.get_blocking_chain()
        elif tool == "get_connection_stats":
            tb.get_connection_stats()
        elif tool == "get_vacuum_horizon":
            tb.get_vacuum_horizon()
        elif tool == "get_database_stats":
            tb.get_database_stats()
        elif tool == "simulate_index":
            tb.simulate_index(
                "CREATE INDEX ON orders(user_id, status)", hot, uid)

    def run_phase(self, phase: Phase, tb: Toolbox, st: EpisodeState,
                  ctx: dict) -> Phase:
        hot = ctx["hot_query"]
        uid = {"uid": 4242}

        if phase is Phase.MONITOR:
            sess = tb.get_active_sessions()
            # Establish cumulative counter baselines and source epochs.  The
            # first read is UNKNOWN by design and cannot support a diagnosis.
            tb.get_database_stats()
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
            return Phase.INVESTIGATE

        if phase is Phase.INVESTIGATE:
            needs = [EvidenceNeed.from_dict(item) for item in
                     ctx.get("explanation", {}).get("needs", [])]
            tools = list(dict.fromkeys(
                need.candidate_tools[0] for need in needs
                if need.candidate_tools))
            for tool_name in tools:
                self._collect(tool_name, tb, hot, uid)
            return Phase.DIAGNOSE

        if phase is Phase.DIAGNOSE:
            explanation = st.explanation_graph
            if explanation is None or not explanation.selected_path_ids:
                st.outcome_note = "没有可选择的已支持解释路径"
                return Phase.INVESTIGATE
            if st.claimed_fault_class != "missing_index":
                st.outcome_note = (f"已选择路径 {explanation.selected_path_ids}；"
                                   "确定性基线未实现该类修复")
                return Phase.REPORT
            return Phase.PLAN if ctx.get("allow_repair", False) else Phase.REPORT

        if phase is Phase.PLAN:
            options = [option for option in intervention_options(
                st, executable_only=True)
                if option.get("action_type") == "create_index"]
            interventions = {
                (option.get("target_node_id"), option.get("fix"),
                 option.get("action_type"))
                for option in options
            }
            if len(interventions) != 1:
                st.outcome_note = "选中解释没有唯一的可执行建索引干预"
                return Phase.ESCALATE
            paths = st.explanation_graph.path_map()
            option = sorted(options, key=lambda item: (
                -float(paths[item["path_id"]].score_components.get(
                    "total", 0.0)),
                item["path_id"],
            ))[0]
            bad_attempt = self.bad_fix and st.repair_attempts == 0
            sql = (
                "CREATE INDEX CONCURRENTLY idx_wrong_fix "
                "ON orders(user_id, status)"
                if bad_attempt else
                "CREATE INDEX CONCURRENTLY idx_orders_user_status "
                "ON orders(user_id, status)"
            )

            # Intervention predicates are plan preconditions, not diagnosis
            # evidence.  Collect and bind them against the concrete fix only
            # after a path and SQL definition have been selected, then rerun
            # ESC for the new explanation revision before creating the plan.
            from knowledge.causal_graph import graph as causal_graph
            evidence = causal_graph.load().nodes["counterfactual_index"]
            need = EvidenceNeed.create(
                path_ids=[option["path_id"]],
                target_kind=EvidenceTargetKind.INTERVENTION,
                target_ids=[option["fix"]],
                evidence_type="counterfactual_index",
                predicate_id=str(evidence["predicate_id"]),
                required=True,
                freshness_seconds=int(evidence.get("freshness_seconds", 300)),
                candidate_tools=[str(evidence["obtained_by"])],
                reason="validate the concrete intervention definition",
            )
            before = len(st.scratchpad)
            tb.simulate_index(sql, hot, uid)
            refs = {
                str(entry.get("raw_ref") or "")
                for entry in st.scratchpad[before:]
                if entry.get("evidence_type") == "counterfactual_index" and
                entry.get("raw_ref")
            }
            bind_evidence(st, explicit_needs=[need], raw_refs=refs)
            from agent.esc import check_explanation
            current_esc = check_explanation(st)
            if current_esc["verdict"] != "SUFFICIENT":
                raise ValueError(
                    "intervention evidence invalidated explanation sufficiency")

            if bad_attempt:
                # W4 的失败结果由外部验收器注入；SQL 本身必须通过与正常
                # 修复完全相同的反事实和 GATE 前置条件，才能真实走到 undo。
                tb.submit_proposal(
                    action_type="create_index",
                    sql=sql,
                    rollback="DROP INDEX CONCURRENTLY idx_wrong_fix",
                    rationale="受控的首次修复，用于验证失败结果后的回滚路径",
                    predicted_impact={"p99_ms": "<50"},
                    selected_path_id=option["path_id"],
                    fix_id=option["fix"],
                    intervention_target=option["target_node_id"])
            else:
                tb.submit_proposal(
                    action_type="create_index",
                    sql=sql,
                    rollback="DROP INDEX CONCURRENTLY idx_orders_user_status",
                    rationale="补上覆盖 user_id+status 谓词的复合索引，消除全表扫",
                    predicted_impact={"cost": "180975 -> 52"},
                    selected_path_id=option["path_id"],
                    fix_id=option["fix"],
                    intervention_target=option["target_node_id"])
            return Phase.GATE

        raise RuntimeError(f"ScriptedPolicy 未实现阶段 {phase}")
