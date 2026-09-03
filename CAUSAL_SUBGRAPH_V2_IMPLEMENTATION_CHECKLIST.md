# pgDoctor 因果解释子图 v2：Code Agent 实施清单

这份文档是实施规格，不是方向性建议。Code Agent 应按顺序完成，每个批次都先读现有实现、做小范围修改、运行对应验收，再进入下一批。除非本清单明确要求，不要重写现有因果图，不要删除旧轨迹或旧学习产物，不要回滚工作区里已有的未提交修改。

## 0. 最终目标

把当前“因果图召回一组根因 ID，再逐个调查”的流程，升级为“生成、验证、选择和执行一个 Episode 级解释子图”的流程。

最终系统必须同时满足：

- 因果图仍负责多跳推理，不退化成根因清单。
- `Cause / Mechanism / Symptom` 是一条具体路径中的动态角色，不是节点的互斥永久类型。
- `HYPOTHESIZE` 生成候选路径，`INVESTIGATE` 验证路径和分叉，`DIAGNOSE` 选择解释子图。
- Subagent 的任务单位是路径分叉或路径片段，不是单个根因。
- 工具由系统依据当前路径前沿动态分配；Subagent 不自行获得完整工具箱。
- ESC 判断“解释子图是否足以进入 PLAN”，GATE 判断“选定干预是否与解释子图、安全策略和证据绑定”。
- VERIFY 验证干预对路径下游的预测，ROLLBACK 只反证正确的作用域，不能因为一个修复失败就直接否定整个根因。
- L1-L4 学习的对象全部改成解释路径、条件化调查决策、边/路径权重和工具信息增益，并且在线决策确实消费学习结果。
- 所有可达 P0 分支都成为显式义务，不能被 top-k、平均排除率或普通候选预算稀释。

## 1. 当前基线：必须保留，不能重做坏

以下能力已经完成，是 v2 的迁移基线：

- 累计统计使用事故窗口差分；基线持久化在 `EpisodeState.cumulative_baselines`。
- `EvidenceStatus` 已有 `OBSERVED / UNKNOWN / ERROR`；后两者不能支持或反证假设。
- P0 在 `HYPOTHESIZE` 召回，ESC 只消费已经持久化的候选。
- 当前四个 P0：`autovacuum_starvation`、`disk_pressure`、`stale_replication_slot`、`orphaned_prepared_transaction`。
- `get_vacuum_horizon`、`get_database_stats`、工具权限和 `raw_ref` 已接通。
- autovacuum 修复需要 `CONFIRM`；disk、prepared transaction、replication slot 只能升级人工；P0 永不 `AUTO`。
- Shield、pglast AST 校验、risk tier、blast radius、rollback、undo journal、只读/写凭据隔离必须原样保留。
- WSL PostgreSQL 16 端口 `55433` 已验证；CPU 采集有 `psutil` 回退。
- 现有 `.dev/cumulative_evidence_check.py`、`.dev/p0_recall_check.py`、`.dev/p0_gate_check.py` 和既有回归必须继续通过。

开始编码前先运行并保存基线结果：

```bash
git status --short
git diff --check
bash .dev/checkall.sh
python3 .dev/graph_lint.py
python3 .dev/harness_lint.py
python3 .dev/p0_recall_check.py
python3 .dev/p0_gate_check.py
```

如果基线本来就有失败，记录失败项，不要把无关修复混入本次改造。

## 2. 不可妥协的设计约束

- 保留 `knowledge/causal_graph/nodes.yaml` 中现有静态类别：`Symptom`、`RootCause`、`Evidence`、`Fix` 仍用于 schema 校验和工具映射。
- 不新增静态 `Cause/Mechanism/Symptom` 三分法。运行时角色由节点在选中路径中的位置决定。
- 同一节点可以在一条路径上是根，在另一条更长路径上是 mechanism。例如 `autovacuum_starvation` 在 `autovacuum_starvation -> table_bloat -> disk_pressure -> disk_growing` 中是上游原因，在 `stale_replication_slot -> autovacuum_starvation -> ...` 中是中间机制。
- 路径统一按图中 `CAUSES` 方向保存：`上游原因 -> 中间机制 -> 观测症状`。不要一部分 API 正向、一部分 API 反向。
- 模型可以提出调查意图和修复提案，但不能自行填写可信的路径状态、证据方向、ESC 结果或 GATE 因果上下文。
- 支持/反证必须由确定性 predicate 根据结构化工具结果计算，不能根据模型写的 note 或自然语言摘要计算。
- 同一 `raw_ref` 在同一判断中只能计分一次；`UNKNOWN/ERROR` 永远不计为支持或反证。
- 学习只能调整召回、排序和调查顺序，不能降低 ESC/GATE 的证据门槛。
- 学习系统不能在运行时自动新增生效节点、`CAUSES`、`CONFIRMED_BY`、`REFUTED_BY` 或 `FIXED_BY` 边。结构变化只能形成提案，人工批准后才能进入 promoted 图。
- `knowledge/learned/candidate_edges.yaml` 当前有明显共现污染，禁止直接 promote。

## 3. 先落地统一数据契约

主要修改 [agent/episode_state.py](agent/episode_state.py)，必要时新建 `agent/explanation.py`。模型必须可 JSON 序列化，所有 ID 稳定、可重放。

### 3.1 枚举

- [ ] 保留 `EvidenceStatus = OBSERVED | UNKNOWN | ERROR`。
- [ ] 新增 `CausalStatus = UNTESTED | SUPPORTED | REFUTED | INCONCLUSIVE`，用于节点和边；不要把采集失败混进因果裁决枚举。
- [ ] 新增 `DynamicRole = OBSERVED_SYMPTOM | MECHANISM | ROOT_CAUSE`。角色记录在“路径中的节点引用”上，不写回静态图节点。
- [ ] 新增 `ObligationStatus = OPEN | SUPPORTED | REFUTED | INCONCLUSIVE | UNAVAILABLE`。
- [ ] 新增 `InterventionKind = CORRECTIVE | MITIGATION | CONTAINMENT | MANUAL`。
- [ ] 新增 `ExplanationScope = FULL | PARTIAL`。

