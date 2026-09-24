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
from agent.failure_class import is_agent_terminal, is_infra_failure
from agent.hooks import make_phase_hook
from agent.orchestrator import run_evidence_investigation
from agent.permissions import Role, allowed_tools
from agent.policy import Policy
from agent.state_machine import Phase
from agent.tool_planner import ToolPlanningConfig
from agent.tool_planner import infer_target_context
from agent.toolbox import Toolbox, tool_result_text

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
# 判据本身搬去 agent/failure_class.py —— 它有好几个读取点（这里、跑批探针、
# 取证子 agent 的失败记账），抄一份就漏一处。旧的 _UNAVAILABLE_HINTS 只长在
# 这个文件里，而取证子 agent 的失败根本不路过这里，于是整条出口没通电。
SERVER = "pgdoctor"
V2_MODEL_FORBIDDEN = frozenset({"set_hypothesis", "declare_root_cause",
                                "report_verdict"})


def _forbidden_for(tb: Toolbox) -> frozenset:
    """v2 下模型不可用的裁决类工具。裁决与根因由解释图投影产生；原来只从 allowed_tools
    名单里减掉，工具仍注册在 MCP server 上、hook 与 Toolbox 都不拦，模型在 PLAN 里照样能
    改写 claimed_fault_class 直到打分（2026-09-23 审计）。三处读取点共用这一个函数：
    这里（注册）、make_phase_hook（拒绝）、Toolbox（抛错）。"""
    return V2_MODEL_FORBIDDEN if getattr(tb.st, "schema_version", 2) == 2 else frozenset()


def _model_tools(tb: Toolbox) -> list:
    forbidden = _forbidden_for(tb)
    return [t for t in _build_tools(tb) if getattr(t, "name", "") not in forbidden]


def _proxy_env() -> dict[str, str]:
    """CLI 是独立二进制，得把代理显式传给它，否则会被地区封锁挡住。"""
    out = {}
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "no_proxy"):
        v = os.environ.get(k)
        if v:
            out[k] = v
    return out


def _enum_or_str(values, description: str) -> dict:
    values = sorted({str(v) for v in values if v})
    if values:
        return {"type": "string", "enum": values, "description": description}
    return {"type": "string", "description": description}


def _root_cause_ids() -> list[str]:
    from knowledge.causal_graph import graph as _G
    return sorted(n for n, d in _G.load().nodes(data=True) if d.get("kind") == "RootCause")


def _fix_action_types() -> list[str]:
    from knowledge.causal_graph import graph as _G
    return sorted({str(f.get("action_type")) for rc in _root_cause_ids()
                   for f in _G.fixes_for(rc) if f.get("action_type")})


def _proposal_schema(tb: Toolbox) -> dict:
    """submit_proposal 的完整 JSON schema：封闭集合在 API 层定死，不靠提示词。

    action_type 原来是裸 str，v2 不做同义词纠正，模型写 "analyze" 只得到 "0 matches"，
    换个同义词再试直到撞 max_turns（2026-09-23 审计；与 collection_status 那个 635/655
    被拒的老问题同构）。路径/修复/目标 id 按当前解释的可执行干预选项生成枚举。"""
    options: list[dict] = []
    try:
        from agent import explanation_runtime as _xr
        options = list(_xr.intervention_options(tb.st, executable_only=True))
    except Exception:
        options = []
    types = [o.get("action_type") for o in options] or _fix_action_types()
    props = {
        "action_type": _enum_or_str(types, "动作类型，必须与 SQL 的 AST 分类一致"),
        "sql": {"type": "string", "description": "恰好一条语句"},
        "rollback": {"type": "string",
                     "description": "回滚语句；会话控制写 IRREVERSIBLE，"
                                    "ANALYZE/VACUUM 写 NO_ROLLBACK_NEEDED"},
        "rationale": {"type": "string"},
        "selected_path_id": _enum_or_str([o.get("path_id") for o in options], "所选解释路径"),
        "fix_id": _enum_or_str([o.get("fix") for o in options], "因果图修复节点"),
        "intervention_target": _enum_or_str(
            [o.get("target_node_id") for o in options], "干预目标节点"),
    }
    required = ["action_type", "sql", "rollback", "rationale"]
    if options:
        required += ["selected_path_id", "fix_id", "intervention_target"]
    return {"type": "object", "properties": props, "required": required}


