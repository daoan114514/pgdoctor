# 端到端评测缺陷报告（2026-09-24，eval split 5 场景）

## 批次信息

| 项 | 值 |
|---|---|
| 代码 | `2da6494`（git_dirty=False），5 个场景同一 harness |
| 因果图 | `graph_7f61ea9266abd6c163f5fea1` |
| SDK / CLI | claude-agent-sdk 0.2.157 / CLI 2.1.277 |
| 策略 | llm，max_steps 30，ESC 开、案例库开、L1-L4 开；确定性取证开（无参 + 钉参工具），子 agent 并发 4 |
| 结果文件 | `eval/results/quarantine/guard_*_20260924_2da6494.json`，合并版 `eval/results/quarantine/llm_eval_guarded_20260924_2da6494.json`（本报告的缺陷修复后被复跑取代，移入隔离区） |
| 时间 | 14:35 起、15:13 结束，一次跑完，无停机、无重跑 |
| 成本 | 5 个计分 episode 合计 **$0.89** |

## 结果总览

| 场景 | 真值 | 声明 | D 报告口径 | D 曾选对 | D 严格 | O | S | 步数 | 成本 | episode 用时 |
|---|---|---|---|---|---|---|---|---|---|---|
| connection_exhaustion | connection_exhaustion | connection_exhaustion | ✓ | ✓ | ✓ | ✓ | ✓ | 16 | $0.198 | 213s |
| lock_contention | lock_contention | lock_contention | ✓ | ✓ | ✓ | ✓ | ✓ | 24 | $0.202 | 283s |
| misleading_idle_txn | long_idle_transaction | long_idle_transaction | ✓ | ✓ | ✗ | ✓ | ✓ | 16 | $0.195 | 211s |
| missing_index | missing_index | missing_index | ✓ | ✓ | ✓ | ✓ | ✓ | 17 | $0.117 | 507s |
| stale_statistics | stale_statistics | stale_statistics | ✓ | ✓ | ✓ | ✓ | ✓ | 20 | $0.177 | 242s |

汇总：可用 5/5，**D 报告口径 5/5，D 严格 4/5，O 5/5，S 5/5**，无损 5/5，合并拒绝 0，不可得 0，infra 0，危险动作提出 0。

对照 9 月 23 日全量（`1c051d4`，缺陷报告 `defect_report_20260923.md`）：D 报告口径 2/5、D 曾选对 4/5、O 2/5、S 2/5、$5.02。那份报告的 P1-1 至 P1-6、P2-2 在 `308cd15` 修复，本轮跑批前的取证架构修复在 `2da6494`。

## 提速对照

同口径取 episode 自身的 `elapsed_s`（不含环境重置、负载预热与等告警）：

| 场景 | 对照批次 | 对照用时 / 成本 | 本批用时 / 成本 | 用时 | 成本 |
|---|---|---|---|---|---|
| connection_exhaustion | `308cd15`（隔离区 20260924） | 340s / $0.68 | 213s / $0.198 | -37% | -71% |
| lock_contention | `308cd15` | 598s / $1.28 | 283s / $0.202 | -53% | -84% |
| misleading_idle_txn | `308cd15` | 516s / $0.88 | 211s / $0.195 | -59% | -78% |
| missing_index | `1c051d4`（308cd15 批没跑到） | 999s / $0.76 | 507s / $0.117 | -49% | -85% |
| stale_statistics | `1c051d4` | 1078s / $1.57 | 242s / $0.177 | -78% | -89% |

提速来自取证改由系统确定性执行：5 个 episode 共 13 轮取证、62 个确定性任务，子 agent 任务只有 4 个（全是要设计索引定义的 `simulate_index`，missing_index 一个都没有）。missing_index 的 507 秒里大头是在 1200 万行表上 `CREATE INDEX CONCURRENTLY` 与 VERIFY 窗口。

代价：累计计数器窗口读数前要等满 30 秒（见下），每个 episode 恰好等了一次，16-29 秒；没有一次"等完仍 INSUFFICIENT"的空转。