### 3.2 `EvidenceBinding`

至少包含：

```text
binding_id
episode_id
raw_ref
evidence_type
status                       # EvidenceStatus
observed_at
window_start / window_end    # 窗口证据必须填写
source_epoch                 # 累计统计重置/重启识别
target_node_ids
target_edge_ids
predicate_id
predicate_result             # SUPPORTS / REFUTES / NEUTRAL / NOT_APPLICABLE
summary
value_digest                 # 防止摘要被改后仍冒充原始证据
fresh_until
```

- [ ] `raw_ref` 必须属于当前 Episode，格式和 trace 文件都可核验。
- [ ] predicate 读取结构化工具结果，不读取 `summary` 里的中文关键词。
- [ ] 同一 `raw_ref + predicate_id + target` 生成稳定的 `binding_id`，重复调用绑定逻辑不得重复计分。
- [ ] 没有 `raw_ref` 的 agent note 可以保留在 scratchpad，但不能成为 ESC/GATE 的可信 `EvidenceBinding`。

### 3.3 `CausalPath`

至少包含：

```text
path_id                       # graph_version + 有序节点/边计算的稳定 ID
node_ids                      # 上游原因到症状
edge_ids
observed_symptom_id
root_node_id
node_roles                    # path-local dynamic roles
score_components
source                        # graph / case_template / exploration
status
required_evidence_types
evidence_binding_ids
```

- [ ] 单节点“路径”不算解释链；至少包含一条 `CAUSES` 边。
- [ ] 中间所有 cause 节点的角色为 `MECHANISM`，最上游节点为 `ROOT_CAUSE`，已观测末端为 `OBSERVED_SYMPTOM`。
- [ ] 同一条结构路径不因不同召回来源生成多个 `path_id`；来源合并到列表或 score components。

### 3.4 `P0Obligation`

至少包含：

```text
cause_id
reachable_path_ids
status
required_evidence_types
evidence_binding_ids
resolution_reason
truncated                     # P0 路径枚举被截断时必须为 true
```

- [ ] 每个从当前观测症状可达的 P0 原因都必须有一项义务。
- [ ] P0 不占普通候选预算。
- [ ] `truncated=true` 的 P0 义务不能被 ESC 判为已充分排除。

### 3.5 `ExplanationGraph`

至少包含：

```text
explanation_id
schema_version                # 固定为 2
graph_version
revision
episode_id
observed_symptoms
candidate_paths
node_status
edge_status
evidence_bindings
selected_path_ids
selected_root_causes
unexplained_symptoms
p0_obligations
scope                         # FULL / PARTIAL
created_at / updated_at
```

- [ ] `revision` 每次可信状态变化递增，GATE 用它防止旧计划绑定新解释。
- [ ] `selected_root_causes` 必须从 `selected_path_ids` 推导，不能由模型单独写。
- [ ] 多条路径可以共享 mechanism 或 symptom；合并后不能丢边状态和证据绑定。
- [ ] `unexplained_symptoms` 必须显式记录，不能因召回不到路径就从上下文消失。

### 3.6 `EvidenceNeed` 和 `EvidenceReport`

`EvidenceNeed` 是系统发给工具规划器/Subagent 的任务：

```text
need_id
path_ids
target_kind                   # NODE / EDGE / BRANCH / P0 / INTERVENTION
target_ids
evidence_type
predicate_id
required
freshness_seconds
candidate_tools
reason
```

`EvidenceReport` 是 Subagent 的唯一结构化回传：

```text
need_id
tool
raw_refs
observations
collection_status             # OBSERVED / UNKNOWN / ERROR
limitations
```

Subagent 不返回 `CONFIRMED/REFUTED`，方向由 predicate 层计算。新增 `report_evidence` 工具；旧 `report_verdict` 仅保留为 v1 兼容入口，v2 流程不得调用。

### 3.7 `InterventionPlan` 和 `CausalGateContext`

`InterventionPlan` 至少包含：

```text
plan_id
explanation_id / explanation_revision
selected_path_id
intervention_target
fix_id
intervention_kind
action_type / sql / rollback
expected_effect_nodes
expected_effects              # 指标、方向、阈值、观察窗口
rationale
```

`CausalGateContext` 只能由状态机根据持久状态构造：

```text
explanation_id / explanation_revision
selected_path_ids
intervention_target
fix_id
intervention_kind
expected_effect_nodes
esc_report_id
evidence_refs
unresolved_p0_paths
```

模型提交的对象中不得接受同名“可信字段”覆盖系统值。

### 3.8 旧轨迹兼容

- [ ] `EpisodeState` 新增 `explanation_graph`、`esc_reports`、`intervention_plan`，保留 `hypothesis_candidates`、`ledger`、`claimed_fault_class` 作为 v1 兼容投影。
- [ ] 新轨迹写 `schema_version=2`；旧轨迹无版本时按 v1 加载。
- [ ] v1 加载后可以构造最小只读投影，但不能伪造中间边已被验证。
- [ ] `render_context()` 优先展示选中路径、未决分叉、P0 义务和缺证据；v1 才展示旧根因台账。
- [ ] 增加 round-trip、缺字段、旧轨迹读取、重复恢复四类测试。

完成标准：单独运行数据模型测试时，保存、加载、再次保存内容稳定；没有因 `asdict`/enum/默认字段造成轨迹丢失。

## 4. 扩展图运行时：从根因召回改为路径召回

主要修改 [knowledge/causal_graph/graph.py](knowledge/causal_graph/graph.py)。保留 `candidate_causes()` 供 v1/旧测试使用，新增 v2 API，不要在旧函数里塞入全部新语义。

### 4.1 新 API

