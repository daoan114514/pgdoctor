# pgdoctor

面向 PostgreSQL 的**自主运维 Agent**：从告警出发，自主诊断根因，并在确定性安全门的保护下执行修复与验证。

> 状态：W1 完成（故障注入沙箱可用，单故障可复现可回滚）。Agent 主循环开发中。

## 为什么这件事不容易

2026 年的 DBA-Bench 显示，在生产保真度的故障场景下，最好的自动化 agent **Safe Pass 只有 17.9%**，而人类 DBA 是 **93.4%**。差距不在"能不能说出根因"，而在**能不能安全地把问题解决掉而不弄坏别的东西**。

本项目正是冲这个差值去的，核心不是"接个大模型问它数据库为什么慢"，而是一套约束它的工程结构。

## 设计要点

**双硬闸**——LLM 负责推理，状态机负责保证它不越界：

| 闸 | 位置 | 管什么 |
|---|---|---|
| 证据充分性检查 (ESC) | DIAGNOSE → PLAN | 结论**够不够格**：证据是否充分 |
| 护盾 + 安全门 | PLAN → EXECUTE | 动作**安不安全**：是否可逆、是否灾难 |

**安全是结构保证，不是提示约束**：agent 只持有只读连接 `agent_ro`，写权限 `agent_rw` 由安全门独占，agent 物理上没有改库的手。

**核心不变式**：上下文是可丢弃的缓存；`EpisodeState` 与 undo journal 才是持久真相源。任何系统正确性依赖的东西，都不许只存在于上下文里。

## 故障注入沙箱

自建的可复现基准，同时充当评测台与自进化的度量台。对标 DBA-Bench 的三率：Diagnosis / Outcome / **Safe Pass**。

首个场景 `missing_index_orders_user_status_v1` 的实测：

```
健康基线    p50=2.29ms    plan=Index Scan
注入故障    DROP idx_orders_user_status
故障态      p50=346ms  p99=1207ms  plan=Seq Scan
            Rows Removed by Filter: 3,999,995      劣化 151x
快照回滚    43.9s
恢复校验    p50=0.90ms    plan=Index Scan          基线已恢复
```

数据规模：orders 1200 万行（1662 MB），PENDING 占比约 10%。

## 快速开始

```bash
cd docker && docker compose up -d      # 起沙箱，首次会灌 1200 万行（约数分钟）
python3 -m sandbox.snapshot create     # 固化健康基线为 golden 模板
python3 .dev/w1_check.py               # 端到端验收：基线→注入→回滚→恢复
```

## 路线

- [x] **W1** 沙箱：容器、数据基线、负载生成器、注入器、快照回滚
- [ ] **W2** 只读观测工具层 + 判分器 + 回归套件
- [ ] **W3** Agent 主循环 + MAPE-K 状态机
- [ ] **W4** 安全门 + 护盾 + undo journal（单故障端到端闭环）
- [ ] W5–W9 subagent 编排、因果图与 ESC、案例记忆库、消融实验、Demo

## 已知局限

首版覆盖 8–10 类故障，非全谱；未做强化学习，自进化走非参数路线（记忆与知识库演化）；沙箱是简化的生产环境；生产环境建议仅启用只读诊断。
