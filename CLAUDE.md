# pgdoctor — 给改这个项目的人（和 agent）的第一原则

## 首要原则：正确率优先，可以牺牲性能和速度

**先保证质量和正确率。为此可以牺牲性能和速度，这个取舍永远成立，不需要再讨论。**

为什么这条排在最前面：本项目冲的是 DBA-Bench 上 Safe Pass **17.9%** 这个差值，而
差值的成因是**静默失败**——agent 基于错误根因去动生产库，没有报错、没有异常、没有
任何信号告诉你它错了。任何"这样更快但可能不准"的取舍，都是在给静默失败让路。
**快而错的诊断，价值是负的。**

遇到取舍时直接选准的那个，不要先做优化再回头补正确性。

### 已经踩过的坑（都是这条原则的反例）

- `sandbox/observe.py::_stats_range_drift` 第一版为省 250 毫秒（走索引 2.5ms vs
  全表并行扫 253ms），只测"有 btree 索引做前导列"的列。结果把测量的**覆盖范围**
  变成了索引存在性的函数：`missing_index` 丢掉索引后，承载信号的列直接从测量里
  消失，凭空造出一条 `missing_index ⇝ stale_statistics` 的污染边，与已有的
  `stale_statistics ⇝ missing_index` 形成 2-环。**为省 250 毫秒换来一个环。**
- `agent/esc.py` 的可用性记账原先只按 `need_id` 和 `evidence_type` 认失败，工具整体
  失败（权限不足、超时）因为记在别的类型上而被漏掉，`EXHAUSTED` 这个出口从头到尾
  没通电，`lock_contention` 空转 47 轮直到预算耗尽。

## 推论：三条硬规则

**1. 只能低估的部分结果，不许做否定裁决。**
测不全就报 UNKNOWN / NOT_APPLICABLE，不要拿一个偏低的数去 REFUTE。反过来是安全的：
下界已经越过阈值，真值必然也越过，SUPPORTS 成立。

**2. 判别 A 和 B 的证据，不能与 A 或 B 共享失效模式。**
关键不是数据准不准（`reltuples`、`n_live_tup`、`pgstattuple_approx` 全是估计，禁掉
就没工具可用），而是**假设成立时会不会让判它的证据变得不可靠**。把
`A ⇝ B`（A 为真会让某条证据关于 B 的裁决出错，A≠B）建成图后：

| | 怎么修 |
|---|---|
| 缺口（该根因所有可反证证据都被污染） | **改证据**，补一条不经过那个失效模式的可反证证据 |
| 有污染但仍有干净的可反证证据 | 不用改结构，可信度规则会让顺序自己涌现 |
| 自环 | 合法，那是"用真实数据检验该假设自己的声称"，不用改 |

**不要做环检测。** 接地覆盖检查完全覆盖它 —— 互相污染且都没有干净证据必然
表现为接地缺口 —— 而且环检测会误报（粗粒度的根因级环，可能因为某个根因另有
一条干净证据而实际可解），报出来也只说"这儿有个环"，不说该补什么。

这些不用手写：证据节点标 `provenance`（值由什么算出来），
`knowledge/causal_graph/nodes.yaml` 的 `provenance_rules` 段落把它映射到
失效条件，`graph.invalidators_of()` / `ungrounded_root_causes()` 推导，
`graph_lint` 第 [8] 节把缺口报成失败。

已确认的污染边：`stale_statistics ⇝ missing_index`，经 `explain_seq_scan` /
`explain_plan` / `counterfactual_index`——它们都是**规划器的输出**，而规划器吃的
就是统计信息。所以统计过期时，用执行计划去判缺索引是循环论证。

**3. 安全是结构保证，不是提示约束。**
agent 全程只持有只读连接 `agent_ro`，没有任何能改数据库的工具。任何"约好了不写"
的方案都不算数——实测 `default_transaction_read_only` 一条 `SET` 就能关掉。
写权限由安全门独占，执行是系统阶段。

## 改动时必须一起改的地方

- **新增工具**：`state_machine.READ_TOOLS`、`toolbox`、`observe`、`investigator`（两处）、
  `llm_policy`、`policy`、`depth_policy`、`tool_planner`（三处）、`evolution`。漏一处的
  症状是"模型不听话"，实际是自己的 hook 把出口拦死了。能并进已有工具就别新增。
- **改因果图**：`graph_lint` 会查结构（含 provenance 与接地覆盖）；权威数据集锁着
  `graph_version`，要跑 `python eval/build_authoritative_cases.py --install-l1-seeds`
  重生成（它会自己同步 `manifest.yaml` 的 `graph_version`）。
- **新增证据类型**：除图上的节点与边，还要补 `knowledge/evolution.py` 的 `TOOL_OF`
  和 `.dev/p0_gate_check.py` 的 SUPPORT_VALUES / REFUTE_VALUES。窗口类判据还要在
  夹具里传 `window=`，否则一律 NOT_APPLICABLE、反证根本不成立。
  `.dev/graph_expand_check.py` 会查 `TOOL_OF` 覆盖率。
- **改场景实例定义**：bump `revision`，再跑 `python .dev/relock.py` 更新实例锁，
  否则 `harness_lint` 报错（历史结果就分不清对应哪个版本了）。

## 验收

```bash
bash .dev/checkall.sh          # 离线回归，必须 0 失败
python .dev/harness_lint.py    # 评测台自身的一致性
python .dev/graph_lint.py      # 因果图结构
```

活库验收（需要 `cd docker && docker compose up -d` 并 `python -m sandbox.snapshot create`）：
`w1_check` / `w2_env_check` / `w4_check` / `p0_gate_check --live` / `p0_recall_check --live`。

**跑活库 train split 会写 L1/L3 学习数据**（`knowledge/learned/v2/`）。那是设计行为，
但如果只是测试，跑完要退回去——每条复用都记了 episode id，可精确归因回退。
