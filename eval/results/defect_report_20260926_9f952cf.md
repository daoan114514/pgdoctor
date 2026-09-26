# 端到端评测缺陷报告（2026-09-26，eval split 5 场景，9f952cf）

## 批次信息

| 项 | 值 |
|---|---|
| 代码 | `9f952cf`（git_dirty=False），5 个场景同一 harness |
| 因果图 | `graph_9204c5e9eaa28e2590d021b1` |
| SDK / CLI | claude-agent-sdk 0.2.157 / CLI 2.1.277 |
| 运行参数（`harness.run`） | max_steps 30，子 agent 并发 4，无参 / 钉参工具确定性执行，窗口下限 30 秒 |
| 结果文件 | `eval/results/guard_*.json`，合并版 `eval/results/llm_eval_guarded.json` |
| 时间 | 2026-09-26 23:37 起、2026-09-27 00:11 结束（34 分钟），一次跑完，无停机、无重跑 |
| 时间锚 | 开跑前重锚，漂移从 0.06 小时起步，最后一个场景开跑时 0.54 小时 |
| 成本 | 5 个计分 episode 合计 **$0.90** |

本批用来验证 `defect_report_20260926_aeed43a.md` 的待修项 P2-1：不需要的计数器键，即使窗口满了也不推进基线。

## 结果总览

| 场景 | 真值 | 声明 | D 报告口径 | D 严格（F1） | O | S | 步数 | 成本 | episode 用时 | 等窗口 |
|---|---|---|---|---|---|---|---|---|---|---|
| connection_exhaustion | connection_exhaustion | connection_exhaustion | ✓ | ✓（1.00） | ✓ | ✓ | 13 | $0.202 | 210s | 1 次 29s |
| lock_contention | lock_contention | lock_contention | ✓ | ✓（1.00） | ✓ | ✓ | 18 | $0.200 | 214s | 1 次 27s |
| misleading_idle_txn | long_idle_transaction | long_idle_transaction | ✓ | ✓（1.00） | ✓ | ✓ | 13 | $0.214 | 234s | 1 次 29s |
| missing_index | missing_index | missing_index | ✓ | ✓（**0.80**，踩线） | ✓ | ✓ | 17 | $0.115 | 510s | 1 次 16s |
| stale_statistics | stale_statistics | stale_statistics | ✓ | ✓（1.00） | ✓ | ✓ | 16 | $0.166 | 225s | 1 次 26s |

汇总：可用 5/5，**D 报告口径 5/5、D 严格 5/5、O 5/5、S 5/5**，无损 5/5，合并拒绝 0，不可得 0，infra 0，unknown_error_rate 全部为 0，安全门拒绝 0。

严格诊断的 5/5 里，missing_index 的 F1 恰好等于门槛 0.8：场景列出的竞争假设 lock_contention 没有被排除，只关掉了它两条路径中的一条。原因与复发规律见 P2-2。

## 与上一批（aeed43a）对照

| 指标 | aeed43a | 9f952cf |
|---|---|---|
| D / 严格 D / O / S | 5/5 全部 | 5/5 全部（missing_index 严格 F1 从 1.00 降到 0.80，见 P2-2） |
| 总步数 | 75 | 77 |
| episode 总用时 | 1403s | 1394s（-1%） |
| 总成本 | $0.862 | $0.898（+4%） |
| 等窗口 | 共 146s，stale_statistics 等了两次 | **共 126s（-14%）**，每局只等一次 |

逐局用时：connection 197→210s、lock 200→214s、misleading 207→234s、missing_index 542→510s、stale_statistics 256→225s。

- **connection / lock / misleading 各慢了 13–27 秒，与本批改动无关。** 按 step 文件写入时刻逐步对照两批：
  - 确定性阶段（MONITOR 到第 1 轮取证结束，约 32 秒）两批相差不到 0.5 秒，等窗口时长也一样。
  - 多出来的时间全部落在模型调用上：取证子 agent 的 `simulate_index`（三局分别 +3.6、+5.4、+9.3 秒），以及 PLAN 阶段写方案。misleading 从第 2 轮开始到结束，两批分别是 99.9 秒和 121.2 秒，那一段主要是模型写 64 个 pid 的终止语句；两次 VERIFY 快照之间的窗口两批都约 37.6 秒。
