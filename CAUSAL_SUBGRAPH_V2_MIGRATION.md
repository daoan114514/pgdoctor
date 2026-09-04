# 因果解释子图 v2 迁移说明

更新日期：2026-09-03

## 迁移结果

v2 把 episode 的诊断对象从“候选根因列表”改为 `ExplanationGraph`。系统现在保存候选因果路径、当前取证 frontier、选中解释子图、证据绑定、P0 义务、干预计划和预期下游效果。因果图继续负责多跳召回；它没有退化成根因清单。

新建 `EpisodeState` 使用 `schema_version=2`。旧 trace 按自身版本读取；没有版本号的 trace 视为 v1，并通过只读投影提供给 v2 代码。此次迁移没有批量改写历史 trace，也没有把 v1 学习文件导入 v2。

本批保留以下兼容字段和读取路径：

- `EpisodeState.hypothesis_candidates`
- `EpisodeState.ledger` 和 `EpisodeState.claimed_fault_class`
- ESC 的 `report_id` 别名；v2 主字段为 `esc_report_id`
- v1 hypothesis ledger 和 `compat_explanation_projection()`
- `knowledge/learned/` 下的 v1 playbook、graph delta、query library 和 candidate edges

这些字段仍有旧 trace、重放工具和兼容测试在使用，当前不满足删除条件。

## 数据对象变化

| v1 | v2 | 说明 |
|---|---|---|
| 候选根因 ID 列表 | `ExplanationGraph.candidate_paths` | 路径保留多跳机制、症状归属和稳定 edge ID |
| 根因 verdict 台账 | `EvidenceReport` + predicate + `EvidenceBinding` | 工具结果与语义判定分开；`UNKNOWN/ERROR` 不等于反证 |
| 每个根因一个 Subagent | `EvidenceTask` | 任务绑定 need、path、node/edge、预算和 1–3 个工具 |
| 根因级 `evidence_order` | frontier-conditioned L2 record | 同一根因的不同分叉可以选择不同下一步 |
| 根因 prior / 人话边键 | 稳定 edge/path ID 的 L3 权重 | 支持 cause-to-cause 边，正负更新对称 |
| 根因到工具的出现次数 | `frontier + need + tool` 的 L4 统计 | 统计信息增益、成本、可用性、重复和 UNKNOWN/ERROR |
| 修复后只看总 KPI | `InterventionPlan.expected_effects` | VERIFY 先验证路径下游预测，再检查健康 KPI 和回归 |

`hypothesis_candidates` 现在只是候选路径根节点的兼容投影。ESC 不会用它重建竞争关系或 P0 覆盖。

## 修改文件清单

以下清单按 v2 责任边界分类。工作区中的既有未提交修改均保留；本次没有改写 `knowledge/learned/*.yaml` 下的 v1 审计数据。

### 数据模型

- `agent/explanation.py`：稳定 ID、枚举、`EvidenceBinding`、`CausalPath`、`P0Obligation`、`ExplanationGraph`、`EvidenceNeed`、`EvidenceReport`、`InterventionPlan`、`CausalGateContext` 和 v1 只读投影。
- `agent/episode_state.py`：v2 Episode 字段、`InterventionAttempt`、版本化保存/恢复、兼容投影和上下文渲染。
- `agent/explanation_runtime.py`：工具观察到 predicate/binding，再到节点、边、路径和 P0 状态的确定性更新。

### 状态机与 Episode runtime

- `agent/loop.py`、`agent/state_machine.py`：十三阶段的解释子图流程、回退路由、`REPORT/ESCALATE -> DONE` 和最终持久化。
- `agent/policy.py`、`agent/llm_policy.py`、`agent/depth_policy.py`：路径级 prompt/动作契约，移除 v2 根因字符串作为真相源。
- `demo.py`、`sandbox/env.py`、`sandbox/metrics.py`、`sandbox/observe.py`：v2 驱动、窗口/source epoch、结构化观察和验证指标接线。

### 因果图、predicate 与干预语义

- `knowledge/causal_graph/graph.py`：路径枚举/合并、frontier、evidence needs、替代路径、路径内下游、干预选项、P0 预算外召回、可解释评分和 graph version。
- `knowledge/causal_graph/nodes.yaml`、`knowledge/causal_graph/edges.yaml`：`FIXED_BY` 干预类型/前置条件/预期效果，带 scope 的 `REFUTED_BY`，物理膨胀证据和语义准确的 fix。
- `knowledge/evidence_predicates.py`：结构化 `SUPPORTS/REFUTES/NEUTRAL/NOT_APPLICABLE` 判定；不读取自然语言摘要。
- `knowledge/structure.py`：v2 提案状态机、审批后加载和 v1 candidate 隔离。
- `sandbox/injectors/p0.py`、`sandbox/scenarios/p0/*.yaml`：四个 P0 的注入、oracle 和幂等清理场景。

### 工具与 Subagent

- `agent/tool_planner.py`：四重权限交集、环境可用性、1–3 工具裁剪、required/P0 保底和可重放探索。
- `agent/investigator.py`、`agent/orchestrator.py`：路径片段任务、只含观察的报告、predicate 主流程绑定、迟到结果隔离和学习观测。
- `agent/permissions.py`、`agent/toolbox.py`、`agent/hooks.py`：统一权限权威、`report_evidence`、PLAN 构造和运行时复核。

