"""Investigator —— 单假设调查子 agent。

每个假设一个独立上下文。它拿到的是一份窄配置：
  单一命题     只确认或排除这一个假设，不管别的
  只读连接     agent_ro，物理上无写权限
  窄工具集     只有取证工具 + 结构化回传；连 set_hypothesis 都不给
  独立预算     步数与 turns 上限

为什么不给它下裁决的权力：裁决要在看到所有假设的证据之后做，
子 agent 只看得见自己那一条线索，让它直接改台账会导致后跑完的
覆盖先跑完的。它的职责是把证据带回来。

隔离的代价是彼此看不见，靠共享便签补偿：调查 A 假设时顺手看到的
东西，可能正是排除 B 假设的决定性证据。
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                              ToolUseBlock, create_sdk_mcp_server, query,
                              tool)

from agent.hooks import make_phase_hook
from agent.permissions import (INVESTIGATOR_DENIED, Role, allowed_tools)
from agent.state_machine import Phase
from agent.toolbox import Toolbox
from agent.explanation import EvidenceNeed, EvidenceReport
from agent.tool_planner import (PlannedEvidenceTask, environment_availability,
                                infer_target_context,
                                task_environment_tools)

SERVER = "pgdoctor"
# 子 agent 用小模型：它的任务是窄的（跑几条查询、把结果带回来），
# 不需要主 agent 那种收敛判断力。这也让 K 路并行的成本可以承受。
SUB_MODEL = os.getenv("PGDOCTOR_SUB_MODEL", "claude-haiku-4-5-20251001")

# 子 agent 不允许碰的：裁决与提案是主 agent 的事
# 保留这个名字给既有引用；权威定义在 agent.permissions
SUB_DENIED = set(INVESTIGATOR_DENIED)

# 每个假设只给它真正需要的取证工具。
# 上一轮给了全部 8 个，结果子 agent 把 turn 全耗在逐个检索工具 schema 上，
# 撞到 max_turns 时还没来得及汇报裁决。窄工具集不只是安全考虑，
# 也直接决定它能不能在预算内把活干完。
DEFAULT_TOOLSET = ["explain_query", "get_indexes", "get_table_stats"]

# 手工兜底：图里查不到时用。正常路径是从因果图推导。
FALLBACK_TOOLSETS: dict[str, list[str]] = {
    "missing_index": ["explain_query", "get_indexes", "simulate_index"],
    "stale_statistics": ["get_table_stats", "explain_query"],
    "lock_contention": ["get_blocking_chain", "get_active_sessions"],
    "table_bloat": ["get_table_stats", "get_physical_bloat"],
    "connection_exhaustion": ["get_connection_stats", "get_active_sessions"],
    "long_idle_transaction": ["get_active_sessions", "get_connection_stats"],
    "autovacuum_starvation": ["get_table_stats"],
    "disk_pressure": ["get_database_stats"],
    "stale_replication_slot": ["get_vacuum_horizon"],
    "orphaned_prepared_transaction": ["get_vacuum_horizon"],
}


def toolset_for(hypothesis: str) -> list[str]:
    """从因果图推导该假设需要哪些取证工具。

    "该查什么"图里本来就有：CONFIRMED_BY / REFUTED_BY 边指向的 Evidence
    节点，其 obtained_by 就是该调的工具。硬编码一份工具集意味着每加一个
    故障类型都要改两处代码，而且很容易漏 —— W8 的跨故障实验里
    connection_exhaustion 拿不到判别性证据就是这么来的。

    现在加故障类型只需改图。
    """
    try:
        from knowledge.causal_graph import graph as G

        g = G.load()
        if hypothesis in g:
            tools = set()
            for _, ev, k in g.out_edges(hypothesis, keys=True):
                if k not in ("CONFIRMED_BY", "REFUTED_BY"):
                    continue
                t = g.nodes.get(ev, {}).get("obtained_by")
                if t:
                    tools.add(t)
            if tools:
                # L4：历史上帮着确认过该根因的查询排前面。
                # 只调顺序不裁剪集合 —— 裁剪会让没被用过的工具永远
                # 没有出头之日，把偶然的历史固化成盲区。
                try:
                    from knowledge.evolution import top_queries_for
                    pref = top_queries_for(hypothesis)
                    ordered = [t for t in pref if t in tools]
                    ordered += sorted(t for t in tools if t not in ordered)
                    return ordered
                except Exception:
                    return sorted(tools)
    except Exception:
        pass
    return FALLBACK_TOOLSETS.get(hypothesis, DEFAULT_TOOLSET)


@dataclass
class HypothesisVerdict:
    """结构化裁决。

    刻意不是自由文本摘要 —— 散文还要主 agent 再解析一次，信息又损失一层。
    evidence 里每条都指向轨迹里真实的工具调用，ESC 核验的是这个。
    """
    hypothesis: str
    verdict: str = "INCONCLUSIVE"   # CONFIRMED / REFUTED / INCONCLUSIVE
    confidence: float = 0.0
    reasoning: str = ""
    evidence: list[dict] = field(default_factory=list)
    incidental: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    # 被 hook 拦下的越权请求。实测子 agent 会尝试调用 Bash，
    # 拦截本身生效了，但记录留在局部变量里没传出来 —— 审计轨迹
    # 缺了这一段就说不清"它试过什么"。
    blocked: list[str] = field(default_factory=list)
    turns: int = 0
    cost_usd: float = 0.0
    error: str = ""


@dataclass
class EvidenceTaskResult:
    need_id: str
    task_id: str = ""
    need_ids: list[str] = field(default_factory=list)
    report: EvidenceReport | None = None
    reports: list[EvidenceReport] = field(default_factory=list)
    explanation_id: str = ""
    explanation_revision: int = -1
    tools_used: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    turns: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    error: str = ""



_CONF_WORDS = {
    "high": 0.9, "很高": 0.9, "高": 0.85, "strong": 0.9,
    "medium": 0.6, "中": 0.6, "moderate": 0.6,
    "low": 0.3, "低": 0.3, "weak": 0.3,
    "none": 0.0, "无": 0.0,
}


def _parse_conf(raw) -> float:
    """置信度宽容解析。

    模型实测会返回 "high" / "0.9" / "90%" 各种形式，硬转 float 会抛异常，
    而这个字段并不值得为它中断一次调查。解析不出来就给 0.5（中性）。
    """
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return max(0.0, min(1.0, float(raw)))
    t = str(raw).strip().lower().rstrip("%")
    try:
        v = float(t)
        return max(0.0, min(1.0, v / 100 if v > 1 else v))
    except ValueError:
        pass
    for k, v in _CONF_WORDS.items():
        if k in t:
            return v
    return 0.5

SUB_SYSTEM = """你是一名 PostgreSQL 排障工程师，本次只负责调查一个假设。