def _hypothesis_schema(tb: Toolbox) -> dict:
    candidates = list(getattr(tb.st, "hypothesis_candidates", []) or [])
    return {"type": "object", "properties": {
        "name": _enum_or_str(candidates, "候选假设"),
        "verdict": {"type": "string", "enum": ["CONFIRMED", "REFUTED", "INCONCLUSIVE"]},
        "note": {"type": "string", "description": "证据依据，确认/排除都必须给"},
    }, "required": ["name", "verdict", "note"]}


def _root_cause_schema() -> dict:
    return {"type": "object", "properties": {
        "fault_class": {"type": "string", "enum": _root_cause_ids()},
        "root_cause": {"type": "string", "description": "为何它最能解释症状（>=20 字）"},
    }, "required": ["fault_class", "root_cause"]}


def _session_view(rows: list[dict]) -> dict:
    """get_active_sessions 给主 agent 的视图：pid 列表放最前面、完整给出。

    连接打满时 idle 行有几十个，逐行给会被截断到十来行（每行 300 多字），模型只能拿到
    少数几个 pid，多 pid 终止无从谈起。列表里只放可终止的客户端连接（排除系统/诊断连接），
    完整行仍在 trace 里，前置条件读的是那份。"""
    def usable(row, states):
        return (str(row.get("state", "")) in states and
                not row.get("is_system_or_diagnostic", True) and
                not row.get("is_current_diagnostic_connection", True))
    return {
        "idle_client_pids": [r["pid"] for r in rows if usable(r, {"idle"})],
        "idle_in_transaction_pids": [
            r["pid"] for r in rows
            if usable(r, {"idle in transaction", "idle in transaction (aborted)"})],
        "session_count": len(rows),
        "sessions_sample": rows[:6],
        "note": ("终止多个会话时写成一条 SELECT pg_terminate_backend(p1), "
                 "pg_terminate_backend(p2), ...（最多 64 个，只能用上面列出的 pid）"),
    }