### ESC、GATE、VERIFY 与 rollback

- `agent/esc.py`：ExplanationGraph 充分性、替代路径/P0/证据可信度/版本检查和四类 verdict。
- `safety/gate.py`：`CausalGateContext` 校验、机器可读 denial/retry phase，再进入既有安全门。
- `agent/verification.py`：逐项 expected/actual effect 与作用域正确的失败归因。
- `safety/shield.py`、`safety/undo_journal.py`：保留 AST/风险/影响面边界，并补充 v2 执行和 undo 状态。

### 学习

- `knowledge/case_store.py`：`CaseV2` 写入、路径模板检索、provenance/split 和效用更新。
- `knowledge/evolution.py`：L2 条件策略、L3 edge/path 权重、L4 信息增益及版本陈旧治理。
- `knowledge/learned/v2/manifest.yaml`、`schema.yaml`、`cases.yaml`、`investigation_policy.yaml`、`causal_weights.yaml`、`tool_information_gain.yaml`、`structure_proposals.yaml`：独立 v2 存储；`v1_import.enabled=false`。

### 评测与验收

- `eval/metrics_v2.py`、`eval/run_suite.py`、`eval/replay.py`：带分母的新指标、revision/split、逐层消融和 v1 replay 空分母兼容。
- `.dev/checkall.sh`：把 v2 独立验收纳入总回归。
- 新增独立验收：`.dev/explanation_model_check.py`、`path_recall_check.py`、`p0_obligation_check.py`、`evidence_predicate_check.py`、`dynamic_tool_planner_check.py`、`subagent_path_task_check.py`、`esc_explanation_check.py`、`causal_gate_context_check.py`、`causal_verify_check.py`、`terminal_done_check.py`、`evolution_v2_check.py`、`structure_v2_check.py`、`eval_metrics_v2_check.py`、`e2e_explanation_check.py`。
- 兼容/细分回归：`.dev/causal_gate_v2_check.py`、`causal_semantics_check.py`、`esc_v2_check.py`、`learning_v2_check.py`、`mape_k_v2_check.py`、`tool_planner_v2_check.py`、`verify_rollback_v2_check.py`，以及本次调整的既有 `.dev/*.py`。
- `README.md`、`requirements.txt`、`docker/docker-compose.yml`、`docker/init/01_extensions_roles.sql`、`CAUSAL_SUBGRAPH_V2_IMPLEMENTATION_CHECKLIST.md`：运行说明、依赖、测试角色/扩展和规格对照。

## 实际序列化样例

下面的持久化样例来自活库 W4 Episode `ep_missing_index_orders_user_status_v1_1788398606`。完整 `ExplanationGraph`、ESC、计划、GATE 上下文、VERIFY 和 attempts 位于 `traces/ep_missing_index_orders_user_status_v1_1788398606/episode_state.json`；`raw_ref` 指向同目录的真实 step 文件。为避免复制 295 KB 状态文件，这里展示各对象的代表性完整对象或字段切片。

### `EvidenceBinding`

```json
{
  "binding_id": "binding_71e44cdf1a18b28e2a8d1dce",
  "episode_id": "ep_missing_index_orders_user_status_v1_1788398606",
  "raw_ref": "trace://ep_missing_index_orders_user_status_v1_1788398606/step_007",
  "evidence_type": "slow_query_ranking",
  "status": "OBSERVED",
  "observed_at": 1788398678.5974946,
  "window_start": null,
  "window_end": null,
  "source_epoch": "",
  "target_node_ids": [],
  "target_edge_ids": ["edge_b49e7deec519b386d9903171"],
  "predicate_id": "slow_query_ranking_v2",
  "predicate_result": "SUPPORTS",
  "summary": "structured observation collected",
  "value_digest": "df3945a9ab2af4253b250b1b64f72df010b9f7121b60776114ea23c99466f4c5",
  "fresh_until": 1788398978.5974946
}
```

### `CausalPath`

此实际候选展示同一节点的路径局部角色，而不是静态类型：

```json
{
  "path_id": "path_bade904f0a0a6eb29795398d",
  "node_ids": ["stale_replication_slot", "autovacuum_starvation", "table_bloat", "latency_p99_up"],
  "edge_ids": ["edge_1597ad83748f73815cade6bf", "edge_244a45b46b657e5f2253c262", "edge_7cebde81f4a33fc40a53032f"],
  "observed_symptom_id": "latency_p99_up",
  "root_node_id": "stale_replication_slot",
  "node_roles": {
    "stale_replication_slot": "ROOT_CAUSE",
    "autovacuum_starvation": "MECHANISM",
    "table_bloat": "MECHANISM",
    "latency_p99_up": "OBSERVED_SYMPTOM"
  },
  "score_components": {
    "manual_causes_likelihood": 0.476,
    "manual_root_prior": 0.02,
    "learned_root_prior_adjustment": 0.0,
    "l1_path_template_adjustment": 0.0,
    "l3_edge_adjustment": 0.0,
    "l3_path_adjustment": 0.0,
    "symptom_coverage_reward": 0.1,
    "hop_penalty": -0.06,
    "redundancy_penalty": 0.0,
    "total": 0.536
  },
  "source": ["graph"],
  "status": "REFUTED",
  "required_evidence_types": ["autovacuum_health", "physical_bloat_ratio", "replication_slot_age"],
  "evidence_binding_ids": ["binding_2187fa944c2f1d55e8056563", "binding_9236b8222cf0a622d159d3b1", "binding_af3f39fcea389b63a9fec1f8", "binding_d16df0b4803c1acb4429973f", "binding_f505212bfb1c88c105dc08ca", "binding_6327580b63a57d46c54d30c7"]
}
```