规则：
- 只用工具取证，不要臆测。没有工具证据支撑的判断一律不作数。
- 三种裁决，必须诚实选择：
    CONFIRMED      有直接证据支撑该假设
    REFUTED        有直接证据排除该假设
    INCONCLUSIVE   证据不足以判断 —— 这是合法答案，不要硬凑一个结论
- 调查完必须调用 report_verdict 汇报，这是你唯一的输出方式。
- 若途中看到与本假设无关但可能对其他假设有用的现象，写进 incidental。

数据库很大（orders 表 1200 万行），注意区分"慢"与"扫了太多行"。"""


def _tools_for(tb: Toolbox, sink: dict, *, include_evidence_refs: bool = False,
               call_cache: dict | None = None) -> list:
    def wrap(fn):
        async def run(args: dict[str, Any]) -> dict[str, Any]:
            try:
                # One task owns one collection tool.  Repeated calls, even
                # with model-mutated arguments, reuse the first observation.
                cache_key = id(fn)
                if call_cache is not None and cache_key in call_cache:
                    return call_cache[cache_key]
                before = len(tb.st.scratchpad)
                r = fn(args)
                if include_evidence_refs:
                    refs = list(dict.fromkeys(
                        entry.get("raw_ref", "")
                        for entry in tb.st.scratchpad[before:]
                        if entry.get("raw_ref")))
                    r = {"result": r, "evidence_raw_refs": refs,
                         "reused": False}
                response = {"content": [{"type": "text",
                                          "text": json.dumps(
                                              r, ensure_ascii=False,
                                              default=str)[:3500]}]}
                if call_cache is not None:
                    call_cache[cache_key] = response
                return response
            except Exception as exc:
                return {"content": [{"type": "text",
                                     "text": f"ERROR: {type(exc).__name__}: {exc}"}],
                        "is_error": True}
        return run

    @tool("report_verdict",
          "汇报本次调查结论。verdict 取 CONFIRMED / REFUTED / INCONCLUSIVE。"
          "reasoning 说明依据；incidental 记录与本假设无关但可能对其他假设"
          "有用的发现。",
          {"verdict": str, "confidence": str, "reasoning": str,
           "incidental": str, "missing_evidence": str})
    async def report_verdict(args):
        sink["verdict"] = args.get("verdict", "INCONCLUSIVE")
        sink["confidence"] = _parse_conf(args.get("confidence"))
        sink["reasoning"] = args.get("reasoning", "")
        for key, dst in (("incidental", "incidental"),
                         ("missing_evidence", "missing_evidence")):
            raw = args.get(key, "") or ""
            sink[dst] = [x.strip() for x in str(raw).split(";") if x.strip()]
        return {"content": [{"type": "text", "text": "已记录"}]}

    @tool("report_evidence",
          "v2 调查回传。只报告工具观测、raw_ref、采集状态和局限；"
          "不得返回根因结论或支持/反证方向。",
          {"need_id": str, "tool": str, "raw_refs": str,
           "observations": str, "collection_status": str,
           "limitations": str})
    async def report_evidence(args):
        try:
            observations = json.loads(args.get("observations", "[]") or "[]")
            if not isinstance(observations, list):
                observations = [observations]
            report = EvidenceReport.from_dict({
                "need_id": args.get("need_id", ""),
                "tool": args.get("tool", ""),
                "raw_refs": [x.strip() for x in
                             str(args.get("raw_refs", "")).split(";")
                             if x.strip()],
                "observations": observations,
                "collection_status": args.get("collection_status", "ERROR"),
                "limitations": [x.strip() for x in
                                str(args.get("limitations", "")).split(";")
                                if x.strip()],
            })
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return {"content": [{"type": "text",
                                 "text": f"EvidenceReport 无效: {exc}"}],
                    "is_error": True}
        sink.setdefault("evidence_reports", {})[report.need_id] = report.to_dict()
        sink["evidence_report"] = report.to_dict()  # single-need compatibility
        return {"content": [{"type": "text", "text": "已记录证据回传"}]}

    return [
        tool("explain_query", "取执行计划，返回扫描类型、Rows Removed、"
             "估计与实际行数偏差", {"sql": str, "uid": int})(
            wrap(lambda a: tb.explain_query(a["sql"], {"uid": a.get("uid", 4242)}))),
        tool("get_indexes", "列出表上的索引", {"table": str})(
            wrap(lambda a: tb.get_indexes(a.get("table", "orders")))),
        tool("get_table_stats", "活元组/死元组清理压力/last_analyze/大小",
             {"table": str})(
            wrap(lambda a: tb.get_table_stats(a.get("table", "orders")))),
        tool("get_physical_bloat", "结构化物理膨胀测量（不可用时返回 UNKNOWN）",
             {"table": str})(
            wrap(lambda a: tb.get_physical_bloat(a.get("table", "orders")))),
        tool("get_top_queries", "最慢查询排行", {"n": int})(
            wrap(lambda a: tb.get_top_queries(int(a.get("n", 5))))),
        tool("get_active_sessions", "异常会话及等待事件", {})(
            wrap(lambda a: tb.get_active_sessions())),
        tool("get_blocking_chain", "锁阻塞链", {})(
            wrap(lambda a: tb.get_blocking_chain())),
        tool("get_connection_stats",
             "连接数与上限、按状态与角色的分布。连接打满时会话大多是 idle，"
             "用会话列表看不出问题，必须直接看总数与上限的关系。", {})(
            wrap(lambda a: tb.get_connection_stats())),
        tool("get_vacuum_horizon",
             "XID 年龄、复制槽、预备事务与 backend_xmin 的 vacuum 视界证据",
             {})(wrap(lambda a: tb.get_vacuum_horizon())),
        tool("get_database_stats",
             "库级累计统计窗口差分与数据目录所在文件系统的即时使用率",
             {})(wrap(lambda a: tb.get_database_stats())),
        tool("simulate_index", "hypopg 假设索引，不改数据库",
             {"create_sql": str, "test_sql": str, "uid": int})(
            wrap(lambda a: tb.simulate_index(a["create_sql"], a["test_sql"],
                                             {"uid": a.get("uid", 4242)}))),
        report_verdict,
        report_evidence,
    ]


def _proxy_env() -> dict[str, str]:
    return {k: os.environ[k] for k in
            ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "no_proxy")
            if k in os.environ}


async def investigate(hypothesis: str, brief: str, tb: Toolbox,
                      scratchpad_view: str, hot_query: str,
                      model: str = SUB_MODEL, max_turns: int = 16,
                      verbose: bool = True) -> HypothesisVerdict:
    sink: dict = {}
    blocked: list[str] = []
    used: list[str] = []
    target_context = infer_target_context(hot_query, table="orders")
    availability = environment_availability(
        tb, target_context=target_context)
    environment_tools = {name for name, item in availability.items()
                         if item.available} | {"report_verdict"}
    wanted = allowed_tools(
        Phase.INVESTIGATE, Role.INVESTIGATOR, hypothesis,
        environment_tools=environment_tools)
    scoped_tb = tb.scoped(
        role=Role.INVESTIGATOR, hypothesis=hypothesis,
        environment_tools=environment_tools)
    all_tools = _tools_for(scoped_tb, sink)
    tools = [t for t in all_tools if getattr(t, "name", "") in wanted]
    srv = create_sdk_mcp_server(SERVER, "1.0.0", tools)
    names = [f"mcp__{SERVER}__{t}" for t in sorted(wanted)]

    opts = ClaudeAgentOptions(
        model=model,
        system_prompt=SUB_SYSTEM,
        mcp_servers={SERVER: srv},
        allowed_tools=names,
        # 阶段仍是 INVESTIGATE，再叠加子 agent 的额外禁用集
        hooks=make_phase_hook(Phase.INVESTIGATE, blocked,
                              role=Role.INVESTIGATOR, hypothesis=hypothesis,
                              environment_tools=environment_tools),
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        setting_sources=None,
        env=_proxy_env(),
    )

    prompt = f"""告警指向的慢查询：
{hot_query}