- [ ] `enumerate_causal_paths(symptoms, max_hops=4, ...)`：枚举 `CAUSES` 简单路径，输出 `CausalPath`，拒绝环和重复节点。
- [ ] `merge_paths(paths)`：合并共享节点/边，生成候选 `ExplanationGraph`。
- [ ] `path_frontier(explanation)`：返回离已观测区域最近、尚未判定且最能区分路径的节点/边。
- [ ] `evidence_needs(explanation)`：从 required/supporting/refuting/discriminator 关系生成 `EvidenceNeed`。
- [ ] `alternatives_for(path_id)`：找共享症状或前缀、但在关键分叉处不同的路径。
- [ ] `intervention_options(path_id)`：只返回路径节点上合法的 `FIXED_BY` 干预。
- [ ] `downstream_on_path(path_id, node_id)`：GATE/VERIFY 只使用选中路径的下游，不使用整个图的无界后代集合。
- [ ] `graph_version()`：由种子 YAML 与 promoted delta 的内容计算，写入解释对象和学习产物。

### 4.2 候选路径召回策略

默认配置先写入一个集中配置对象，不要散落魔数：

```text
max_hops = 4
ordinary_path_budget = 12
max_paths_per_root_symptom = 3
exploration_path_budget = 2
p0_max_paths_per_cause = 20
```

召回必须按以下顺序：

- [ ] 从每个已映射观测症状反向枚举所有可达原因路径。
- [ ] 对每个可达 P0 建义务；P0 根因不受 `ordinary_path_budget` 限制。
- [ ] 普通路径先保证症状覆盖，再保证关键第一分叉的多样性，然后按分数填满预算。
- [ ] 至少保留 `exploration_path_budget` 条非最高先验但结构不同的路径，防止学习把候选集锁死。
- [ ] 相似案例只能增加已有图路径的召回/分数；案例中的未知节点只能进入结构提案，不能直接进入 live explanation。
- [ ] 超出 P0 路径上限时标记 `P0Obligation.truncated=true`，ESC 必须阻止自动进入 PLAN。

路径评分必须保留可解释分量，不只存一个总分：

```text
手工 CAUSES likelihood
+ 有上限的根因 prior 调整
+ 有上限的 L1 路径模板相似度
+ 有上限的 L3 边/路径调整
+ 症状覆盖奖励
- hop penalty
- 与已选路径的冗余惩罚
```

P0 是否进入候选不能依赖分数。所有学习调整必须有相对手工先验的上限，并支持 `use_learned=False` 消融。

### 4.3 图 lint

- [ ] 每个 `CAUSES` 路径方向一致、节点存在、无意外自环。
- [ ] 每个 required evidence 都有真实 `obtained_by` 工具和 predicate。
- [ ] 每个 live fix 都有 action type、risk tier、rollback 语义、intervention kind、expected effect 或 manual 声明。
- [ ] P0 必须有 required evidence；自动可执行 P0 必须不是 `AUTO`。
- [ ] promoted delta 只能来自批准状态；candidate/ready 不进入运行图。

完成标准：多跳真因不能因为离症状超过一跳而丢失；扩大召回后，同症状的候选不只是同一类路径的重复项。

## 5. 修正 `FIXED_BY` / `REFUTED_BY` 的语义

这一步会修改 [knowledge/causal_graph/nodes.yaml](knowledge/causal_graph/nodes.yaml)、[knowledge/causal_graph/edges.yaml](knowledge/causal_graph/edges.yaml) 和图加载器，但不改静态节点类别。

### 5.1 `FIXED_BY` 改成干预关系

给 fix 或关联边增加：`intervention_kind`、`preconditions`、`expected_effect_nodes`、`execution`。

- [ ] `missing_index -> create_covering_index`：`CORRECTIVE`。
- [ ] `stale_statistics -> analyze_table`：`CORRECTIVE`。
- [ ] `table_bloat -> vacuum_table`：标记 `MITIGATION`；普通 VACUUM 不会把文件空间返还文件系统，不能声称物理膨胀已纠正。
- [ ] `lock_contention -> terminate_blocker`：`CONTAINMENT`，只允许最上游阻塞者，并保持不可逆确认。
- [ ] 新增语义准确的 `terminate_idle_transaction`，替换 `long_idle_transaction -> terminate_blocker`；它是 `CONTAINMENT`，必须绑定具体 PID、事务年龄、角色和阻塞影响。
- [ ] `autovacuum_starvation -> enable_autovacuum`：`CORRECTIVE`，仍为 P0 + `CONFIRM`。
- [ ] `connection_exhaustion -> terminate_idle_backend`：`CONTAINMENT`，不得终止诊断连接、系统后台或非 idle 会话。
- [ ] `xid_wraparound_risk -> vacuum_database`：只有确认不存在仍持有 xmin 的 slot/prepared/long transaction，且目标数据库/表可 vacuum 时才能计划；否则先处理/升级上游阻塞者。
- [ ] `disk_pressure -> remediate_disk_capacity`：`MANUAL + escalate_only`，保持不能映射到普通 VACUUM。
- [ ] stale slot、prepared transaction 保持 `MANUAL + escalate_only`。
- [ ] `work_mem_spill -> raise_work_mem`：`MITIGATION`；只做会话/事务局部设置，必须校验并发度和总内存预算，禁止据一次外溢盲目改全局。
- [ ] `checkpoint_pressure -> raise_max_wal_size`：`MITIGATION + escalate_only`；前置检查归档、磁盘余量、WAL 保留者和 checkpoint 窗口。
- [ ] 删除 `deadlock -> terminate_blocker`；死锁已由 PostgreSQL 中止一方。新增 `remediate_deadlock_pattern`，描述事务顺序/重试策略，设为 `MANUAL + escalate_only`。

### 5.2 `REFUTED_BY` 必须带作用域和 predicate

每条反证关系增加 `predicate_id` 和 `scope = NODE | PATH | INTERVENTION`，需要事故窗口的再增加 `window_required: true`。