### `P0Obligation`

```json
{
  "cause_id": "stale_replication_slot",
  "reachable_path_ids": ["path_bade904f0a0a6eb29795398d"],
  "status": "REFUTED",
  "required_evidence_types": ["replication_slot_age"],
  "evidence_binding_ids": ["binding_9236b8222cf1d55e8056563"],
  "resolution_reason": "required predicate refuted P0",
  "truncated": false
}
```

### `ExplanationGraph`

实际对象为 schema 2、revision 127，含 15 条候选路径、62 个绑定和 3 项当前症状可达 P0；完整数组/映射保存在上述 trace。以下是同一持久化对象的顶层投影：

```json
{
  "explanation_id": "explanation_ab70ef483d633c4f3495db78",
  "schema_version": 2,
  "graph_version": "graph_e08cdb62d9441aeaa798a033",
  "revision": 127,
  "episode_id": "ep_missing_index_orders_user_status_v1_1788398606",
  "observed_symptoms": ["cpu_saturated", "latency_p99_up", "throughput_down"],
  "selected_path_ids": ["path_3bdc01ada30d740a3c8557ba", "path_cdaa7aec65723d504da523d5", "path_0390358c3f285e12feeb7f50"],
  "selected_root_causes": ["missing_index"],
  "unexplained_symptoms": [],
  "scope": "FULL"
}
```

`selected_root_causes` 在恢复时会丢弃磁盘上的同名值并从 `selected_path_ids` 重算。实际 `candidate_paths`、`node_status`、`edge_status`、`evidence_bindings` 和 `p0_obligations` 不能由该投影替代。

### `EvidenceNeed` 与 `EvidenceReport`

W4 使用 `ScriptedPolicy`，所以没有把 Subagent report 留在该 Episode。以下对象由当前 serializer 使用 W4 的真实 path、edge 和 `step_007` 生成，并通过契约测试；它们是序列化样例，不计作该 Episode 的工具调用：

```json
{
  "need_id": "need_e94d52f685889476b2d2d89c",
  "path_ids": ["path_cdaa7aec65723d504da523d5"],
  "target_kind": "EDGE",
  "target_ids": ["edge_4d0e39960c2b59c5b8eddebc"],
  "evidence_type": "slow_query_ranking",
  "predicate_id": "slow_query_ranking_v2",
  "required": false,
  "freshness_seconds": 300,
  "candidate_tools": ["get_slow_queries"],
  "reason": "distinguish the selected latency path"
}
```

```json
{
  "need_id": "need_e94d52f685889476b2d2d89c",
  "tool": "get_slow_queries",
  "raw_refs": ["trace://ep_missing_index_orders_user_status_v1_1788398606/step_007"],
  "observations": [{
    "queryid": "6383125513210400435",
    "calls": 4127,
    "mean_ms": 36.44,
    "total_ms": 150367.84,
    "rows": 49516,
    "query": "SELECT id, total, created_at FROM orders WHERE user_id = $1 AND status = $2"
  }, {
    "queryid": "6189335025602565763",
    "calls": 1970,
    "mean_ms": 45.67,
    "total_ms": 89963.67,
    "rows": 23607,
    "query": "SELECT id, total, created_at FROM orders WHERE user_id = $1 AND status = $2"
  }, {
    "queryid": "5925593268675158292",
    "calls": 1,
    "mean_ms": 1636.45,
    "total_ms": 1636.45,
    "rows": 1,
    "query": "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM products), (SELECT count(*) FROM orders), (SELECT count(*) FR"
  }, {
    "queryid": "6085125734290557843",
    "calls": 1,
    "mean_ms": 821.61,
    "total_ms": 821.61,
    "rows": 0,
    "query": "ANALYZE orders"
  }, {
    "queryid": "9069661048250515891",
    "calls": 1,
    "mean_ms": 782.17,
    "total_ms": 782.17,
    "rows": 0,
    "query": "ANALYZE"
  }],
  "collection_status": "OBSERVED",
  "limitations": []
}
```

`EvidenceReport` 没有 `CONFIRMED/REFUTED` 字段；添加 `verdict`、`predicate_result` 等字段会在反序列化时拒绝。

### `InterventionPlan`