其他调查线程已经记录的证据（可直接引用，不必重复取证）：
{scratchpad_view or "  （暂无）"}

你本次只调查这一个假设：
  {hypothesis} —— {brief}

取证后**必须**调用 report_verdict 汇报，否则本次调查作废。
工具很少，直接调用即可；不要在检索工具上浪费轮次。"""

    async def stream(text):
        yield {"type": "user", "message": {"role": "user", "content": text},
               "parent_tool_use_id": None, "session_id": f"inv_{hypothesis}"}

    v = HypothesisVerdict(hypothesis=hypothesis)
    try:
        async for msg in query(prompt=stream(prompt), options=opts):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, ToolUseBlock):
                        short = b.name.split("__")[-1]
                        used.append(short)
                        if verbose:
                            print(f"        [{hypothesis}] · {short}")
            elif isinstance(msg, ResultMessage):
                v.turns = getattr(msg, "num_turns", 0) or 0
                v.cost_usd = getattr(msg, "total_cost_usd", 0.0) or 0.0
    except Exception as exc:
        v.error = f"{type(exc).__name__}: {exc}"

    if not sink and not v.error:
        v.error = "子 agent 未调用 report_verdict（多半是 turn 预算耗尽）"
    v.verdict = sink.get("verdict", "INCONCLUSIVE")
    v.confidence = sink.get("confidence", 0.0)
    v.reasoning = sink.get("reasoning", "")
    v.incidental = sink.get("incidental", [])
    v.missing_evidence = sink.get("missing_evidence", [])
    v.tools_used = used
    v.blocked = blocked
    if v.verdict not in ("CONFIRMED", "REFUTED", "INCONCLUSIVE"):
        v.verdict = "INCONCLUSIVE"
    return v


SUB_SYSTEM_V2 = """You investigate one causal path segment or branch.

