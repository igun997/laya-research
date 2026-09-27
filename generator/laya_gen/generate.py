"""laya-research :: synthetic grocery datasheet generator.

Streams a `(day, store, product)` fact table plus its store/product dimensions
into Postgres, as described in `README.md`.

Row count arithmetic
--------------------

    rows(market_facts) = LAYA_STORES * LAYA_PRODUCTS * LAYA_DAYS

    defaults:            140 * 2400 * 21 = 7,056,000

The stream is emitted as `LAYA_DAYS` days, each holding `LAYA_STORES` blocks of
`LAYA_PRODUCTS` rows, so the product telescopes exactly::

    21 days * 140 stores * 2400 products = 7,056,000 rows
    = 21 * 336,000 rows per day
    = 7,056,000 rows total

The count is exact, not approximate: every `(store, product)` pair gets one row
for every one of the `LAYA_DAYS` days.  `LAYA_DAYS` days are laid out
*backwards* from `day_max = date.today()` (the container clock): the first day
generated is `day_max - (LAYA_DAYS - 1)`, the last is `day_max`, which is
therefore fully populated for every `(store, product)` pair.

Environment
-----------

===========================  ==============  ==========================================
variable                     default         meaning
===========================  ==============  ==========================================
`DATABASE_URL`               (required)      libpq connection string
`LAYA_SEED`                  20260926        RNG seed; same seed => same rows, same ids
`LAYA_DAYS`                  21              days of history (day_max inclusive)
`LAYA_STORES`                140             store dimension size (ids 1..N)
`LAYA_PRODUCTS`              2400            product dimension size (ids 1..N)
`LAYA_MODE`                  ensure          `ensure` | `force`
===========================  ==============  ==========================================

`ensure` skips the load when `dataset_meta.generation->>'seed'` already equals
`LAYA_SEED` **and** `market_facts` is non-empty; otherwise it generates.
`force` truncates `market_facts`, deletes `signals` and always regenerates.
Either way the fact table is truncated before loading, so the load can never
duplicate a `(day, store_id, product_id)` primary key.

Demand model
------------

Facts are produced by numpy, vectorised over products for one `(store, day)`
block at a time, so every row of a given `(store, product)` shares one
baseline::

    baseline   = base_units[product] * store_demand[store]
    demand     = baseline * dow[product] * trend * shock[product-day]
                          * category_effect[category-day] * promo_response[cell]
                          * noise
    shelf      = list_price[product] * price_index[store] * price_factor[product-day]
    price      = shelf * (1 - promo_discount)          -- 0 when not promoted
    unit_cost  = shelf * cost_ratio[product] * cost_noise
    inventory  = closing forward cover, 0-6 days of baseline demand
    units_sold = round(demand)                         -- demand model only

* `dow` is weekly seasonality: Saturday carries the category `weekend_lift`
  (1.05-1.25), Sunday 0.97x that, Friday 1.10, weekdays 0.95-1.00.
* `category_effect` is a per `(category, day)` calendar/weather multiplier plus
  a private-label bias.  ~12% of category-days run 1.25-1.75x, which makes
  `CATEGORY_DRIFT` reachable; ~20% bias private label up 1.05-1.25x while
  pulling national brands down, moving `pl_share` by >= 0.02 so
  `PRIVATE_LABEL_GAIN` is reachable.
* `promo_response` is 1 off-promo.  ~8% of `(store, product, day)` cells are
  promoted: ~2.5% of product-days run a store-wide campaign covering 40-95% of
  stores, plus 6% of cells scattered at random.  Three quarters of campaigns
  lift units 1.4-2.2x (times the price elasticity of the 8-20% discount);
  one quarter are *ineffective* promos - a deep cut with 0.95-1.12x units,
  which is what makes `PROMO_INEFFECTIVE` and `MARGIN_SQUEEZE` reachable.
* `unit_cost` is derived from the *shelf* price, never the promo price, so
  `unit_cost < price` holds for every non-promo row by construction while a
  deep promo on a high-cost-ratio category pushes cost above price.
* `inventory` is *closing* forward cover of that store/product's demand,
  scaled down on supply-stress product-days (5% of them, 0.10-0.45x) so
  product-level cover drops below 0.6 and `STOCKOUT_RISK` fires.  `units_sold`
  is the demand model alone - stock is not clamped to it - so demand signals
  and stock signals stay independent.
* `on_order` is 0 for most rows and only populated when cover is thin.

Loading
-------

Rows are written with psycopg3 `cursor.copy()` in `FORMAT TEXT` batches of at
most `BATCH_ROWS` (200,000 <= the contract's 250,000 cap).  The full dataset
is never materialised: at most one batch of text plus one `(store, day)`
product vector is in memory at any time.

`market_facts` is loaded in one transaction: drop its five secondary indexes
and two foreign keys, `COPY` the stream, rebuild them, commit.  Maintaining
those b-trees and FK triggers row-by-row while 7M rows append costs minutes of
random I/O; one sort-based build afterwards costs seconds.  DDL is
transactional in Postgres, so the committed table carries exactly the objects
`db/init/01_schema.sql` declares and a crash rolls the load back rather than
leaving an unindexed table behind.  The primary key is never dropped, so a
duplicate `(day, store_id, product_id)` would still fail the load.

Exit code is 0 on success, 1 with a message on stderr on failure.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np
import psycopg

from . import catalog

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

DEFAULT_SEED = 20260926
DEFAULT_DAYS = 21
DEFAULT_STORES = 140
DEFAULT_PRODUCTS = 2400
DEFAULT_MODE = "ensure"
MODES = ("ensure", "force")

#: Copy batch ceiling.  Contract allows <= 250k rows per batch.
BATCH_ROWS = 200_000

# ---------------------------------------------------------------------------
# Model constants
# ---------------------------------------------------------------------------

#: Monday..Friday multipliers; Saturday and Sunday come from the per-category
#: `weekend_lift` (Saturday is the lift itself, Sunday 0.97x it).
WEEKDAY_FACTOR = (0.95, 0.96, 1.00, 1.05, 1.10)

#: Mild multi-week growth so the series has a trend.
DAILY_TREND = 0.005

#: Cell-level promo probability on top of the store-wide campaigns.
SCATTER_PROMO_P = 0.06
#: Probability that a given product-day runs a store-wide campaign.
CAMPAIGN_P = 0.025
#: Share of stores participating in a campaign.
CAMPAIGN_SHARE = (0.40, 0.95)
#: Fraction of campaigns that fail to move units.
INEFFECTIVE_PROMO_P = 0.25
#: Units lift of an effective / ineffective campaign.
EFFECTIVE_LIFT = (1.4, 2.2)
INEFFECTIVE_LIFT = (0.95, 1.12)
#: Price reduction of an effective / ineffective campaign.
EFFECTIVE_DISCOUNT = (0.08, 0.18)
INEFFECTIVE_DISCOUNT = (0.10, 0.20)

#: Probability that a product-day suffers a demand shock (collapse).
SHOCK_P = 0.06
SHOCK_FACTOR = (0.25, 0.60)
#: Probability that a product-day suffers a supply-stress day (thin cover).
STRESS_P = 0.05
STRESS_FACTOR = (0.10, 0.45)

#: Category-day calendar/weather multipliers: a category-wide surge on
#: ~12% of category-days (drives CATEGORY_DRIFT) and a quiet day on ~8%.
CATEGORY_SURGE_P = 0.12
CATEGORY_SURGE = (1.25, 1.75)
CATEGORY_LULL_P = 0.08
CATEGORY_LULL = (0.70, 0.88)
#: Category-day private-label bias, applied to units: house brands up
#: 1.05-1.25x while national brands are pulled down, which moves pl_share by
#: >= 0.02 on ~20% of category-days (drives PRIVATE_LABEL_GAIN).
PL_BIAS_P = 0.20
PL_BIAS = (1.05, 1.25)

#: Forward-cover sampling band, in days of baseline demand.
COVER_DAYS = (0.0, 6.0)
#: Extra cover carried on promoted cells (retailers stock up for promos).
PROMO_COVER_BONUS = 1.5
#: Cover below which replenishment is placed.
ORDER_COVER = 1.5
ORDER_PROB = 0.6
ORDER_DAYS = (1.0, 4.0)

#: Shelf-price drift: day-to-day jitter plus occasional repricing campaigns.
PRICE_JITTER_SIGMA = 0.012
PRICE_UP_P = 0.05
PRICE_UP_FACTOR = (1.06, 1.18)
PRICE_DOWN_P = 0.06
PRICE_DOWN_FACTOR = (0.86, 0.94)

#: Cell demand noise and cost noise (cost noise is clipped so that
#: `cost_ratio * noise < 1` always, which keeps cost below shelf price).
DEMAND_SIGMA = 0.15
COST_SIGMA = 0.03
COST_NOISE_CLIP = (0.90, 1.10)

#: Store demand multiplier dispersion (log-normal sigma).
STORE_DEMAND_SIGMA = 0.25
#: Per-store shelf price index.
STORE_PRICE_INDEX = (0.9, 1.1)

COPY_SQL = (
    "COPY market_facts "
    "(day, store_id, product_id, price, unit_cost, units_sold, "
    " promo_flag, inventory, on_order) "
    "FROM STDIN WITH (FORMAT TEXT)"
)

#: Secondary indexes on `market_facts`, exactly as `db/init/01_schema.sql`
#: declares them.  They are dropped for the bulk load and recreated in the same
#: transaction: maintaining five b-trees row-by-row while COPY appends 7M rows
#: costs several minutes of random I/O, while one sort-based build afterwards
#: costs seconds.  The schema itself is never modified and the indexes that
#: exist when the transaction commits are identical to the declared ones.
MARKET_FACT_INDEXES: tuple[tuple[str, str], ...] = (
    ("market_facts_product_day_idx",
     "CREATE INDEX market_facts_product_day_idx ON market_facts (product_id, day DESC)"),
    ("market_facts_store_day_idx",
     "CREATE INDEX market_facts_store_day_idx ON market_facts (store_id, day DESC)"),
    ("market_facts_day_idx",
     "CREATE INDEX market_facts_day_idx ON market_facts (day DESC)"),
    ("market_facts_promo_idx",
     "CREATE INDEX market_facts_promo_idx ON market_facts (day DESC) WHERE promo_flag"),
    ("market_facts_price_idx",
     "CREATE INDEX market_facts_price_idx ON market_facts (price)"),
)

#: Foreign keys on `market_facts`, recreated after the load for the same reason.
MARKET_FACT_FKS: tuple[tuple[str, str], ...] = (
    ("market_facts_store_id_fkey",
     "ALTER TABLE market_facts ADD CONSTRAINT market_facts_store_id_fkey "
     "FOREIGN KEY (store_id) REFERENCES stores (store_id)"),
    ("market_facts_product_id_fkey",
     "ALTER TABLE market_facts ADD CONSTRAINT market_facts_product_id_fkey "
     "FOREIGN KEY (product_id) REFERENCES products (product_id)"),
)

GENERATION_SQL = """
INSERT INTO dataset_meta (key, value)
VALUES ('generation', %s::jsonb)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
"""

CATALOG_SQL = """
INSERT INTO dataset_meta (key, value)
VALUES ('catalog', %s::jsonb)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
"""


def log(message: str) -> None:
    """One progress line on stdout, flushed so `docker compose logs` is live."""
    print(f"[generator] {message}", flush=True)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Resolved generator configuration."""

    database_url: str
    seed: int
    days: int
    stores: int
    products: int
    mode: str

    @property
    def rows(self) -> int:
        """Exact number of `market_facts` rows this configuration produces."""
        return self.stores * self.products * self.days

    def row_formula(self) -> str:
        return (
            f"{self.stores} stores x {self.products} products x {self.days} days "
            f"= {self.rows:,} rows"
        )


