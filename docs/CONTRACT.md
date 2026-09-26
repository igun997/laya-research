# laya-research :: service contract v1

Frozen interface between `db`, `generator`, `api`, `sim`, `web`.
**Do not change a shape here without updating all four services in the same commit.**

Domain: grocery market `(day, store, product)` facts -> searchable datasheet -> rule-based
decision patterns -> live UI.

Row volume: `LAYA_STORES x LAYA_PRODUCTS x LAYA_DAYS` (default `140 x 2400 x 21 = 7,056,000`).

---

## 1. Datasheet schema

Authoritative DDL: `db/init/01_schema.sql`. Summary:

| object | grain | notes |
|---|---|---|
| `stores` | `store_id` | `region, city, format, size_sqm, opened_on` |
| `products` | `product_id` | `sku, name, brand, category, subcategory, uom, pack_size, is_private_label, is_perishable, list_price, search_tsv` |
| `market_facts` | `(day, store_id, product_id)` | `price, unit_cost, units_sold, promo_flag, inventory, on_order` |
| `mv_product_day` | `(product_id, day)` | `units, revenue, cogs, avg_price, inventory, store_count, promo_stores` |
| `mv_category_day` | `(category, day)` | `units, revenue, cogs, pl_units, product_count` |
| `signals` | `signal_id` | decision output, see §4 |
| `dataset_meta` | `key` | provenance / idempotency |

Derived conventions (all services MUST use these definitions verbatim):

```
margin        = (revenue - cogs) / revenue            -- 0 when revenue = 0
avg_price     = avg(price)                            -- unweighted store mean, both MV and live path
pl_share      = pl_units / units
promo_share   = promo_stores / store_count
```

`products.search_tsv` is a generated column, weights `name`/`sku` = A, `brand`/`category` = B,
`subcategory` = C. Query it with `websearch_to_tsquery('english', $q)`.

`dataset_meta` keys written by the generator:

| key | value |
|---|---|
| `generation` | `{"seed":int,"days":int,"stores":int,"products":int,"rows":int,"day_min":"YYYY-MM-DD","day_max":"YYYY-MM-DD","generated_at":iso}` |
| `catalog` | `{"categories":[...],"formats":[...],"regions":[...],"brands":[...]}` |

---

## 2. `generator` (one-shot container, exits 0)

Deterministic: same `LAYA_SEED` -> byte-identical `market_facts`.

Env: `DATABASE_URL`, `LAYA_SEED`, `LAYA_DAYS`, `LAYA_STORES`, `LAYA_PRODUCTS`, `LAYA_MODE`.

`LAYA_MODE`:
- `ensure` (default) — if `dataset_meta.generation.seed == LAYA_SEED` and `market_facts` is
  non-empty, log `dataset present, skipping` and exit 0. Otherwise generate.
- `force` — `TRUNCATE market_facts`, delete `signals`, regenerate.

Required behaviour:
1. Idempotent under `ensure`; never duplicate rows (PK is `(day,store_id,product_id)`).
2. Stream rows into Postgres via `COPY` (psycopg3 `cursor.copy()`); **never** materialise the
   full dataset in memory. Chunk size <= 250k rows.
3. Realism that makes the rules meaningful:
   - per-product `list_price`, category-dependent cost ratio (~0.62-0.88 of price),
     elasticity ~ -1.6, weekly seasonality (weekend lift), per-store demand multiplier,
     promo lift ~1.4-2.2x with ~8% of product-days promoted,
     inventory = forward cover 0-6 days (deliberately producing occasional stockouts).
   - a `day_max` "current" day that is fully populated for every (store, product) pair.
4. After loading: `REFRESH MATERIALIZED VIEW mv_product_day` and `mv_category_day`
   (non-concurrent is fine, first populate), then write `dataset_meta.generation` + `catalog`.
5. `ANALYZE stores, products, market_facts`.

Catalog content: >= 10 categories, >= 4 subcategories each, >= 250 distinct brands,
~35% private label, ~30% perishable, plausible grocery product names so full-text
search has something to bite on.

---

## 3. `sim` (long-running)

Env: `DATABASE_URL`, `LAYA_TICK_CHANNEL` (default `laya_events`), `LAYA_TICK_SECONDS` (4),
`LAYA_TICK_ROWS` (250), `LAYA_SEED`, `LAYA_REFRESH_SECONDS` (30).

Loop, every `LAYA_TICK_SECONDS`:
1. `target_day` = `max(day)` from `market_facts` (re-read each tick; fall back to `CURRENT_DATE`
   if the table is empty, and idle without publishing).
