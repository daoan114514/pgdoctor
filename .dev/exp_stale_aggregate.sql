\timing on
\echo '=== 复位 ==='
DELETE FROM orders WHERE total = 1.0 AND created_at > now() - interval '3 hours';
ALTER TABLE orders SET (autovacuum_enabled = true);
ANALYZE orders;
SELECT count(*) AS rows_now FROM orders;

\echo ''
\echo '=== 健康态：这条聚合查询本来多快 ==='
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT count(*), sum(total) FROM orders WHERE created_at > now() - interval '1 hour';

\echo ''
\echo '=== 注入 200 万行（不 ANALYZE）==='
ALTER TABLE orders SET (autovacuum_enabled = false);
INSERT INTO orders (user_id, status, total, created_at)
SELECT 1 + (g % 100000), 'PENDING', 1.0, now()
FROM generate_series(1, 2000000) g;

\echo ''
\echo '--- 统计过期时的计划 ---'
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT count(*), sum(total) FROM orders WHERE created_at > now() - interval '1 hour';

\echo ''
\echo '--- ANALYZE 之后的计划（这才是"修复"）---'
ANALYZE orders;
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT count(*), sum(total) FROM orders WHERE created_at > now() - interval '1 hour';

\echo ''
\echo '=== 关键对照：同样 200 万行，但统计是对的 ==='
\echo '（若这个耗时和"过期"时差不多，说明慢的原因是数据量而不是坏计划）'

\echo ''
\echo '=== 复位 ==='
DELETE FROM orders WHERE total = 1.0 AND created_at > now() - interval '3 hours';
ALTER TABLE orders SET (autovacuum_enabled = true);
ANALYZE orders;
SELECT count(*) FROM orders;
