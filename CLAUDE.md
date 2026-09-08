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

## 推论：九条硬规则

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

**4. 改判据/改测量之前，先画出"谁在什么时候读这条数据"。**
同一条可信规则往往有多个读取点，只堵一个等于没堵。实测栽过：把"观测窗跨越
自己写操作就不可信"只放进 `agent/esc.py::_binding_trust`，完全无效 —— 路径
状态与由它投影出的假设台账由 `explanation_runtime.recompute_statuses` 计算，
那里直接读绑定、不走 ESC 的可信过滤。规则要放在最底层的读取点，多处共用
同一个函数，别各写一份。

**5. 阈值必须用真实观测值标定，构造值只能验逻辑分支、验不了刻度。**
`seq_scan_volume` 的夹具用的是 `seq_tup_read/seq_scan = 100%` 的理想全表扫，
而真实的 `Parallel Seq Scan` 每个 worker 各自计数、各读 1/N，实测只有 45% ——
按单进程定的 0.5 阈值于是**反证了正确的根因**。checkall 35 项全绿，只有活库
会炸。同理：任何"看起来更严谨"的改写都要先量，`created_at >
(SELECT max(created_at) ...)` 看似消除了时间漂移，实际让规划器查不了直方图、
凭空制造 258 倍的估计偏差，永久误确认 `stale_statistics`。

**6. agent 自己的动作会制造证据。**
累计计数器分不清是谁写的：建一个 1200 万行的索引会外溢约 495MB 临时文件，
`temp_file_volume` 读的是 `pg_stat_database` 的库级计数器，于是 `work_mem_spill`
被确认。这是"动作污染证据"，与规则 2 的"根因污染证据"是两个类别 —— 前者取决于
本 episode 做过什么，后者取决于图的结构。窗口类证据的观测窗若与本 episode
执行成功的干预重叠，不可信。

**3. 安全是结构保证，不是提示约束。**
agent 全程只持有只读连接 `agent_ro`，没有任何能改数据库的工具。任何"约好了不写"
的方案都不算数——实测 `default_transaction_read_only` 一条 `SET` 就能关掉。
写权限由安全门独占，执行是系统阶段。

**7. 告警判据与成功判据不能有交集。**
存在一组读数同时满足两边，"成功"就不等于"故障没了"。实测 `lock_contention`
告警 `errors > 3`、成功 `errors < 5`，`errors=4` 两边都成立 —— 一个每 30 秒
仍在报 4 个错的库能拿到 Outcome=True，跑批完全看不出来，只会让指标偏高。
`harness_lint` 第 [8] 节现在会穷举阈值两侧去找这样一组读数并打印出来。
互补（`> T` 与 `< T`）不算交集，两簇挨得近时刻意留死区反而会把真实修复判成失败。

**8. 观测面的采样率不能是被观测故障的函数。**
负载生成器原来按 `random() < 0.08` 决定这一轮要不要新建连接，而新建连接是
连接打满类故障**唯一**的可观测面。采样率 = 0.08 × 循环速度，循环速度又被故障
本身拖慢（查询阻塞时从每秒两百轮掉到每秒零点二轮）—— 故障越重，能看见它的
探针打得越少。实测 `connection_exhaustion` 3/94、`misleading_idle_txn` 12/87
(14%) 的 episode 故障态 errors=0，告警根本打不响，且非零值散在 5 到 600 之间。
改成固定时间节拍（每线程 0.1s）之后实测 563/208/551/710。
判据是"最近 30 秒攒够 N 个"，那么要保证的就是单位**时间**的采样数。

**9. 滑动时间窗的基准数据会随真实时间漂走。**
种子把 `created_at` 铺在"播种时刻往前 365 天"上，锚点是播种那一刻；而场景热
查询用 `created_at > now() - interval '1 day'`。沙箱放两天，窗口就滑过数据末端。
实测漂 22.6 小时时最近 1 天只剩 1,890 行 / 1200 万，最近 1 小时是 0 行 ——
查询很快、基线漂亮、告警照响（丢了索引仍是全表扫），但测的已经不是索引收益
而是"在空结果集上扫不扫全表"。跑批不报错，只是安静地失去意义。
`env.reset` 超过 6 小时漂移直接抛 `GoldenAnchorStale`，跑批开跑前也拦一道；
`python3 .dev/reanchor_time.py` 重锚（UPDATE 后必须 CLUSTER，否则物理相关性
被打乱，那正是规则 5 里 missing_index 判不出 Outcome 的原因）。
不要试图在查询里锚 —— 见规则 5 末尾那个 258 倍假偏差。

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