2. Pick `LAYA_TICK_ROWS` random `(store_id, product_id)` pairs for `target_day`
   (`ORDER BY random()` on a sampled set is acceptable; must be index-friendly, not a full scan).
3. Mutate `price`, `units_sold`, `inventory`, `promo_flag`, `on_order` with a bounded random walk
   (price +/-3%, units +/-25% with promo interaction, inventory decremented by units then
   replenished). **Never** change `day`, `store_id`, `product_id`. **Never** insert or delete rows.
4. `pg_notify(LAYA_TICK_CHANNEL, payload)` — see §6.
5. Background thread, at most every `LAYA_REFRESH_SECONDS`:
   `REFRESH MATERIALIZED VIEW CONCURRENTLY mv_product_day` and `mv_category_day`
   (autocommit connection — CONCURRENTLY cannot run in a transaction block).
   Rollup staleness is expected and documented; do not block the tick loop on it.
6. After a successful refresh, upsert `dataset_meta` key `rollup_refresh`:
   `{"at":"<iso-8601 UTC, ms>","views":["mv_product_day","mv_category_day"],"duration_ms":int}`.
   This is the only honest source for "how stale are the rollups" — see §5 `health` and
   `overview`. `REFRESH ... CONCURRENTLY` re-aggregates all of `market_facts`, so at the default
   dataset size one refresh is ~20-25 s; the "skip this slot if one is still running" guard keeps
   slots from stacking, which means the effective cadence is `max(LAYA_REFRESH_SECONDS, duration)`.

`dataset_meta` is otherwise untouched by `sim`. Log one line per tick: tick number, target day,
rows mutated, distinct products, ms.

---

## 4. Decision patterns (rule engine, owned by `api`)

Catalog is declarative in `api/app/rules.py` and served verbatim by `GET /api/patterns`.

Baseline for subject at day `d` = **median** over `d-14 .. d-1` (`percentile_cont(0.5)`),
`baseline_days = 14`, suppressed when fewer than `min_obs = 4` observations exist.

| id | scope | fires when | severity bands | score |
|---|---|---|---|---|
| `DEMAND_SURGE` | product | `lift = units/med(units)`, requires `med(units) >= 20` | warn `>=1.8`, critical `>=2.6` | `lift` |
| `DEMAND_COLLAPSE` | product | `ratio = units/med(units)`, requires `med(units) >= 20` | warn `<=0.55`, critical `<=0.35` | `1 - ratio` |
| `PRICE_SPIKE` | product | `pr = avg_price/med(avg_price)` | warn `>=1.06`, critical `>=1.15` | `pr` |
| `PRICE_CUT_UNANSWERED` | product | `pr <= 0.94` **and** `lift < 1.05` | warn | `pr` |
| `MARGIN_SQUEEZE` | product | `margin < med(margin) - 0.03` **and** `margin < 0.18` | warn, critical `margin < 0.10` | `med(margin) - margin` |
| `STOCKOUT_RISK` | product | `cover = inventory/med(units)` | warn `<1.2`, critical `<0.6` | `max(0, 1.2 - cover)` |
| `PROMO_INEFFECTIVE` | product | `promo_share >= 0.5` **and** `lift < 1.15` | warn | `promo_share` |
| `CATEGORY_DRIFT` | category | `lift = units/med(units)` on `mv_category_day` | info `>=1.25`, warn `>=1.6` | `lift` |
| `PRIVATE_LABEL_GAIN` | category | `delta = pl_share - med(pl_share)` | info `>=0.02` | `delta` |

A subject fires at most one severity per pattern; when several rules hit, emit all of them.
`day` in `signals` is the day the pattern was evaluated for (`target_day`).

`evidence` jsonb MUST contain at least:
`{"metric":"units|avg_price|margin|cover|share","value":num,"baseline":num|null,
"window_days":14,"observations":int}`
plus rule-specific keys (`lift`, `promo_share`, `pl_share_delta`, `store_count`, ...).
`subject_label` = product `name`, or the category name for category-scope rules.

`action` is server-rendered prose with the numbers baked in, e.g.
`DEMAND_SURGE` -> `"Replenish <name>: 412 units vs 180 baseline (2.29x). Confirm competitor stockout before raising price."`
The UI renders `action` verbatim — never re-template it client-side.

Persistence: `INSERT ... ON CONFLICT (pattern, subject_type, subject_id, day) DO UPDATE`
(severity, score, evidence, action, `fired_at = now()`). Same day + same subject + same pattern
= one row, updated.