Rules:
- Use only the tools provided for this EvidenceNeed.
- Report observations, raw refs, collection status, and limitations.
- Never return CONFIRMED, REFUTED, SUPPORTS, or REFUTES.  The deterministic
  predicate layer assigns causal direction after your report.
- Call report_evidence once per assigned need.  It is your only valid output
  channel.
"""


def task_tool_names(task: PlannedEvidenceTask) -> set[str]:
    """Schema exposure uses the same permission authority as hook/runtime."""
    return allowed_tools(
        Phase.INVESTIGATE, Role.INVESTIGATOR,
        task_context=task, environment_tools=task_environment_tools(task))


async def investigate_task(task: PlannedEvidenceTask, needs: list[EvidenceNeed],
                           tb: Toolbox, scratchpad_view: str,
                           hot_query: str, model: str = SUB_MODEL,
                           max_turns: int = 12,
                           verbose: bool = True) -> EvidenceTaskResult:
    """Collect one branch/segment task without granting causal verdict powers."""
    sink: dict = {}
    blocked: list[str] = []
    used: list[str] = []
    assigned = {need.need_id: need for need in needs
                if need.need_id in task.need_ids}
    environment_tools = task_environment_tools(task)
    wanted = task_tool_names(task)
    scoped_tb = tb.scoped(
        role=Role.INVESTIGATOR, task_context=task,
        environment_tools=environment_tools)
    call_cache: dict = {}
    tools = [item for item in _tools_for(
        scoped_tb, sink, include_evidence_refs=True, call_cache=call_cache)
        if getattr(item, "name", "") in wanted]
    srv = create_sdk_mcp_server(SERVER, "1.0.0", tools)
    names = [f"mcp__{SERVER}__{tool_name}" for tool_name in sorted(wanted)]
    opts = ClaudeAgentOptions(
        model=model,
        system_prompt=SUB_SYSTEM_V2,
        mcp_servers={SERVER: srv},
        allowed_tools=names,
        hooks=make_phase_hook(Phase.INVESTIGATE, blocked,
                              role=Role.INVESTIGATOR, task_context=task,
                              environment_tools=environment_tools),
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        setting_sources=None,
        env=_proxy_env(),
    )
    prompt = f"""Hot query:
{hot_query}