```json
{
  "plan_id": "plan_2452243f1207240769141b57",
  "explanation_id": "explanation_ab70ef483d633c4f3495db78",
  "explanation_revision": 127,
  "selected_path_id": "path_cdaa7aec65723d504da523d5",
  "intervention_target": "missing_index",
  "fix_id": "create_covering_index",
  "intervention_kind": "CORRECTIVE",
  "action_type": "create_index",
  "sql": "CREATE INDEX CONCURRENTLY idx_orders_user_status ON orders(user_id, status)",
  "rollback": "DROP INDEX CONCURRENTLY idx_orders_user_status",
  "execution": "gated",
  "manual": false,
  "expected_effect_nodes": ["latency_p99_up"],
  "expected_effects": [{"metric": "latency_p99_ms", "direction": "decrease", "minimum_change": 0.2, "window_seconds": 300}],
  "rationale": "补上覆盖 user_id+status 谓词的复合索引，消除全表扫"
}
```

实际对象还持久化了两个 `preconditions`、对应 `precondition_results` 和 5 个绑定到路径/目标的 `evidence_refs`。

### `CausalGateContext`

```json
{
  "explanation_id": "explanation_ab70ef483d633c4f3495db78",
  "explanation_revision": 127,
  "selected_path_ids": ["path_3bdc01ada30d740a3c8557ba", "path_cdaa7aec65723d504da523d5", "path_0390358c3f285e12feeb7f50"],
  "intervention_target": "missing_index",
  "fix_id": "create_covering_index",
  "intervention_kind": "CORRECTIVE",
  "expected_effect_nodes": ["latency_p99_up"],
  "expected_effects": [{"metric": "latency_p99_ms", "direction": "decrease", "minimum_change": 0.2, "window_seconds": 300}],
  "esc_report_id": "esc_report_789f6c756117d9c64b2b4d20",
  "evidence_refs": ["trace://ep_missing_index_orders_user_status_v1_1788398606/step_007", "trace://ep_missing_index_orders_user_status_v1_1788398606/step_019", "trace://ep_missing_index_orders_user_status_v1_1788398606/step_022", "trace://ep_missing_index_orders_user_status_v1_1788398606/step_028", "trace://ep_missing_index_orders_user_status_v1_1788398606/step_033"],
  "unresolved_p0_paths": []
}
```

### `ESCReport`

v2 报告以 JSON dict 持久化。W4 最终报告的核心字段如下；完整对象还包含 9 个 dimension 的逐项结果：

```json
{
  "esc_report_id": "esc_report_789f6c756117d9c64b2b4d20",
  "explanation_id": "explanation_ab70ef483d633c4f3495db78",
  "explanation_revision": 127,
  "graph_version": "graph_e08cdb62d9441aeaa798a033",
  "scope": "FULL",
  "selected_path_ids": ["path_3bdc01ada30d740a3c8557ba", "path_cdaa7aec65723d504da523d5", "path_0390358c3f285e12feeb7f50"],
  "selected_root_causes": ["missing_index"],
  "evidence_need_ids": [],
  "unresolved_p0_paths": [],
  "unexplained_symptoms": [],
  "unsupported_path_ids": [],
  "unresolved_competing_path_ids": [],
  "partial_fix_suspected": false,
  "verdict": "SUFFICIENT"
}
```

### `InterventionAttempt`

```json
{
  "attempt_id": "attempt_965369b9f964055952d0ef30",
  "plan_id": "plan_7bdcd57b27ad3e7bb3a8adce",
  "explanation_revision": 126,
  "selected_path_id": "path_cdaa7aec65723d504da523d5",
  "intervention_target": "missing_index",
  "fix_id": "create_covering_index",
  "intervention_kind": "CORRECTIVE",
  "execution_status": "SUCCEEDED",
  "execution_undo_id": "undo_1788398793255",
  "expected": [{"metric": "latency_p99_ms", "direction": "decrease", "minimum_change": 0.2, "window_seconds": 300}],
  "actual": [{"target_node_id": "latency_p99_up", "before": 4792.78, "actual": 4792.78, "observed_change": 0.0, "met": false, "result": "REFUTED", "raw_ref": "trace://ep_missing_index_orders_user_status_v1_1788398606/step_031"}],
  "outcome": "FAILED",
  "failure_scope": "INTERVENTION",
  "affected_edge_ids": [],
  "rollback_attempted": true,
  "rollback_status": "SUCCEEDED",
  "learnable": true
}
```

### `CaseV2` 与学习 YAML

当前持久化 L1 共 72 条，全部是 `human_labeled` 的 train split：70 条来自权威回放数据集（`eval/authoritative_cases_v2.yaml`，见 `eval/build_authoritative_cases.py`）的冷启动 seed，2 条是人工标注 fixture。没有一条来自生产 Episode，也没有 sandbox 样本：

```yaml
case_id: fixture_missing_index_no_wait
provenance: human_labeled
split: train
fingerprint:
  wait_profile: {none: 1}
  onset: sudden
graph_version: graph_2f970ee9f278d248e8e6aedb
observed_symptoms: [latency_p99_up]
candidate_paths:
  - path_id: path_6c7d7a16156bd79ea9b6dc7a
    node_ids: [missing_index, latency_p99_up]
    edge_ids: [edge_4d0e39960c2b59c5b8eddebc]
    selected: true
selected_path_ids: [path_6c7d7a16156bd79ea9b6dc7a]
outcome: VERIFIED_FIXTURE
utility_score: 0.75
status: active
```

