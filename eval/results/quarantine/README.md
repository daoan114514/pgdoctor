# 隔离区：被基础设施停机污染的跑批结果

这些文件不是无效数据，是 **bug 的证据**，所以只搬不删。它们被移出 `eval/results/` 是
因为 `eval/run_suite.py::_already_valid` 和守护脚本的 `usable_episodes` 都非递归地
glob `eval/results/*.json`，放在这里就不会再被当成"已完成"。

| 文件 | episode | 为什么污染 |
|---|---|---|
| `guard_long_idle_transaction_20260921.json` | `ep_misleading_idle_txn_eval_v1_1789982550` | 29/29 条取证请求全报 `You've hit your session limit`，`evidence_reports` 为空，却以 `unusable=False`、D/O/S=False 入账 |
| `guard_connection_exhaustion_20260921.json` | `ep_connection_exhaustion_eval_v1_1789974597` | 63 条不可得里 18 条是 session limit（另 45 条 max_turns），`evidence_reports` 为空；D=True 是在 18 条停机造出的 UNAVAILABLE 事实上测的 |
| `guard_lock_contention_20260921.json` | `ep_lock_contention_eval_v1_1789980308` | **干净**（0 条 session limit）。搬来只因为决定 5 个场景在修好判据后统一重跑，保证同一份 harness 下可比 |
| `llm_eval_worktree_20260911.json` | `ep_misleading_idle_txn_eval_v1_1789026655` + 4 行 `GoldenAnchorStale` | 默认 tag 覆盖了 commit `f2391d2` 的基线；那个 64 步/19 小时/$15.53 的 episode trace 里有 2235 处 `error result: success`，同一类停机 |

根因与修复见 `agent/failure_class.py` 的模块 docstring；语料回放检查在 `.dev/infra_failure_check.py`。

**不要**把这里的 trace 喂给 `.dev/relearn.py`：它不看 split、不看 unusable，会把
"零证据下声称 lock_contention"当成真实误诊写进 v1 学习状态。

## 2026-09-22 下午（修判据之后、修子 agent 之前）

| 文件 | episode | 为什么污染 |
|---|---|---|
| `guard_lock_contention_20260922_infra65.json` | `ep_lock_contention_eval_v1_1790054344` | `infra_failures=65`（WSL CLI 仍在 Pro 窗口撞 `session limit`）；且当时有 11:37 被杀 episode 留下的孤儿 `sandbox.workload` 在打库 |
| `guard_long_idle_transaction_20260922_infra52.json` | `ep_misleading_idle_txn_eval_v1_1790057302` | `infra_failures=52`（Max 窗口 14:54 打满）；同样叠着孤儿负载；子 agent 655 次汇报 635 次被 `invalid collection status` 拒——这份 trace 是定位子 agent 根因的语料 |

这两份的 `unusable=false` 是新判据的**单向**规则：有 OBSERVED 就计分但标记，不从分母删。
守护认 `infra_failures>0` 为未完成，会重跑。

## 2026-09-22 傍晚（修完子 agent 之后）

| 文件 | episode | 为什么隔离 |
|---|---|---|
| `guard_lock_contention_20260922_rev3_selfheal.json` | `ep_lock_contention_eval_v1_1790070226` | 干净（infra=0、子 agent 正常），但 `claimed=None`、零 SQL 却 `O=True`：场景 revision 3 的持锁 `duration_s=900` 在 19 分钟的 episode 里自动回滚，打分时补采的 KPI 已恢复。场景已改为 revision 4（`duration_s=7200`），`scoring.py` 也已规定"无干预不得 Outcome"，这份要在新定义下重跑 |
