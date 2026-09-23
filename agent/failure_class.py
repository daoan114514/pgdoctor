"""区分"模型调不通"和"agent 没做到"——唯一的一份判据。

为什么要单独一个模块：这条规则有多个读取点（CLAUDE.md 硬规则 4）。原先它以
`_UNAVAILABLE_HINTS` 的形式只长在 `agent/llm_policy.py` 里，而取证子 agent 的
失败根本不路过那里 —— `agent/investigator.py` 把异常 stringify 进
`EvidenceTaskResult.error`，`agent/orchestrator.py` 把它记成
`evidence_need_unavailable` 审计事件，`res.error` 始终是空串，于是
`eval/run_suite.py` 的 unusable 判定从头到尾没通电。

实测后果（2026-09-21 那轮 eval 跑批，两个 episode）：

- `ep_misleading_idle_txn_eval_v1_1789982550`：29 条取证请求全部报
  `You've hit your session limit · resets 9:40pm`，`evidence_reports` 是空的，
  ESC 走 INSUFFICIENT → EXHAUSTED，最后以 D=False O=False S=False、
  **unusable=False** 入账。一次额度停机被当成一次"模型没诊断出来"。
- `ep_connection_exhaustion_eval_v1_1789974597`：63 条不可得里 18 条是
  `session limit`（另外 45 条是 max_turns），同样 `evidence_reports` 为空，
  却拿到 D=True。它的三率是在 18 条停机造出来的 UNAVAILABLE 事实之上测的。

直接原因是字符串没对上：CLI 说的是 "session limit"，而 `_UNAVAILABLE_HINTS`
里写的是 "usage limit"。但真正的教训不是少写了一个词，而是**同一条规则被抄在
几个地方，抄漏了一处就等于没有**。所以这里只留一份，所有读取点都调它。

## 方向性：只做保守认定

这条判据只有一个安全方向。把停机误判成诊断失败，代价是三率偏低（可发现、可
复测）；把真实的诊断失败误判成停机，代价是**把它从分母里删掉**，三率凭空变好
——那是 CLAUDE.md 首要原则里"快而错的诊断价值为负"的同一类错误，而且更隐蔽。
所以：认不出来就返回 False，宁可把一次停机留在分母里。

具体体现为三条：

1. **agent 侧的失败先判、立即返回。** `max_turns` 是 `agent/investigator.py`
   配的 turn 预算（12），子 agent 用完了没汇报是能力问题，不是停机。实测那两个
   episode 里 max_turns 和 session limit 是**混在同一批**出现的，所以必须先排除
   agent 侧，否则一条混合批次会被整批认成停机。
2. **不认 HTTP 4xx（408/429 除外）。** 400 invalid_request 是我们自己把请求拼错了
   （prompt 超长、工具 schema 写坏），那种失败确定且整批一致，长得和停机一模一样。
   把它认成停机，整轮实验会安静地全部作废，而真正的 harness bug 一点痕迹都不留 ——
   `agent/llm_policy.py` 里 `ModelUnavailable` 的 docstring 记的就是同一类坑
   （当时是代理没配，报错却像额度）。
3. **不做宽松的子串。** 旧表里的 `"429"` 会命中任何含这三个数字的文本，
   `"exceeded"` 和合法的 `"Budget exhausted - 预算耗尽"` 只差一个词。都删掉，
   429 改由 `api_error_status` 这个**类型化**字段认。

## 词表的标定

词表不是想出来的，是从真实 trace 里数出来的（CLAUDE.md 硬规则 5：阈值必须用
真实观测值标定）。扫过 594 份 `traces/*/episode_state.json` 与
`eval/results/*.json`：命中只有 47 条 session-limit、4908 条
`error result: success`、10 条 ModelUnavailable，与领域文本零碰撞，与
"reached maximum number of turns" 那一类零碰撞。
`.dev/infra_failure_check.py` 把这个语料回放成断言，改词表会在那里报警。
"""
from __future__ import annotations

# agent 侧失败：先判这些，命中就立刻 False。
# "reached maximum number of turns" 是 investigator 配的 turn 预算用完了；
# 其余几条是子 agent 跑完了但没按约定汇报，都属于能力问题。
_AGENT_SUBTYPES = frozenset({"error_max_turns"})
_AGENT_TERMINAL = frozenset({"max_turns", "aborted_streaming", "aborted_tools"})
_AGENT_MARKERS = (
    "reached maximum number of turns",
    "did not call report_evidence",
    "invalid evidencereport",
    "report need_id does not match",
    "report tool was not assigned",
    "missing evidencereport for needs",
    "预算耗尽",
    "budget exhausted",
)