## 本批在活库上确认生效的改动

- **多 pid 终止**：connection_exhaustion 与 misleading_idle_txn 都一次提交多个已观测到的 pid，过门、执行、VERIFIED。misleading_idle_txn 的 O 由 ✗ 变 ✓（判据加了连接使用率）。
- **EXPLAIN ANALYZE 自身执行量扣除**（规则 6）：missing_index 的 `seq_scan_volume` 窗口 40.7 秒，顺序扫描 286 次，其中自家 EXPLAIN 3 次 / 3600 万行被扣掉；每次平均 545 万行（全表 1200 万，并行 3 路 45%），判据正确支持 missing_index。
- **窗口读数前等满下限**：每局等 1 次（16-29 秒），此后的窗口都不短于 30 秒、全部有判定力。修复前离线夹具里同样的流程 ESC 4 轮变 30 轮空转。
- **确定性执行顺序**：窗口读者先于 `explain_query`，审计里每轮都有 `evidence_execution_order`。

## 待修缺陷（按优先级）

### P1-1　跨根因的"鉴别"需求结构上永远绑不上（两处真相源不一致）

**现象**：ESC 会向一个根因要只和另一个根因有方向关系的证据。missing_index 第 2 轮（rev 85）的 ESC 需求里，`seq_scan_volume` ×3、`temp_file_volume` ×3 的目标是 stale_statistics 节点及它的两条边；5 个 episode 最后一轮的 ESC 指令里都有同类条目：`lock_blocking_chain` 给 missing_index 节点、`slow_query_ranking` 给 work_mem_spill 节点、`index_existence` 给 stale_statistics 节点。

**为什么绑不上**：`bind_evidence` 的 `_entry_matches` 要求需求的目标根因与 scratchpad 条目的 `bears_on` 有交集，而 `bears_on` 是 toolbox 里写死的（`seq_scan_volume` 只写了 `["missing_index"]`，`temp_file_volume` 只写了 `["work_mem_spill"]`）。图上这些证据对被要证据的根因只有 `DISCRIMINATES`（采集优先级，按 `_causal_relation` 的约定不定方向）。所以这类需求：工具照调、步数照扣，条目永远进不了绑定；就算进了也不定方向。

**影响**：本批没有挡住 SUFFICIENT，但每轮都在为不可满足的需求花工具调用与预算（lock_contention 用了 24/30 步）。它是 CLAUDE.md 反复出现的那类静默合同缺口：系统要一样结构上拿不到的东西，而任何地方都不报错。场景一难，就可能变成 ESC 挂着不可满足的需求直到预算耗尽。

**建议**：图是唯一真相源。证据"关于哪些根因"由图的关系推出（CONFIRMED_BY / REFUTED_BY），toolbox 不再手写 `bears_on`；需求生成端不发"目标根因与证据类型之间没有方向关系"的需求，要么把 DISCRIMINATES 的语义定义清楚（它到底让谁变可判）。这会改变绑定语义，改前后必须跑 `.dev/pollution_edges.py` 做 diff、`graph_lint` 与回放回归。

### P1-2　misleading_idle_txn 严格诊断：中间机制无法与"独立根因"区分（存量）

**现象**：真路径 `long_idle_transaction → connection_exhaustion → throughput_down` 被选中，但竞争假设 connection_exhaustion 作为独立根因的路径 `connection_exhaustion → throughput_down` 也是 SUPPORTED（未选中），没有被反证。严格口径要求把场景列出的竞争假设判成 REFUTED，所以 D 严格 ✗。

**难点**：两条路径共享节点 connection_exhaustion 和边 connection_exhaustion→throughput_down，只差"谁是根"。现有的 NODE / PATH 范围反证都会连带反证真路径上的中间节点，这正是用户提醒过的污染边风险。