L2/L3/L4 和结构提案的当前持久文件是合法的空冷启动状态，不能伪造样本：

```yaml
# investigation_policy.yaml / tool_information_gain.yaml
schema_version: 2
records: {}

# causal_weights.yaml
schema_version: 2
processed_outcomes: []
edge_stats: {}
path_stats: {}

# structure_proposals.yaml
schema_version: 2
proposals: {}
```

## 十三阶段中的行为变化

| 阶段 | v2 行为 |
|---|---|
| MONITOR / OBSERVE | 记录原始告警和规范化症状，不提前指定根因 |
| HYPOTHESIZE | 从症状反向多跳召回候选路径；为所有可达 P0 建立显式义务 |
| INVESTIGATE | 根据当前 frontier 生成 `EvidenceNeed`，再按阶段、证据产出、环境能力和学习策略裁剪工具 |
| DIAGNOSE | 选择能解释已观测症状的路径集合，记录 FULL/PARTIAL 和未解释症状 |
| ESC | 检查症状覆盖、根节点必需证据、路径连续性、主要替代路径、P0、证据可信度、图版本和 PARTIAL 风险 |
| PLAN | 只在选中路径上选干预目标；fix 必须来自 `FIXED_BY`；记录预期下游效果 |
| GATE | 先校验 explanation revision、path/target/fix/evidence/effect 绑定，再执行 AST、风险、影响面和回滚检查 |
| EXECUTE | 先写 undo journal，再由独占写连接执行 |
| VERIFY | 对照 expected/actual effect、健康 KPI 和回归查询；失败按 intervention/path/node 分作用域归因 |
| ROLLBACK | 恢复数据库状态，但保留失败尝试、证据绑定和 explanation revision |
| REPORT / ESCALATE | 输出完整因果链、主要替代路径、P0 处理、干预位置和残余不确定性 |
| DONE | REPORT 和 ESCALATE 都必须持久化终态，不能停在中间阶段 |

## Reader、写入和回滚规则

当前规则如下：

1. 新 episode 写 v2；读取时按 trace 自带 `schema_version` 分派。
2. 无版本 trace 按 v1 读取，只做内存投影，不补写虚构的边状态或证据绑定。
3. `knowledge/learned/v2/manifest.yaml` 中 `v1_import.enabled=false`，v1 sources 标为 `untrusted_audit_only`。
4. v1 与 v2 学习文件分目录保存。v2 writer 不修改 v1 YAML，eval split 不写 L1–L4。
5. 任何批次若出现 P0 recall 低于 100%、GATE 因果上下文绕过、v1 文件 hash 变化、旧 trace 无法重放、活库清理残留或 undo `needs_attention>0`，停止推广 v2 写入路径，并保留 v1 reader 处理旧数据。

本批允许新 episode 继续使用 v2 的停止条件是：完整离线回归、W4 A/B/C、P0 live、图 lint 和活库清理全部通过。兼容字段的删除条件更严格：所有存量 v1 trace 可重放、所有外部 reader 已迁移、仓库内只有兼容测试还读取旧字段，并在删除批次重新跑完上述验收。当前尚未满足这一删除条件。

本批没有删除 `hypothesis_candidates`、`report_id`、v1 ledger/projection，也没有切换旧 trace 的默认 reader。任一批次未达到上述停止条件时，这些兼容面继续保留。

## v1 学习统计

以下统计直接读取现有 v1 YAML；它们是审计记录，不会自动迁移。

| 项目 | 当前值 |
|---|---:|
| L1 legacy case files | 0 |
| L2 `connection_exhaustion` playbook | 成功 1 / 失败 2 |
| L2 `lock_contention` playbook | 成功 2 / 失败 3 |
| L2 `missing_index` playbook | 成功 4 / 失败 4 |
| L2 `stale_statistics` playbook | 成功 1 / 失败 3 |
| L3 prior：`connection_exhaustion` | +0.040 |
| L3 prior：`lock_contention` | +0.075 |
| L3 prior：`missing_index` | -0.075 |
| L3 prior：`stale_statistics` | +0.075 |
| L4 根因条目 | 10 |
| v1 structure proposals | 25 proposed；其中 23 达到旧 ready 阈值；0 promoted |

v1 的问题也保留在这些文件里：playbook 把整段 `evidence_order` 绑到根因；部分 L3 键带运行数值；L4 主要表达出现次数和顺序；candidate edge 可由共现积累出高 support。它们只能用于审计和历史重放。

## v2 学习统计

| 层 | 当前持久数据 |
|---|---:|
| L1 cases | 72 |
| L1 provenance | 72 条均为 `human_labeled`：70 条权威回放冷启动 seed + 2 条人工标注 fixture |
| L1 sandbox / production cases | 0 / 0 |
| L2 investigation policy records | 0 |
| L3 edge stats / path stats | 0 / 0 |
| L3 processed outcomes | 0 |
| L4 tool information gain records | 0 |
| v2 structure proposals | 0 |