Freshness path (sub-second): `sim` only mutates `target_day`. So `api` recomputes the
`target_day` rollup row for affected products **live** from `market_facts`
(`WHERE day = $target AND product_id = ANY($ids) GROUP BY product_id`), and reads
`mv_product_day` / `mv_category_day` for all earlier days. Both paths use the §1 definitions.

---

## 5. HTTP API (FastAPI, mounted under `/api`, port 8000)

All responses JSON, snake_case. Errors: `{"detail":"..."}` with 4xx/5xx.
`GET /api/health` -> `{"status":"ok","db":true,"rollup_lag_seconds":num|null,"dataset":{...}}`

`rollup_lag_seconds` = seconds since `dataset_meta.rollup_refresh.at`, i.e. how far the rollup
views trail the continuously-mutated facts. It is **not** the age of the newest dataset day —
those are different quantities and conflating them produces a number that looks like a lag but
only reflects how long ago the dataset was generated. `null` when the simulator has not stamped a
refresh yet. Negative or missing values report `null`.

`GET /api/meta`
```json
{"dataset":{"facts":7056000,"products":2400,"stores":140,"day_min":"...","day_max":"...","seed":20260926},
 "categories":[{"category":"Produce","products":310}],
 "brands":["..."],"formats":["discounter"],"regions":["..."]}
```

`GET /api/products?q=&category=&brand=&private_label=&perishable=&limit=25&offset=0`
```json
{"total":123,"limit":25,"offset":0,
 "items":[{"product_id":1,"sku":"SKU-000001","name":"...","brand":"...","category":"...",
           "subcategory":"...","uom":"each","pack_size":1.0,"is_private_label":false,
           "is_perishable":true,"list_price":2.49,
           "latest_day":"...","latest_units":12,"latest_price":2.55,"latest_margin_pct":0.31}]}
```
`q`: `websearch_to_tsquery('english', q)` against `search_tsv` ordered by
`ts_rank DESC, name ASC`. If tsquery yields zero rows, retry with `name % q` / `similarity`
(`pg_trgm`) ordered by similarity. Never return an error for punctuation-only input.

`GET /api/search?...` — fact-grain search, the workhorse.
Params: `q, product_id, store_id, category, brand, format, region, promo(bool),
min_price, max_price, min_units, date_from, date_to, sort=revenue|units|price|margin|day,
limit=50, offset=0`
```json
{"total":4213,"limit":50,"offset":0,
 "items":[{"day":"...","store_id":3,"store_name":"...","region":"...","format":"...",
           "product_id":1,"sku":"...","product":"...","brand":"...","category":"...",
           "price":2.55,"unit_cost":1.7,"units_sold":12,"revenue":30.6,"margin_pct":0.3333,
           "promo_flag":false,"inventory":41,"on_order":0}],
 "totals":{"revenue":12345.67,"units":4321,"margin_pct":0.29,"rows":4213}}
```
`totals` covers the whole filtered set, not the page. `limit <= 500`.

`GET /api/overview?days=14`
```json
{"days":[{"day":"...","units":1,"revenue":1.0,"cogs":1.0,"margin_pct":0.3,
          "pl_units":1,"pl_share":0.35}],
 "categories":[{"category":"...","units":1,"revenue":1.0,"margin_pct":0.3,"pl_share":0.4,
                "units_dod_pct":0.02}],
 "movers":[{"product_id":1,"name":"...","category":"...","units":1,"baseline":1.0,"lift":2.3,"revenue":1.0}],
 "signal_counts":{"critical":0,"warn":3,"info":7},
 "as_of":"<iso>","rollups_as_of":"<iso>"}
```
`rollups_as_of` = `dataset_meta.rollup_refresh.at` (the same instant `health.rollup_lag_seconds`
is measured from), falling back to `max(day)` of `mv_product_day` before the simulator's first
refresh. The UI renders it as relative time; the rollup trail is user-visible on purpose.

`movers` = top 10 by `lift` (descending) from the latest day with a valid baseline.

`GET /api/patterns` -> `{"patterns":[{"id","label","scope","description","thresholds":{...},
"severity_bands":[{"severity","when"}],"action_template","baseline_days","min_obs"}]}`

`GET /api/signals?pattern=&severity=&subject_type=&subject_id=&since=&limit=50&only_unseen=false`
```json
{"items":[{"signal_id":1,"fired_at":"...","day":"...","pattern":"DEMAND_SURGE",
           "severity":"warn","subject_type":"product","subject_id":1,"subject_label":"...",
           "score":2.29,"evidence":{"...":"..."},"action":"..."}],
 "counts":{"critical":0,"warn":3,"info":7},"total":10}
```
`POST /api/signals/seen` body `{"signal_ids":[1,2]}` -> `{"updated":2}`

