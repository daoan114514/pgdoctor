"""LLMPolicy —— 让模型进场，但只在真正需要判断的阶段。

省额度不是权宜之计，而是架构 C 的直接红利：因为"决策"和"流程"是分离的，
可以让模型只负责鉴别诊断，其余阶段走确定性代码。单个 episode 只有
三次模型调用（HYPOTHESIZE / INVESTIGATE / DIAGNOSE）。

纵深防御：
  - 状态机在 Toolbox 里拦一层（阶段外工具直接抛异常）
  - can_use_tool 在 SDK 侧再拦一层（模型连请求都发不出去）
两层都不依赖提示词，模型即使被诱导也调不动越界工具。
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from agent.episode_state import EpisodeState
from agent.explanation import EvidenceNeed
from agent.hooks import make_phase_hook
from agent.orchestrator import run_evidence_investigation
from agent.permissions import Role, allowed_tools
from agent.policy import Policy
from agent.state_machine import Phase
from agent.tool_planner import ToolPlanningConfig
from agent.toolbox import Toolbox

MODEL = os.getenv("PGDOCTOR_MODEL", "claude-sonnet-4-5")


class ModelUnavailable(RuntimeError):
    """模型调不通（额度、限流、认证、网络），与"模型答错了"是两回事。

    别把这几种混为一谈地报给人看：原先提示语只写"疑似额度或限流"，
    而真正的原因是启动脚本没配代理、直连拿到 403 —— 那句提示让我
    朝着"等额度恢复"查了好几轮。

    必须区分：前者该把 episode 判为不可用，后者才是实验数据。
    混在一起的话，一次额度耗尽会让整轮实验静默变成 0/4。
    """


# 额度/限流的特征串。cost=$0 且立刻失败是最可靠的旁证。
_UNAVAILABLE_HINTS = (
    "error result: success", "usage limit", "rate limit",
    "quota", "overloaded", "429", "exceeded",
)
SERVER = "pgdoctor"
V2_MODEL_FORBIDDEN = frozenset({"set_hypothesis", "declare_root_cause",
                                "report_verdict"})


def _proxy_env() -> dict[str, str]:
    """CLI 是独立二进制，得把代理显式传给它，否则会被地区封锁挡住。"""
    out = {}
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "no_proxy"):
        v = os.environ.get(k)
        if v:
            out[k] = v
    return out


def _build_tools(tb: Toolbox) -> list:
    """把 Toolbox 包成 SDK 工具。阶段校验仍由 Toolbox 内部执行。"""

    def wrap(fn):
        async def run(args: dict[str, Any]) -> dict[str, Any]:
            try:
                r = fn(args)
                return {"content": [{"type": "text",
                                     "text": json.dumps(r, ensure_ascii=False,
                                                        default=str)[:4000]}]}
            except Exception as exc:
                # 把拒绝原因如实返回，模型据此调整，而不是反复撞墙
                return {"content": [{"type": "text",
                                     "text": f"ERROR: {type(exc).__name__}: {exc}"}],
                        "is_error": True}
        return run

    return [
        tool("explain_query", "对一条 SQL 取执行计划，返回扫描类型、"
             "Rows Removed by Filter、估计与实际行数偏差等结构化摘要",
             {"sql": str, "uid": int})(
            wrap(lambda a: tb.explain_query(a["sql"], {"uid": a.get("uid", 4242)}))),

        tool("get_indexes", "列出某张表上的索引及其定义与使用次数",
             {"table": str})(
            wrap(lambda a: tb.get_indexes(a.get("table", "orders")))),

        tool("get_table_stats", "表统计：活元组/死元组清理压力/last_analyze/"
             "autovacuum 有效开关、触发阈值与 worker 状态/大小",
             {"table": str})(
            wrap(lambda a: tb.get_table_stats(a.get("table", "orders")))),

        tool("get_physical_bloat",
             "用 pgstattuple_approx 测量物理可回收比例；不可用时返回 UNKNOWN",
             {"table": str})(
            wrap(lambda a: tb.get_physical_bloat(a.get("table", "orders")))),

        tool("get_top_queries", "按累计耗时排序的最慢查询", {"n": int})(
            wrap(lambda a: tb.get_top_queries(int(a.get("n", 5))))),

        tool("get_active_sessions", "当前异常会话及其等待事件", {})(
            wrap(lambda a: tb.get_active_sessions())),

        tool("get_blocking_chain", "锁阻塞链：谁挡住了谁", {})(
            wrap(lambda a: tb.get_blocking_chain())),

        tool("get_connection_stats",
             "连接数与上限、按状态与角色的分布，以及 idle in transaction 数量",
             {})(
            wrap(lambda a: tb.get_connection_stats())),

        tool("get_vacuum_horizon",
             "谁挡着 xmin 前进：XID 年龄与回卷风险、复制槽 / 预备事务 / "
             "长事务各自持住的 xmin 年龄。死元组回收不掉时先查这个 —— "
             "挡住 vacuum 的不只是长事务",
             {})(
            wrap(lambda a: tb.get_vacuum_horizon())),

        tool("get_database_stats",
             "库级累计计数器的窗口差分：死锁、临时文件外溢、检查点定时/"
             "请求式次数与耗时；同时返回数据目录文件系统的即时使用率。"
             "累计项首次调用只建立基线，证据为 UNKNOWN；"
             "故障持续一段时间后再次调用，才能得到可判定的窗口增量",
             {})(
            wrap(lambda a: tb.get_database_stats())),

        tool("simulate_index", "用 hypopg 建假设索引并对比执行计划成本。"
             "不会真正修改数据库，可在动手前证伪一个缺索引判断",
             {"create_sql": str, "test_sql": str, "uid": int})(
            wrap(lambda a: tb.simulate_index(a["create_sql"], a["test_sql"],
                                             {"uid": a.get("uid", 4242)}))),

        tool("fetch_raw", "按 raw_ref 回取此前落盘的原始输出（如完整"
             "执行计划）。摘要不够用时才调，正常诊断不需要。",
             {"ref": str})(
            wrap(lambda a: tb.fetch_raw(a["ref"]))),

        tool("set_hypothesis", "给某个假设下裁决。verdict 取 CONFIRMED / "
             "REFUTED / INCONCLUSIVE。必须给出依据。",
             {"name": str, "verdict": str, "note": str})(
            wrap(lambda a: tb.set_hypothesis(a["name"], a["verdict"],
                                             a.get("note", "")))),

        tool("declare_root_cause", "声明最终根因。fault_class 必须来自给定枚举。",
             {"fault_class": str, "root_cause": str})(
            wrap(lambda a: tb.declare_root_cause(a["fault_class"],
                                                 a["root_cause"]))),

        tool("submit_proposal",
             "提交修复提案给安全门。这里不会执行任何东西 —— 提案要经过"
             "AST 校验、风险分级与确认后才由系统执行。必须提供可回滚语句。",
             {"action_type": str, "sql": str, "rollback": str,
              "rationale": str, "selected_path_id": str,
              "fix_id": str, "intervention_target": str})(
            wrap(lambda a: tb.submit_proposal(
                a["action_type"], a["sql"], a["rollback"],
                a.get("rationale", ""),
                selected_path_id=a.get("selected_path_id", ""),
                fix_id=a.get("fix_id", ""),
                intervention_target=a.get("intervention_target", "")))),
    ]


SYSTEM = """你是一名资深 PostgreSQL DBA，正在排查一次线上告警。