这组数字说明 v2 机制已接入，但还没有真实 episode 学习收益。不能写成“v2 已提高诊断准确率”“已降低工具调用”或“结构会自动扩展”。

## L1-L4 写入、读取与在线消费证据

| 层 | 写入端 | 读取端 | 在线消费点 | 已验证的下一次行为变化 |
|---|---|---|---|---|
| L1 案例记忆 | `knowledge/case_store.py::write_case_v2` 写路径结构、分叉、P0、计划、attempt 和 effect | `knowledge/case_store.py::search_v2` 按 fingerprint/env/graph version 检索 | `agent/loop.py` 在 HYPOTHESIZE 注入 case template；`knowledge/causal_graph/graph.py` 把有上限的模板调整写入 score components | 相同 `latency_p99_up` 在 no-wait 与 Lock wait profile 下召回不同模板；关闭 L1 后 template adjustment 为 0，恢复手工排序 |
| L2 条件调查策略 | `knowledge/evolution.py::_update_v2_tool_learning` 只接收与当前 need/path 有关且改变 posterior/剪枝/required completion 的 observation | `knowledge/evolution.py::v2_tool_learning_components` 按 frontier/evidence/P0/capability/need/tool 键读取 | `agent/tool_planner.py` 把 `l2_conditional_policy` 直接加入工具分数 | 受控写入 5 条 observation 后首选从 `get_active_sessions` 切到 `get_blocking_chain`；关闭 L2 后该分量归零 |
| L3 边/路径权重 | `knowledge/evolution.py::_update_l3_v2` 按 outcome 一次性对稳定 edge/path ID 做有上限、正负对称更新 | `knowledge/evolution.py::load_l3_v2_adjustments` | `knowledge/causal_graph/graph.py` 分别消费 edge adjustment 和 path adjustment | 给 cause-to-cause edge/path 注入正例时关联路径上升，负例时下降；单独关闭 edge 或 path 通道时只移除对应影响；`learned=False` 恢复静态分数 |
| L4 工具信息增益 | `knowledge/evolution.py::_update_v2_tool_learning` 写 calls、OBSERVED/UNKNOWN/ERROR、耗时、覆盖 need、剪枝、entropy/posterior、决策变化和重复次数 | `knowledge/evolution.py::v2_tool_learning_components` 按 frontier+need+tool 读取并做最小样本收缩 | `agent/tool_planner.py` 把 L4 gain/cost/error/repeat 分量用于排序并最终裁到 1–3 个工具 | 不同 frontier 选择不同集合；5 个样本后 learned 工具替代静态首选；关闭 L4 后信息增益分量归零，required/P0 工具仍保底 |

共同治理证据：`eval/run_suite.py` 对 L1-L4 提供独立开关和 writer 开关；eval split 在写入前拒绝；graph/scenario/tool schema 不匹配的策略标 stale；`knowledge/learned/v2/manifest.yaml` 禁用 v1 import。验收在临时目录写入并恢复，所以仓库当前 L2/L3/L4 的 0 条持久记录是预期结果，不代表读取端未接通。

## 逐层消融

下表均为受控 fixture/replay，不是生产事故统计。

| 层 | 开启学习 | 关闭或隔离该层 | 结论 |
|---|---|---|---|
| L1 | 相同症状在不同 wait profile 下召回不同路径 | 恢复手工路径顺序 | L1 被 HYPOTHESIZE 实际读取 |
| L2 + L4 | 5 条观测后，首选从 `get_active_sessions` 切到 `get_blocking_chain` | 保持静态 discriminator 顺序 | 学习结果会改变下一工具 |
| L4 | 两个合法候选被裁到一个 | 保留静态候选集合 | L4 不只是重排 |
| L4 frontier | 不同 frontier 得到不同工具集合 | 不使用条件统计 | 统计键包含当前分叉和 need |
| L3 正例 | 目标 edge/path 所在路径上升 | `learned=False` 恢复静态分数 | 写入端和图评分读取端都生效 |
| L3 通道 | edge 与 path 通道可分别改变排序 | 单独关闭对应通道即消失 | 两个通道没有互相冒充 |
| L3 负例 | 目标路径对称降权 | 无学习时不变 | 奖励和惩罚方向一致 |
| L3 cause-to-cause | 中间机制边使用稳定 ID 更新 | v1 人话键不参与 | 多跳边可以被学习 |
| 安全不变量 | 开关学习时可达 P0 recall 不变 | ESC/GATE 判据不变 | 学习不能删 required/P0 need 或放宽安全门 |
| 污染隔离 | eval episode 不写 L1–L4 | train fixture 可写 | split 隔离生效 |
| v1 隔离 | 测试前后 v1 YAML hash 相同 | 无隐式 import | v2 不改写历史学习文件 |

## 活库端到端结果

环境：WSL Ubuntu 24.04、PostgreSQL 16、数据库 `shop`、端口 `55433`。