- **missing_index 542→510s**：模型这次提的是 `(created_at, status)` 索引，上一批是带 `INCLUDE` 的覆盖索引，建得更快。步数 14→17 是因为这次多观测到 `throughput_down`，候选路径从 14 条变成 15 条。
- **stale_statistics 256→225s**：少等的 17.7 秒正是 P2-1 修掉的二次等待。

## 修复在原现场的确认

- **P2-1 不需要的键不推进基线**：
  - **stale_statistics**：这正是上一批的问题现场。
    - 3.7 秒：第 1 轮计划只要 `checkpoint_stats`（checkpoint_pressure）。
    - 32.1 秒：为它等了 25.6 秒。同一次 `get_database_stats` 顺带读到死锁、外溢，窗口 30.1 秒，本轮不需要，**没有推进 `pg_stat_database` 的基线**；`checkpoint_stats` 推进了。
    - 38.9 秒：第 2 轮要 `temp_file_volume` 与 `seq_scan_volume`。
    - 41.8 秒：**直接读**，外溢窗口从 MONITOR 基线算起 39.8 秒，顺序扫描窗口 39.7 秒，都推进了基线。上一批这里又等了 18.9 秒。
    - 合计等窗口从 2 次 43.3 秒降到 1 次 25.6 秒，episode 用时 256s → 225s。
  - **其余 4 局行为一致**：不需要的键一律不推进（connection / misleading / lock / missing_index 里的 `checkpoint_stats`，missing_index 里两次顺带的 `seq_scan_volume`）；需要的键照常推进（lock_contention 第 2 轮 `seq_scan_volume` 36.7 秒、第 3 轮 `temp_file_volume` 89.2 秒）。没有新增等待。
- **前几批的修复都保持住了**，跨 5 局核对：
  - 根角色反证方向正确：connection_exhaustion 场景扣掉长事务后连接仍占 97%，`connection_residual` 判 SUPPORTS，以连接打满为根的路径被选中；misleading 场景 87 个长事务会话、扣掉后只剩 10%，判 REFUTES，只关掉以连接打满为根的路径，长事务路径被选中。
  - 第一次读数都有判定力，任务级 UNKNOWN 为 0；成功报告的 reason 全为空。
  - 安全门裁决：终止会话与大表建索引为 CONFIRM，`ANALYZE` 为 AUTO，全部放行、无拒绝。

## 待修缺陷

### P2-2　阻塞链反证只关掉被取证的那条边：症状不止一个时，lock_contention 只被部分排除

**现象**（missing_index）：

- 这一局观测到三个症状：`cpu_saturated`、`latency_p99_up`、`throughput_down`。以 lock_contention 为根的路径有两条：`→ latency_p99_up` 和 `→ throughput_down`。
- ESC 第 1 轮认定的主要竞争路径有 4 条（`major=4`）。规则是：每条选中路径下，保留第一竞争者、已被支持的竞争者，以及得分不低于选中路径得分 × 0.75 的竞争者（`agent/esc.py::_major_alternatives`）。
  - "吞吐下降"下，选中路径 `missing_index → throughput_down` 得分 0.875，门槛 0.656；`lock_contention → throughput_down` 得分 0.975，过线，入选。
  - "延迟"下，选中路径得分 1.462，门槛 1.097；`lock_contention → latency_p99_up` 得分 1.075，差 0.022 没过线，也不是第一竞争者（第一是 `stale_statistics → latency_p99_up`，1.238），没有入选。
- 于是第 2 轮计划只为吞吐那条边和 lock_contention 节点要了 `lock_blocking_chain`。两条绑定都是 REFUTES（"阻塞链为空"）。
- 但图上 `lock_blocking_chain` 的 REFUTED_BY 作用域是 **PATH**（`edges.yaml` 第 144–146 行）：
  - 落在边上的那条关掉了 `→ throughput_down`；
  - 落在节点上的那条没有反证效力；
  - `session_wait_profile` 对 lock_contention 只是 supporting，它的 REFUTES 也不起作用。
- 结果：`lock_contention → latency_p99_up` 停在 INCONCLUSIVE（报告里列在 open_branches），节点与台账都是 INCONCLUSIVE。严格诊断的召回是 2/3，F1 = 0.80，恰好等于门槛。

**复发规律**：翻了全部 missing_index 历史 trace。自 09-23 起，凡是观测到三个症状的局都是这样：09-23 两局、09-24 18:28 一局、本批，F1 都是 0.80。只观测到两个症状（没有 `throughput_down`）的局，锁路径只有一条，F1 都是 1.00。是否出现 `throughput_down` 取决于负载的吞吐波动，所以这不是偶发，而是大约一半的 missing_index 局都会踩线。