工作方式：
- 只能用给定工具取证。不要臆测，每个结论都要有工具返回的证据支撑。
- 调查对象是系统给出的路径分叉或路径片段，不是孤立的根因字符串。
- 只提交调查意图、结构化观测和修复提案；节点/边状态、证据方向、
  ESC 与 GATE 因果上下文均由系统根据持久状态确定。
- 数据库很大（orders 表 1200 万行），注意区分"慢"和"扫了太多行"。

简洁行动，不要复述工具输出。"""


class LLMPolicy(Policy):
    name = "llm"

    CANDIDATES = ["missing_index", "stale_statistics", "lock_contention"]

    def __init__(self, model: str = MODEL, max_turns_per_phase: int = 12,
                 verbose: bool = True, use_subagents: bool = True,
                 batch_size: int = 2):
        self.model = model
        self.max_turns = max_turns_per_phase
        self.verbose = verbose
        # 关掉就退回单 agent 一把梭，用于对照隔离编排到底带来什么
        self.use_subagents = use_subagents
        self.batch_size = batch_size
        self.orchestration = None
        self.usage: list[dict] = []
        self.blocked: list[str] = []
        self.unavailable_hits = 0

    # ── SDK 调用 ─────────────────────────────────────────
    @staticmethod
    async def _stream(text: str):
        """can_use_tool 只在流式输入下可用，所以 prompt 必须包成异步迭代器。
        为了保住纵深防御的第二层，宁可多这几行也不去掉那个回调。"""
        yield {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
            "session_id": "default",
        }

    async def _ask(self, prompt: str, tb: Toolbox, phase: Phase) -> str:
        allowed = sorted(allowed_tools(phase, Role.MAIN) - V2_MODEL_FORBIDDEN)
        srv = create_sdk_mcp_server(SERVER, "1.0.0", _build_tools(tb))
        names = [f"mcp__{SERVER}__{t}" for t in allowed]

        opts = ClaudeAgentOptions(
            model=self.model,
            system_prompt=SYSTEM,
            mcp_servers={SERVER: srv},
            allowed_tools=names,
            hooks=make_phase_hook(phase, self.blocked),
            max_turns=self.max_turns,
            permission_mode="bypassPermissions",
            setting_sources=None,      # 不加载用户/项目设置，保证可复现
            env=_proxy_env(),
            cwd=str(os.getcwd()),
        )

        text: list[str] = []
        try:
            return await self._drain(prompt, opts, phase, text)
        except Exception as exc:
            # 先重试一次：部分失败确实是瞬时的
            if self.verbose:
                print(f"      [{phase.value}] 调用失败，重试一次: "
                      f"{str(exc)[:80]}")
            await asyncio.sleep(8)
            try:
                text = []
                return await self._drain(prompt, opts, phase, text)
            except Exception as exc2:
                msg = f"{exc2}".lower()
                self.unavailable_hits += 1
                if any(h in msg for h in _UNAVAILABLE_HINTS):
                    # 连续两次都是这个特征 -> 判为模型不可用而非答错，
                    # 让跑批把该 episode 标成不可用
                    raise ModelUnavailable(
                        f"模型调用不可用（额度/限流/认证/网络，看下方原文）: {exc2}") from exc2
                raise

    async def _drain(self, prompt: str, opts, phase, text: list[str]) -> str:
        async for msg in query(prompt=self._stream(prompt), options=opts):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        text.append(b.text)
                    elif isinstance(b, ToolUseBlock) and self.verbose:
                        short = b.name.split("__")[-1]
                        args = json.dumps(b.input, ensure_ascii=False)[:90]
                        print(f"      · {short}({args})")
            elif isinstance(msg, ResultMessage):
                u = {"phase": phase.value,
                     "cost_usd": getattr(msg, "total_cost_usd", None),
                     "turns": getattr(msg, "num_turns", None),
                     "usage": getattr(msg, "usage", None)}
                self.usage.append(u)
                if self.verbose:
                    print(f"      [{phase.value}] turns={u['turns']} "
                          f"cost={u['cost_usd']}")
        return "\n".join(text)

    def _run(self, coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        return loop.run_until_complete(coro)

    # ── 阶段实现 ─────────────────────────────────────────
    def run_phase(self, phase: Phase, tb: Toolbox, st: EpisodeState,
                  ctx: dict) -> Phase:
        hot = ctx["hot_query"]

        # 确定性阶段：固定动作，不花模型额度
        if phase is Phase.MONITOR:
            tb.get_active_sessions()
            tb.get_database_stats()
            return Phase.OBSERVE

        if phase is Phase.OBSERVE:
            tb.get_top_queries(5)
            return Phase.HYPOTHESIZE

        if phase is Phase.HYPOTHESIZE:
            # Path recall and P0 obligations are system-owned and already
            # persisted by the loop before policy code runs.
            return Phase.INVESTIGATE

        if phase is Phase.INVESTIGATE:
            needs = [EvidenceNeed.from_dict(item) for item in
                     ctx.get("explanation", {}).get("needs", [])]
            if not needs:
                return Phase.DIAGNOSE
            if self.use_subagents:
                # Planning owns tool selection and merges one tool call across
                # every need it can satisfy.  Subagents only return reports;
                # deterministic predicates update the explanation graph.
                result = self._run(run_evidence_investigation(
                    st, tb, needs, hot, max_concurrency=self.batch_size,
                    planning_config=ToolPlanningConfig(
                        use_learned=bool(ctx.get("use_learned", True)),
                        use_l2="l2" in set(ctx.get("learned_layers", [])),
                        use_l4="l4" in set(ctx.get("learned_layers", []))),
                    verbose=self.verbose, model=self.model))
                self.orchestration = result
                self.usage.append({"phase": "INVESTIGATE(subagents)",
                                   "cost_usd": result.cost_usd,
                                   "turns": result.turns,
                                   "usage": None})
                for task_result in result.task_results:
                    if task_result.error:
                        st.note("investigator", "subagent_error",
                                task_result.error[:180])
                    for blocked in task_result.blocked:
                        st.note("investigator", "blocked_call", blocked[:180])
                return Phase.DIAGNOSE

            prompt = f"""{st.render_context()}