def _env_int(env: dict, name: str, default: int, minimum: int = 1) -> int:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def load_config(environ: dict | None = None) -> Config:
    """Read and validate the environment."""
    env = os.environ if environ is None else environ
    url = (env.get("DATABASE_URL") or "").strip()
    if not url:
        raise ValueError("DATABASE_URL is required (e.g. postgresql://laya:laya@db:5432/laya)")
    mode = (env.get("LAYA_MODE") or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        raise ValueError(f"LAYA_MODE must be one of {MODES}, got {mode!r}")
    return Config(
        database_url=url,
        seed=_env_int(env, "LAYA_SEED", DEFAULT_SEED, minimum=0),
        days=_env_int(env, "LAYA_DAYS", DEFAULT_DAYS),
        stores=_env_int(env, "LAYA_STORES", DEFAULT_STORES),
        products=_env_int(env, "LAYA_PRODUCTS", DEFAULT_PRODUCTS),
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Model parameters
# ---------------------------------------------------------------------------


def product_model(rng: random.Random, products: list[dict]) -> dict:
    """Per-product model parameters, sampled in product-id order."""
    n = len(products)
    base_units = np.empty(n, dtype=np.float64)
    cost_ratio = np.empty(n, dtype=np.float64)
    elasticity = np.empty(n, dtype=np.float64)
    weekend_lift = np.empty(n, dtype=np.float64)
    list_price = np.empty(n, dtype=np.float64)
    product_ids = np.empty(n, dtype=np.int64)
    category_index = np.empty(n, dtype=np.int64)
    is_private_label = np.empty(n, dtype=bool)
    for i, product in enumerate(products):
        cat = catalog.CATEGORY_BY_NAME[product["category"]]
        velocity = rng.uniform(*cat.units_per_store_day)
        if product["uom"] == "pack":
            # Multipacks move fewer units than singles.
            velocity /= math.sqrt(product["pack_size"])
        if product["is_private_label"]:
            velocity *= 1.2
        base_units[i] = max(velocity, 0.05)
        cost_ratio[i] = rng.uniform(*cat.cost_ratio)
        elasticity[i] = rng.uniform(*cat.elasticity)
        weekend_lift[i] = rng.uniform(*cat.weekend_lift)
        list_price[i] = float(product["list_price"])
        product_ids[i] = product["product_id"]
        category_index[i] = catalog.CATEGORY_INDEX[product["category"]]
        is_private_label[i] = product["is_private_label"]
    return {
        "product_ids": product_ids,
        "base_units": base_units,
        "cost_ratio": cost_ratio,
        "elasticity": elasticity,
        "weekend_lift": weekend_lift,
        "list_price": list_price,
        "category_index": category_index,
        "is_private_label": is_private_label,
    }


def store_model(rng: random.Random, stores: list[dict]) -> dict:
    """Per-store demand multiplier, price index and campaign participation."""
    n = len(stores)
    demand = np.empty(n, dtype=np.float64)
    price_index = np.empty(n, dtype=np.float64)
    campaign_u = np.empty(n, dtype=np.float64)
    store_ids = np.empty(n, dtype=np.int64)
    for i, store in enumerate(stores):
        fmt = catalog.FORMAT_BY_NAME[store["format"]]
        demand[i] = fmt.demand * rng.lognormvariate(0.0, STORE_DEMAND_SIGMA)
        price_index[i] = rng.uniform(*STORE_PRICE_INDEX)
        campaign_u[i] = rng.random()
        store_ids[i] = store["store_id"]
    return {
        "store_ids": store_ids,
        "demand": demand,
        "price_index": price_index,
        "campaign_u": campaign_u,
    }


def day_plan(nprng: np.random.Generator, n_products: int,
             elasticity: np.ndarray) -> dict:
    """Per-product-day and per-category-day draws for one day.

    All randomness for a day is drawn here, in a fixed order, so the fact
    stream is reproducible for a given seed.
    """
    n_categories = len(catalog.CATEGORIES)
    campaign = nprng.random(n_products) < CAMPAIGN_P
    ineffective = nprng.random(n_products) < INEFFECTIVE_PROMO_P
    share = np.where(campaign, nprng.uniform(*CAMPAIGN_SHARE, n_products), 0.0)
    lift = np.where(
        ineffective,
        nprng.uniform(*INEFFECTIVE_LIFT, n_products),
        nprng.uniform(*EFFECTIVE_LIFT, n_products),
    )
    discount = np.where(
        ineffective,
        nprng.uniform(*INEFFECTIVE_DISCOUNT, n_products),
        nprng.uniform(*EFFECTIVE_DISCOUNT, n_products),
    )
    # Effective campaigns also carry the price elasticity of the discount.
    response = np.where(
        ineffective,
        lift,
        lift * np.power(1.0 - discount, elasticity),
    )
    shock = np.where(
        nprng.random(n_products) < SHOCK_P,
        nprng.uniform(*SHOCK_FACTOR, n_products),
        1.0,
    )
    stress = np.where(
        nprng.random(n_products) < STRESS_P,
        nprng.uniform(*STRESS_FACTOR, n_products),
        1.0,
    )
    price_factor = np.clip(nprng.normal(1.0, PRICE_JITTER_SIGMA, n_products), 0.95, 1.05)
    price_factor = price_factor * np.where(
        nprng.random(n_products) < PRICE_UP_P,
        nprng.uniform(*PRICE_UP_FACTOR, n_products),
        1.0,
    )
    price_factor = price_factor * np.where(
        nprng.random(n_products) < PRICE_DOWN_P,
        nprng.uniform(*PRICE_DOWN_FACTOR, n_products),
        1.0,
    )

    # --- category-day calendar effects -------------------------------------
    surge = nprng.random(n_categories) < CATEGORY_SURGE_P
    lull = nprng.random(n_categories) < CATEGORY_LULL_P
    category_drift = np.where(
        surge,
        nprng.uniform(*CATEGORY_SURGE, n_categories),
        np.where(lull, nprng.uniform(*CATEGORY_LULL, n_categories), 1.0),
    )
    pl_bias = np.where(
        nprng.random(n_categories) < PL_BIAS_P,
        nprng.uniform(*PL_BIAS, n_categories),
        1.0,
    )

    return {
        "campaign_share": share,
        "campaign_response": response,
        "campaign_discount": discount,
        "shock": shock,
        "stress": stress,
        "price_factor": price_factor,
        "category_drift": category_drift,
        "pl_bias": pl_bias,
    }


def _stochastic_round(values: np.ndarray, rand: np.ndarray) -> np.ndarray:
    """Round to integers without the bias of `np.rint` on low-velocity items."""
    floor = np.floor(values)
    return (floor + (rand < (values - floor))).astype(np.int64)


def fact_block(nprng: np.random.Generator, plan: dict, model: dict,
               store_demand: float, price_index: float, campaign_u: float,
               dow_factor: np.ndarray, trend: float) -> dict:
    """One `(store, day)` block: every product for a single store and day."""
    n = model["base_units"].shape[0]

    scatter = nprng.random(n) < SCATTER_PROMO_P
    promo = (campaign_u < plan["campaign_share"]) | scatter
    discount = np.where(promo, plan["campaign_discount"], 0.0)
    response = np.where(promo, plan["campaign_response"], 1.0)

    # Category-day calendar effect, with a private-label bias on house brands.
    category_effect = plan["category_drift"][model["category_index"]]
    pl_bias = np.where(
        model["is_private_label"],
        plan["pl_bias"][model["category_index"]],
        1.0 / plan["pl_bias"][model["category_index"]],
    )

    baseline = model["base_units"] * store_demand
    demand = baseline * dow_factor * trend * plan["shock"] * category_effect * pl_bias
    demand = demand * response * np.exp(nprng.normal(0.0, DEMAND_SIGMA, n))
    units = _stochastic_round(np.maximum(demand, 0.0), nprng.random(n))

    cover_days = plan["stress"] * nprng.uniform(*COVER_DAYS, n)
    cover_days = cover_days + np.where(promo, PROMO_COVER_BONUS, 0.0)
    inventory = _stochastic_round(np.maximum(cover_days * baseline, 0.0), nprng.random(n))

    order_mask = (cover_days < ORDER_COVER) & (nprng.random(n) < ORDER_PROB)
    on_order = np.where(
        order_mask,
        np.rint(baseline * nprng.uniform(*ORDER_DAYS, n)).astype(np.int64),
        0,
    )

    shelf = model["list_price"] * price_index * plan["price_factor"]
    price = np.round(shelf * (1.0 - discount), 2)
    cost_noise = np.clip(nprng.normal(1.0, COST_SIGMA, n), *COST_NOISE_CLIP)
    unit_cost = np.round(shelf * model["cost_ratio"] * cost_noise, 2)

    # Invariant the model guarantees: cost is derived from the shelf price and
    # the cost ratio is <= 0.88 with noise clipped at 1.10, so a non-promo row
    # can never sell below cost.  Guard it so a future edit cannot break it.
    if np.any((unit_cost >= price) & ~promo):
        raise AssertionError("model invariant broken: non-promo unit_cost >= price")

    return {
        "product_ids": model["product_ids"],
        "price": price,
        "unit_cost": unit_cost,
        "units_sold": units,
        "promo_flag": promo,
        "inventory": inventory,
        "on_order": on_order,
    }


def format_block(day: str, store_id: int, block: dict) -> str:
    """Render one block as COPY TEXT lines (`\\t` separated, `\\n` terminated)."""
    head = f"{day}\t{store_id}\t"
    return "\n".join(
        [
            head + f"{p}\t{pr:.2f}\t{cs:.2f}\t{u}\t{'t' if pf else 'f'}\t{iv}\t{oo}"
            for p, pr, cs, u, pf, iv, oo in zip(
                block["product_ids"].tolist(),
                block["price"].tolist(),
                block["unit_cost"].tolist(),
                block["units_sold"].tolist(),
                block["promo_flag"].tolist(),
                block["inventory"].tolist(),
                block["on_order"].tolist(),
            )
        ]
    )


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def dataset_present(cur, seed: int) -> bool:
    """`ensure` guard: seed matches `dataset_meta.generation` and facts exist.

    Both halves are required: a matching seed with an empty `market_facts`
    (a truncated or half-loaded dataset) must still regenerate, and a populated
    table written from a different seed must not be silently reused.
    """
    cur.execute(
        """
        SELECT (SELECT value->>'seed' FROM dataset_meta
                 WHERE key = 'generation'
                   AND (value->>'seed') ~ '^-?[0-9]+$')::bigint,
               EXISTS (SELECT 1 FROM market_facts)
        """
    )
    stored_seed, has_rows = cur.fetchone()
    return stored_seed == seed and has_rows


def drop_fact_indexes(cur) -> None:
    """Drop `market_facts` secondary indexes and foreign keys for a bulk load."""
    for name, _ in MARKET_FACT_FKS:
        cur.execute(f"ALTER TABLE market_facts DROP CONSTRAINT IF EXISTS {name}")
    for name, _ in MARKET_FACT_INDEXES:
        cur.execute(f"DROP INDEX IF EXISTS {name}")


def recreate_fact_indexes(cur) -> None:
    """Rebuild the indexes and foreign keys declared in the schema."""
    for _, ddl in MARKET_FACT_INDEXES:
        cur.execute(ddl)
    for _, ddl in MARKET_FACT_FKS:
        cur.execute(ddl)


def _copy_dimension(cur, table: str, columns: list[str], rows: list[tuple],
                    key: str) -> None:
    """COPY a dimension into a temp table, then upsert it into the real table."""
    collist = ", ".join(columns)
    cur.execute(f"CREATE TEMP TABLE tmp_{table} (LIKE {table}) ON COMMIT DROP")
    with cur.copy(f"COPY tmp_{table} ({collist}) FROM STDIN WITH (FORMAT TEXT)") as copy:
        copy.write("\n".join("\t".join(_literal(v) for v in row) for row in rows) + "\n")
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != key)
    cur.execute(
        f"INSERT INTO {table} ({collist}) SELECT {collist} FROM tmp_{table} "
        f"ON CONFLICT ({key}) DO UPDATE SET {updates}"
    )
    cur.execute(f"DELETE FROM {table} WHERE {key} > %s", (len(rows),))


def _literal(value) -> str:
    """TEXT COPY literal for a python scalar (dates/ints/bools/text)."""
    if isinstance(value, bool):
        return "t" if value else "f"
    return str(value)


def _load_stores(cur, stores: list[dict]) -> None:
    rows = [
        (s["store_id"], s["name"], s["region"], s["city"], s["format"],
         s["size_sqm"], s["opened_on"].isoformat())
        for s in stores
    ]
    _copy_dimension(
        cur, "stores",
        ["store_id", "name", "region", "city", "format", "size_sqm", "opened_on"],
        rows, "store_id",
    )


def _load_products(cur, products: list[dict]) -> None:
    rows = [
        (p["product_id"], p["sku"], p["name"], p["brand"], p["category"],
         p["subcategory"], p["uom"], f"{p['pack_size']:.2f}",
         _literal(p["is_private_label"]), _literal(p["is_perishable"]),
         f"{p['list_price']:.2f}")
        for p in products
    ]
    _copy_dimension(
        cur, "products",
        ["product_id", "sku", "name", "brand", "category", "subcategory", "uom",
         "pack_size", "is_private_label", "is_perishable", "list_price"],
        rows, "product_id",
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def generate(cfg: Config) -> None:
    """Build the dataset and load it.  Raises on failure."""
    started = time.monotonic()
    log(f"config: seed={cfg.seed} days={cfg.days} stores={cfg.stores} "
        f"products={cfg.products} mode={cfg.mode}")
    log(f"rows(market_facts) = LAYA_STORES x LAYA_PRODUCTS x LAYA_DAYS = {cfg.row_formula()}")

    # --- dimensions (deterministic from the seed) --------------------------
    rng = random.Random(cfg.seed)
    stores = catalog.build_stores(rng, cfg.stores)
    products = catalog.build_products(rng, cfg.products)
    pmodel = product_model(rng, products)
    smodel = store_model(rng, stores)
    log(f"catalog: {len(catalog.CATEGORIES)} categories, "
        f"{sum(len(c.subcategories) for c in catalog.CATEGORIES)} subcategories, "
        f"{len(catalog.ALL_BRANDS)} brands, "
        f"{sum(p['is_private_label'] for p in products) / len(products):.0%} private label, "
        f"{sum(p['is_perishable'] for p in products) / len(products):.0%} perishable")

    # --- days: backwards from today's date on the container clock ----------
    day_max = date.today()
    days = [day_max - timedelta(days=cfg.days - 1 - i) for i in range(cfg.days)]
    day_min = days[0]
    log(f"days: {day_min.isoformat()} .. {day_max.isoformat()} ({len(days)} days, "
        f"day_max fully populated for every (store, product) pair)")

    nprng = np.random.default_rng(cfg.seed)

    with psycopg.connect(cfg.database_url) as conn:
        # Clear facts first: the dimension load prunes rows above the new id
        # range, which the market_facts foreign keys would otherwise block.
        with conn.cursor() as cur:
            cur.execute("TRUNCATE market_facts")
            cur.execute("DELETE FROM signals")
        conn.commit()
        log("market_facts truncated, signals cleared")

        with conn.cursor() as cur:
            _load_stores(cur, stores)
            _load_products(cur, products)
        conn.commit()
        log(f"dimensions loaded: {len(stores)} stores, {len(products)} products")

        total = cfg.rows
        step = max(total // 10, 1)
        next_log = step
        rows_done = 0
        buffer: list[str] = []
        buffer_rows = 0

        # Drop -> COPY -> rebuild -> commit as one transaction, so the table is
        # never observable without its declared indexes and a crash rolls the
        # whole load back (DDL is transactional in Postgres).  Maintaining five
        # b-trees plus two FK triggers row-by-row while COPY appends 7M rows
        # costs minutes of random I/O; one sort-based build afterwards costs
        # seconds, and the objects committed are identical to the declared ones.
        with conn.cursor() as cur:
            drop_fact_indexes(cur)
            log("market_facts indexes/FKs dropped for bulk load")
            with cur.copy(COPY_SQL) as copy:
                for day_index, day in enumerate(days):
                    dow = day.weekday()
                    if dow == 5:
                        dow_factor = pmodel["weekend_lift"]
                    elif dow == 6:
                        dow_factor = pmodel["weekend_lift"] * 0.97
                    else:
                        dow_factor = np.full(cfg.products, WEEKDAY_FACTOR[dow])
                    trend = 1.0 + DAILY_TREND * day_index
                    plan = day_plan(nprng, cfg.products, pmodel["elasticity"])
                    day_text = day.isoformat()
                    for store_index, store_id in enumerate(smodel["store_ids"].tolist()):
                        block = fact_block(
                            nprng,
                            plan,
                            pmodel,
                            float(smodel["demand"][store_index]),
                            float(smodel["price_index"][store_index]),
                            float(smodel["campaign_u"][store_index]),
                            dow_factor,
                            trend,
                        )
                        buffer.append(format_block(day_text, store_id, block))
                        buffer_rows += cfg.products
                        rows_done += cfg.products
                        if buffer_rows >= BATCH_ROWS:
                            copy.write("\n".join(buffer) + "\n")
                            buffer.clear()
                            buffer_rows = 0
                        if rows_done >= next_log:
                            log(f"{rows_done:,}/{total:,} rows "
                                f"({100.0 * rows_done / total:.0f}%) "
                                f"elapsed {time.monotonic() - started:.1f}s")
                            next_log += step
                if buffer:
                    copy.write("\n".join(buffer) + "\n")
                    buffer.clear()
            log(f"streamed {total:,} rows in {time.monotonic() - started:.1f}s, "
                f"rebuilding indexes")
            recreate_fact_indexes(cur)
        conn.commit()
        log(f"loaded {total:,} rows and rebuilt indexes "
            f"({time.monotonic() - started:.1f}s)")

        with conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW mv_product_day")
            cur.execute("REFRESH MATERIALIZED VIEW mv_category_day")
            log(f"materialized views refreshed ({time.monotonic() - started:.1f}s)")
            cur.execute("ANALYZE stores, products, market_facts")
            log("ANALYZE done")

            generation = {
                "seed": cfg.seed,
                "days": cfg.days,
                "stores": cfg.stores,
                "products": cfg.products,
                "rows": total,
                "day_min": day_min.isoformat(),
                "day_max": day_max.isoformat(),
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            meta_catalog = {
                "categories": list(catalog.categories()),
                "formats": list(catalog.formats()),
                "regions": list(catalog.regions()),
                "brands": list(catalog.brands()),
            }
            cur.execute(GENERATION_SQL, (json.dumps(generation),))
            cur.execute(CATALOG_SQL, (json.dumps(meta_catalog),))
        conn.commit()
        log(f"dataset_meta written: generation={json.dumps(generation)}")
    log(f"done in {time.monotonic() - started:.1f}s")


def run(cfg: Config) -> int:
    """Apply `LAYA_MODE`, then generate if needed.  Returns the exit code."""
    if cfg.mode == "ensure":
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                if dataset_present(cur, cfg.seed):
                    print("dataset present, skipping", flush=True)
                    return 0
        log("dataset missing or seed changed - generating")
    else:
        log("mode=force - truncating and regenerating")
    generate(cfg)
    return 0


def main() -> int:
    """Entry point.  Returns the process exit code."""
    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001 - configuration errors are fatal
        print(f"[generator] configuration error: {exc}", file=sys.stderr, flush=True)
        return 1
    try:
        return run(cfg)
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
        print(f"[generator] ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
