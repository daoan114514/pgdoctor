#!/usr/bin/env bash
# 灌数 + 建索引 + ANALYZE。健康基线在这里成型。
set -euo pipefail

ORDERS="${SEED_ORDERS:-12000000}"
USERS="${SEED_USERS:-100000}"
PRODUCTS="${SEED_PRODUCTS:-10000}"
ITEMS_MAX=2000000          # order_items 只灌一部分，MVP 场景用不到它
CHUNK=1000000

run() { psql -v ON_ERROR_STOP=1 -q --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "$1"; }

echo "[seed] users=$USERS products=$PRODUCTS orders=$ORDERS"

run "INSERT INTO users (email)
     SELECT 'user' || g || '@example.com' FROM generate_series(1, $USERS) g;"

run "INSERT INTO products (sku, name, price)
     SELECT 'SKU-' || g, 'Product ' || g, (random() * 200 + 5)::numeric(10,2)
     FROM generate_series(1, $PRODUCTS) g;"

# orders 分块插入：避免单事务过大，并提供进度输出
# status 倾斜分布 —— PENDING 约 10%，这是缺索引场景里被全表扫的那一片
done_rows=0
while [ "$done_rows" -lt "$ORDERS" ]; do
  n=$(( ORDERS - done_rows )); [ "$n" -gt "$CHUNK" ] && n=$CHUNK
  run "INSERT INTO orders (user_id, status, total, created_at)
       SELECT 1 + (s.g % $USERS),
              CASE WHEN s.rnd < 0.10 THEN 'PENDING'
                   WHEN s.rnd < 0.35 THEN 'PAID'
                   WHEN s.rnd < 0.60 THEN 'SHIPPED'
                   WHEN s.rnd < 0.95 THEN 'DELIVERED'
                   ELSE 'CANCELLED' END,
              (random() * 500 + 1)::numeric(10,2),
              now() - (random() * interval '365 days')
       FROM (SELECT g, random() AS rnd FROM generate_series(1, $n) g) s;"
  done_rows=$(( done_rows + n ))
  echo "[seed] orders $done_rows / $ORDERS"
done

items=$(( ORDERS < ITEMS_MAX ? ORDERS : ITEMS_MAX ))
run "INSERT INTO order_items (order_id, product_id, qty, unit_price)
     SELECT 1 + (g % $ORDERS), 1 + (g % $PRODUCTS),
            1 + (random() * 4)::int, (random() * 200 + 5)::numeric(10,2)
     FROM generate_series(1, $items) g;"
echo "[seed] order_items $items"

# ★ 健康基线包含 idx_orders_status。missing_index 注入器的动作就是 DROP 掉它，
#   这样"健康"有明确定义，故障注入后的 KPI 落差也才干净可测。
echo "[seed] building indexes ..."
run "CREATE INDEX idx_orders_user_status ON orders(user_id, status);"
run "CREATE INDEX idx_orders_created_at ON orders(created_at);"
run "CREATE INDEX idx_order_items_order ON order_items(order_id);"

echo "[seed] ANALYZE ..."
run "ANALYZE;"

run "SELECT status, count(*) FROM orders GROUP BY status ORDER BY 2 DESC;"
echo "[seed] done."
