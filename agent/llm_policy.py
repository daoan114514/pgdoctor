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

from agent.episode_state import EpisodeState, Verdict
from agent.policy import Policy
from agent.hooks import make_phase_hook
from agent.orchestrator import run_investigation
from agent.state_machine import ALLOWED_TOOLS, Phase
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

        tool("get_table_stats", "表统计：活元组/死元组/膨胀率/last_analyze/大小",
             {"table": str})(
            wrap(lambda a: tb.get_table_stats(a.get("table", "orders")))),

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
             "库级累计计数器：死锁数、临时文件外溢量、检查点定时/请求式"
             "次数与耗时、I/O 等待时间",
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
              "rationale": str})(
            wrap(lambda a: tb.submit_proposal(
                a["action_type"], a["sql"], a["rollback"],
                a.get("rationale", "")))),
    ]


SYSTEM = """你是一名资深 PostgreSQL DBA，正在排查一次线上告警。

工作方式：
- 只能用给定工具取证。不要臆测，每个结论都要有工具返回的证据支撑。
- 做鉴别诊断：不是"找到一个能解释的就收工"，而是要主动排除竞争假设。
- 一个假设被排除，也要用 set_hypothesis 记下来并说明依据。
- 数据库很大（orders 表 1200 万行），注意区分"慢"和"扫了太多行"。

可用的 fault_class 枚举：
  missing_index      缺少可用索引，导致全表扫
  stale_statistics   统计信息过期，优化器选了坏计划
  lock_contention    锁等待/阻塞链
  table_bloat        表膨胀
  connection_exhaustion  连接打满

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
        allowed = sorted(ALLOWED_TOOLS[phase])
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
            st.symptoms = ctx.get("symptoms", [])
            return Phase.OBSERVE

        if phase is Phase.OBSERVE:
            tb.get_top_queries(5)
            return Phase.HYPOTHESIZE

        if phase is Phase.HYPOTHESIZE:
            # W6 起候选集改由故障因果图多跳遍历给出以保证覆盖率；
            # 现在先用固定枚举，模型只负责后续取证与排除。
            st.ensure_hypotheses(ctx.get("candidates") or self.CANDIDATES)
            return Phase.INVESTIGATE

        if phase is Phase.INVESTIGATE:
            if self.use_subagents:
                # 每条假设一个独立上下文：取证的中间数据（大量 EXPLAIN、
                # 视图输出）全留在子上下文里，主上下文只收结构化裁决。
                import asyncio as _aio
                r = self._run(run_investigation(
                    st, tb, ctx.get("candidates") or self.CANDIDATES, hot,
                    batch_size=self.batch_size, verbose=self.verbose,
                    case_prior=ctx.get("case_prior", "")))
                self.orchestration = r
                self.usage.append({"phase": "INVESTIGATE(subagents)",
                                   "cost_usd": r.cost_usd, "turns": r.turns,
                                   "usage": None})
                if self.verbose and r.conflicts:
                    for c in r.conflicts:
                        print(f"      冲突: {c}")
                return Phase.DIAGNOSE

            prompt = f"""{st.render_context()}

{ctx.get("case_prior", "")}
{ctx.get("playbook_hint", "")}

告警指向的慢查询：
{hot}

请逐条调查下列假设，每条都要用工具取证，然后用 set_hypothesis 记录裁决：
{chr(10).join('  - ' + c for c in (ctx.get('candidates') or self.CANDIDATES))}

注意做真正的鉴别诊断：确认一个的同时也要排除其他的。"""
            self._run(self._ask(prompt, tb, phase))
            return Phase.DIAGNOSE

        if phase is Phase.DIAGNOSE:
            prompt = f"""{st.render_context()}

告警指向的慢查询：
{hot}

现在收敛结论。要求：
1. 若某个根因证据充分，先用 simulate_index 之类的手段做反事实验证
   （对缺索引类问题尤其重要：不改数据库就能预先证伪）。
2. 验证通过后用 declare_root_cause 声明根因。
3. 若证据不足以区分多个假设，不要硬下结论，直接说明缺什么证据。"""
            out = self._run(self._ask(prompt, tb, phase))

            if not st.claimed_fault_class:
                st.outcome_note = f"模型未声明根因: {out[:200]}"
                return Phase.ESCALATE
            return Phase.PLAN

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

            prompt = f"""{st.render_context()}

{ctx.get("playbook_hint", "")}

告警指向的慢查询：
{hot}

已确认根因：{st.claimed_fault_class} — {st.claimed_root_cause}
{denial}
此前试过且失败的修复（不要重复提交）：
{tried}

请用 submit_proposal 提交一个修复方案。

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
4. 提交前可以用 simulate_index 确认该索引确实会被优化器采用。

提案会经过 AST 校验与风险分级，不合规会被拒。"""
            out = self._run(self._ask(prompt, tb, phase))
            if st.proposal:
                return Phase.GATE
            st.outcome_note = f"模型未提交合规提案: {out[:200]}"
            return Phase.ESCALATE

        raise RuntimeError(f"LLMPolicy 未实现阶段 {phase}")