**建议**：需要"根角色范围"的反证。只对"以 connection_exhaustion 为根"的路径生效，证据用被占连接的构成，即 idle in transaction 占比（pg_stat_activity 的直接观测，不经过规划器）。先在图语义上设计清楚，再跑污染边 diff。不建议用加边、放宽 ESC 的方式绕过（9 月 24 日试过 `long_idle_transaction → lock_contention` 边，已回退）。

### P2-1　成功报告里带着过程中的失败说明

lock_contention 与 stale_statistics 的最终报告 `reason` 是"没有可选择的已支持解释路径"。这句话是第 1 轮 DIAGNOSE 还没选中路径时写进 `st.outcome_note` 的（`agent/llm_policy.py:488`、`agent/policy.py:182`），之后 SUFFICIENT、修复 VERIFIED 也没清，`final_report`（`agent/explanation_runtime.py:1417/1531`）原样带出。不影响决策与评分，但报告自相矛盾。建议：`outcome_note` 只在终止出口写，过程性说明另记。

### P2-2　有判定力的"顺带"观测没人用；需求定义不落盘

- missing_index 第 1 轮 `get_database_stats` 是为 checkpoint_pressure 的两个需求调的，顺带取到了 30.2 秒窗口、外溢 0 MB 的 `temp_file_volume`，本可反证 work_mem_spill。绑定只由需求驱动，这条观测一直躺在 scratchpad 里，work_mem_spill 最后是 INCONCLUSIVE（只有 MONITOR 建基线时那条 UNKNOWN 绑定）。第 2 轮 ESC 发的 temp 需求又全是 P1-1 那类绑不上的。
- ESC 在 work_mem_spill INCONCLUSIVE、table_bloat UNTESTED 时判 SUFFICIENT，依据是 `ALTERNATIVE_PATHS: major=2, unresolved=0`，只要求得分最高的两个竞争路径被解决。这是现行设计，不是 bug，但与"正确率优先"的取舍值得再审一次。
- 排查这件事时，需求定义没有落盘，审计里只有 need id，只能靠枚举哈希反推出目标。建议计划审计里带上每个需求的 `evidence_type / target_kind / target_ids / predicate_id`。

### P3　harness 与观察项

- **harness 身份没记实际 max_steps**：结果文件的 `harness` 只有 `default_max_steps: 40`，本批实际是 30（守护传入），`defect_report.py` 的表头因此写成 40。应把本次运行参数（max_steps、并发、确定性开关）记进 harness 身份。
- **初始候选排序**：root_recall@1 在 lock_contention、misleading_idle_txn、stale_statistics 上仍是 0（与 9 月 23 日相同），诊断靠取证纠正。排序偏差现在的代价只是多一两轮确定性取证，优先级低。
- **misleading_idle_txn 告警偶发不响**：标定时出现过一次 `fired: False`，本批 5/5 都响。原因未查，留作观察项。

## 建议的下一步顺序

1. P2-1（outcome_note）与 P2-2 的需求定义落盘：改动小，后者是查 P1-1 的前提。
2. P1-1：先把需求生成端和 `DISCRIMINATES` 的语义查清，再决定是"不发"还是"由图推出 bears_on"。改后跑污染边 diff、graph_lint、回放回归、checkall，再用守护全量复跑。
3. P1-2：先写"根角色范围反证"的语义设计与污染分析，确认不会反证真路径上的中间节点，再动图。
4. P3 的 harness 身份补运行参数。

## 修复状态（2026-09-24 晚，复跑前）

