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

## 2026-09-22 晚（修完 collection_status 之后、修 raw_refs 之前）—— 整批 5 场景

| 文件 | 为什么隔离 |
|---|---|
| `guard_*_20260922_rawrefs.json`（5 个）+ `llm_eval_guarded_20260922_rawrefs.json` | 子 agent 把多条 `raw_refs` 写成 JSON 列表字符串，解析器只按分号切，合并层以 "raw_ref was not collected by this task" 整条拒掉：回扫会话记录，359 条 OBSERVED 报告里 242 条被静默丢弃。D 2/5、O 0/5、S 0/5 是在三分之二证据缺席下测的，不代表修好后的系统（828239b）。此外 b2694eb 之后安全门、评分口径、采集状态记账都变了，需在同一 harness 下重跑 |

守护只认 `eval/results/guard_*.json`，搬到这里之后 5 个场景会全部重跑。

## 2026-09-23 下午（ad03398，pid 前置条件修复之前）

| 文件 | 为什么隔离 |
|---|---|
| `guard_connection_exhaustion_20260923_prepidfix.json` | 诊断正确（D=True、ESC SUFFICIENT）但 O=False：模型在 PLAN 里正确调了 `get_active_sessions(include_idle=true)` 并提交 `pg_terminate_backend(<idle pid>)`，`create_intervention_plan` 却报四条 pid 前置条件不满足 —— PLAN 阶段的观测进不了绑定（绑定会 bump revision，与 GATE 的 ESC 同 revision 契约冲突），terminate 类修复历史上从未过门。修复后 5 个场景统一重跑 |

## 2026-09-23 下午（ffd6e53，证据方向修复之前）

| 文件 | 为什么隔离 |
|---|---|
| `guard_connection_exhaustion_20260923_ffd6e53.json` | pid 修复已生效：terminate 过门并执行，但单 pid 终止达不到"使用率 30s 内降 5%"（0.95→0.96），两次用尽升级，O=False。修复粒度与场景判据问题记入缺陷报告 |
| `guard_lock_contention_20260923_ffd6e53.json` | D=True 但 ESC INSUFFICIENT×4 到预算耗尽：missing_index 被自家 `_stats_range_drift` 全表扫污染成 seq_scan_volume SUPPORTS，且 index_existence/slow_query_ranking 这两条"采集即 SUPPORTS"的门与真正的反证混成 INCONCLUSIVE，永远无法反证；explain_seq_scan 需求在走索引时永远拿不到观测，被索取 18 次。四条修复后 5 个场景统一重跑 |

## 2026-09-23 晚（2461f73，PLAN 阶段观测进前置条件的通用修复之前）

| 文件 | 为什么隔离 |
|---|---|
| `guard_missing_index_20260923_2461f73.json` | D=True 严格 D=True，但 O=False：模型在 PLAN 里 simulate_index 三次（would_be_used=True）后提交同签名的 CREATE INDEX CONCURRENTLY，四次被 "concrete_index_definition_bound, counterfactual_index_v2" 拒 —— 与 pid 前置条件同一类：PLAN 阶段的观测进不了绑定。修复（_fresh_observations 通用化）后单场景重跑 |
