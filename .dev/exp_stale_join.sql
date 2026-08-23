\timing on
\echo '=== 复位 ==='
DELETE FROM orders WHERE total = 1.0 AND created_at > now() - interval '3 hours';
ANALYZE orders;

\echo ''
\echo '=== 健康态 ==='
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT o.status, count(*), sum(o.total) FROM orders o JOIN users u ON u.id = o.user_id
WHERE o.created_at > now() - interval '1 hour' GROUP BY o.status;

\echo ''
\echo '=== 注入 30 万行，不 ANALYZE ==='
ALTER TABLE orders SET (autovacuum_enabled = false);
INSERT INTO orders (user_id, status, total, created_at)
SELECT 1 + (g % 100000), 'PENDING', 1.0, now() FROM generate_series(1, 300000) g;

\echo ''
\echo '--- 统计过期时的计划（期望 Nested Loop）---'
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT o.status, count(*), sum(o.total) FROM orders o JOIN users u ON u.id = o.user_id
WHERE o.created_at > now() - interval '1 hour' GROUP BY o.status;

\echo ''
\echo '--- ANALYZE 之后（期望 Hash Join）---'
ANALYZE orders;
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT o.status, count(*), sum(o.total) FROM orders o JOIN users u ON u.id = o.user_id
WHERE o.created_at > now() - interval '1 hour' GROUP BY o.status;

\echo ''
\echo '=== 复位 ==='
DELETE FROM orders WHERE total = 1.0 AND created_at > now() - interval '3 hours';
ALTER TABLE orders SET (autovacuum_enabled = true);
ANALYZE orders;
