\timing on
\echo '=== 清理上次实验残留 ==='
DELETE FROM orders WHERE total = 1.0 AND status = 'PENDING' AND created_at > now() - interval '2 hours';
ALTER TABLE orders SET (autovacuum_enabled = true);
ANALYZE orders;
SELECT reltuples::bigint AS est, (SELECT count(*) FROM orders) AS actual FROM pg_class WHERE relname='orders';

\echo ''
\echo '=== 基线：统计正确时的 created_at 范围查询 ==='
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT count(*), sum(total) FROM orders WHERE created_at > now() - interval '1 hour';

\echo ''
\echo '=== 注入：灌 4M 行 created_at=now() 但不 ANALYZE ==='
ALTER TABLE orders SET (autovacuum_enabled = false);
INSERT INTO orders (user_id, status, total, created_at)
SELECT 1 + (g % 100000), 'PENDING', 1.0, now()
FROM generate_series(1, 4000000) g;
SELECT reltuples::bigint AS est, (SELECT count(*) FROM orders) AS actual FROM pg_class WHERE relname='orders';

\echo ''
\echo '=== 过期统计下：优化器以为最近一小时几乎没数据 ==='
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT count(*), sum(total) FROM orders WHERE created_at > now() - interval '1 hour';

\echo ''
\echo '=== ANALYZE 后对照：证明是统计信息的锅 ==='
ANALYZE orders;
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT count(*), sum(total) FROM orders WHERE created_at > now() - interval '1 hour';

\echo ''
\echo '=== 复原 ==='
DELETE FROM orders WHERE total = 1.0 AND status = 'PENDING' AND created_at > now() - interval '2 hours';
ALTER TABLE orders SET (autovacuum_enabled = true);
ANALYZE orders;
SELECT count(*) FROM orders;
