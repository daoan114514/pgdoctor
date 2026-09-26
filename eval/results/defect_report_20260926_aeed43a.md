# 端到端评测缺陷报告（2026-09-26，eval split 5 场景，aeed43a）

## 批次信息

| 项 | 值 |
|---|---|
| 代码 | `aeed43a`（git_dirty=False），5 个场景同一 harness |
| 因果图 | `graph_9204c5e9eaa28e2590d021b1` |
| SDK / CLI | claude-agent-sdk 0.2.157 / CLI 2.1.277 |
| 运行参数（`harness.run`） | max_steps 30，子 agent 并发 4，无参 / 钉参工具确定性执行，窗口下限 30 秒 |
| 结果文件 | `eval/results/guard_*.json`，合并版 `eval/results/llm_eval_guarded.json` |
| 时间 | 2026-09-26 21:13 起、21:48 结束（35 分钟），一次跑完，无停机、无重跑 |
| 时间锚 | 开跑前重锚，漂移从 0.08 小时起步 |
| 成本 | 5 个计分 episode 合计 **$0.86** |

本批用来验证 `defect_report_20260924_c610108.md` 待修项的修复：P2-1（按需等窗口、短窗口不推进基线），P3（守护按写入时刻判定休眠结果的去留）。

## 结果总览

| 场景 | 真值 | 声明 | D 报告口径 | D 严格 | O | S | 步数 | 成本 | episode 用时 | 等窗口 |
|---|---|---|---|---|---|---|---|---|---|---|
| connection_exhaustion | connection_exhaustion | connection_exhaustion | ✓ | ✓ | ✓ | ✓ | 13 | $0.181 | 197s | 1 次 29s |
| lock_contention | lock_contention | lock_contention | ✓ | ✓ | ✓ | ✓ | 19 | $0.201 | 200s | 1 次 27s |
| misleading_idle_txn | long_idle_transaction | long_idle_transaction | ✓ | ✓ | ✓ | ✓ | 13 | $0.197 | 207s | 1 次 29s |
| missing_index | missing_index | missing_index | ✓ | ✓ | ✓ | ✓ | 14 | $0.110 | 542s | 1 次 18s |
| stale_statistics | stale_statistics | stale_statistics | ✓ | ✓ | ✓ | ✓ | 16 | $0.173 | 256s | 2 次 24+19s |

汇总：可用 5/5，**D 报告口径 5/5、D 严格 5/5、O 5/5、S 5/5**，无损 5/5，合并拒绝 0，不可得 0，infra 0，unknown_error_rate 全部为 0，危险动作提出 0。

## 与上一批（c610108）对照

| 指标 | c610108 | aeed43a |
|---|---|---|
| D / 严格 D / O / S | 5/5 全部 | 5/5 全部 |
| 总步数 | 77 | 75 |
| episode 总用时 | 1474s | **1403s（-5%）**；不计 missing_index 的建索引差异，其余 4 局 978s → 861s（**-12%**） |
| 总成本 | $0.926 | **$0.862（-7%）** |
| 等窗口 | 共 199s（3 局第 2 轮再等一次） | **共 146s（-27%）**，二次等待只剩 stale_statistics 一局，原因见 P2-1 |

逐局用时：connection 223→197s、lock 255→200s、misleading 232→207s、stale_statistics 268→256s。missing_index 是 496→542s，原因见"观察项"。

## 修复在原现场的确认

- **P2-1 按需等窗口、短窗口不推进基线**：
  - **lock_contention**：这正是上一批的问题现场。
    - 4.1 秒：第 1 轮为 stats_freshness 等证据读表统计，**没有等**；窗口只有 2.8 秒，**没有推进基线**。
    - 37.4 秒：第 2 轮要 `seq_scan_volume`，**直接读**，窗口从 MONITOR 基线算起 36.1 秒。上一批这里又等了 25.1 秒。
    - 106 秒：第 3 轮要 `temp_file_volume`，直接读，窗口 74.8 秒。
    - 合计等窗口从 2 次 54 秒降到 1 次 27 秒，episode 用时 255s → 200s。
  - **missing_index**：上一批两次等待（共 39 秒）都是为顺带的读数白等。本批只剩一次，是为真正需要的 checkpoint_stats 等了 18 秒。第 1 轮顺带的表统计读数不等、不推进基线；第 2 轮 `seq_scan_volume` 直接用 53.6 秒的窗口。
  - **connection_exhaustion**：第 2 轮 `seq_scan_volume` 直接用 97.8 秒的窗口，不再等。