| 缺陷 | 修法 | 验证 |
|---|---|---|
| P1-1 跨根因鉴别需求 | `graph.evidence_needs` 不再按 `discriminators_of` 发需求（判别力只留在 frontier 排序里）；`_entry_matches` 以图的 CONFIRMED_BY / REFUTED_BY（`graph.causes_bearing`）并上 toolbox 的 bears_on 放行 | `evidence_direction_check` [12]：7 个症状召回的全部需求都与证据有方向关系；图上有关系而 bears_on 没写的能绑、两边都没有的仍不能绑 |
| P1-2 根角色反证 | 新 REFUTED_BY 范围 `ROOT`（`graph.REFUTER_SCOPES` 一处定义）：只把以该节点为根的路径判 REFUTED，节点与边不动；新证据 `connection_residual`（同一次 `get_connection_stats`、同一个快照，扣掉持续 >=30 秒的 idle in transaction 后是否仍逼近上限），`live_state`，挂在 connection_exhaustion 上；需求、ESC 竞争路径关闭都认它 | `root_scope_check` 22 项；污染边前后对比只多一条干净的可反证证据、无新污染边；活库标定见上面判据注释（97% SUPPORTS / 10% REFUTES） |
| P2-1 成功报告里的过程说明 | 非终止出口（DIAGNOSE 回 INVESTIGATE、GATE 无提案、批准计划过期）改记 `st.progress_notes`，`outcome_note` 只留给终止 / 升级出口 | `terminal_done_check`：三种策略的 DIAGNOSE 无选中路径都不写 outcome_note |
| P2-2 需求定义不落盘 | 每轮 `evidence_plan` 审计带需求的类型 / 目标 / 判据 / 理由与任务分派 | `deterministic_evidence_check` |
| P3 harness 身份 | 结果的 `harness.run` 记实际 max_steps、子 agent 并发、确定性开关、窗口下限 | `deterministic_evidence_check` |

P2-2 里"顺带观测没人用"与 ESC 只要求两个主要竞争路径被解决，属于设计取舍，本轮不改。

## 附录：每个 episode 的原始素材

由 `.dev/defect_report.py` 从结果文件与 trace 生成。表头里的 max_steps 40 是默认值，实际为 30（见 P3）。

### connection_exhaustion_eval_v1  ·  真值 `connection_exhaustion`  ·  声明 `connection_exhaustion`

- episode: `ep_connection_exhaustion_eval_v1_1790231729` | 终态 DONE | steps 16 | 213s | $0.198 | infra 0 | harness_error ``
- D(报告口径) **True** | 曾选对 True | 严格 True | O **True** | S **True** | 无损 True
- outcome_note: -
- ESC 序列: INSUFFICIENT → SUFFICIENT
- 最后一次 ESC（SUFFICIENT）失败维度: 无
- 路径:
  - `connection_exhaustion -> throughput_down` SUPPORTED **[选中]**
  - `lock_contention -> throughput_down` REFUTED
  - `missing_index -> throughput_down` REFUTED
  - `deadlock -> throughput_down` REFUTED
  - `long_idle_transaction -> connection_exhaustion -> throughput_down` REFUTED
- 证据绑定 27: {'NOT_APPLICABLE': 6, 'REFUTES': 12, 'SUPPORTS': 6, 'NEUTRAL': 3}
- 取证观测（执行者, 状态）: {('deterministic', 'OBSERVED'): 31, ('subagent', 'OBSERVED'): 3}
- 合并: 接受 32 | 拒绝 0 
- 子 agent 用到的工具: {'simulate_index': 3}
- 门裁决:
  - CONFIRM approved=True `SELECT pg_terminate_backend(128442), pg_terminate_backend(128443), pg_terminate_` — 终止会话不可撤销，已显式声明并需人工确认
- 干预:
  - terminate_idle_backend exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `SELECT pg_terminate_backend(128442), pg_terminate_backend(128443), pg_`