# 基础设施失败。每一条都能在真实 trace 或既有代码注释里找到出处，
# 不要凭想象往里加词 —— 加了就去 .dev/infra_failure_check.py 补语料断言。
_INFRA_MARKERS = (
    "hit your session limit",      # 2026-09-21 实测，CLI 的新文案
    "session limit",
    "usage limit",
    "rate limit",
    "rate_limit",                  # API 错误类型名是 rate_limit_error，带下划线；
                                   # 只写空格版会漏掉走 text= 路径的那一半
    "quota",
    "overloaded",
    "credit balance",
    "oauth session expired",       # 2026-09-14 实测：登录过期，不是额度
    "failed to authenticate",
    "error result: success",       # 额度耗尽时 SDK 返回的 is_error ResultMessage
    "modelunavailable",
)
_INFRA_TYPES = frozenset({"ModelUnavailable", "CLINotFoundError",
                          "CLIConnectionError"})
# 只认"重试可能有用"的状态码。4xx（408/429 除外）是请求本身有问题，
# 属于我们自己的 bug，必须留在分母里让它被看见。
_INFRA_HTTP = frozenset({408, 429, 500, 502, 503, 504, 529})


def is_infra_failure(exc: BaseException | None = None, text: str = "") -> bool:
    """这次失败是"模型调不通"吗？认不出来一律 False。

    传 `exc` 时优先读 SDK 的类型化字段（subtype / terminal_reason /
    api_error_status），因为那是唯一能把 max_turns 和停机可靠分开的东西；
    字符串匹配只是兜底。传 `text` 时只能走字符串 —— 事后扫 trace 属于这种。
    """
    blob = text
    if exc is not None:
        # 先排除 agent 侧：混合批次里 max_turns 和 session limit 会同时出现，
        # 顺序反了就会把 max_turns 也认成停机。
        if (getattr(exc, "subtype", None) in _AGENT_SUBTYPES or
                getattr(exc, "terminal_reason", None) in _AGENT_TERMINAL):
            return False
        if type(exc).__name__ in _INFRA_TYPES:
            return True
        status = getattr(exc, "api_error_status", None)
        if isinstance(status, int) and status in _INFRA_HTTP:
            return True
        errors = getattr(exc, "errors", None) or []
        blob = " ".join(str(item) for item in errors)
        blob += " " + str(getattr(exc, "result", "") or "") + " " + str(exc)

    low = blob.lower()
    if any(marker in low for marker in _AGENT_MARKERS):
        return False
    return any(marker in low for marker in _INFRA_MARKERS)


def observed_subagent_count(audit) -> int:
    """子 agent 渠道真正取到证据的条数。

    读 `tool_learning_observation.collection_status == OBSERVED`（orchestrator 只在
    该 need 至少有一份报告 OBSERVED 时才记 OBSERVED）。不读 `st.evidence_reports`：
    2026-09-21 那三个 episode 里它全是空的 —— 包括 OBSERVED=23 和 OBSERVED=8 的两个 ——
    报告在合并时被 raw_ref 检查丢掉了（一个独立的、先于本模块的问题）。用它做门，
    "有停机就作废"的判据会退化成只看停机条数，一次瞬时失败就能把有真实证据的
    episode 判作废，方向反了。
    """
    return sum(
        1 for item in audit or []
        if item.get("event") == "tool_learning_observation"
        and item.get("collection_status") == "OBSERVED")


def infra_rows_of(audit) -> list[dict]:
    """从 episode 的 evidence_task_audit 里挑出由基础设施失败造成的条目。

    事后判定用这个：跑批结束时扫一遍就知道这个 episode 有没有被停机污染，
    不需要在 episode 执行途中做任何拦截 —— 途中拦截会把"偶发一次失败"
    误杀成整个 episode 作废，那正是反方向的错误。
    """
    out = []
    for item in audit or []:
        if item.get("event") != "evidence_need_unavailable":
            continue
        if is_infra_failure(text=str(item.get("reason") or "")):
            out.append(item)
    return out
