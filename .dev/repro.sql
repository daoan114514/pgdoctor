\timing on
\echo '###### 1. 健康基线：热查询走复合索引 ######'
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT id, user_id, total, created_at FROM orders
WHERE status = 'PENDING' ORDER BY created_at DESC LIMIT 20;

\echo ''
\echo '###### 2. 注入故障：DROP 掉该索引 ######'
DROP INDEX idx_orders_status_created;
ANALYZE orders;

\echo ''
\echo '###### 3. 故障态：同一条查询 ######'
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT id, user_id, total, created_at FROM orders
WHERE status = 'PENDING' ORDER BY created_at DESC LIMIT 20;

\echo ''
\echo '###### 4. 注入器 verify_injected 的判据 ######'
SELECT count(*) AS idx_present FROM pg_indexes
WHERE tablename='orders' AND indexname='idx_orders_status_created';

\echo ''
\echo '###### 5. D5 反事实：hypopg 模拟索引，不真建 ######'
SELECT hypopg_reset();
SELECT indexname FROM hypopg_create_index('CREATE INDEX ON orders(status, created_at DESC)');
EXPLAIN (COSTS ON, ANALYZE OFF)
SELECT id, user_id, total, created_at FROM orders
WHERE status = 'PENDING' ORDER BY created_at DESC LIMIT 20;
SELECT hypopg_reset();