- [ ] `counterfactual_index` 只反证当前具体索引定义/`create_covering_index` 方案，`scope=INTERVENTION`；不得反证整个 `missing_index` 节点。
- [ ] 删除“dead tuple ratio 很低即可反证物理 table bloat”的节点级反证。没有可靠物理膨胀证据时应 `INCONCLUSIVE/UNKNOWN`，不能硬排。
- [ ] `dead_tuple_ratio` 也不能单独确认物理 `table_bloat`。增加真正测量物理膨胀的结构化证据（例如明确算法和可用性状态的 `physical_bloat_ratio`）；工具/扩展不可用时保持 `UNKNOWN`。在该证据落地前，不允许 table-bloat 路径通过 required evidence 门。
- [ ] 锁链为空只反证当前事故窗口的锁路径，不形成永久根因级负例。
- [ ] deadlock/temp/checkpoint 的累计值反证必须绑定同一事故窗口、同一 source epoch，且继续使用差分值。
- [ ] slot/prepared/autovacuum/disk 的反证继续使用已修正的结构化判据；`UNKNOWN/ERROR` 不得触发反证。
- [ ] `when` 人话说明可以保留给文档，但运行代码只认 `predicate_id`。

建议新增 `knowledge/evidence_predicates.py`，把 [agent/esc.py](agent/esc.py) 当前 `_supports()` 中的自然语言解析迁出。每个 predicate 输入结构化 value、窗口信息和目标，输出 `SUPPORTS / REFUTES / NEUTRAL / NOT_APPLICABLE` 及机器可审计理由。

完成标准：同一证据对“节点、某条路径、某个修复方案”的影响不会串级；反证一个索引方案不会杀死 missing-index 的其他方案。

## 6. 十三阶段 MAPE-K 改造

主要修改 [agent/loop.py](agent/loop.py)、[agent/state_machine.py](agent/state_machine.py)、[agent/policy.py](agent/policy.py)、[agent/llm_policy.py](agent/llm_policy.py)。十三个阶段保留，阶段内对象改为解释子图。

### 6.1 各阶段职责

- [ ] `MONITOR`：记录健康基线、事故起点、指标窗口和 source epoch；不生成根因。
- [ ] `OBSERVE`：采集首轮只读快照，将告警映射为图症状，保留无法映射的原始症状。
- [ ] `HYPOTHESIZE`：调用路径召回，生成并持久化 `ExplanationGraph`、P0 义务和案例路径先验。这里不确认路径。
- [ ] `INVESTIGATE`：循环计算路径前沿和 `EvidenceNeed`，派发分叉/路径片段给 Subagent，绑定工具证据，更新节点/边/P0 状态。
- [ ] `DIAGNOSE`：选择能共同解释观测的最小一致子图，生成 `selected_path_ids`、派生根因、未解释症状和 FULL/PARTIAL scope。不能只写一个根因字符串。
- [ ] `PLAN`：在选中路径上选择干预节点和合法 `FIXED_BY`，明确 corrective/mitigation/containment/manual 与下游预期效果。
- [ ] `GATE`：确定性构造 `CausalGateContext`，先做因果绑定校验，再走现有安全门。
- [ ] `EXECUTE`：仍是唯一写区，只执行 GATE 已批准且版本未过期的计划。
- [ ] `VERIFY`：按 `expected_effect_nodes` 和观察窗口验证下游变化，同时跑现有 KPI/回归检查。
- [ ] `ROLLBACK`：撤销可撤销操作，登记“干预/路径片段/节点”中正确的失败作用域，决定回 PLAN、INVESTIGATE 或 HYPOTHESIZE。
- [ ] `REPORT`：报告完整因果链、关键证据、未解释症状、P0 结论、干预类型和验证结果。
- [ ] `ESCALATE`：报告缺失证据、未决分叉、不可自动处置 P0、手工操作建议，不提交数据库写入。
- [ ] `DONE`：唯一终态。

### 6.2 状态转移修正

- [ ] 修复 `StateMachine.terminal()`：只在 `DONE` 返回 true。当前把 `REPORT/ESCALATE` 当终态会导致 DONE 通常不执行。
- [ ] 给 loop 增加确定性的 `REPORT -> DONE`、`ESCALATE -> DONE` 处理，并设置 `finished=True`、最终保存。
- [ ] ESC `INSUFFICIENT` 回 `INVESTIGATE`；新症状或候选覆盖不足才回 `HYPOTHESIZE`。
- [ ] GATE 因证据过期/目标证据缺失而拒绝时允许回 `INVESTIGATE`；仅 SQL/rollback/方案形态问题回 `PLAN`。
- [ ] ROLLBACK 后：干预方案失败但路径仍支持时回 `PLAN`；路径片段被反证时回 `INVESTIGATE`；候选根改变或出现新症状时回 `HYPOTHESIZE`；无法安全继续时 `ESCALATE`。
- [ ] 每次回退都从 `EpisodeState` 重建上下文，不恢复旧 prompt 缓存。

### 6.3 去除压平

- [ ] 删除 [agent/loop.py](agent/loop.py) 中“`candidate_details` 立即压成 `ctx['candidates']` 并以 ID 为主对象”的 v2 路径。
- [ ] `ctx` 只保留解释图 ID/revision、当前 frontier 和紧凑投影；完整对象从持久状态读取。
- [ ] `hypothesis_candidates` 只作为 v1 兼容投影，不再是 ESC/GATE 的 v2 真相源。

完成标准：轨迹中能看到一条路径如何从 UNTESTED 逐段变成 SUPPORTED/REFUTED，而不是只看到根因 verdict 变化。

## 7. 动态工具规划与 Subagent 改造

主要修改 [agent/investigator.py](agent/investigator.py)、[agent/orchestrator.py](agent/orchestrator.py)、[agent/permissions.py](agent/permissions.py)、[agent/toolbox.py](agent/toolbox.py)、[agent/hooks.py](agent/hooks.py)。建议新增 `agent/tool_planner.py`。