Incident window and source epochs:
{json.dumps(task.incident_window, ensure_ascii=False)}

Relevant local causal subgraph only:
{json.dumps(task.local_subgraph, ensure_ascii=False)}

Assigned EvidenceNeeds:
{json.dumps([assigned[need_id].to_dict() for need_id in task.need_ids
             if need_id in assigned], ensure_ascii=False)}

Available collection tools:
{json.dumps(task.selected_tools, ensure_ascii=False)}

Call each collection tool at most once.  A single observation may cover several
needs.  Then call report_evidence once for every assigned need_id, reusing the
same raw_refs where appropriate.  The exact output fields are need_id, tool,
raw_refs, observations (JSON list), collection_status, and limitations.  Do not
decide causal direction."""

    async def stream(text):
        yield {"type": "user", "message": {"role": "user", "content": text},
               "parent_tool_use_id": None, "session_id": f"task_{task.task_id}"}

    result = EvidenceTaskResult(
        need_id=task.need_ids[0] if task.need_ids else "",
        task_id=task.task_id, need_ids=list(task.need_ids),
        explanation_id=task.explanation_id,
        explanation_revision=task.explanation_revision)
    try:
        async for msg in query(prompt=stream(prompt), options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        short = block.name.split("__")[-1]
                        used.append(short)
                        if verbose:
                            print(f"        [{task.task_id[:12]}] · {short}")
            elif isinstance(msg, ResultMessage):
                result.turns = getattr(msg, "num_turns", 0) or 0
                result.cost_usd = getattr(msg, "total_cost_usd", 0.0) or 0.0
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"

    payloads = sink.get("evidence_reports", {})
    if payloads:
        try:
            for need_id, payload in payloads.items():
                report = EvidenceReport.from_dict(payload)
                if need_id not in assigned or report.need_id != need_id:
                    raise ValueError("report need_id does not match assigned task")
                if report.tool not in task.selected_tools:
                    raise ValueError("report tool was not assigned to this task")
                result.reports.append(report)
            result.reports.sort(key=lambda item: item.need_id)
            result.report = result.reports[0] if len(result.reports) == 1 else None
            missing = sorted(set(task.need_ids) -
                             {report.need_id for report in result.reports})
            if missing:
                raise ValueError(f"missing EvidenceReport for needs: {missing}")
        except ValueError as exc:
            result.error = f"invalid EvidenceReport: {exc}"
    elif not result.error:
        result.error = "subagent did not call report_evidence"
    result.tools_used = used
    result.blocked = blocked
    return result


async def investigate_need(need: EvidenceNeed, tb: Toolbox,
                           scratchpad_view: str, hot_query: str,
                           model: str = SUB_MODEL, max_turns: int = 12,
                           verbose: bool = True) -> EvidenceTaskResult:
    """Compatibility wrapper for callers that have not adopted ToolPlan yet."""
    explanation = tb.st.explanation_graph
    explanation_id = explanation.explanation_id if explanation else "compat"
    revision = explanation.revision if explanation else 0
    target_context = infer_target_context(hot_query, table="orders")
    task = PlannedEvidenceTask(
        task_id=f"compat_{need.need_id}", explanation_id=explanation_id,
        explanation_revision=revision, need_ids=[need.need_id],
        path_ids=list(need.path_ids), target_kind=need.target_kind,
        target_ids=list(need.target_ids), evidence_types=[need.evidence_type],
        selected_tools=list(need.candidate_tools)[:3], score_components={},
        local_subgraph={"paths": [], "target_ids": need.target_ids},
        incident_window=dict(tb.st.incident_window),
        target_context=target_context)
    availability = environment_availability(
        tb, target_context=target_context)
    task.selected_tools = [tool_name for tool_name in task.selected_tools
                           if availability.get(tool_name) and
                           availability[tool_name].available]
    if not task.selected_tools:
        return EvidenceTaskResult(
            need_id=need.need_id, task_id=task.task_id,
            need_ids=[need.need_id], explanation_id=explanation_id,
            explanation_revision=revision,
            error="no candidate tool is available in the current environment")
    return await investigate_task(
        task, [need], tb, scratchpad_view, hot_query, model=model,
        max_turns=max_turns, verbose=verbose)


async def investigate_needs(needs: list[EvidenceNeed], tb: Toolbox,
                            scratchpad_view: str, hot_query: str,
                            **kwargs) -> list[EvidenceTaskResult]:
    return list(await asyncio.gather(*[
        investigate_need(need, tb, scratchpad_view, hot_query, **kwargs)
        for need in needs
    ]))


async def investigate_many(items: list[tuple[str, str]], tb: Toolbox,
                           scratchpad_view: str, hot_query: str,
                           **kw) -> list[HypothesisVerdict]:
    """并行调查多个假设。各自独立上下文，互不污染。"""
    return list(await asyncio.gather(*[
        investigate(h, b, tb, scratchpad_view, hot_query, **kw)
        for h, b in items]))