告警指向的慢查询：
{hot}

请采集下面这些路径前沿所需的结构化证据。只调用 candidate_tools，
不要用 set_hypothesis 或自然语言自行判断支持/反证：
{json.dumps([need.to_dict() for need in needs[:6]], ensure_ascii=False)}"""
            self._run(self._ask(prompt, tb, phase))
            return Phase.DIAGNOSE

        if phase is Phase.DIAGNOSE:
            explanation = st.explanation_graph
            if explanation is None or not explanation.selected_path_ids:
                st.outcome_note = "没有可选择的已支持解释路径"
                return Phase.INVESTIGATE
            return Phase.PLAN if ctx.get("allow_repair", False) else Phase.REPORT

        if phase is Phase.PLAN:
            tried = "\n".join(
                f"  - {a.sql}  ->  {a.verdict}" for a in st.attempts) or "  （无）"
            denial = ""
            if st.last_gate_denial:
                g = st.last_gate_denial
                denial = (
                    "\n★ 上一个提案被安全门拒绝了，先看清原因再改：\n"
                    f"  被拒 SQL : {g.get('sql', '')}\n"
                    f"  action_type: {g.get('action_type', '')}   "
                    f"rollback: {g.get('rollback', '')}\n"
                    f"  档位     : {g.get('tier', '')}\n"
                    f"  理由     : {'; '.join(g.get('reasons', []))}\n"
                    "  必须针对上面的理由修改，原样重提只会再被拒一次。\n")
            options = ctx.get("remediation_options", [])
            graph_fixes = "\n".join(
                f"  - {f['fix']}: action_type={f['action_type']}, "
                f"path_id={f['path_id']}, target={f['target_node_id']}, "
                f"kind={f['intervention_kind']}, "
                f"最低门槛={f['risk_tier']}, SQL 模板={f['template']}, "
                f"rollback={f['rollback']}"
                for f in options) or "  （因果图没有可执行修复）"

            prompt = f"""{st.render_context()}

