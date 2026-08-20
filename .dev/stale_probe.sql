\timing on
\echo '=== 基线：统计信息正确 ==='
ANALYZE orders;
SELECT reltuples::bigint AS est, (SELECT count(*) FROM orders) AS actual
FROM pg_class WHERE relname='orders';

\echo ''
\echo '--- 候选查询 A: status 上的聚合（估计选择性直接决定计划）---'
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT count(*) FROM orders WHERE status = 'PENDING';

\echo ''
\echo '--- 候选查询 B: 与 users 的 join（估计错会导致 nested loop 爆炸）---'
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT u.id, count(*) FROM orders o JOIN users u ON u.id = o.user_id
WHERE o.status = 'PENDING' GROUP BY u.id LIMIT 20;

\echo ''
\echo '=== 制造统计信息过期：灌 4M 行 PENDING 但不 ANALYZE ==='
ALTER TABLE orders SET (autovacuum_enabled = false);
INSERT INTO orders (user_id, status, total, created_at)
SELECT 1 + (g % 100000), 'PENDING', 1.0, now()
FROM generate_series(1, 4000000) g;
SELECT reltuples::bigint AS est, (SELECT count(*) FROM orders) AS actual
FROM pg_class WHERE relname='orders';

\echo ''
\echo '--- A 在过期统计下 ---'
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT count(*) FROM orders WHERE status = 'PENDING';

\echo ''
\echo '--- B 在过期统计下 ---'
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT u.id, count(*) FROM orders o JOIN users u ON u.id = o.user_id
WHERE o.status = 'PENDING' GROUP BY u.id LIMIT 20;

\echo ''
\echo '=== ANALYZE 之后对照（证明确实是统计信息的锅）==='
ANALYZE orders;
EXPLAIN (ANALYZE, COSTS ON, TIMING OFF)
SELECT u.id, count(*) FROM orders o JOIN users u ON u.id = o.user_id
WHERE o.status = 'PENDING' GROUP BY u.id LIMIT 20;