- W4 Episode A：从多路径召回和 P0 义务开始，经 INVESTIGATE、ESC、PLAN 反事实、GATE、EXECUTE、VERIFY 到 DONE；Diagnosis、Outcome、Safe Pass 全部通过。
- W4 Episode B：一个包含 `DROP TABLE` 的多语句提案被护盾拒绝，索引和表数据未改变。
- W4 Episode C：第一次合法索引方案执行后，由夹具把观察窗结果固定为故障态；VERIFY 只反证该 `INTERVENTION`，自动回滚后返回 PLAN。第二个方案成功，失败知识保留，`needs_attention=0`。
- 只读多跳：`long_idle_transaction -> connection_exhaustion -> conn_near_limit` 经动态工具规划取得两类证据，必须先过 ESC，再持久化 REPORT 并进入 DONE；无 proposal、GATE 或 SQL。
- 双根因：`missing_index` 与 `autovacuum_starvation` 同时成立。索引路径的预期效果成功但总 KPI 未恢复，归因为 `CONTEXT`，回滚测试索引，不反证任一正确根因或路径边。
- manual P0：disk / slot / prepared 都从正式路径召回开始，绑定充分证据并通过 ESC；随后只生成无 SQL 的 `escalate_only` 计划，不进入 GATE/EXECUTE。prepared 的下游可执行节点不能绕开其 manual 根因。
- P0 live：autovacuum 真实 reloption 经路径召回、动态取证、ESC、CONFIRM GATE、EXECUTE、VERIFY 后用 undo 恢复并 cleanup；prepared transaction 和 physical replication slot 使用真实 PostgreSQL 对象验证 oracle，disk 使用容量 provider。四类注入和重复清理全部通过。
- L1-L4 replay：开启后路径排序和工具选择发生变化；同一 autovacuum CONFIRM 与 slot DENY 提案在学习记录写入前后的 GATE 签名完全一致。

Episode C 验证的是失败归因和回滚控制流，不证明第一次索引在真实负载下自然无效。

## 核心指标

### 最新 W4 Episode

指标来自最终重跑生成的 `ep_missing_index_orders_user_status_v1_1788400618`，由 `eval.metrics_v2.compute_episode_metrics` 在持久化 explanation 的 `updated_at` 时点重算；这样 replay 使用事故时点而不是当前墙钟判断 freshness。该 Episode 的完整路径真值来源是 `inferred_from_root`，不是人工标注的逐边真值，因此 path/edge 指标只能作为当前 fixture 的一致性检查。

| 指标 | 结果 |
|---|---:|
| root recall@1 / @3 / @5 / @12 | 1/1，1/1，1/1，1/1 |
| path recall@1 / @3 / @5 / @12 | 1/3，2/3，2/3，3/3 |
| 当前症状可达 P0 obligation recall | 3/3 |
| observed symptom coverage | 3/3 |
| unexplained symptom rate | 0/3 |
| selected edge precision / recall / F1 | 3/3，3/3，6/6 |
| required evidence completion | 2/2 |
| raw_ref validity / freshness | 62/62，62/62 |
| duplicate EvidenceBinding identity rate | 0/62 |
| ESC unsafe pass | 0/1 |
| ESC over-conservative | 0/0；该 Episode 没有 denial 分母 |
| GATE causal-context bypass | 0 |
| expected effect hit | 1/1 |
| failure attribution accuracy | 1/1 |
| rollback completeness | 1/1 |
| planner tool calls / information gain / UNKNOWN+ERROR / repeats | 0/0；W4 使用 `ScriptedPolicy`，没有 `tool_learning_observation`，不得据此声称工具成本下降 |

### 受控 fixture 与 P0

| 指标 | 结果与边界 |
|---|---|
| 四 P0 recall | 4/4；独立 P0 fixture 覆盖可达/不可达、普通预算外保留、截断阻断和逐项解决 |
| 工具调用 | 2 次物理调用；总 entropy gain 0.5，即 0.25/call |
| UNKNOWN/ERROR rate | 1/2；只降权，不产生反证 |
| 重复工具调用率 | 0/2 |
| ESC verdict | 独立验收覆盖 `SUFFICIENT/INSUFFICIENT/AMBIGUOUS/EXHAUSTED` 的确定性分支；这不是生产分布 |
| GATE 学习前后 | autovacuum 始终 `CONFIRM`；stale slot 始终 `DENY/P0_MANUAL_REQUIRED`；L1-L4 开关不改变结果 |

2026-09-03 的指标复核发现并修复了一个 replay 计时缺陷：`required_evidence_completion` 过去忽略 `compute_episode_metrics(..., now=...)`，使用墙钟调用 `binding.is_trusted()`，会把历史上已通过 ESC 的新鲜证据误报为 `0/2`，并连带误报 `esc_unsafe_pass=1/1`。修复后 `_selected_required_evidence` 显式传递 replay 时点；新鲜 fixture 为 2/2、过期 fixture 为 0/2 且分母保留，W4 为 2/2 和 0/1。

## 残余问题