### 7.1 有效工具集

实现唯一公式：

```text
有效工具集 = 阶段白名单
          ∩ Subagent 角色白名单
          ∩ 当前 EvidenceNeed 候选工具
          ∩ 当前环境实际可用工具
```

- [ ] `permissions.allowed_tools()` 改为接受 `EvidenceNeed`/任务上下文，不再只接受单个 `hypothesis`。
- [ ] 取消“图推导为空时退回完整工具集”的 v2 行为。推导为空应生成 `UNAVAILABLE` need 或升级，不应扩大权限。
- [ ] 环境可用性检查至少覆盖：方法是否存在、只读角色权限、扩展是否安装、当前阶段、目标对象是否明确。
- [ ] hooks、工具 schema 暴露和运行时 `_enter()` 校验必须调用同一权限权威，避免展示可用但调用被拒。
- [ ] v2 INVESTIGATOR 永远拿不到 `set_hypothesis`、`declare_root_cause`、`submit_proposal`。

### 7.2 工具选择算法

系统对当前 frontier 生成工具候选，并按以下可审计分量排序：

```text
路径/分叉区分力
+ 覆盖的未决路径数
+ required evidence 奖励
+ P0 义务奖励
+ L2 条件策略收益
+ L4 预期信息增益
- 调用延迟/资源成本
- UNKNOWN/ERROR 历史概率
- 同 raw_ref/同 evidence type 重复惩罚
```

- [ ] 每个 Subagent 默认只得到 1-3 个工具。
- [ ] required/P0 证据不能被学习排序永久裁掉；L4 只能在安全必需集合内排序，或裁掉明确冗余项。
- [ ] 保留可配置探索比例，默认 10%，用于选择一个低样本但合法的工具；固定随机种子时可重放。
- [ ] 一个工具能同时解决多个 need 时合并调用，分别生成 EvidenceBinding，不重复查询。
- [ ] 已有且仍新鲜的证据不重复调用；过期证据生成新 need。

### 7.3 Subagent 任务边界

- [ ] 任务单位改为“验证某个分叉”或“验证路径 A 的节点 X -> Y”，不是“调查 root_cause X”。
- [ ] prompt 只提供相关局部子图、EvidenceNeed、工具列表、事故窗口和输出 schema。
- [ ] Subagent 只能报告观察值、raw_ref、采集状态和局限；不能给最终根因 verdict。
- [ ] 主流程收到 `EvidenceReport` 后，由 predicate 层生成 EvidenceBinding，再确定节点/边状态。
- [ ] 并行结果合并必须按 `need_id` 和 `binding_id` 幂等；迟到结果若 explanation revision 已变化，只能作为证据候选重新校验，不能直接覆盖状态。

完成标准：同一个根因在不同路径前沿会拿到不同工具；同一分叉的不同工具结果会改变下一次选择；权限矩阵仍通过且实际暴露工具不超过计算集合。

## 8. ESC 改为解释子图充分性检查

主要修改 [agent/esc.py](agent/esc.py)。保留 `SUFFICIENT / INSUFFICIENT / AMBIGUOUS / EXHAUSTED` 四个外部 verdict，但报告对象升级并持久化 `esc_report_id`。

ESC 只读取：持久化 ExplanationGraph、可信 EvidenceBinding、当前图版本和 Episode 预算。不要重新召回候选，不要解析模型自述。

### 8.1 必查条件

- [ ] 症状覆盖：每个观测症状都在选中路径中，或明确进入 `unexplained_symptoms`。
- [ ] 上游直接证据：每个选中根因的 required evidence 都是当前 Episode 的 `OBSERVED` 支持绑定。
- [ ] 因果连续性：关键 mechanism、分叉和所选路径的关键边有支持证据，不能从根因直接跳到远端症状。
- [ ] 替代路径：主要竞争路径已经被有作用域的证据区分；不能只统计 ledger 中写了多少个 REFUTED。
- [ ] P0：所有可达 P0 义务已被支持或被当前、可审计的证据排除；`OPEN/INCONCLUSIVE/UNAVAILABLE/truncated` 均不得放行。
- [ ] 来源与新鲜度：raw_ref 属于当前 Episode，trace 存在，证据未过期，累计值来自同一 source epoch。
- [ ] 去重：同一 raw_ref 不得同时当多条独立证据增加充分度。
- [ ] 图版本：解释图基于的种子/promoted 版本仍是当前版本；版本变化后重新诊断。

### 8.2 verdict 规则

- [ ] `SUFFICIENT`：必需条件全过，得到一张内部一致的选中解释子图；报告 FULL/PARTIAL scope。一张子图可以包含多个有证据的独立根因。
- [ ] `INSUFFICIENT`：存在可通过允许工具补齐的明确 need；返回按优先级排序的 EvidenceNeed/directives。
- [ ] `AMBIGUOUS`：互相竞争、不能同时成立的解释仍有相当支持，或未解释症状指向未决独立根因/P0，继续取证也没有明显高价值分叉。多个根因若分别解释不同症状且各自证据充分，应合并为多根解释子图，而不是仅因根因数量大于一就判 AMBIGUOUS。
- [ ] `EXHAUSTED`：预算耗尽，或 required 证据因权限/环境长期不可得，不能安全继续。

PARTIAL 不能静默冒充 FULL：

- [ ] 未解释症状若可能连接未决 P0 或独立高风险路径，必须 `AMBIGUOUS/INSUFFICIENT`。
- [ ] 允许 PARTIAL `SUFFICIENT` 时，必须记录 `partial_fix_suspected=true`，后续不得 AUTO，预期效果只能覆盖选中路径；VERIFY 失败不能直接反证根因。

完成标准：扩大候选召回不会线性抬高普通“排除一半”的负担，但每条可达 P0 仍必须单独解决；用 note 无脑排除候选不能通过。

## 9. PLAN 与 GATE 的因果绑定

主要修改 [agent/toolbox.py](agent/toolbox.py)、[safety/gate.py](safety/gate.py)、[agent/loop.py](agent/loop.py)。

