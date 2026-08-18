\echo '=== extensions ==='
SELECT extname FROM pg_extension WHERE extname IN ('pg_stat_statements','hypopg','pgstattuple','pg_buffercache') ORDER BY 1;
\echo '=== roles ==='
SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname IN ('agent_ro','agent_rw','app_owner') ORDER BY 1;
\echo '=== row counts ==='
SELECT 'orders' t, count(*) FROM orders UNION ALL SELECT 'users', count(*) FROM users
UNION ALL SELECT 'products', count(*) FROM products UNION ALL SELECT 'order_items', count(*) FROM order_items;
\echo '=== indexes on orders ==='
SELECT indexname FROM pg_indexes WHERE tablename='orders' ORDER BY 1;
\echo '=== table size ==='
SELECT pg_size_pretty(pg_total_relation_size('orders')) AS orders_total;