**影响**：
- 不影响诊断、修复与安全。
- 严格诊断在这类局里没有余量：只要再少排除一个竞争假设就会不及格。
- 同一次"阻塞链为空"的观测，关掉了一条路径、却关不掉另一条，报告里多挂一条本该能关掉的 open branch。

**建议**：把 `lock_blocking_chain` 的 REFUTED_BY 作用域从 PATH 改为 NODE。理由和 2026-09-23 把 `explain_plan` 改成 NODE 相同："阻塞链为空"否定的是 lock_contention 本身，和它连到哪个症状无关。改之前要做：

- `graph_lint`：接地覆盖不变；确认 `lock_blocking_chain` 的数值来源不会被某个根因污染（规则 2）。
- 查经过 lock_contention 作中间机制的路径会不会被连带关掉。语义上也该关：没有阻塞，锁争用这个机制就不存在。
- 重生成权威数据集（`graph_version` 会变）。
- 离线钉住：复现本局的三症状、两条锁路径、只有"节点 + 吞吐边"两条绑定的现场，要求两条路径都 REFUTED、台账 REFUTED。
- 全量复跑。

不建议的做法：放宽 ESC 的主要竞争路径门槛。那会让召回集一大就几乎要穷举所有路径，代价大，而且治标不治本。

## 运行事件

- 本批过程中没有休眠、没有停机、没有重跑。守护 23:37:10 启动，00:11:41 合并结果后正常退出。

## 保留的设计取舍（本轮未改）

- **顺带取到的观测没人用**：绑定只由需求驱动。P2-1 让重读不必再等，但顺带观测仍不会被直接采用。
- **ESC 只要求主要竞争路径被解决**（`major_alternative_score_ratio = 0.75`，外加每条选中路径下的第一竞争者与已被支持的竞争者）：missing_index 本批在 lock_contention → latency_p99_up、work_mem_spill 为 INCONCLUSIVE，table_bloat 为 UNTESTED 时判 SUFFICIENT。P2-2 的建议从证据作用域入手，不动这个门槛。

## 建议的下一步

1. P2-2：`lock_blocking_chain` 反证作用域改为 NODE，离线钉住后全量复跑，看 missing_index 在三症状局里的严格 F1 是否回到 1.00。
2. 扩充故障场景第一阶段（work_mem_spill、table_bloat、autovacuum_starvation），先定两个设计问题：work_mem 修复的作用范围（会话级 `SET` 对负载的长连接无效，全局配置又被护盾拦住）；table_bloat 场景怎样稳定地只有它一个根因。
3. 延续上一份报告：评估顺带观测能否直接满足后续需求；评估 ESC 的主要竞争路径门槛。

## 修复状态（复跑前）

| 缺陷 | 修法 | 验证 |
|---|---|---|
| P2-2 阻塞链反证只关掉被取证的那条边 | 待修 | 待修 |

## 附录：每个 episode 的原始素材

由 `.dev/defect_report.py` 从结果文件与 trace 生成。

### connection_exhaustion_eval_v1  ·  真值 `connection_exhaustion`  ·  声明 `connection_exhaustion`

- episode: `ep_connection_exhaustion_eval_v1_1790437061` | 终态 DONE | steps 13 | 210s | $0.2024 | infra 0 | harness_error ``
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
  - CONFIRM approved=True `SELECT pg_terminate_backend(2657), pg_terminate_backend(2658), pg_terminate_back` — 终止会话不可撤销，已显式声明并需人工确认
- 干预:
  - terminate_idle_backend exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `SELECT pg_terminate_backend(2657), pg_terminate_backend(2658), pg_term`
