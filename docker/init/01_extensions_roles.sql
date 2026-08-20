-- 诊断所需扩展
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;  -- 找罪魁查询
CREATE EXTENSION IF NOT EXISTS hypopg;              -- 假设索引 → ESC 的 D5 反事实验证
CREATE EXTENSION IF NOT EXISTS pgstattuple;         -- 量化膨胀，bloat 类故障的 oracle
CREATE EXTENSION IF NOT EXISTS pg_buffercache;      -- 缓存命中分析

-- ── 权限隔离：安全架构的物理基础 ────────────────────────────────
-- agent_ro : agent 唯一持有的连接，纯只读
-- agent_rw : 仅安全门(remediation_server)持有，agent 拿不到
-- app_owner: 持有表对象；CREATE INDEX / VACUUM 在 PG16 需要 owner 权限，
--            通过把 app_owner 授予 agent_rw 来传递，而不给 agent_ro
CREATE ROLE app_owner NOLOGIN;

CREATE ROLE agent_ro LOGIN PASSWORD 'ro_pw_dev_only';
CREATE ROLE agent_rw LOGIN PASSWORD 'rw_pw_dev_only';

GRANT CONNECT ON DATABASE shop TO agent_ro, agent_rw;
GRANT USAGE  ON SCHEMA public  TO agent_ro, agent_rw;

-- 只读角色：SELECT + 全量统计视图（能看到其他会话的 query 文本与等待事件）
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO agent_ro;
GRANT pg_read_all_stats TO agent_ro;

-- 写角色：DML + 通过 app_owner 获得 DDL/VACUUM 能力
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO agent_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agent_rw;
GRANT pg_read_all_stats TO agent_rw;
GRANT CREATE ON SCHEMA public TO agent_rw;
GRANT app_owner TO agent_rw;

-- 连接池打满时诊断角色仍需能连上，否则该类故障无法被诊断。
-- 用 PG16 的非超级用户保留位，避免把 agent 提成 superuser。
GRANT pg_use_reserved_connections TO agent_ro, agent_rw;
-- 注意不要授予 app_user：它被拒正是这个故障的症状本身。

-- app_user 模拟业务应用：它没有保留连接位，所以连接池打满时它会被拒，
-- 这正是该故障应有的症状。而 agent_ro 有保留位，仍能连上做诊断 ——
-- 保留位机制只有在应用角色与诊断角色分开时才成立。
CREATE ROLE app_user LOGIN PASSWORD 'app_pw_dev_only';
GRANT CONNECT ON DATABASE shop TO app_user;
GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE ON TABLES TO app_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;