### 9.1 PLAN

- [ ] 只允许在 `selected_path_ids` 的节点上选干预目标。
- [ ] fix 必须来自该目标的 `FIXED_BY`，并满足 preconditions。
- [ ] 明确 `InterventionKind`；mitigation/containment 不能在 rationale 或报告里声称“根因已消除”。
- [ ] `expected_effect_nodes` 必须是干预目标在选中路径上的下游子集。
- [ ] 每个 expected effect 都要有指标、方向、最低变化、观察窗口；不能只写自由文本 predicted impact。
- [ ] manual/escalate-only fix 不生成 SQL 提案，直接生成带证据的升级计划。

### 9.2 GATE

先做因果校验，再执行现有 Shield/AST/risk/blast-radius/rollback 校验：

- [ ] explanation ID/revision 与当前状态一致。
- [ ] selected path、intervention target、fix ID 真实存在且相互绑定。
- [ ] ESC report 属于同一 explanation revision，结果为 SUFFICIENT。
- [ ] evidence refs 是该路径/目标实际绑定的当前新鲜证据，不再从 scratchpad 最后十条泛化收集。
- [ ] expected effect 节点都在路径下游。
- [ ] 没有 unresolved P0 path；PARTIAL、P0、containment、不可逆动作不得 AUTO。
- [ ] fix 的 graph risk tier 只能抬高门槛，不能降低 AST/实际影响判断。
- [ ] 模型传入的 root cause、fix、ESC verdict、evidence refs 与系统上下文冲突时，拒绝而不是覆盖可信值。

GATE denial 增加机器可读：`reason_code`、`retry_phase = PLAN | INVESTIGATE | ESCALATE`。证据过期和因果绑定缺证回 INVESTIGATE；SQL/rollback/参数问题回 PLAN；manual/P0 不可自动处置走 ESCALATE。

完成标准：构造一个 SQL 完全安全、但目标不在选中路径上的提案时必须拒绝；构造路径正确但证据过期的提案时必须回调查，而不是只让模型重写 SQL。

## 10. VERIFY、ROLLBACK 与报告

### 10.1 VERIFY

- [ ] 先确认执行确实成功，再进入因果效果验证。
- [ ] 按计划等待足够观察窗口，采集 expected effect nodes 对应证据/KPI。
- [ ] 逐项记录 `expected / actual / met / raw_ref`，再计算总体 outcome。
- [ ] 继续运行现有健康 KPI 和回归套件，因果效果通过不能替代安全回归。
- [ ] 成功只能支持“该干预在该上下文下改善了这些下游节点”，不要自动把整张图永久标成真。

### 10.2 失败归因和回退

- [ ] SQL/执行前置失败：记为 execution failure，不反证因果节点。
- [ ] SQL 成功但某个具体方案无预期效果：优先记 `INTERVENTION` 级反证。
- [ ] 干预目标状态改变，但下游机制未变：反证对应路径片段/expected-effect 边，回 INVESTIGATE。
- [ ] 存在 PARTIAL scope、独立故障或未解释症状时，KPI 未恢复不能算到选中根因头上。
- [ ] 只有多个独立、正确执行、覆盖合理且结果一致的干预失败，才允许降低节点级信心；不能沿用“失败两次就永久反证根因”的粗粒度规则。
- [ ] 回滚数据库状态，但失败知识、EvidenceBinding、InterventionAttempt 和 explanation revision 单调保留。

扩展 `RemediationAttempt` 或新增 `InterventionAttempt`，至少记录：`plan_id`、path/target/fix、expected/actual、failure_scope、affected_edge_ids、rollback result、是否可用于 L1-L4 学习。

### 10.3 REPORT / ESCALATE

输出必须包含：

- 观测症状。
- 选中的完整因果链和每段状态。
- 关键支持/反证证据及 raw_ref。
- 未解释症状和仍开放的分叉。
- 四个 P0 中哪些可达、如何排除/确认。
- 干预节点、类型、预期与实际效果。
- manual/escalate 项的负责人所需信息，不生成伪 SQL。

完成标准：报告能回答“为什么是这条链、为什么不是主要替代链、改了链上的哪里、哪些结果证明有效”。

## 11. L1-L4 自进化 v2：四层都必须改，且必须证明被消费

不要覆盖当前学习文件。把现有 `knowledge/learned/*.yaml` 视为 v1 审计记录；新增 `knowledge/learned/v2/` 和 schema/manifest。v2 在线路径默认忽略 v1 delta、playbook、query library、candidate edges，除非通过显式、可测试的迁移器。

每一层都必须同时有：写入端、读取端、在线决策影响、版本/污染治理、`learned on/off` 消融。仅生成 YAML 不算完成。

### 11.1 L1 案例记忆

修改 [knowledge/case_store.py](knowledge/case_store.py)。案例单位从“症状 -> 一个根因”改为：

```text
症状/环境指纹
-> 候选与选中解释子图
-> 关键 EvidenceBinding 和被排除分叉
-> P0 义务结果
-> 干预计划/失败尝试
-> expected/actual effect
-> 最终结果
```

- [ ] `CaseV2` 保存路径结构，不只保存 `root_cause` 和 evidence 顺序。
- [ ] 正例只来自诊断、干预和安全验证可信的 Episode；有清晰 failure scope 的失败案例可作为负例。
- [ ] eval 永不入训练案例；生产/沙箱/人工标注 provenance 分开。
- [ ] 检索返回已有图中的路径模板、常见分叉和负例，不直接填充当前证据状态。
- [ ] L1 只在 HYPOTHESIZE 影响路径召回/分数；ESC required evidence 不变。
- [ ] 案例复用效用按“该路径模板是否帮助召回/减少工具调用/避免失败方案”更新，不再只看最终根因是否相同。
- [ ] 建立最小样本的冷启动 fixture，不能继续让案例库 0 例却宣称 L1 生效。

