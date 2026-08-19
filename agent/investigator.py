"""Investigator —— 单假设调查子 agent。

每个假设一个独立上下文。它拿到的是一份窄配置：
  单一命题     只确认或排除这一个假设，不管别的
  只读连接     agent_ro，物理上无写权限
  窄工具集     只有取证工具 + report_verdict；连 set_hypothesis 都不给
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
                              TextBlock, ToolUseBlock, create_sdk_mcp_server, query,
                              tool)

from agent.hooks import make_phase_hook
from agent.state_machine import Phase
from agent.toolbox import Toolbox

SERVER = "pgdoctor"
# 子 agent 用小模型：它的任务是窄的（跑几条查询、把结果带回来），
# 不需要主 agent 那种收敛判断力。这也让 K 路并行的成本可以承受。
SUB_MODEL = os.getenv("PGDOCTOR_SUB_MODEL", "claude-haiku-4-5-20251001")

# 子 agent 不允许碰的：裁决与提案是主 agent 的事
SUB_DENIED = {"set_hypothesis", "declare_root_cause", "submit_proposal"}

# 每个假设只给它真正需要的取证工具。
# 上一轮给了全部 8 个，结果子 agent 把 turn 全耗在逐个检索工具 schema 上，
# 撞到 max_turns 时还没来得及汇报裁决。窄工具集不只是安全考虑，
# 也直接决定它能不能在预算内把活干完。
TOOLSETS: dict[str, list[str]] = {
    "missing_index": ["explain_query", "get_indexes", "simulate_index"],
    "stale_statistics": ["get_table_stats", "explain_query"],
    "lock_contention": ["get_blocking_chain", "get_active_sessions"],
    "table_bloat": ["get_table_stats"],
    "connection_exhaustion": ["get_active_sessions"],
}
DEFAULT_TOOLSET = ["explain_query", "get_indexes", "get_table_stats"]


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


def _tools_for(tb: Toolbox, sink: dict) -> list:
    def wrap(fn):
        async def run(args: dict[str, Any]) -> dict[str, Any]:
            try:
                r = fn(args)
                return {"content": [{"type": "text",
                                     "text": json.dumps(r, ensure_ascii=False,
                                                        default=str)[:3500]}]}
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

    return [
        tool("explain_query", "取执行计划，返回扫描类型、Rows Removed、"
             "估计与实际行数偏差", {"sql": str, "uid": int})(
            wrap(lambda a: tb.explain_query(a["sql"], {"uid": a.get("uid", 4242)}))),
        tool("get_indexes", "列出表上的索引", {"table": str})(
            wrap(lambda a: tb.get_indexes(a.get("table", "orders")))),
        tool("get_table_stats", "活元组/死元组/膨胀率/last_analyze/大小",
             {"table": str})(
            wrap(lambda a: tb.get_table_stats(a.get("table", "orders")))),
        tool("get_top_queries", "最慢查询排行", {"n": int})(
            wrap(lambda a: tb.get_top_queries(int(a.get("n", 5))))),
        tool("get_active_sessions", "异常会话及等待事件", {})(
            wrap(lambda a: tb.get_active_sessions())),
        tool("get_blocking_chain", "锁阻塞链", {})(
            wrap(lambda a: tb.get_blocking_chain())),
        tool("simulate_index", "hypopg 假设索引，不改数据库",
             {"create_sql": str, "test_sql": str, "uid": int})(
            wrap(lambda a: tb.simulate_index(a["create_sql"], a["test_sql"],
                                             {"uid": a.get("uid", 4242)}))),
        report_verdict,
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
    wanted = set(TOOLSETS.get(hypothesis, DEFAULT_TOOLSET)) | {"report_verdict"}
    all_tools = _tools_for(tb, sink)
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
                              extra_denied=SUB_DENIED),
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


async def investigate_many(items: list[tuple[str, str]], tb: Toolbox,
                           scratchpad_view: str, hot_query: str,
                           **kw) -> list[HypothesisVerdict]:
    """并行调查多个假设。各自独立上下文，互不污染。"""
    return list(await asyncio.gather(*[
        investigate(h, b, tb, scratchpad_view, hot_query, **kw)
        for h, b in items]))
