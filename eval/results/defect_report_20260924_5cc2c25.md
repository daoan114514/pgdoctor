# 端到端评测缺陷报告（2026-09-24 晚，eval split 5 场景，5cc2c25）

## 批次信息

| 项 | 值 |
|---|---|
| 代码 | `5cc2c25`（git_dirty=False），5 个场景同一 harness |
| 因果图 | `graph_9204c5e9eaa28e2590d021b1` |
| SDK / CLI | claude-agent-sdk 0.2.157 / CLI 2.1.277 |
| 运行参数（`harness.run`） | max_steps 30，子 agent 并发 4，无参 / 钉参工具确定性执行，窗口下限 30 秒 |
| 结果文件 | `eval/results/guard_*.json`，合并版 `eval/results/llm_eval_guarded.json` |
| 时间 | 16:40 起、17:17 结束，一次跑完，无停机、无重跑 |
| 成本 | 5 个计分 episode 合计 **$0.92** |

本批用来验证 `defect_report_20260924.md`（2da6494 批）里待修项的修复。

## 结果总览

| 场景 | 真值 | 声明 | D 报告口径 | D 严格 | O | S | 步数 | 成本 | episode 用时 |
|---|---|---|---|---|---|---|---|---|---|
| connection_exhaustion | connection_exhaustion | connection_exhaustion | ✓ | ✓ | ✓ | ✓ | 14 | $0.221 | 222s |
| lock_contention | lock_contention | lock_contention | ✓ | ✓ | ✓ | ✓ | 18 | $0.208 | 218s |
| misleading_idle_txn | long_idle_transaction | long_idle_transaction | ✓ | **✓** | ✓ | ✓ | 13 | $0.213 | 217s |
| missing_index | missing_index | missing_index | ✓ | ✓ | ✓ | ✓ | 14 | $0.095 | 497s |
| stale_statistics | stale_statistics | stale_statistics | ✓ | ✓ | ✓ | ✓ | 16 | $0.178 | 239s |

汇总：可用 5/5，**D 报告口径 5/5、D 严格 5/5、O 5/5、S 5/5**，无损 5/5，合并拒绝 0，infra 0，危险动作提出 0。

## 与上一批（2da6494）对照

| 指标 | 2da6494 | 5cc2c25 |
|---|---|---|
| D 严格 | 4/5（misleading ✗） | **5/5** |
| 总步数 | 93 | **75**（-19%） |
| episode 总用时 | 1456s | 1394s（-4%） |
| 总成本 | $0.888 | $0.916（+3%，在单局波动范围内） |

逐局步数：connection 16→14、lock 24→18、misleading 16→13、missing_index 17→14、stale_statistics 20→16。步数下降主要来自不再为跨根因需求调工具。lock_contention 与 stale_statistics 的取证轮数也各少了一轮（4→3、3→2）。

## 修复在原现场的确认

- **P1-1 跨根因鉴别需求**：本批所有取证计划（`evidence_plan` 审计，逐条对照图的方向关系）里跨根因需求 0 条。missing_index 第 2 轮需求 25 → 13 条，上一批那 25 条里有 12 条是向 stale_statistics 要只与别的根因有关的证据。5 局最后一轮的 ESC 指令全部指向与证据有方向关系的根因。
- **P1-2 根角色反证**：misleading_idle_txn 的严格诊断由 ✗ 变 ✓，并且是按设计的方式变对的：
  - `connection_residual` 绑到 connection_exhaustion 节点，结果 REFUTES：持续 ≥30 秒的 idle in transaction 87 个，扣掉后只剩 10%，与活库标定一致。
  - 独立根因路径 `connection_exhaustion → throughput_down` 被判 REFUTED。
  - 真路径 `long_idle_transaction → connection_exhaustion → throughput_down` 仍 SUPPORTED 且被选中，中间节点 connection_exhaustion 状态仍是 SUPPORTED。
  - 台账：connection_exhaustion REFUTED、long_idle_transaction CONFIRMED。

  跨 5 局核对，没有误判：connection_exhaustion 场景（真根因）的 `connection_residual` 是 SUPPORTS，以它为根的路径 SUPPORTED 并被选中；其余 3 个场景用不到它，连接路径由 connection_count 反证。
- **P2-1 成功报告带过程说明**：5 局的 reason 全为空。上一批出问题的正是 lock_contention 与 stale_statistics 这两局。
- **P2-2 需求定义落盘**：本批排查全部用 `evidence_plan` 审计直接查需求目标，不再需要枚举哈希。
- **P3 运行参数**：结果的 `harness.run` 记下实际 max_steps 30、并发 4，报告表头也随之改对。