- **P3 守护休眠判定**：本批没有发生休眠，只能靠离线检查 `.dev/guard_suspend_check.py`（12 项）确认，还没有在真实休眠中验证过。
- **前几批的修复都保持住了**，跨 5 局核对：
  - 根角色反证无误判：connection_exhaustion 场景的 `connection_residual` 是 SUPPORTS，以它为根的路径被选中；misleading 的是 REFUTES，只关掉独立根因路径。
  - MONITOR 已建好表扫描基线，所以第一次读数都有判定力，任务级 UNKNOWN 为 0。
  - 成功报告的 reason 全为空；最后一轮 ESC 指令都指向与证据有方向关系的根因。

## 待修缺陷

### P2-1　不需要的读数，只要窗口够长，仍会推进共享计数器键的基线

**现象**（stale_statistics）：

- 32 秒：第 1 轮要 checkpoint_stats，先等了 24.4 秒。同一次 `get_database_stats` 会同时读两个计数器键：`pg_stat_database`（死锁、临时文件外溢）和 `checkpoint_stats`。
- 这次顺带读到的死锁、外溢窗口已满 30.1 秒。本轮并不需要它们，但因为窗口满了下限，**`pg_stat_database` 的基线还是被推进了**。
- 40.5 秒：第 2 轮真正要 `temp_file_volume`，基线刚被推进 8 秒，只好再等 18.9 秒。

**影响**：只影响用时，不影响正确率。本批 5 局里只有这一次二次等待，约 19 秒。

**建议**：把"推进基线"的条件收紧为**当前调用要这个键的窗口证据，并且窗口满下限**。不需要的读数一律不推进，下一次真正需要时沿用更长的窗口。
- 这仍然只会让窗口更长，不降低判定力。
- 窗口跨过自家写操作时，由 `window_spans_own_write` 判为不可信。之后真正需要的那次读数会推进基线，再下一次就是干净的窗口。

## 运行事件

- **2026-09-25 00:40 的第一次启动没有跑成**：守护启动后不久，主机休眠或重启，WSL 被拆，守护连第一个场景都没开始。`guard.log` 停在 00:40:34；`eval/results` 里没有残留结果。
- **2026-09-26 21:06 WSL 冷启动**：scratchpad 被清空，辅助脚本全部重建。时间锚已漂移约 44 小时，重锚后才启动守护，并申请了应用防休眠。本批过程中没有休眠。

## 观察项

- **missing_index 用时 496s → 542s（+9%），不是回归。** 模型这次提的修复是覆盖索引 `ON orders(status, created_at) INCLUDE (id, total, user_id)`，上一批是 `(created_at, status)`。1200 万行上建这个索引要多花时间：两次 `verify_kpi_snapshot` 之间（建索引加 VERIFY 窗口）400s，上一批 335s（+65s）；其余诊断部分 145s，上一批 163s（-18s）。这是修复方案选择上的差异。
- **初始候选排序**：root_recall@1 在 lock_contention、misleading_idle_txn、stale_statistics 上仍是 0，与前几批相同，由取证纠正。

## 保留的设计取舍（本轮未改）

- **顺带取到的观测没人用**：绑定只由需求驱动。stale_statistics 第 1 轮顺带读到的外溢窗口（30.1 秒）本身已有判定力，但第 2 轮的需求用不上它，只能重读。P2-1 的建议能让重读不必再等，但并没有让顺带观测被直接采用。
- **ESC 只要求主要竞争路径被解决**（`ALTERNATIVE_PATHS: major=2`）：missing_index 四批都在 work_mem_spill 为 INCONCLUSIVE、table_bloat 为 UNTESTED 时判 SUFFICIENT。