验收：相同症状、不同 wait profile/onset 应召回不同路径；开启 L1 后目标路径名次或召回发生可测变化，关闭 L1 恢复基线。

### 11.2 L2 条件化调查策略

修改 [knowledge/evolution.py](knowledge/evolution.py)。废弃“每个根因一条固定 evidence_order”作为 v2 决策单元，改为条件化决策记录：

```text
frontier_signature
+ 已有 evidence 状态
+ 未决分叉/P0
+ 环境能力
-> 下一 EvidenceNeed / tool
-> 结果状态
-> 剪掉/支持了哪些路径
-> 成本
```

- [ ] 只学习真正改变路径 posterior、剪枝、补齐 required evidence 或减少调用的步骤。
- [ ] 同一 episode 中与当前路径无关的工具结果不能混入流程。当前 playbook 中跨线程/无关证据污染必须在 v2 过滤。
- [ ] 学习输出可以是小型决策树、条件表或 bandit state，但必须可 YAML 审计、可重放。
- [ ] L2 直接进入 tool planner 的评分，不只渲染成 prompt hint。
- [ ] 图版本、场景 revision、工具 schema 变化时旧策略标 stale。

验收：同一根因的不同 frontier 会选择不同下一工具；历史里先查过的证据不会再次出现在建议首位。

### 11.3 L3 边和路径权重

修改 [knowledge/evolution.py](knowledge/evolution.py)、[knowledge/structure.py](knowledge/structure.py)、图评分读取端。

- [ ] 学习所有 `CAUSES` 边，包括 cause->cause，不再只调 root->symptom 或根因 prior。
- [ ] 学习键使用稳定 node/edge/path ID，禁止带运行数值的人话键，例如 `root->错误 5086`。
- [ ] 更新量按手工 likelihood/prior 相对比例封顶；无结论 Episode 不更新；奖励和惩罚对称且每个结局只写一次。
- [ ] 路径分数实际读取 edge adjustment；增加单边和整路径消融，防止再次出现“写了但没人读”。
- [ ] 结构提案不能依据“成功 Episode 里共同出现过”生成 `CONFIRMED_BY`。只有证据是该边的决定性 predicate、具备作用域、跨独立 Episode 重复，才可提案。
- [ ] `CAUSES` 提案至少要求时间顺序、跨场景复现、能减少孤儿症状且不存在已知反例；共现只记观察，不进入 ready。
- [ ] `FIXED_BY` 永不自动提案/推广；安全干预必须人工设计。
- [ ] 提案状态使用 `proposed -> ready_for_review -> approved/promoted | rejected | quarantined`；只有 approved/promoted 被图加载器读取。
- [ ] 当前 candidate edges 整体标 v1/untrusted，不迁移 support 次数。

验收：给 cause->cause 边注入受控正/负样本会改变包含该边的路径排序；无关工具高频共现不会再产生 `missing_index -> session_wait_profile` 一类伪边。

### 11.4 L4 工具信息增益

修改查询库和 `agent/tool_planner.py`。统计键改为：

```text
frontier_signature + evidence_need + tool
```

至少记录：调用数、OBSERVED/UNKNOWN/ERROR、耗时、覆盖 need 数、剪枝路径数、posterior/entropy 变化、是否改变下一决策、重复调用数、场景/图/工具版本。

- [ ] 效用基于预期信息增益/成本，而不是“成功 Episode 中出现次数”。
- [ ] 根因相同但分叉不同的工具统计分开。
- [ ] 有最小样本和向总体先验收缩，避免 1 次偶然成功垄断选择。
- [ ] L4 实际把 Subagent 工具裁到 1-3 个；当前“只排序不裁剪”不算完成。
- [ ] required/P0 need 有保底通道；探索配额保留；UNKNOWN/ERROR 率高的工具降权但不伪造反证。

验收：在两个不同 frontier 上，L4 选择不同工具集合；`learned=False` 时恢复静态 discriminator 顺序；总工具调用下降且 path recall/P0 recall 不下降。

## 12. 测试与评测必须随对象升级

不要只改现有测试让它们通过。新增下列独立验收脚本，并加入 `.dev/checkall.sh`；命名可以微调，但职责不能合并丢失。

- [ ] `.dev/explanation_model_check.py`：v2 模型、稳定 ID、持久化、v1 兼容、revision。
- [ ] `.dev/path_recall_check.py`：多跳、共享机制、路径多样性、探索配额、不可达节点不召回。
- [ ] `.dev/p0_obligation_check.py`：四个 P0 的可达/不可达、预算外保留、截断、逐项解决。
- [ ] `.dev/evidence_predicate_check.py`：结构化 supports/refutes、作用域、UNKNOWN/ERROR、窗口/source epoch、raw_ref 去重。
- [ ] `.dev/dynamic_tool_planner_check.py`：四重交集、1-3 工具、环境不可用、required 保底、探索可重放。
- [ ] `.dev/subagent_path_task_check.py`：局部子图任务、结构化回传、无 verdict/提案权限、迟到结果幂等。
- [ ] `.dev/esc_explanation_check.py`：覆盖、因果连续性、替代路径、P0、新鲜度、FULL/PARTIAL、四类 verdict。
- [ ] `.dev/causal_gate_context_check.py`：路径/目标/fix/effect/证据/revision 绑定和伪造字段拒绝。
- [ ] `.dev/causal_verify_check.py`：预期下游效果、干预级/路径级失败归因、partial 不误伤根因。
- [ ] `.dev/terminal_done_check.py`：REPORT/ESCALATE 都真正进入 DONE 且持久化 finished。
- [ ] `.dev/evolution_v2_check.py`：L1-L4 各自写入、读取、改变下一次行为、learned on/off 消融。
- [ ] `.dev/structure_v2_check.py`：共现不成边、审批后才 promote、v1 candidate 不加载。

### 12.1 场景覆盖

保留现有普通和四个 P0 场景，新增/组合以下测试：