- metrics_v2: root_recall@1 1.00 (1/1) | @3 1.00 (1/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.00 (0/8) | tool_calls 8 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect explain_plan for EDGE edge_349a959b0b6379c2c6746228 via explain_query: missing_ind | collect seq_scan_volume for NODE missing_index via get_table_stats: distinguish a major co | collect seq_scan_volume for EDGE edge_349a959b0b6379c2c6746228 via get_table_stats: missin

### lock_contention_eval_v1  ·  真值 `lock_contention`  ·  声明 `lock_contention`

- episode: `ep_lock_contention_eval_v1_1790437408` | 终态 DONE | steps 18 | 214s | $0.2003 | infra 0 | harness_error ``
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
  - CONFIRM approved=True `SELECT pg_terminate_backend(15445)` — 终止会话不可撤销，已显式声明并需人工确认
- 干预:
  - terminate_blocker exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `SELECT pg_terminate_backend(15445)`
- metrics_v2: root_recall@1 0.00 (0/1) | @3 0.00 (0/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.00 (0/13) | tool_calls 13 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect temp_file_volume for NODE work_mem_spill via get_database_stats: work_mem_spill: s | collect temp_file_volume for EDGE edge_f3ccdc9851a605d259af8895 via get_database_stats: di

### misleading_idle_txn_eval_v1  ·  真值 `long_idle_transaction`  ·  声明 `long_idle_transaction`

- episode: `ep_misleading_idle_txn_eval_v1_1790437761` | 终态 DONE | steps 13 | 234s | $0.2143 | infra 0 | harness_error ``
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
  - CONFIRM approved=True `SELECT pg_terminate_backend(20633), pg_terminate_backend(20634), pg_terminate_ba` — 终止会话不可撤销，已显式声明并需人工确认
- 干预:
  - terminate_idle_transaction exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `SELECT pg_terminate_backend(20633), pg_terminate_backend(20634), pg_te`
- metrics_v2: root_recall@1 0.00 (0/1) | @3 0.00 (0/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.00 (0/8) | tool_calls 8 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect explain_plan for EDGE edge_349a959b0b6379c2c6746228 via explain_query: missing_ind | collect seq_scan_volume for NODE missing_index via get_table_stats: distinguish a major co | collect seq_scan_volume for EDGE edge_349a959b0b6379c2c6746228 via get_table_stats: missin

### missing_index_eval_v1  ·  真值 `missing_index`  ·  声明 `missing_index`

- episode: `ep_missing_index_eval_v1_1790438119` | 终态 DONE | steps 17 | 510s | $0.1153 | infra 0 | harness_error ``
- D(报告口径) **True** | 曾选对 True | 严格 True | O **True** | S **True** | 无损 True
- outcome_note: -
- ESC 序列: INSUFFICIENT → SUFFICIENT
- 最后一次 ESC（SUFFICIENT）失败维度: 无
- 路径:
  - `missing_index -> cpu_saturated` SUPPORTED **[选中]**
  - `missing_index -> latency_p99_up` SUPPORTED **[选中]**
  - `connection_exhaustion -> throughput_down` REFUTED
  - `stale_statistics -> latency_p99_up` REFUTED
  - `lock_contention -> latency_p99_up` INCONCLUSIVE
  - `lock_contention -> throughput_down` REFUTED
  - `work_mem_spill -> latency_p99_up` INCONCLUSIVE
  - `deadlock -> throughput_down` REFUTED
  - `missing_index -> throughput_down` SUPPORTED **[选中]**
  - `table_bloat -> latency_p99_up` UNTESTED
  - `long_idle_transaction -> connection_exhaustion -> latency_p99_up` REFUTED
  - `long_idle_transaction -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `orphaned_prepared_transaction -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `stale_replication_slot -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
- 证据绑定 43: {'NOT_APPLICABLE': 4, 'REFUTES': 21, 'SUPPORTS': 16, 'NEUTRAL': 2}
- 取证观测（执行者, 状态）: {('deterministic', 'OBSERVED'): 29}
- 合并: 接受 29 | 拒绝 0 
- 子 agent 用到的工具: {}
- 门裁决:
  - CONFIRM approved=True `CREATE INDEX CONCURRENTLY idx_orders_created_status ON orders(created_at, status` — large_table 上建索引：不锁表但耗 IO 且耗时
- 干预:
  - create_covering_index exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `CREATE INDEX CONCURRENTLY idx_orders_created_status ON orders(created_`
- metrics_v2: root_recall@1 1.00 (1/1) | @3 1.00 (1/1) | required_evidence_completion 1.00 (2/2) | unknown_error_rate 0.00 (0/10) | tool_calls 10 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect stats_freshness for EDGE edge_b49e7deec519b386d9903171 via get_table_stats: stale_ | collect stats_freshness for NODE stale_statistics via get_table_stats: stale_statistics: s | collect stats_range_drift for NODE stale_statistics via get_table_stats: distinguish a maj

### stale_statistics_eval_v1  ·  真值 `stale_statistics`  ·  声明 `stale_statistics`

- episode: `ep_stale_statistics_eval_v1_1790438764` | 终态 DONE | steps 16 | 225s | $0.166 | infra 0 | harness_error ``
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