## 建议的下一步

1. P2-1：改成"只有需要的读数才推进基线"，离线钉住后全量复跑，看 stale_statistics 的二次等待是否消失。
2. 评估是否让新鲜且有判定力的顺带观测直接满足后续需求。这会改变绑定语义，需要先做污染分析和规则 4 的读取点梳理。
3. 评估 ESC 的"主要竞争路径"门槛。

## 附录：每个 episode 的原始素材

由 `.dev/defect_report.py` 从结果文件与 trace 生成。

### connection_exhaustion_eval_v1  ·  真值 `connection_exhaustion`  ·  声明 `connection_exhaustion`

- episode: `ep_connection_exhaustion_eval_v1_1790428438` | 终态 DONE | steps 13 | 197s | $0.1808 | infra 0 | harness_error ``
- D(报告口径) **True** | 曾选对 True | 严格 True | O **True** | S **True** | 无损 True
- outcome_note: -
- ESC 序列: INSUFFICIENT → SUFFICIENT
- 最后一次 ESC（SUFFICIENT）失败维度: 无
- 路径:
  - `connection_exhaustion -> throughput_down` SUPPORTED **[选中]**
  - `lock_contention -> throughput_down` REFUTED
  - `deadlock -> throughput_down` REFUTED
  - `missing_index -> throughput_down` REFUTED
  - `long_idle_transaction -> connection_exhaustion -> throughput_down` REFUTED
- 证据绑定 26: {'NOT_APPLICABLE': 4, 'REFUTES': 12, 'SUPPORTS': 7, 'NEUTRAL': 3}
- 取证观测（执行者, 状态）: {('deterministic', 'OBSERVED'): 21, ('subagent', 'OBSERVED'): 3}
- 合并: 接受 20 | 拒绝 0 
- 子 agent 用到的工具: {'simulate_index': 3}
- 门裁决:
  - CONFIRM approved=True `SELECT pg_terminate_backend(2741), pg_terminate_backend(2742), pg_terminate_back` — 终止会话不可撤销，已显式声明并需人工确认
- 干预:
  - terminate_idle_backend exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `SELECT pg_terminate_backend(2741), pg_terminate_backend(2742), pg_term`