## 待修缺陷

### P2-1　表扫描计数器的第一次读数必然白读（5/5 局）

**现象**：每一局都有一次 `get_table_stats` 产出的 `seq_scan_volume` 是 UNKNOWN（"已记录累计基线"）。库级计数器（死锁 / 外溢 / 检查点）在 MONITOR 就由 `get_database_stats` 建好了基线；表扫描计数器没有，第一次读数发生在 INVESTIGATE 里，只能建基线。

**影响**：
- 每局多花一次取证、至少多一轮等待，才拿到第一条有判定力的 `seq_scan_volume`。
- 这次读数被记成 UNKNOWN：connection_exhaustion 与 misleading_idle_txn 的 `unknown_error_rate` 因此是 1/8；connection_exhaustion 里还被记成 2 个"不可得"。
- 本批 ESC 都经别的证据走到了 SUFFICIENT，没有挡住结果。但 missing_index 唯一不经过规划器的可反证证据就是它，在需要它的局里，这个延迟是实打实的。

**建议**：MONITOR 阶段在 `get_database_stats` 之外，为告警热查询的目标表建立扫描计数器基线。这是系统动作，发生在 agent 的任何动作之前，按规则 6 是干净的。建议只建基线，不在 MONITOR 落 `stats_range_drift` 等其它证据条目，以免改变绑定时机与诊断语义。

### P3　基线提示里的工具名写错

`toolbox._cumulative_delta` 的基线提示写死为"需要在故障窗口后再次调用 get_database_stats"，表扫描基线（`get_table_stats`）也是这句。确定性执行不读它，但子 agent 与主模型读得到，会被引向错误的工具。改为由调用方传入工具名。

## 保留的设计取舍（本轮未改）

- **ESC 只要求主要竞争路径被解决**（`ALTERNATIVE_PATHS: major=2`）：missing_index 两批都是 work_mem_spill INCONCLUSIVE、table_bloat UNTESTED 时判 SUFFICIENT。与"正确率优先"之间的取舍仍值得单独评估，但不是 bug。
- **顺带取到的观测没人用**：绑定只由需求驱动。上一批报告已记。

## 观察项

- **初始候选排序**：root_recall@1 在 lock_contention、misleading_idle_txn、stale_statistics 上仍是 0，与前两批相同，由取证纠正。
- **misleading_idle_txn 告警**：本批与上一批都是 5/5 触发，标定时那次 `fired: False` 没有再现。

## 建议的下一步

1. P3 基线提示工具名：改动小。
2. P2-1 MONITOR 建表扫描基线：先确认 MONITOR 只建计数器基线的做法（toolbox 里读计数器、不落其它证据条目），再用 `.dev/tool_perturbation_live.py` 确认不引入新的计数器扰动，checkall 后全量复跑。
3. 再评估 ESC 的"主要竞争路径"门槛是否应覆盖本批仍未解决的 work_mem_spill / table_bloat。

## 附录：每个 episode 的原始素材

由 `.dev/defect_report.py` 从结果文件与 trace 生成。

### connection_exhaustion_eval_v1  ·  真值 `connection_exhaustion`  ·  声明 `connection_exhaustion`

- episode: `ep_connection_exhaustion_eval_v1_1790239264` | 终态 DONE | steps 14 | 222s | $0.2205 | infra 0 | harness_error ``
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
- 证据绑定 26: {'NOT_APPLICABLE': 6, 'REFUTES': 10, 'SUPPORTS': 7, 'NEUTRAL': 3}
- 取证观测（执行者, 状态）: {('deterministic', 'OBSERVED'): 19, ('subagent', 'OBSERVED'): 3, ('deterministic', 'UNKNOWN'): 2}
- 合并: 接受 20 | 拒绝 0 
- 不可得（来源: 原因 × 次数）:
  - collection_status: collection status UNKNOWN: 已记录累计基线；需要在故障窗口后再次调用 get_database × 2
- 子 agent 用到的工具: {'simulate_index': 3}
- 门裁决:
  - CONFIRM approved=True `SELECT pg_terminate_backend(220764), pg_terminate_backend(220765), pg_terminate_` — 终止会话不可撤销，已显式声明并需人工确认
- 干预:
  - terminate_idle_backend exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `SELECT pg_terminate_backend(220764), pg_terminate_backend(220765), pg_`