def _build_tools(tb: Toolbox) -> list:
    """把 Toolbox 包成 SDK 工具。阶段校验仍由 Toolbox 内部执行。"""

    def wrap(fn):
        async def run(args: dict[str, Any]) -> dict[str, Any]:
            try:
                r = fn(args)
                # 截断可见且列表按条截（与子 agent 共用 toolbox.tool_result_text）
                return {"content": [{"type": "text", "text": tool_result_text(r, 4000)}]}
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

        tool("get_active_sessions", "异常会话及等待事件。默认不含 idle；要终止空闲会话或空闲事务时传 "
             "include_idle=true。返回可终止的 idle / idle in transaction 客户端 pid 列表"
             "（已排除系统与诊断连接）和少量样例行",
             {"include_idle": bool})(
            wrap(lambda a: _session_view(
                tb.get_active_sessions(bool(a.get("include_idle", False)))))),

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
             _hypothesis_schema(tb))(
            wrap(lambda a: tb.set_hypothesis(a["name"], a["verdict"],
                                             a.get("note", "")))),

        tool("declare_root_cause", "声明最终根因。fault_class 必须来自给定枚举。",
             _root_cause_schema())(
            wrap(lambda a: tb.declare_root_cause(a["fault_class"],
                                                 a["root_cause"]))),

        tool("submit_proposal",
             "提交修复提案给安全门。这里不会执行任何东西 —— 提案要经过"
             "AST 校验、风险分级与确认后才由系统执行。必须提供可回滚语句。",
             _proposal_schema(tb))(
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
                 batch_size: int = 2, sub_model: str | None = None):
        self.model = model
        # 取证子 agent 的模型。原来这里把主模型硬传给编排器，investigator.SUB_MODEL
        # （PGDOCTOR_SUB_MODEL）成了死配置，子 agent 实际全跑在主模型上（2026-09-23 架构
        # 评审第 9 条）。默认仍与主模型一致 —— 子 agent 的任务是机械的（调一个工具、抄观测），
        # 换小模型是合理的成本杠杆，但要先量过再换，不在这里静默降级。
        self.sub_model = (sub_model or os.getenv("PGDOCTOR_SUB_MODEL") or model)
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
        srv = create_sdk_mcp_server(SERVER, "1.0.0", _model_tools(tb))
        names = [f"mcp__{SERVER}__{t}" for t in allowed]

        opts = ClaudeAgentOptions(
            model=self.model,
            system_prompt=SYSTEM,
            mcp_servers={SERVER: srv},
            allowed_tools=names,
            hooks=make_phase_hook(phase, self.blocked,
                                  extra_denied=_forbidden_for(tb)),
            max_turns=self.max_turns,
            permission_mode="bypassPermissions",
            # 结构性移除内建工具（CLAUDE.md 硬规则 3）：allowed_tools 只是免确认名单，
            # bypassPermissions 下 Bash/Read/Agent 仍在模型的工具表里，全靠 PreToolUse hook
            # 逐次拦。2026-09-23 实测 tools=["ToolSearch"] 后模型自报的非 MCP 工具只剩
            # ToolSearch（它负责加载延迟的 MCP 工具 schema，permissions.BUILTIN_ALLOW 也只放它）。
            # hook 保留为第二道防线并继续管 MCP 工具的阶段/角色。
            tools=["ToolSearch"],
            # [] 才是 SDK 的隔离模式；None 是"全部加载"（含 ~/.claude/settings.json 的
            # hooks/env/permissions），2026-09-23 审计发现注释写反了。
            setting_sources=[],
            env=_proxy_env(),
            cwd=str(os.getcwd()),
        )

        text: list[str] = []
        try:
            return await self._drain(prompt, opts, phase, text)
        except Exception as exc:
            if is_agent_terminal(exc):
                # max_turns 是能力/预算结果，不是瞬时故障：不重试、不抛，把已收集的
                # 文本交回阶段逻辑，让 PLAN→ESCALATE / INVESTIGATE→DIAGNOSE 的正常出口
                # 接手（重跑会重复工具调用，第二次失败会把正确诊断随异常作废）。
                self.usage.append({"phase": phase.value, "agent_terminal":
                                   str(getattr(exc, "subtype", "") or
                                       getattr(exc, "terminal_reason", ""))})
                if self.verbose:
                    print(f"      [{phase.value}] 阶段在 agent 侧终止（{str(exc)[:60]}），"
                          "不重试，交回阶段逻辑")
                return "\n".join(text)
            # 先重试一次：部分失败确实是瞬时的
            if self.verbose:
                print(f"      [{phase.value}] 调用失败，重试一次: "
                      f"{str(exc)[:80]}")
            await asyncio.sleep(8)
            try:
                text = []
                return await self._drain(prompt, opts, phase, text)
            except Exception as exc2:
                self.unavailable_hits += 1
                if is_infra_failure(exc2):
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
                    target_context=infer_target_context(
                        hot, probe_uid=ctx.get("probe_uid")),
                    planning_config=ToolPlanningConfig(
                        use_learned=bool(ctx.get("use_learned", True)),
                        use_l2="l2" in set(ctx.get("learned_layers", [])),
                        use_l4="l4" in set(ctx.get("learned_layers", []))),
                    verbose=self.verbose, model=self.sub_model))
                self.orchestration = result
                self.usage.append({"phase": "INVESTIGATE(subagents)",
                                   "cost_usd": result.cost_usd,
                                   "turns": result.turns,
                                   "usage": None})
                for task_result in result.task_results:
                    if task_result.error and not (
                            getattr(task_result, "infra", False) or
                            is_infra_failure(text=task_result.error)):
                        # 停机的报错不进 scratchpad。st.note 默认 status=OBSERVED，
                        # 而 scratchpad_view 会把它塞进下一个子 agent 的 prompt，
                        # 14 条的窗口还会被它挤占 —— 这就是 CLAUDE.md 规则 6 说的
                        # "动作/故障制造证据"：一次额度停机会以"已观测证据"的身份
                        # 参与后面的推理。实测那个 episode 的 scratchpad 里有 10 条
                        # 这样的 subagent_error，全文都是 session limit 的原话。
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
   会话控制一律**先观测、再提交**：先调 get_active_sessions(include_idle=true)，
   只用它返回列表里的 pid；pid 超过 5 分钟没重新观测会被拒。连接被大量空闲连接
   占满时，一个 pid 不够把使用率降下来 —— 把列表里的 idle 客户端 pid 写进同一条
   SELECT pg_terminate_backend(p1), pg_terminate_backend(p2), ...（最多 64 个），
   不要写 FROM/WHERE，护盾只接受常量 pid。执行前系统还会逐个复核状态。
5. 提交前可以用 simulate_index 确认该索引确实会被优化器采用。

提案会经过 AST 校验与风险分级，不合规会被拒。"""
            out = self._run(self._ask(prompt, tb, phase))
            if st.proposal:
                return Phase.GATE
            st.outcome_note = f"模型未提交合规提案: {out[:200]}"
            return Phase.ESCALATE

        raise RuntimeError(f"LLMPolicy 未实现阶段 {phase}")