- [ ] 多跳级联：`long_idle_transaction -> autovacuum_starvation -> table_bloat -> disk_pressure -> disk_growing`。
- [ ] 隐蔽上游：`stale_replication_slot -> autovacuum_starvation -> table_bloat -> disk_growing`。
- [ ] 双分支：prepared transaction 同时指向 autovacuum starvation 和 xid risk。
- [ ] 表象相同分叉：deadlock vs lock contention；missing index vs stale statistics vs work_mem spill。
- [ ] 独立双根因：一个路径修复后仍有未解释症状，不能误反证已确认路径。
- [ ] 方案反证：一个 hypothetical index 无效，但另一索引方案仍可继续。
- [ ] 证据过期：ESC 后到 GATE 前窗口过期，必须回 INVESTIGATE。
- [ ] 工具不可用：扩展/权限缺失导致 UNKNOWN/UNAVAILABLE，不能被当成反证。

能用真实 PostgreSQL 验证的继续走 WSL 活库；难以稳定注入的长级联先用受控 trace/replay 验证推理语义，再至少对每个 P0 的直接证据采集做活库验证。不要把合成轨迹的结果写成生产成功率。

### 12.2 新指标

在 [eval/run_suite.py](eval/run_suite.py)、[eval/replay.py](eval/replay.py) 增加：

- root recall@K、path recall@K、P0 obligation recall（必须 100%）。
- observed symptom coverage、unexplained symptom rate。
- selected path edge precision/recall/F1。
- required evidence completion、raw_ref validity/freshness、duplicate evidence rate。
- 工具调用数、有效信息增益/调用、重复调用率、UNKNOWN/ERROR 率。
- ESC unsafe pass、over-conservative、AMBIGUOUS/EXHAUSTED 分布。
- GATE 因果上下文绕过数（必须 0）。
- expected effect 命中率、失败归因准确率、rollback 完整率。
- L1-L4 各层开启/关闭的逐层消融，不只做总开关。

评测集必须带 revision，训练案例/学习产物不能读取 eval split。小样本结果必须同时报告分母，不能只报百分比。

## 13. 推荐实施批次和每批停止条件

### 批次 A：模型与兼容层

- 完成第 3 节。
- 只新增对象和持久化，不切换 live loop。
- 停止条件：新旧轨迹测试全过，现有回归无变化。

### 批次 B：图路径 API、predicate 和关系语义

- 完成第 4、5 节。
- v1 `candidate_causes/required_evidence/fixes_for` 继续工作。
- 停止条件：path recall、graph lint、predicate scope 测试全过。

### 批次 C：HYPOTHESIZE/INVESTIGATE/DIAGNOSE 与工具规划

- 完成第 6、7 节，只读诊断先跑通。
- `allow_repair=False` 下验证解释子图和报告。
- 停止条件：多跳路径逐段取证，Subagent 权限和工具裁剪全过。

### 批次 D：ESC、PLAN、GATE

- 完成第 8、9 节。
- 先用离线提案和拒绝路径测试，再跑 autovacuum 的 CONFIRM 活库链。
- 停止条件：证据不足、过期、未决 P0、路径外修复全部被正确拦截。

### 批次 E：VERIFY/ROLLBACK/REPORT

- 完成第 10 节。
- 停止条件：成功、错误方案、partial、多根因、不可逆升级五类路径归因正确，DONE 真执行。

### 批次 F：L1-L4 v2

- 按 L1 -> L2 -> L3 -> L4 顺序完成第 11 节，每层单独做 online consumption 测试。
- 停止条件：每层都能证明“学前和学后下一次行为不同”，且 ESC/GATE 标准不变。

### 批次 G：全量评测与文档

- 完成第 12 节。
- 更新 README 中旧的“候选根因列表、根因级 playbook、只重排工具”等描述和已知局限。
- 输出迁移说明、v1/v2 学习统计、逐层消融和残余问题。

任何批次没有达到停止条件，不要提前删除兼容字段或切换默认 reader。

## 14. WSL 端到端验收

在 WSL 使用现有环境：

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

最终 E2E 必须覆盖：

- [ ] 只读多跳诊断：路径召回、动态取证、ESC、REPORT、DONE。
- [ ] autovacuum P0：证据 -> ESC -> PLAN -> CONFIRM GATE -> EXECUTE -> VERIFY -> rollback/cleanup。
- [ ] disk/slot/prepared：证据充分也只能 ESCALATE，不产生写提案。
- [ ] 一个错误修复：执行后预期效果未出现，正确回滚，只反证干预/路径片段。
- [ ] 一个 PARTIAL/双根因：修复一条路径后不误判另一条，也不反证正确根因。
- [ ] L1-L4 on/off replay：开启后召回/工具行为改变，安全门结果不被放宽。

活库测试后确认没有残留：复制槽、prepared transaction、表级 reloptions、测试索引、失败 undo、未关闭连接。`undo needs_attention` 必须为 0。

## 15. 最终交付物

Code Agent 完成后必须给出：

- 修改文件清单，按数据模型、状态机、图、工具、ESC/GATE、学习、评测分类。
- v1 到 v2 的兼容说明，以及何时可以删除旧投影字段。
- 每个新对象的实际 JSON/YAML 样例。
- 所有测试命令与结果摘要，区分离线、WSL 活库和合成 replay。
- L1-L4 每层“写入了什么、哪里读取、如何改变下一次行为”的证据。
- 路径召回、P0 recall、ESC、工具成本、GATE bypass、VERIFY/rollback 的核心指标。
- 尚未解决的问题和风险，不得用“测试通过”掩盖样本量、合成场景或人工操作限制。

最终完成定义不是“代码里出现 ExplanationGraph”，而是一次 Episode 从症状开始，能够持久化并验证一张多跳解释子图，依据子图动态选工具，经过 ESC/GATE 安全地干预其中一个节点，按下游预测验证结果，并把有正确作用域的经验回流到 L1-L4，使下一次调查发生可审计的变化。