- metrics_v2: root_recall@1 1.00 (1/1) | @3 1.00 (1/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.00 (0/11) | tool_calls 11 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect explain_plan for EDGE edge_349a959b0b6379c2c6746228 via explain_query: missing_ind | collect lock_blocking_chain for EDGE edge_349a959b0b6379c2c6746228 via get_blocking_chain: | collect lock_blocking_chain for NODE missing_index via get_blocking_chain: missing_index: 

### lock_contention_eval_v1  ·  真值 `lock_contention`  ·  声明 `lock_contention`

- episode: `ep_lock_contention_eval_v1_1790232099` | 终态 DONE | steps 24 | 283s | $0.2021 | infra 0 | harness_error ``
- D(报告口径) **True** | 曾选对 True | 严格 True | O **True** | S **True** | 无损 True
- outcome_note: 没有可选择的已支持解释路径
- ESC 序列: INSUFFICIENT → INSUFFICIENT → INSUFFICIENT → SUFFICIENT
- 最后一次 ESC（SUFFICIENT）失败维度: 无
- 路径:
  - `missing_index -> latency_p99_up` REFUTED
  - `connection_exhaustion -> throughput_down` REFUTED
  - `stale_statistics -> latency_p99_up` REFUTED
  - `lock_contention -> latency_p99_up` SUPPORTED **[选中]**
  - `lock_contention -> throughput_down` SUPPORTED **[选中]**
  - `work_mem_spill -> latency_p99_up` REFUTED
  - `missing_index -> throughput_down` REFUTED
  - `deadlock -> throughput_down` REFUTED
  - `table_bloat -> latency_p99_up` REFUTED
  - `checkpoint_pressure -> latency_p99_up` REFUTED
  - `long_idle_transaction -> connection_exhaustion -> latency_p99_up` REFUTED
  - `long_idle_transaction -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `orphaned_prepared_transaction -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
  - `stale_replication_slot -> autovacuum_starvation -> table_bloat -> latency_p99_up` REFUTED
- 证据绑定 59: {'NOT_APPLICABLE': 12, 'SUPPORTS': 14, 'REFUTES': 22, 'NEUTRAL': 11}
- 取证观测（执行者, 状态）: {('deterministic', 'OBSERVED'): 55, ('subagent', 'OBSERVED'): 4}
- 合并: 接受 59 | 拒绝 0 
- 子 agent 用到的工具: {'simulate_index': 4}
- 门裁决:
  - CONFIRM approved=True `SELECT pg_terminate_backend(141444)` — 终止会话不可撤销，已显式声明并需人工确认
- 干预:
  - terminate_blocker exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `SELECT pg_terminate_backend(141444)`
- metrics_v2: root_recall@1 0.00 (0/1) | @3 0.00 (0/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.00 (0/18) | tool_calls 18 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect slow_query_ranking for NODE work_mem_spill via get_top_queries: work_mem_spill: br | collect temp_file_volume for EDGE edge_f3ccdc9851a605d259af8895 via get_database_stats: di | collect temp_file_volume for NODE work_mem_spill via get_database_stats: work_mem_spill: b

### misleading_idle_txn_eval_v1  ·  真值 `long_idle_transaction`  ·  声明 `long_idle_transaction`

- episode: `ep_misleading_idle_txn_eval_v1_1790232535` | 终态 DONE | steps 16 | 211s | $0.1945 | infra 0 | harness_error ``
- D(报告口径) **True** | 曾选对 True | 严格 False | O **True** | S **True** | 无损 True
- outcome_note: -
- ESC 序列: INSUFFICIENT → SUFFICIENT
- 最后一次 ESC（SUFFICIENT）失败维度: 无
- 路径:
  - `connection_exhaustion -> throughput_down` SUPPORTED
  - `lock_contention -> throughput_down` REFUTED
  - `missing_index -> throughput_down` REFUTED
  - `deadlock -> throughput_down` REFUTED
  - `long_idle_transaction -> connection_exhaustion -> throughput_down` SUPPORTED **[选中]**
- 证据绑定 27: {'NOT_APPLICABLE': 6, 'REFUTES': 10, 'SUPPORTS': 8, 'NEUTRAL': 3}
- 取证观测（执行者, 状态）: {('deterministic', 'OBSERVED'): 31, ('subagent', 'OBSERVED'): 3}
- 合并: 接受 32 | 拒绝 0 
- 子 agent 用到的工具: {'simulate_index': 3}
- 门裁决:
  - CONFIRM approved=True `SELECT pg_terminate_backend(146805), pg_terminate_backend(146806), pg_terminate_` — 终止会话不可撤销，已显式声明并需人工确认
- 干预:
  - terminate_idle_transaction exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `SELECT pg_terminate_backend(146805), pg_terminate_backend(146806), pg_`
- metrics_v2: root_recall@1 0.00 (0/1) | @3 0.00 (0/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.00 (0/11) | tool_calls 11 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect explain_plan for EDGE edge_349a959b0b6379c2c6746228 via explain_query: missing_ind | collect lock_blocking_chain for EDGE edge_349a959b0b6379c2c6746228 via get_blocking_chain: | collect lock_blocking_chain for NODE missing_index via get_blocking_chain: missing_index: 

### missing_index_eval_v1  ·  真值 `missing_index`  ·  声明 `missing_index`

- episode: `ep_missing_index_eval_v1_1790232908` | 终态 DONE | steps 17 | 507s | $0.1168 | infra 0 | harness_error ``
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
- 证据绑定 44: {'NOT_APPLICABLE': 6, 'REFUTES': 20, 'SUPPORTS': 15, 'NEUTRAL': 3}
- 取证观测（执行者, 状态）: {('deterministic', 'OBSERVED'): 40}
- 合并: 接受 40 | 拒绝 0 
- 子 agent 用到的工具: {}
- 门裁决:
  - CONFIRM approved=True `CREATE INDEX CONCURRENTLY idx_orders_status_created ON orders(status, created_at` — large_table 上建索引：不锁表但耗 IO 且耗时
- 干预:
  - create_covering_index exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `CREATE INDEX CONCURRENTLY idx_orders_status_created ON orders(status, `
- metrics_v2: root_recall@1 1.00 (1/1) | @3 1.00 (1/1) | required_evidence_completion 1.00 (2/2) | unknown_error_rate 0.00 (0/10) | tool_calls 10 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect index_existence for NODE stale_statistics via get_indexes: stale_statistics: branc | collect row_estimate_deviation for EDGE edge_ad3de7fcc974be9871fc0be4 via explain_query: s | collect row_estimate_deviation for NODE stale_statistics via explain_query: distinguish a 

### stale_statistics_eval_v1  ·  真值 `stale_statistics`  ·  声明 `stale_statistics`

- episode: `ep_stale_statistics_eval_v1_1790233579` | 终态 DONE | steps 20 | 242s | $0.1769 | infra 0 | harness_error ``
- D(报告口径) **True** | 曾选对 True | 严格 True | O **True** | S **True** | 无损 True
- outcome_note: 没有可选择的已支持解释路径
- ESC 序列: INSUFFICIENT → INSUFFICIENT → SUFFICIENT
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
- 证据绑定 44: {'NOT_APPLICABLE': 8, 'REFUTES': 22, 'SUPPORTS': 10, 'NEUTRAL': 4}
- 取证观测（执行者, 状态）: {('deterministic', 'OBSERVED'): 41, ('subagent', 'OBSERVED'): 3}
- 合并: 接受 44 | 拒绝 0 
- 子 agent 用到的工具: {'simulate_index': 3}
- 门裁决:
  - AUTO approved=True `ANALYZE orders` — ANALYZE 只更新统计信息，可自动执行
- 干预:
  - analyze_table exec=SUCCEEDED outcome=VERIFIED rollback=NOT_NEEDED `ANALYZE orders`
- metrics_v2: root_recall@1 0.00 (0/1) | @3 1.00 (1/1) | required_evidence_completion 1.00 (1/1) | unknown_error_rate 0.00 (0/16) | tool_calls 16 | esc_over_conservative {'numerator': 0, 'denominator': 0, 'value': None}
- 最后的 ESC 指令: collect slow_query_ranking for NODE work_mem_spill via get_top_queries: work_mem_spill: br | collect temp_file_volume for EDGE edge_f3ccdc9851a605d259af8895 via get_database_stats: di | collect temp_file_volume for NODE work_mem_spill via get_database_stats: work_mem_spill: b