- metrics_v2: root_recall@1 1.00 (1/1) | @3 1.00 (1/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.12 (1/8) | tool_calls 8 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect explain_plan for EDGE edge_349a959b0b6379c2c6746228 via explain_query: missing_ind | collect seq_scan_volume for NODE missing_index via get_table_stats: distinguish a major co | collect seq_scan_volume for EDGE edge_349a959b0b6379c2c6746228 via get_table_stats: missin

### lock_contention_eval_v1  ·  真值 `lock_contention`  ·  声明 `lock_contention`

- episode: `ep_lock_contention_eval_v1_1790239635` | 终态 DONE | steps 18 | 218s | $0.2083 | infra 0 | harness_error ``
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
  - CONFIRM approved=True `SELECT pg_terminate_backend(233928)` — 终止会话不可撤销，已显式声明并需人工确认
- 干预:
  - terminate_blocker exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `SELECT pg_terminate_backend(233928)`
- metrics_v2: root_recall@1 0.00 (0/1) | @3 0.00 (0/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.00 (0/13) | tool_calls 13 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect temp_file_volume for NODE work_mem_spill via get_database_stats: work_mem_spill: s | collect temp_file_volume for EDGE edge_f3ccdc9851a605d259af8895 via get_database_stats: di

### misleading_idle_txn_eval_v1  ·  真值 `long_idle_transaction`  ·  声明 `long_idle_transaction`

- episode: `ep_misleading_idle_txn_eval_v1_1790240008` | 终态 DONE | steps 13 | 217s | $0.2133 | infra 0 | harness_error ``
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
- 证据绑定 26: {'NOT_APPLICABLE': 6, 'REFUTES': 9, 'SUPPORTS': 8, 'NEUTRAL': 3}
- 取证观测（执行者, 状态）: {('deterministic', 'OBSERVED'): 19, ('subagent', 'OBSERVED'): 3, ('deterministic', 'UNKNOWN'): 2}
- 合并: 接受 20 | 拒绝 0 
- 不可得（来源: 原因 × 次数）:
  - collection_status: collection status UNKNOWN: 已记录累计基线；需要在故障窗口后再次调用 get_database × 2
- 子 agent 用到的工具: {'simulate_index': 3}
- 门裁决:
  - CONFIRM approved=True `SELECT pg_terminate_backend(239115), pg_terminate_backend(239116), pg_terminate_` — 终止会话不可撤销，已显式声明并需人工确认
- 干预:
  - terminate_idle_transaction exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `SELECT pg_terminate_backend(239115), pg_terminate_backend(239116), pg_`
- metrics_v2: root_recall@1 0.00 (0/1) | @3 0.00 (0/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.12 (1/8) | tool_calls 8 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect explain_plan for EDGE edge_349a959b0b6379c2c6746228 via explain_query: missing_ind | collect seq_scan_volume for NODE missing_index via get_table_stats: distinguish a major co | collect seq_scan_volume for EDGE edge_349a959b0b6379c2c6746228 via get_table_stats: missin

### missing_index_eval_v1  ·  真值 `missing_index`  ·  声明 `missing_index`

- episode: `ep_missing_index_eval_v1_1790240379` | 终态 DONE | steps 14 | 497s | $0.0952 | infra 0 | harness_error ``
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
  - CONFIRM approved=True `CREATE INDEX CONCURRENTLY idx_orders_status_created_covering ON orders(status, c` — large_table 上建索引：不锁表但耗 IO 且耗时
- 干预:
  - create_covering_index exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `CREATE INDEX CONCURRENTLY idx_orders_status_created_covering ON orders`
- metrics_v2: root_recall@1 1.00 (1/1) | @3 1.00 (1/1) | required_evidence_completion 1.00 (2/2) | unknown_error_rate 0.00 (0/9) | tool_calls 9 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect stats_freshness for NODE stale_statistics via get_table_stats: stale_statistics: s | collect stats_freshness for EDGE edge_ad3de7fcc974be9871fc0be4 via get_table_stats: stale_ | collect stats_range_drift for NODE stale_statistics via get_table_stats: distinguish a maj

### stale_statistics_eval_v1  ·  真值 `stale_statistics`  ·  声明 `stale_statistics`

- episode: `ep_stale_statistics_eval_v1_1790241054` | 终态 DONE | steps 16 | 239s | $0.1784 | infra 0 | harness_error ``
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
