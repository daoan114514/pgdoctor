\timing on
\echo '=== 建候选基线索引 (user_id, status) ==='
CREATE INDEX idx_orders_user_status ON orders(user_id, status);
ANALYZE orders;

\echo ''
\echo '=== 健康态 ==='
EXPLAIN (ANALYZE, COSTS OFF)
SELECT id, total, created_at FROM orders WHERE user_id = 4242 AND status = 'PENDING';

\echo ''
\echo '=== 注入：DROP 该索引 ==='
DROP INDEX idx_orders_user_status;

\echo ''
\echo '=== 故障态（created_at 索引能否再救一次？）==='
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT id, total, created_at FROM orders WHERE user_id = 4242 AND status = 'PENDING';
