-- 角色是集群级的，只需建一次
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    CREATE ROLE app_user LOGIN PASSWORD 'app_pw_dev_only';
  END IF;
END $$;

GRANT CONNECT ON DATABASE shop TO app_user;
GRANT CONNECT ON DATABASE shop_golden TO app_user;
GRANT pg_use_reserved_connections TO agent_ro, agent_rw;

\c shop
GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE ON TABLES TO app_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;

-- golden 是 reset 的模板：不在这里授权的话，每次 reset 后 app_user 就没权限了
\c shop_golden
GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE ON TABLES TO app_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;

\c shop
SELECT rolname, rolcanlogin FROM pg_roles
WHERE rolname IN ('app_user','agent_ro','agent_rw') ORDER BY 1;
SELECT 'agent_ro 有保留位: ' ||
       pg_has_role('agent_ro','pg_use_reserved_connections','member')::text;
SELECT 'app_user 有保留位: ' ||
       pg_has_role('app_user','pg_use_reserved_connections','member')::text;