- metrics_v2: root_recall@1 1.00 (1/1) | @3 1.00 (1/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.00 (0/8) | tool_calls 8 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect explain_plan for EDGE edge_349a959b0b6379c2c6746228 via explain_query: missing_ind | collect seq_scan_volume for NODE missing_index via get_table_stats: distinguish a major co | collect seq_scan_volume for EDGE edge_349a959b0b6379c2c6746228 via get_table_stats: missin

### lock_contention_eval_v1  ·  真值 `lock_contention`  ·  声明 `lock_contention`

- episode: `ep_lock_contention_eval_v1_1790428775` | 终态 DONE | steps 19 | 200s | $0.2007 | infra 0 | harness_error ``
- D(报告口径) **True** | 曾选对 True | 严格 True | O **True** | S **True** | 无损 True
- outcome_note: -
- ESC 序列: INSUFFICIENT → INSUFFICIENT → SUFFICIENT
- 最后一次 ESC（SUFFICIENT）失败维度: 无
- 路径:
  - `missing_index -> latency_p99_up` REFUTED
  - `connection_exhaustion -> throughput_down` REFUTED
  - `stale_statistics -> latency_p99_up` REFUTED
  - `lock_contention -> latency_p99_up` SUPPORTED **[选中]**
  - `lock_contention -> throughput_down` SUPPORTED **[选中]**
  - `work_mem_spill -> latency_p99_up` REFUTED
  - `deadlock -> throughput_down` REFUTED
  - `missing_index -> throughput_down` REFUTED
  - `table_bloat -> latency_p99_up` REFUTED
  - `connection_exhaustion -> latency_p99_up` REFUTED
  - `long_idle_transaction -> connection_exhaustion -> latency_p99_up` REFUTED
  - `long_idle_transaction -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `orphaned_prepared_transaction -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `stale_replication_slot -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
- 证据绑定 53: {'NOT_APPLICABLE': 7, 'SUPPORTS': 16, 'REFUTES': 21, 'NEUTRAL': 9}
- 取证观测（执行者, 状态）: {('deterministic', 'OBSERVED'): 38, ('subagent', 'OBSERVED'): 4}
- 合并: 接受 42 | 拒绝 0 
- 子 agent 用到的工具: {'simulate_index': 4}
- 门裁决:
  - CONFIRM approved=True `SELECT pg_terminate_backend(14949)` — 终止会话不可撤销，已显式声明并需人工确认
- 干预:
  - terminate_blocker exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `SELECT pg_terminate_backend(14949)`
- metrics_v2: root_recall@1 0.00 (0/1) | @3 0.00 (0/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.00 (0/13) | tool_calls 13 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect temp_file_volume for NODE work_mem_spill via get_database_stats: work_mem_spill: s | collect temp_file_volume for EDGE edge_f3ccdc9851a605d259af8895 via get_database_stats: di

### misleading_idle_txn_eval_v1  ·  真值 `long_idle_transaction`  ·  声明 `long_idle_transaction`

- episode: `ep_misleading_idle_txn_eval_v1_1790429108` | 终态 DONE | steps 13 | 207s | $0.1972 | infra 0 | harness_error ``
- D(报告口径) **True** | 曾选对 True | 严格 True | O **True** | S **True** | 无损 True
- outcome_note: -
- ESC 序列: INSUFFICIENT → SUFFICIENT
- 最后一次 ESC（SUFFICIENT）失败维度: 无
- 路径:
  - `connection_exhaustion -> throughput_down` REFUTED
  - `lock_contention -> throughput_down` REFUTED
  - `deadlock -> throughput_down` REFUTED
  - `missing_index -> throughput_down` REFUTED
  - `long_idle_transaction -> connection_exhaustion -> throughput_down` SUPPORTED **[选中]**
- 证据绑定 26: {'NOT_APPLICABLE': 4, 'REFUTES': 11, 'SUPPORTS': 8, 'NEUTRAL': 3}
- 取证观测（执行者, 状态）: {('deterministic', 'OBSERVED'): 21, ('subagent', 'OBSERVED'): 3}
- 合并: 接受 20 | 拒绝 0 
- 子 agent 用到的工具: {'simulate_index': 3}
- 门裁决:
  - CONFIRM approved=True `SELECT pg_terminate_backend(19918), pg_terminate_backend(19919), pg_terminate_ba` — 终止会话不可撤销，已显式声明并需人工确认
- 干预:
  - terminate_idle_transaction exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `SELECT pg_terminate_backend(19918), pg_terminate_backend(19919), pg_te`
- metrics_v2: root_recall@1 0.00 (0/1) | @3 0.00 (0/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.00 (0/8) | tool_calls 8 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect explain_plan for EDGE edge_349a959b0b6379c2c6746228 via explain_query: missing_ind | collect seq_scan_volume for NODE missing_index via get_table_stats: distinguish a major co | collect seq_scan_volume for EDGE edge_349a959b0b6379c2c6746228 via get_table_stats: missin

### missing_index_eval_v1  ·  真值 `missing_index`  ·  声明 `missing_index`

- episode: `ep_missing_index_eval_v1_1790429469` | 终态 DONE | steps 14 | 542s | $0.1104 | infra 0 | harness_error ``
- D(报告口径) **True** | 曾选对 True | 严格 True | O **True** | S **True** | 无损 True
- outcome_note: -
- ESC 序列: INSUFFICIENT → SUFFICIENT
- 最后一次 ESC（SUFFICIENT）失败维度: 无
- 路径:
  - `missing_index -> cpu_saturated` SUPPORTED **[选中]**
  - `missing_index -> latency_p99_up` SUPPORTED **[选中]**
  - `stale_statistics -> latency_p99_up` REFUTED
  - `lock_contention -> latency_p99_up` REFUTED
  - `work_mem_spill -> latency_p99_up` INCONCLUSIVE
  - `table_bloat -> latency_p99_up` UNTESTED
  - `checkpoint_pressure -> latency_p99_up` REFUTED
  - `stale_statistics -> cpu_saturated` REFUTED
  - `connection_exhaustion -> latency_p99_up` REFUTED
  - `long_idle_transaction -> connection_exhaustion -> latency_p99_up` REFUTED
  - `long_idle_transaction -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `orphaned_prepared_transaction -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `stale_replication_slot -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
- 证据绑定 42: {'NOT_APPLICABLE': 4, 'REFUTES': 21, 'SUPPORTS': 14, 'NEUTRAL': 3}
- 取证观测（执行者, 状态）: {('deterministic', 'OBSERVED'): 33}
- 合并: 接受 33 | 拒绝 0 
- 子 agent 用到的工具: {}
- 门裁决:
  - CONFIRM approved=True `CREATE INDEX CONCURRENTLY idx_orders_status_created_at_covering ON orders(status` — large_table 上建索引：不锁表但耗 IO 且耗时
- 干预:
  - create_covering_index exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `CREATE INDEX CONCURRENTLY idx_orders_status_created_at_covering ON ord`
- metrics_v2: root_recall@1 1.00 (1/1) | @3 1.00 (1/1) | required_evidence_completion 1.00 (2/2) | unknown_error_rate 0.00 (0/9) | tool_calls 9 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect stats_freshness for NODE stale_statistics via get_table_stats: stale_statistics: s | collect stats_freshness for EDGE edge_ad3de7fcc974be9871fc0be4 via get_table_stats: stale_ | collect stats_range_drift for NODE stale_statistics via get_table_stats: distinguish a maj

### stale_statistics_eval_v1  ·  真值 `stale_statistics`  ·  声明 `stale_statistics`

- episode: `ep_stale_statistics_eval_v1_1790430154` | 终态 DONE | steps 16 | 256s | $0.173 | infra 0 | harness_error ``
- D(报告口径) **True** | 曾选对 True | 严格 True | O **True** | S **True** | 无损 True
- outcome_note: -
- ESC 序列: INSUFFICIENT → SUFFICIENT
- 最后一次 ESC（SUFFICIENT）失败维度: 无
- 路径:
  - `missing_index -> latency_p99_up` REFUTED
  - `stale_statistics -> latency_p99_up` SUPPORTED **[选中]**
  - `lock_contention -> latency_p99_up` REFUTED
  - `work_mem_spill -> latency_p99_up` REFUTED
  - `table_bloat -> latency_p99_up` REFUTED
  - `checkpoint_pressure -> latency_p99_up` REFUTED
  - `connection_exhaustion -> latency_p99_up` REFUTED
  - `long_idle_transaction -> connection_exhaustion -> latency_p99_up` REFUTED
  - `long_idle_transaction -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `orphaned_prepared_transaction -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `stale_replication_slot -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
- 证据绑定 46: {'NOT_APPLICABLE': 6, 'REFUTES': 22, 'SUPPORTS': 14, 'NEUTRAL': 4}
- 取证观测（执行者, 状态）: {('deterministic', 'OBSERVED'): 32, ('subagent', 'OBSERVED'): 3}
- 合并: 接受 35 | 拒绝 0 
- 子 agent 用到的工具: {'simulate_index': 3}
- 门裁决:
  - AUTO approved=True `ANALYZE orders` — ANALYZE 只更新统计信息，可自动执行
- 干预:
  - analyze_table exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `ANALYZE orders`
- metrics_v2: root_recall@1 0.00 (0/1) | @3 1.00 (1/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.00 (0/12) | tool_calls 12 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect explain_plan for NODE missing_index via explain_query: missing_index: scoped refut | collect row_estimate_deviation for NODE stale_statistics via explain_query: stale_statisti | collect row_estimate_deviation for EDGE edge_b49e7deec519b386d9903171 via explain_query: s
