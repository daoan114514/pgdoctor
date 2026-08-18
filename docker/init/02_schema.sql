-- 电商 schema。orders 是主角：足够大，且 status 分布倾斜。
-- 注意：索引不在这里建 —— 见 03_seed.sh，灌完数据再建，12M 行时快得多。
CREATE TABLE users (
  id         BIGSERIAL PRIMARY KEY,
  email      TEXT        NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE products (
  id    BIGSERIAL PRIMARY KEY,
  sku   TEXT          NOT NULL,
  name  TEXT          NOT NULL,
  price NUMERIC(10,2) NOT NULL
);

CREATE TABLE orders (
  id         BIGSERIAL PRIMARY KEY,
  user_id    BIGINT        NOT NULL,
  status     TEXT          NOT NULL,   -- PENDING / PAID / SHIPPED / DELIVERED / CANCELLED
  total      NUMERIC(10,2) NOT NULL,
  created_at TIMESTAMPTZ   NOT NULL
);

CREATE TABLE order_items (
  id         BIGSERIAL PRIMARY KEY,
  order_id   BIGINT        NOT NULL,
  product_id BIGINT        NOT NULL,
  qty        INT           NOT NULL,
  unit_price NUMERIC(10,2) NOT NULL
);

ALTER TABLE users       OWNER TO app_owner;
ALTER TABLE products    OWNER TO app_owner;
ALTER TABLE orders      OWNER TO app_owner;
ALTER TABLE order_items OWNER TO app_owner;