1. v2 没有 sandbox/production 学习样本。下一阶段需要积累带 provenance、split 和完整 effect scope 的 episode，再报告每层收益和置信区间。
2. 动态工具规划的调用下降只在受控消融中观察到。W4 使用 `ScriptedPolicy`，相关分母为 0/0；真实事故上还缺“信息增益/调用、重复率、UNKNOWN/ERROR 率”的基线与对照。
3. W4 成功修复目前集中在 `missing_index`。autovacuum 已跑完整 CONFIRM/执行/验证/undo；disk、slot、prepared 按设计只能升级人工，不应把“未自动修复”当成待补功能。
4. 多根解释子图可以诊断，但一个 plan 只处理一个干预目标。独立双根因仍需顺序处理或升级人工。
5. 四个 connection/misleading train/eval 场景没有稳定随机化轴。`harness_lint` 保留警告，不能用不稳定参数制造表面上的随机化覆盖。
6. v1 的 25 条结构提案不可信，不能按 support 次数迁移。v2 结构学习还没有真实跨 episode 提案。
7. `hypothesis_candidates`、`report_id` 和 v1 projection 仍有兼容消费者。删除前需要单独做 reader inventory 和存量 trace 重放。
8. 沙箱不等于生产环境。现阶段生产部署应只开放只读诊断，写操作限于沙箱或预发环境。
9. W4 的完整路径真值由 root 和观测症状推导，不是人工逐边标注。其 3/3 edge precision/recall 不能外推为未知事故上的图精度。
10. disk P0 使用容量 provider 验证门控，没有真实填满文件系统；这是为了避免破坏测试主机，不能写成真实磁盘耗尽演练。

## 测试命令与结果

### 离线与静态回归

| 检查 | 结果 |
|---|---|
| `git diff --check` | PASS |
| `.dev/checkall.sh` | PASS，失败脚本数 0 |
| `graph_lint.py` | PASS，57 节点 / 100 边 |
| `harness_lint.py` | PASS，保留 1 条随机化警告，涉及 4 个 connection/misleading 场景 |
| `p0_recall_check.py` | PASS，四个 P0 的图召回和义务不变量 |
| `p0_gate_check.py` | PASS，disk/slot/prepared 充分证据后只升级人工 |
| `evolution_v2_check.py` | PASS，L1-L4 writer/reader/消费/消融和安全签名 |
| `eval_metrics_v2_check.py` | PASS，含 replay freshness 回归 |

```bash
cd /mnt/c/Users/卢佳尧/Documents/pgdoctor
source /opt/pgdoctor-venv/bin/activate
export PGDOCTOR_PORT=55433 PGDOCTOR_DB=shop PYTHONPATH=.

git diff --check
bash .dev/checkall.sh
python3 .dev/graph_lint.py
python3 .dev/harness_lint.py
python3 .dev/p0_recall_check.py
python3 .dev/p0_gate_check.py
python3 .dev/evolution_v2_check.py
python3 .dev/w4_check.py
```

### 合成/受控 replay

| 检查 | 结果 |
|---|---|
| `e2e_explanation_check.py` | PASS，只读多跳、动态取证、ESC、REPORT、DONE，以及双根因 context failure |
| `w4_check.py` | PASS，A/B/C：成功路径、恶意 SQL 拒绝、错误方案窄作用域反证/回滚/二次成功 |
| `explanation_model_check.py` | PASS，稳定 ID、round-trip、缺字段、v1 读取、重复恢复、可信 raw_ref |
| `path_recall_check.py` / `p0_obligation_check.py` | PASS，多跳、多样性、探索、P0 预算/截断 |
| `evidence_predicate_check.py` | PASS，结构化方向、scope、窗口/source epoch、UNKNOWN/ERROR 和 raw_ref 去重 |
| `dynamic_tool_planner_check.py` / `subagent_path_task_check.py` | PASS，四重交集、1–3 工具、环境 fail-closed、局部任务、无 verdict、迟到幂等 |
| `esc_explanation_check.py` / `causal_gate_context_check.py` | PASS，四 verdict、P0/新鲜度/PARTIAL、版本和伪造字段拒绝 |
| `causal_verify_check.py` / `terminal_done_check.py` | PASS，effect scope、失败归因、rollback 和唯一 DONE 终态 |
| `evolution_v2_check.py` / `structure_v2_check.py` | PASS，L1-L4 行为消融、共现不成边、批准后才 promote、v1 不加载 |

这些检查中难以稳定注入的长级联和错误修复结果来自受控 trace/fixture，不作为生产成功率。

### WSL PostgreSQL 16 活库

| 检查 | 结果 |
|---|---|
| `p0_recall_check.py --live` | PASS，四类 P0 注入/oracle/重复 cleanup；disk 为容量 provider，另三类使用 PostgreSQL 对象/配置 |
| `p0_gate_check.py --live` | PASS，autovacuum 证据、ESC、PLAN、CONFIRM GATE、EXECUTE、VERIFY、undo、cleanup |
| `w4_check.py` | PASS，真实 `shop` 库上的 missing-index A/B/C |
| 活库残留审计 | PASS：replication slot 0、prepared transaction 0、表级测试 reloptions 0、测试索引 0、测试角色连接 0、undo `needs_attention` 0 |

活库命令：

```bash
python3 .dev/p0_recall_check.py --live
python3 .dev/p0_gate_check.py --live
python3 .dev/w4_check.py
```

验收后还要确认：没有 W4/负载生成器进程；只保留 golden 基线索引；没有测试 replication slot 或 prepared transaction；用户表没有测试 reloptions；undo journal 的 `needs_attention` 为 0。
