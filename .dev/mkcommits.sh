#!/usr/bin/env bash
set -e
cd /home/daoan/pgdoctor

c() { git add "${@:2}"; git commit -q -F - <<<"$1"; echo "  ✓ $(echo "$1" | head -1)"; }

c "chore: 初始化项目骨架与跨平台文件约定

约定 LF 行尾并忽略运行期产物。

从 Windows 侧向 WSL 写文件会带入 CRLF，而 bash -n 语法检查照样通过、
运行时才在算术展开处静默出错，排查成本很高，因此用 .gitattributes 从
源头上钉死 eol=lf。" .gitignore .gitattributes requirements.txt

c "docs: 项目说明与路线图

先落一份能说清楚'为什么这件事不容易'的 README：本项目冲的是
DBA-Bench 上 Safe Pass 17.9% vs 人类 93.4% 这个差值，
核心不是接个模型问它数据库为什么慢，而是一套约束它的工程结构。" README.md

c "feat(sandbox): 容器化 PostgreSQL 与诊断所需的可观测基础

装 hypopg 以支持'先模拟再动手'——不真建索引就能预测优化器是否会用它，
这是证据充分性检查里反事实验证那一维的基础，也是数据库这个域相对
其他域的独特优势。

预载 pg_stat_statements 与 auto_explain，让慢查询和执行计划在故障
发生时是可观测的，而不是事后补采。

国内网络下 Debian 与 PGDG 官方源直连极慢（实测单个索引文件 15 秒），
统一换阿里云镜像；PGDG 必须走 http，因为容器内对该镜像的 https
证书校验失败，而 APT 本身有 GPG 签名校验兜底。" docker/Dockerfile.pg docker/docker-compose.yml

c "feat(sandbox): 数据基线与三角色权限隔离

权限隔离是整个安全架构的物理基础，而不是靠提示词约束：
  agent_ro  —— agent 唯一持有的连接，纯只读
  agent_rw  —— 仅安全门持有，agent 拿不到
  app_owner —— 持有表对象，PG16 下 CREATE INDEX/VACUUM 需要 owner
               权限，通过角色授予传递给 agent_rw 而不给 agent_ro

灌数分块进行并输出进度，索引在灌完之后再建——1200 万行时边插边维护
索引会慢很多。status 做倾斜分布，PENDING 约占 10%，让故障场景里被
全表扫的那一片有真实体量。" docker/init/

c "feat(sandbox): 数据库连接层" sandbox/db.py sandbox/__init__.py

c "feat(sandbox): 负载生成器与滚动延迟指标

没有活负载，指标不会动，故障就'看不见'——生产保真度来自活跃负载、
持久状态与多源观测三者同时在场。

按查询类型分别统计：热查询是受故障影响的那条，金丝雀查询不受影响，
后者是 Safe Pass 回归检查的依据（修复不能把本来正常的东西弄慢）。
指标以原子替换方式落盘，读者不会看到写了一半的文件。" sandbox/workload.py

c "feat(sandbox): golden 模板快照与秒级回滚

每个 episode 之间必须回到完全相同的健康态，否则实验不可复现。
逻辑故障走 CREATE DATABASE ... TEMPLATE，1.6GB 的库约 44 秒还原。

回滚时显式重置 pg_stat_statements：它是集群级的，克隆数据库并不会
清掉它，上一个 episode 的慢查询统计会污染下一次的观测。" sandbox/snapshot.py

c "feat(sandbox): 注入器抽象与首个 missing_index 注入器

基类把三条铁律固化成接口约束：可被快照回滚、参数化随机（防止 agent
背答案）、自带 ground truth（供判分器机械比对而非模糊判断）。

选缺索引作为第一个故障，是因为它的 oracle 最干净——EXPLAIN 会直接
给出 Seq Scan 与 Rows Removed by Filter，没有解释空间。

注入后主动 ANALYZE：这样统计信息是新鲜的，'统计信息过期'这个竞争
假设可以被干净地排除，鉴别诊断才有真东西可做。" sandbox/injectors/

c "feat(sandbox): 场景 DSL 与首个故障场景

把一个 episode 的注入参数、负载、健康基线、判分依据写成声明式规格，
故障类别用固定枚举以便诊断结果可结构化匹配。

热查询的谓词组合是实测修正过的：最初用 status + ORDER BY created_at，
但 DROP 掉目标索引后故障根本不出现——优化器改用 created_at 上的索引
倒序扫，只过滤 120 行就凑够 20 条结果，仍是 2ms。改成 user_id + status
后，库里其余索引（主键在 id、另一个在 created_at）都无法用于该谓词，
退化必然是全表扫，证据干净无歧义。实测劣化 151 倍。" sandbox/scenarios/ traces/.gitkeep

c "chore(dev): 开发辅助脚本与 W1 验收测试

WSL 在无进程附着时会回收发行版服务，长任务会随工具调用被杀且 /tmp
会被清空，因此构建/安装一律用 setsid 脱离，脚本与日志放项目内并靠
.done 文件判完成。

w1_check.py 是 W1 的完成判据：走通 基线→注入→验证→回滚→恢复 全链路。" .dev/

echo
git log --oneline
