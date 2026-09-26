-- laya-research :: datasheet schema
-- Owned by db/init/01_schema.sql. Every service codes against this file.
-- Idempotent: safe on re-run against a fresh volume.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Dimensions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS stores (
    store_id    integer PRIMARY KEY,
    name        text        NOT NULL,
    region      text        NOT NULL,
    city        text        NOT NULL,
    format      text        NOT NULL,   -- supermarket | discounter | convenience | hypermarket | online
    size_sqm    integer     NOT NULL CHECK (size_sqm > 0),
    opened_on   date        NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    product_id       integer PRIMARY KEY,
    sku              text          NOT NULL UNIQUE,
    name             text          NOT NULL,
    brand            text          NOT NULL,
    category         text          NOT NULL,
    subcategory      text          NOT NULL,
    uom              text          NOT NULL,   -- each | kg | litre | pack
    pack_size        numeric(10,2) NOT NULL CHECK (pack_size > 0),
    is_private_label boolean       NOT NULL,
    is_perishable    boolean       NOT NULL,
    list_price       numeric(10,2) NOT NULL CHECK (list_price > 0),
    search_tsv       tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(name, '')),        'A') ||
        setweight(to_tsvector('simple',  coalesce(sku, '')),         'A') ||
        setweight(to_tsvector('english', coalesce(brand, '')),       'B') ||
        setweight(to_tsvector('english', coalesce(category, '')),    'B') ||
        setweight(to_tsvector('english', coalesce(subcategory, '')), 'C')
    ) STORED
);

-- ---------------------------------------------------------------------------
-- Fact table: one row per (day, store, product). This is the "datasheet".
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS market_facts (
    day        date          NOT NULL,
    store_id   integer       NOT NULL REFERENCES stores   (store_id),
    product_id integer       NOT NULL REFERENCES products (product_id),
    price      numeric(10,2) NOT NULL CHECK (price >= 0),
    unit_cost  numeric(10,2) NOT NULL CHECK (unit_cost >= 0),
    units_sold integer       NOT NULL CHECK (units_sold >= 0),
    promo_flag boolean       NOT NULL DEFAULT false,
    inventory  integer       NOT NULL CHECK (inventory >= 0),
    on_order   integer       NOT NULL DEFAULT 0 CHECK (on_order >= 0),
    PRIMARY KEY (day, store_id, product_id)
);

CREATE INDEX IF NOT EXISTS market_facts_product_day_idx ON market_facts (product_id, day DESC);
CREATE INDEX IF NOT EXISTS market_facts_store_day_idx   ON market_facts (store_id,   day DESC);
CREATE INDEX IF NOT EXISTS market_facts_day_idx         ON market_facts (day DESC);
CREATE INDEX IF NOT EXISTS market_facts_promo_idx       ON market_facts (day DESC) WHERE promo_flag;
CREATE INDEX IF NOT EXISTS market_facts_price_idx       ON market_facts (price);

CREATE INDEX IF NOT EXISTS products_search_tsv_idx ON products USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS products_name_trgm_idx  ON products USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS products_category_idx   ON products (category, subcategory);
CREATE INDEX IF NOT EXISTS products_brand_idx      ON products (brand);

-- ---------------------------------------------------------------------------
-- Rollups. Refreshed by the simulator after each mutation tick.
-- ---------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_product_day AS
SELECT
    f.product_id,
    f.day,
    sum(f.units_sold)                                  AS units,
    sum(f.units_sold * f.price)                        AS revenue,
    sum(f.units_sold * f.unit_cost)                    AS cogs,
    avg(f.price)                                       AS avg_price,
    sum(f.inventory)                                   AS inventory,
    count(*)                                           AS store_count,
    count(*) FILTER (WHERE f.promo_flag)               AS promo_stores
FROM market_facts f
GROUP BY f.product_id, f.day;

CREATE UNIQUE INDEX IF NOT EXISTS mv_product_day_pk ON mv_product_day (product_id, day);
CREATE INDEX IF NOT EXISTS mv_product_day_day_idx ON mv_product_day (day DESC);

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_category_day AS
SELECT
    p.category,
    f.day,
    sum(f.units_sold)                                                        AS units,
    sum(f.units_sold * f.price)                                              AS revenue,
    sum(f.units_sold * f.unit_cost)                                          AS cogs,
    sum(f.units_sold) FILTER (WHERE p.is_private_label)                       AS pl_units,
    count(DISTINCT f.product_id)                                              AS product_count
FROM market_facts f
JOIN products p USING (product_id)
GROUP BY p.category, f.day;

CREATE UNIQUE INDEX IF NOT EXISTS mv_category_day_pk ON mv_category_day (category, day);
CREATE INDEX IF NOT EXISTS mv_category_day_day_idx ON mv_category_day (day DESC);

-- ---------------------------------------------------------------------------
-- Decision output
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS signals (
    signal_id    bigserial   PRIMARY KEY,
    fired_at     timestamptz NOT NULL DEFAULT now(),
    day          date        NOT NULL,
    pattern      text        NOT NULL,
    severity     text        NOT NULL CHECK (severity IN ('info', 'warn', 'critical')),
    subject_type text        NOT NULL CHECK (subject_type IN ('product', 'category', 'store')),
    subject_id   integer     NOT NULL,
    subject_label text       NOT NULL,
    score        numeric(12,4) NOT NULL,
    evidence     jsonb       NOT NULL,
    action       text        NOT NULL,
    seen         boolean     NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS signals_fired_idx   ON signals (fired_at DESC);
CREATE INDEX IF NOT EXISTS signals_pattern_idx ON signals (pattern, fired_at DESC);
CREATE INDEX IF NOT EXISTS signals_subject_idx ON signals (subject_type, subject_id, fired_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS signals_dedupe_idx
    ON signals (pattern, subject_type, subject_id, day);

-- ---------------------------------------------------------------------------
-- Dataset provenance / idempotency for the generator
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS dataset_meta (
    key        text        PRIMARY KEY,
    value      jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