{ctx.get("playbook_hint", "")}

告警指向的慢查询：
{hot}

已确认根因：{st.claimed_fault_class} — {st.claimed_root_cause}
因果图允许的修复：
{graph_fixes}
{denial}
此前试过且失败的修复（不要重复提交）：
{tried}

请用 submit_proposal 提交一个修复方案，并原样填写所选项的
selected_path_id、fix_id 和 intervention_target。它们只是选择意图，
系统会从持久解释图重新校验，不能覆盖可信因果上下文。

action_type 必须取自：create_index / vacuum_analyze（含 ANALYZE）/
set_parameter / alter_table_options / session_control / dml_update / dml_delete

要求：
1. 一个提案只做一件事，不要把多条语句拼在一起。
2. rollback 字段必须填，三选一：
   - 能撤销的写具体回滚 SQL（如建索引对应 DROP INDEX CONCURRENTLY）
   - 撤不回来的写 IRREVERSIBLE（终止会话）
   - 本就无需撤销的写 NO_ROLLBACK_NEEDED（ANALYZE 只重算统计，
     退回失真的旧统计既做不到也没人想要）
   留空一律拒绝 —— 留空分不清"想过了不需要"和"忘了写"。
3. 建索引一律用 CONCURRENTLY —— 大表上不加它会锁表，安全门会直接拒绝。
4. 修复要对症：统计信息过期用 ANALYZE（action_type 填 vacuum_analyze、
   rollback 填 NO_ROLLBACK_NEEDED），不要用建索引去绕；
   锁竞争用 pg_terminate_backend 终止阻塞源，action_type 填
   session_control、rollback 填 IRREVERSIBLE（终止会话本就撤不回来，
   写假的回滚语句会制造"以为能回滚"的错觉）。
5. 提交前可以用 simulate_index 确认该索引确实会被优化器采用。

提案会经过 AST 校验与风险分级，不合规会被拒。"""
            out = self._run(self._ask(prompt, tb, phase))
            if st.proposal:
                return Phase.GATE
            st.outcome_note = f"模型未提交合规提案: {out[:200]}"
            return Phase.ESCALATE

        raise RuntimeError(f"LLMPolicy 未实现阶段 {phase}")