`POST /api/patterns/scan` body `{"day":"YYYY-MM-DD"|null,"product_ids":[...]|null,
"categories":[...]|null,"persist":true}` -> `{"scanned":N,"signals":[...]}`.
`null` day = latest day. `persist:false` computes without writing.

`GET /api/products/{id}/series?days=30` ->
`{"product":{...},"series":[{"day","units","avg_price","revenue","margin_pct","inventory","promo_stores","store_count"}]}`

`GET /api/categories/{category}/series?days=30` -> `{"category":"...","series":[{...}]}`

---

## 6. Realtime transport

### 6.1 Postgres NOTIFY (sim -> api)

Channel: `LAYA_TICK_CHANNEL`. Payload is JSON, **max 8000 bytes** — cap `rows` at 200 entries
and always include the deduplicated id lists:

```json
{"kind":"tick","tick":42,"at":"<iso>","day":"YYYY-MM-DD",
 "rows":[{"store_id":3,"product_id":1}, ...],
 "product_ids":[1,2,3],"stores":[3,4],"row_count":250}
```

### 6.2 WebSocket `GET /api/stream`

`api` holds exactly **one** `LISTEN` connection (own thread, autocommit, reconnect with
exponential backoff) and fans out to all WS clients. Server -> client frames:

```json
{"type":"hello","at":"<iso>","dataset":{...}}
{"type":"tick","at":"<iso>","day":"...","mutations":[{"store_id":3,"product_id":1}],
 "summary":{"rows":250,"products":84,"stores":19,"tick":42}}
{"type":"signal","at":"<iso>","signal":{... §5 shape ...}}
{"type":"heartbeat","at":"<iso>"}
{"type":"error","at":"<iso>","detail":"..."}
```
Client -> server frames:
```json
{"type":"ping"}                                  -> {"type":"pong","at":"..."}
{"type":"subscribe","patterns":["DEMAND_SURGE"],"severities":["warn","critical"]}
```
`subscribe` filters subsequent `signal` frames per connection (empty array = all).
Heartbeat every 20 s; drop dead sockets. New clients MUST receive `hello` before anything else.
`api` runs the rule engine for the affected products on each tick and emits `signal` frames for
newly fired patterns.

CORS: allow `LAYA_ORIGINS` (`*` in dev).

---

## 7. `web` (SvelteKit + adapter-node, port 3000)

Client-rendered (`src/routes/+layout.ts`: `export const ssr = false`).
All traffic goes through Caddy: `/api/*` -> `api:8000`, everything else -> `web:3000`.
WebSocket URL derived from `location`: `${proto==='https:'?'wss':'ws'}://${location.host}/api/stream`.

Single page (`/`) with:
- **status bar** — dataset size, day range, live WS state, tick counter, rollup lag.
- **search panel** — free text + category / brand / store / format / date range / price band /
  promo toggles. Debounced 250 ms. Drives `GET /api/search`.
- **result table** — paginated rows, sortable by revenue / units / price / margin, totals footer
  from `totals`, promo badge, margin colour. Row click opens product detail.
- **signal feed** — live `signal` frames prepended, severity colouring, `action` text verbatim,
  pattern filter chips, "mark seen" -> `POST /api/signals/seen`. Seeded from `GET /api/signals`.
- **overview panel** — `GET /api/overview`: day bars for units/revenue, category table with
  margin + private-label share, top movers list.
- **product detail** — `GET /api/products/{id}/series` inline SVG sparkline (no chart library).

Realtime behaviour: ticks update a live activity strip and increment counters without
refetching; signal frames prepend to the feed and bump the severity counters.

Requirement: `npm run build` must succeed offline in the Docker image (`npm ci` against the
committed lockfile). No chart libraries, no CSS frameworks — hand-written CSS with
`prefers-color-scheme` support.

---

## 8. Container wiring

`docker compose up --build` brings up `db -> generator (exit) -> api/sim/web -> caddy`.
Edge: `http://localhost:${LAYA_HTTP_PORT:-8090}`.
Compose healthchecks (`db`, `api`, `web`) and `service_completed_successfully` for `generator`
are already declared — services must expose the endpoints those checks hit.

Python images: `python:3.12-slim`, deps pinned in `requirements.txt`, non-root user.
Web image: multi-stage `node:22-alpine`; runtime runs `node build` with `PORT=3000 HOST=0.0.0.0`.

Pinned Python deps: `psycopg[binary]==3.2.3`, `fastapi==0.115.6`, `uvicorn[standard]==0.34.0`,
`pydantic==2.10.4`, `numpy==2.2.1` (generator only).
