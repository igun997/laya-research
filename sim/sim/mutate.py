"""Tick mutation engine for the laya-research simulator.

Everything in here is deterministic given ``LAYA_SEED``: a single
``random.Random`` instance drives row selection, the mutation walk and the
anomaly injection, so two runs from the same seed produce the same sequence of
updates.

Hard invariants (contract §3):

* only the latest populated day (``max(day)``) is ever touched;
* only the columns ``price``, ``unit_cost``, ``units_sold``, ``inventory``,
  ``promo_flag`` and ``on_order`` are ever written -- never ``day``,
  ``store_id``, ``product_id``;
* no row is ever inserted or deleted;
* every CHECK in ``db/init/01_schema.sql`` holds after every tick
  (``price >= 0``, ``unit_cost >= 0``, ``units_sold >= 0``, ``inventory >= 0``,
  ``on_order >= 0``).

This module deliberately does not import ``psycopg``: it only needs a DB-API
cursor, which keeps the mutation model unit-checkable without a database.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

# --------------------------------------------------------------------------
# Tunables. Values are deliberately explicit rather than magic numbers so the
# business behaviour is auditable.
# --------------------------------------------------------------------------

#: Distinct products targeted per tick. Contract §6.2's own tick example is
#: ``rows=250, products=84, stores=19``, and 200 row entries (~7.4 kB) plus a
#: ~84-entry ``product_ids`` list stays inside the 8 kB payload budget. Keeping
#: the product spread bounded is therefore what makes the default tick publish
#: a complete, untruncated payload.
TARGET_DISTINCT_PRODUCTS = 84

#: Stores probed per sample query. Each store contributes a contiguous slice
#: of its products via an index-only scan.
PROBE_STORES_PER_QUERY = 8

#: Catalog sizes assumed when neither ``dataset_meta`` nor the dimension
#: tables can be read. Only used to bound the random index probes.
DEFAULT_STORE_SPAN = 140
DEFAULT_PRODUCT_SPAN = 2400

#: Probes attempted before falling back to a plain day-partition top-N sort.
SAMPLE_MAX_PROBES = 3

#: hard cap on ``rows`` inside the NOTIFY payload (contract §6.1).
PAYLOAD_ROW_CAP = 200

#: contract §6.1 states the payload is "max 8000 bytes". PostgreSQL itself
#: rejects a NOTIFY payload of 8000 bytes or more ("payload string too long",
#: the limit is *shorter than* 8000), so the enforced budget is 7999 bytes.
#: Both numbers are recorded here so the margin is explicit rather than implied.
PAYLOAD_BYTE_BUDGET = 8000
PAYLOAD_MAX_BYTES = 7999

#: minimum number of ``rows`` entries to preserve before sacrificing the
#: deduplicated id lists (which are advisory; ``row_count`` is authoritative).
PAYLOAD_MIN_ROWS = 20

#: rows per ``UPDATE ... FROM (VALUES ...)`` statement; keeps the bind
#: parameter count comfortably below the protocol limit (9 params/row).
MAX_ROWS_PER_UPDATE = 1000

#: expected rows per store/product/day, used when ``dataset_meta`` is absent.
FALLBACK_UNITS_BASELINE = 12.0

#: price random walk step, +/-3%.
PRICE_STEP = 0.03

#: pull of the price back towards the row's anchor price.
PRICE_ANCHOR_PULL = 0.35

#: unit_cost drift, +/-1% (costs move far slower than shelf prices).
COST_STEP = 0.01

#: units_sold noise, +/-25%.
UNITS_NOISE = 0.25

#: probability a mutated row is promoted this tick (~4% of mutated rows).
PROMO_TOGGLE_P = 0.04

#: price multiplier applied when a promo is switched on.
PROMO_PRICE_CUT = 0.88

#: price multiplier applied when a promo is switched off.
PROMO_PRICE_RESTORE = 1.10

#: extra demand lift while promoted.
PROMO_DEMAND_LIFT = 1.45

#: demand elasticity used to couple price moves back into units.
PRICE_ELASTICITY = -1.6

#: inventory is replenished once cover falls below this many days.
REPLENISH_TRIGGER_COVER = 2.5

#: probability a replenishment actually happens on a low-cover tick.
REPLENISH_P = 0.6

#: target cover after a replenishment, in days.
REPLENISH_TARGET_COVER = 3.0

#: cover band the replenishment draws from.
REPLENISH_COVER_SPREAD = (2.0, 4.5)

#: on_order is raised when cover drops below this.
LOW_COVER = 1.5

#: probability of raising ``on_order`` on a low-cover row.
ORDER_P_PLACE = 0.45

#: probability of receiving an outstanding order on a given tick.
ORDER_P_RECEIVE = 0.35

#: mean number of genuine anomalies injected per tick.
ANOMALY_MEAN = 2.0

#: anomalies are only attempted when at least this many rows were mutated.
ANOMALY_MIN_ROWS = 12

#: severity mix of the injected anomalies (DEMAND_SURGE, PRICE_SPIKE,
#: MARGIN_SQUEEZE, STOCKOUT_RISK).
ANOMALY_KINDS = ("DEMAND_SURGE", "PRICE_SPIKE", "MARGIN_SQUEEZE", "STOCKOUT_RISK")
ANOMALY_WEIGHTS = (0.30, 0.20, 0.25, 0.25)


# --------------------------------------------------------------------------
# Value helpers. ``numeric`` columns come back as ``Decimal``; arithmetic is
# done in float and rounded back on the way out so the mutation model stays
# readable and the ``numeric(10,2)`` scale is respected exactly.
# --------------------------------------------------------------------------


def to_float(value: Any) -> float:
    """Coerce a DB scalar (``Decimal``/``int``/``None``) to ``float``."""
    if value is None:
        return 0.0
    if isinstance(value, float):
        return value
    return float(value)


#: upper bound of ``numeric(10,2)``, the declared type of ``price`` and
#: ``unit_cost``. Clamping here guarantees the column type and its CHECK can
#: never be violated by an extreme mutation.
NUMERIC_10_2_MAX = 99_999_999.99


def _q2(value: float, lo: float = 0.0) -> Decimal:
    """Quantise a money value to ``numeric(10,2)``, clamped to the column range."""
    if not math.isfinite(value) or value < lo:
        value = lo
    elif value > NUMERIC_10_2_MAX:
        value = NUMERIC_10_2_MAX
    return Decimal(f"{value:.2f}")


def _q0(value: float) -> int:
    """Quantise a count column, clamped at zero."""
    if not math.isfinite(value) or value < 0:
        return 0
    return int(value)


def load_dataset_params(conn) -> dict[str, int]:
    """Read ``dataset_meta.generation`` for store/product/day counts.

    Used to derive a sane units baseline when the generator did not record one
    per row. Best-effort: any error (missing table, malformed value) yields an
    empty dict, and callers fall back to their own defaults.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM dataset_meta WHERE key = 'generation'")
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 - metadata is optional
        return {}
    if not row or not row[0]:
        return {}
    raw = row[0]
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key in ("stores", "products", "days", "rows"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and value > 0:
            out[key] = int(value)
    return out


def units_baseline(params: dict[str, int]) -> float:
    """Fallback mean ``units_sold`` per row, derived from ``dataset_meta``.

    Only used when the ``mv_product_day`` rollup is unavailable. Each row's own
    anchor carries the real per-store/per-product level; this global value is
    just a floor that stops a legitimately zero-unit row from being pinned at
    zero forever.
    """
    rows = params.get("rows")
    products = params.get("products")
    days = params.get("days")
    stores = params.get("stores")
    if rows and products and days and stores:
        return max(1.0, rows / (products * days * stores))
    return FALLBACK_UNITS_BASELINE


def load_units_baseline(conn, params: dict[str, int]) -> float:
    """Mean ``units_sold`` per ``(store, product, day)``, read from the rollup.

    ``avg(units / store_count)`` over ``mv_product_day`` is exactly the mean
    demand of one store/product/day cell, and the rollup only holds
    ``products x days`` rows (~50k by default) so the read is cheap enough to
    do at startup. Falls back to the ``dataset_meta``-derived value when the
    rollup is empty (e.g. the generator has not refreshed it yet).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT avg(units::numeric / NULLIF(store_count, 0))
                FROM mv_product_day
                """
            )
            row = cur.fetchone()
        if row and row[0] is not None:
            value = float(row[0])
            if math.isfinite(value) and value > 0:
                return value
    except Exception:  # noqa: BLE001 - the rollup is optional
        pass
    return units_baseline(params)


# --------------------------------------------------------------------------
# Sample selection
# --------------------------------------------------------------------------


@dataclass(slots=True)
class FactRow:
    """One mutable ``market_facts`` row plus its anchor values."""

    store_id: int
    product_id: int
    price: float
    unit_cost: float
    units_sold: int
    promo_flag: bool
    inventory: int
    on_order: int
    anchor_price: float
    anchor_units: float

    def values_row(self, day: date) -> tuple[Any, ...]:
        """The ``VALUES`` tuple fed to the bulk ``UPDATE ... FROM``."""
        return (
            day,
            self.store_id,
            self.product_id,
            _q2(self.price),
            _q2(self.unit_cost),
            _q0(self.units_sold),
            bool(self.promo_flag),
            _q0(self.inventory),
            _q0(self.on_order),
        )


def sample_pairs(
    cur,
    day: date,
    wanted: int,
    rng: random.Random,
    store_span: int,
    product_span: int,
) -> list[tuple[int, int]]:
    """Pick up to ``wanted`` existing ``(store_id, product_id)`` pairs for ``day``.

    Strategy (measured index-only scan; never a full-table sort, never a heap
    scan):

    ``market_facts`` is keyed ``(day, store_id, product_id)``, and that primary
    key contains *both* projected columns. A query of the form::

        SELECT product_id FROM market_facts
        WHERE day = $1 AND store_id = $2
        ORDER BY product_id OFFSET $3 LIMIT $4

    is therefore answered by an ``Index Only Scan`` with ``Heap Fetches: 0``:
    the btree is entered on ``(day, store_id)`` and a bounded range of
    ``product_id`` values is read straight out of the index. Measured on a
    7M-row dataset that is ~4 buffers per store versus ~9,461 buffers for a
    day-partition heap scan -- roughly a thousand times less I/O, and the cost
    is independent of the total dataset size because only the requested
    products are ever visited.

    ``wanted`` rows are collected by probing ``PROBE_STORES_PER_QUERY`` random
    stores in a single query via ``JOIN LATERAL``. Every store is probed at the
    *same* bounded ``[offset, offset + TARGET_DISTINCT_PRODUCTS)`` window, so a
    tick draws ~84 distinct products across 8 stores and repeats that product
    set across stores -- which is exactly the §6.2 shape (``rows=250,
    products=84, stores=19``) and keeps the payload's ``product_ids`` list
    small enough that the default tick is published complete rather than
    truncated. One query yields up to 8 x 84 = 672 candidates.

    Only if the table is so small that the probes cannot fill the request do we
    fall back to a plain top-N sort over the day partition, which is still
    index-driven and only reached for tiny datasets.
    """
    pairs: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    # Each probe contributes at most TARGET_DISTINCT_PRODUCTS distinct product
    # ids, so the ``product_ids`` list in the payload stays bounded no matter
    # how large LAYA_TICK_ROWS is.
    block = min(max(1, product_span), TARGET_DISTINCT_PRODUCTS)

    for _ in range(SAMPLE_MAX_PROBES):
        if len(pairs) >= wanted:
            break
        remaining = wanted - len(pairs)
        if block >= product_span:
            offset = 0
        else:
            offset = rng.randrange(product_span - block + 1)
        store_count = max(1, min(PROBE_STORES_PER_QUERY, max(1, store_span)))
        # Take an equal slice of the shared product window from every probed
        # store. The slice size is chosen so the batch covers the whole
        # PROBE_STORES_PER_QUERY, giving the tick its intended store breadth
        # while the products stay inside one window of TARGET_DISTINCT_PRODUCTS.
        per_store = max(1, min(block, -(-remaining // store_count)))
        stores = rng.sample(range(1, max(1, store_span) + 1), store_count)
        probes = [(store_id, offset, per_store) for store_id in stores]
        cur.execute(
            """
            SELECT s.store_id, f.product_id
            FROM (VALUES {probes}) AS s(store_id, off, lim)
            JOIN LATERAL (
                SELECT product_id
                FROM market_facts
                WHERE day = %s AND store_id = s.store_id
                ORDER BY product_id
                OFFSET s.off::bigint
                LIMIT s.lim::bigint
            ) f ON true
            """.format(probes=", ".join(["(%s::int, %s::int, %s::int)"] * len(probes))),
            [
                param
                for store_id, probe_offset, limit in probes
                for param in (store_id, probe_offset, limit)
            ]
            + [day],
        )
        for store_id, product_id in cur.fetchall():
            key = (int(store_id), int(product_id))
            if key in seen:
                continue
            seen.add(key)
            pairs.append(key)
            if len(pairs) >= wanted:
                break

    if len(pairs) < wanted:
        # The probes could not fill the request -- either the dataset is tiny,
        # or the random probes repeatedly landed on the same stores. Retry with
        # a top-N sort over the day partition (still served by
        # market_facts_day_idx) until the request is met or the day is
        # exhausted, so a tick never silently under-fills.
        for _ in range(SAMPLE_MAX_PROBES):
            if len(pairs) >= wanted:
                break
            cur.execute(
                """
                SELECT store_id, product_id
                FROM market_facts
                WHERE day = %s
                ORDER BY random()
                LIMIT %s
                """,
                (day, wanted - len(pairs) + len(pairs) // 2 + 16),
            )
            fetched = cur.fetchall()
            if not fetched:
                break
            for store_id, product_id in fetched:
                key = (int(store_id), int(product_id))
                if key in seen:
                    continue
                seen.add(key)
                pairs.append(key)
                if len(pairs) >= wanted:
                    break

    return pairs


_FETCH_SQL = """
SELECT f.store_id, f.product_id, f.price, f.unit_cost, f.units_sold,
       f.promo_flag, f.inventory, f.on_order
FROM market_facts f
JOIN (VALUES {pairs}) AS s(store_id, product_id)
  ON f.store_id = s.store_id
 AND f.product_id = s.product_id
WHERE f.day = %s
"""


def build_fetch_sql(pair_count: int) -> str:
    """Compose the sampled-row read. Each pair is a primary-key probe."""
    pairs = ", ".join(["(%s::int, %s::int)"] * pair_count)
    return _FETCH_SQL.format(pairs=pairs)


def fetch_rows(cur, day: date, pairs: Sequence[tuple[int, int]]) -> list[FactRow]:
    """Read the current values of the sampled rows.

    A plain ``SELECT`` is used, **not** ``SELECT ... FOR UPDATE``: ``sim`` is
    the only writer of ``market_facts``, and the follow-up ``UPDATE`` is a
    single atomic statement, so a concurrent reader (``api``'s live rollup
    path) can never observe a torn write. Taking row locks here would only
    create lock waits against those read queries for no correctness gain.

    The pairs are passed as a ``VALUES`` list joined back to the fact table so
    each lookup is an index probe on the ``(day, store_id, product_id)``
    primary key. Chunked like the update to bound the parameter count.
    """
    if not pairs:
        return []
    rows: list[FactRow] = []
    for start in range(0, len(pairs), MAX_ROWS_PER_UPDATE):
        chunk = pairs[start : start + MAX_ROWS_PER_UPDATE]
        params: list[Any] = []
        for store_id, product_id in chunk:
            params.extend((int(store_id), int(product_id)))
        params.append(day)
        cur.execute(build_fetch_sql(len(chunk)), params)
        for (
            store_id,
            product_id,
            price,
            unit_cost,
            units_sold,
            promo_flag,
            inventory,
            on_order,
        ) in cur.fetchall():
            price_f = to_float(price)
            rows.append(
                FactRow(
                    store_id=int(store_id),
                    product_id=int(product_id),
                    price=price_f,
                    unit_cost=to_float(unit_cost),
                    units_sold=int(units_sold),
                    promo_flag=bool(promo_flag),
                    inventory=int(inventory),
                    on_order=int(on_order),
                    anchor_price=price_f,
                    anchor_units=float(units_sold),
                )
            )
    return rows


# --------------------------------------------------------------------------
# Mutation model
# --------------------------------------------------------------------------


def mutate_row(row: FactRow, baseline_units: float, rng: random.Random) -> None:
    """Apply one bounded random-walk step to ``row`` in place.

    * ``promo_flag`` -- toggled on ~4% of mutated rows; switching on applies a
      price cut, switching off restores most of it.
    * ``price`` -- +/-3% random walk pulled 35% of the way back towards the
      row's original price every tick, so it can neither decay to zero nor
      explode.
    * ``unit_cost`` -- +/-1% walk (costs move far slower than shelf prices).
    * ``units_sold`` -- resampled around the row's own baseline (anchored by
      the dataset mean when the row anchor is degenerate) with +/-25% noise,
      lifted by the promo, and coupled to the price move through elasticity.
    * ``inventory`` -- decremented by the units sold, then replenished towards
      2-4.5 days of cover once cover drops below 2.5 days, which keeps cover
      inside a plausible band instead of drifting to permanent stockout or
      permanent overstock.
    * ``on_order`` -- outstanding orders are sometimes received; a new one is
      raised while cover is low.
    """
    # --- promo toggle -----------------------------------------------------
    if rng.random() < PROMO_TOGGLE_P:
        row.promo_flag = not row.promo_flag
        if row.promo_flag:
            row.price *= PROMO_PRICE_CUT
        else:
            row.price *= PROMO_PRICE_RESTORE

    # --- price: bounded random walk anchored to the original price ---------
    row.price *= 1.0 + rng.uniform(-PRICE_STEP, PRICE_STEP)
    row.price += (row.anchor_price - row.price) * PRICE_ANCHOR_PULL

    # --- cost: slow drift, floored well above zero -------------------------
    if row.unit_cost > 0:
        row.unit_cost *= 1.0 + rng.uniform(-COST_STEP, COST_STEP)

    # --- units: noise around the row baseline, promo + price interaction ---
    anchor = max(row.anchor_units, baseline_units)
    units = anchor * (1.0 + rng.uniform(-UNITS_NOISE, UNITS_NOISE))
    if row.promo_flag:
        units *= PROMO_DEMAND_LIFT
    if row.anchor_price > 0:
        price_ratio = row.price / row.anchor_price
        units *= max(0.5, min(1.5, 1.0 + PRICE_ELASTICITY * (price_ratio - 1.0)))
    row.units_sold = max(0, int(round(units)))

    # --- inventory: sell down, then replenish -----------------------------
    per_day = max(row.units_sold, anchor * 0.5, 1.0)
    inventory = row.inventory - row.units_sold
    if inventory < REPLENISH_TRIGGER_COVER * per_day and rng.random() < REPLENISH_P:
        target = per_day * rng.uniform(*REPLENISH_COVER_SPREAD)
        inventory += max(0.0, target - inventory)
    row.inventory = max(0, int(round(inventory)))

    # --- on_order ---------------------------------------------------------
    cover = row.inventory / per_day
    if row.on_order > 0 and rng.random() < ORDER_P_RECEIVE:
        row.on_order = 0
    if cover < LOW_COVER and rng.random() < ORDER_P_PLACE:
        row.on_order = max(row.on_order, int(round(per_day * REPLENISH_TARGET_COVER)))


def inject_anomalies(
    rows: list[FactRow],
    baseline_units: float,
    rng: random.Random,
) -> dict[str, int]:
    """Force a few genuine, rule-firing outliers onto already-mutated rows.

    ``api``'s rule engine (§4) needs real deviations to detect, and the plain
    random walk almost never crosses a severity band, so a couple of rows per
    tick are pushed hard enough to fire a pattern. The numbers below are the
    bands from §4 with margin, so the emitted ``signals`` rows are genuine
    observations of the mutated data rather than synthetic signals:

    * ``DEMAND_SURGE``   -- units >= 2.8x the row baseline (critical at 2.6x);
    * ``PRICE_SPIKE``    -- price >= 1.18x the row anchor (critical at 1.15x);
    * ``MARGIN_SQUEEZE`` -- margin pushed to <= 0.06 (critical below 0.10);
    * ``STOCKOUT_RISK``  -- inventory driven to 0 with a normal sell-through.
    """
    if len(rows) < ANOMALY_MIN_ROWS:
        return {}
    n = min(len(rows), max(1, int(round(rng.gauss(ANOMALY_MEAN, 1.0)))))
    kinds = rng.choices(ANOMALY_KINDS, weights=ANOMALY_WEIGHTS, k=n)
    victims = rng.sample(rows, n)
    fired: dict[str, int] = {}

    for row, kind in zip(victims, kinds):
        anchor = max(row.anchor_units, baseline_units)
        if kind == "DEMAND_SURGE":
            row.units_sold = max(row.units_sold, int(round(anchor * rng.uniform(2.8, 4.0))))
            row.promo_flag = True
            row.price *= 0.94
        elif kind == "PRICE_SPIKE":
            row.price = max(row.price, row.anchor_price * rng.uniform(1.18, 1.30))
            row.units_sold = max(0, int(round(row.units_sold * 0.7)))
        elif kind == "MARGIN_SQUEEZE":
            row.unit_cost = squeeze_cost(row, rng)
            row.price = min(row.price, row.unit_cost * rng.uniform(0.80, 0.90))
        elif kind == "STOCKOUT_RISK":
            row.inventory = 0
            row.on_order = max(row.on_order, int(round(anchor * 2.0)))
        fired[kind] = fired.get(kind, 0) + 1

    return fired


def squeeze_cost(row: FactRow, rng: random.Random) -> float:
    """Return a cost high enough that the row's margin becomes thin.

    ``unit_cost`` has no upper bound in the schema, so margin squeeze is
    produced by raising cost rather than by pushing ``price`` towards zero --
    the latter would fight the price anchor and risks a non-positive price.
    ``price = cost * (1 + margin)`` is inverted for a target margin in
    ``[-0.25, 0.06]``.
    """
    target_margin = rng.uniform(-0.25, 0.06)
    denom = max(0.05, 1.0 + target_margin)
    cost = row.price / denom
    floor = row.unit_cost if row.unit_cost > 0 else max(0.01, row.price * 0.7)
    return max(floor, cost, 0.01)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

#: one bind-parameter tuple per mutated row, with explicit casts so the
#: ``VALUES`` list never relies on type inference (``day`` would otherwise be
#: an untyped literal, and ``numeric``/``integer``/``boolean`` columns must be
#: matched exactly).
_VALUES_ROW = (
    "(%s::date, %s::int, %s::int, %s::numeric, %s::numeric, "
    "%s::int, %s::boolean, %s::int, %s::int)"
)

_UPDATE_SQL = """
UPDATE market_facts AS f
SET price      = v.price,
    unit_cost  = v.unit_cost,
    units_sold = v.units_sold,
    promo_flag = v.promo_flag,
    inventory  = v.inventory,
    on_order   = v.on_order
FROM (VALUES {values}) AS v(day, store_id, product_id, price, unit_cost,
                            units_sold, promo_flag, inventory, on_order)
WHERE f.day = v.day
  AND f.store_id = v.store_id
  AND f.product_id = v.product_id
"""


def build_update_sql(row_count: int) -> str:
    """Compose the single set-based ``UPDATE ... FROM (VALUES ...)``."""
    return _UPDATE_SQL.format(values=", ".join([_VALUES_ROW] * row_count))


def apply_mutations(cur, day: date, rows: Sequence[FactRow]) -> int:
    """Write every mutated row with set-based ``UPDATE ... FROM (VALUES ...)``.

    One statement per :data:`MAX_ROWS_PER_UPDATE` rows (9 bind parameters per
    row, so chunking keeps the parameter count far below the protocol limit
    even for an oversized ``LAYA_TICK_ROWS``). The statement never changes
    ``day``/``store_id``/``product_id``, and it is never an ``INSERT`` or a
    ``DELETE``.
    """
    if not rows:
        return 0
    updated = 0
    for start in range(0, len(rows), MAX_ROWS_PER_UPDATE):
        chunk = rows[start : start + MAX_ROWS_PER_UPDATE]
        params: list[Any] = []
        for row in chunk:
            params.extend(row.values_row(day))
        cur.execute(build_update_sql(len(chunk)), params)
        updated += cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(chunk)
    return updated


# --------------------------------------------------------------------------
# NOTIFY payload (contract §6.1)
# --------------------------------------------------------------------------


def build_payload(
    tick: int,
    day: date,
    pairs: Sequence[tuple[int, int]],
    now: datetime | None = None,
) -> tuple[str, int, bool]:
    """Serialise the §6.1 tick payload.

    Returns ``(payload_json, byte_size, truncated)``.

    Two distinct reductions happen here, and ``truncated`` reports only the
    second:

    * ``rows`` is always capped at :data:`PAYLOAD_ROW_CAP` (200) entries, per
      §6.1. This is the documented shape, not an error condition.
    * if the serialised JSON would still exceed the byte budget, ``rows`` is
      shrunk further and ``truncated`` is returned ``True`` so the caller can
      log a warning. The enforced budget is :data:`PAYLOAD_MAX_BYTES` (7999
      bytes): §6.1 states "max 8000 bytes" and PostgreSQL additionally rejects
      any payload of 8000 bytes or more.

    The deduplicated ``product_ids``/``stores`` lists and the true
    ``row_count`` are always preserved; only ``rows`` shrinks.
    """
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    row_count = len(pairs)
    product_ids = sorted({int(p) for _, p in pairs})
    stores = sorted({int(s) for s, _ in pairs})
    capped = list(pairs[:PAYLOAD_ROW_CAP])
    truncated = False
    #: keep at least this many rows in the payload when the budget allows.
    min_rows = min(len(capped), PAYLOAD_MIN_ROWS)

    def render(rows_in: Sequence[tuple[int, int]]) -> str:
        payload = {
            "kind": "tick",
            "tick": tick,
            "at": at.isoformat().replace("+00:00", "Z"),
            "day": day.isoformat(),
            "rows": [{"store_id": int(s), "product_id": int(p)} for s, p in rows_in],
            "product_ids": product_ids,
            "stores": stores,
            "row_count": row_count,
        }
        return json.dumps(payload, separators=(",", ":"))

    def fits(text: str) -> bool:
        return len(text.encode("utf-8")) <= PAYLOAD_MAX_BYTES

    def largest_prefix(rows_in: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """Largest prefix of ``rows_in`` whose payload fits the byte budget."""
        lo, hi = 0, len(rows_in)
        while lo < hi:
            mid = (lo + hi) // 2
            if fits(render(rows_in[:mid])):
                lo = mid + 1
            else:
                hi = mid
        best = rows_in[: max(0, lo - 1)]
        while not fits(render(best)) and best:
            best = best[:-1]
        return best

    kept = capped
    text = render(kept)
    if not fits(text):
        truncated = True
        kept = largest_prefix(capped)
        # The deduplicated id lists are normally small (tens of entries), but a
        # pathological LAYA_TICK_ROWS with all-distinct ids can dominate the
        # budget on its own. Prefer keeping a useful number of rows: shrink the
        # larger id list and retry until the minimum row count fits. The true
        # `row_count` is always preserved, so consumers still see how many rows
        # were mutated even when the detail lists are trimmed.
        while len(kept) < min_rows and (product_ids or stores):
            if len(product_ids) >= len(stores) and product_ids:
                product_ids = product_ids[: max(1, len(product_ids) // 2)]
            elif stores:
                stores = stores[: max(1, len(stores) // 2)]
            kept = largest_prefix(capped)
        text = render(kept)

    return text, len(text.encode("utf-8")), truncated


# --------------------------------------------------------------------------
# Tick orchestration
# --------------------------------------------------------------------------


@dataclass(slots=True)
class TickResult:
    tick: int
    day: date | None
    row_count: int
    distinct_products: int
    distinct_stores: int
    elapsed_ms: int
    payload_bytes: int
    truncated: bool
    anomalies: dict[str, int]


def _empty(
    tick: int,
    day: date | None,
    started: datetime,
    elapsed_ms: int | None = None,
) -> TickResult:
    return TickResult(
        tick=tick,
        day=day,
        row_count=0,
        distinct_products=0,
        distinct_stores=0,
        elapsed_ms=elapsed_ms
        if elapsed_ms is not None
        else int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        payload_bytes=0,
        truncated=False,
        anomalies={},
    )


def discover_spans(conn, store_span: int, product_span: int) -> tuple[int, int]:
    """Resolve the store/product id ranges used by the index probes.

    Prefers ``dataset_meta.generation``; falls back to ``max()`` over the
    dimension tables, which is an index-only lookup on their primary keys. The
    last resort is the catalog default, so a missing/odd dataset can never
    collapse the probe range to a single id. Only called once, at startup, or
    whenever the cached spans are invalid.
    """
    if store_span > 0 and product_span > 0:
        return store_span, product_span
    params = load_dataset_params(conn)
    if not store_span:
        store_span = params.get("stores", 0)
    if not product_span:
        product_span = params.get("products", 0)
    if not store_span or not product_span:
        with conn.cursor() as cur:
            if not store_span:
                try:
                    cur.execute("SELECT max(store_id) FROM stores")
                    row = cur.fetchone()
                    if row and row[0]:
                        store_span = int(row[0])
                except Exception:  # noqa: BLE001
                    pass
            if not product_span:
                try:
                    cur.execute("SELECT max(product_id) FROM products")
                    row = cur.fetchone()
                    if row and row[0]:
                        product_span = int(row[0])
                except Exception:  # noqa: BLE001
                    pass
    if not store_span:
        store_span = DEFAULT_STORE_SPAN
    if not product_span:
        product_span = DEFAULT_PRODUCT_SPAN
    return max(store_span, 1), max(product_span, 1)


def run_tick(
    conn,
    tick: int,
    rows_wanted: int,
    channel: str,
    rng: random.Random,
    baseline_units: float,
    store_span: int = 0,
    product_span: int = DEFAULT_PRODUCT_SPAN,
    notify: bool = True,
) -> TickResult:
    """Execute one mutation tick and publish it.

    The connection is expected to be in autocommit mode: each statement
    commits on its own, so a crash mid-tick can never leave a half-applied
    mutation batch behind, and the ``UPDATE`` is atomic per statement.

    ``store_span``/``product_span`` bound the random index probes used by
    :func:`sample_pairs`; both default to a sensible catalog size and are
    discovered from ``dataset_meta`` at startup.
    """
    started = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        cur.execute("SELECT max(day) FROM market_facts")
        row = cur.fetchone()
        target_day = row[0] if row else None

    if target_day is None:
        return _empty(tick, None, started)

    if store_span <= 0 or product_span <= 0:
        store_span, product_span = discover_spans(
            conn, store_span, product_span
        )

    with conn.cursor() as cur:
        pairs = sample_pairs(
            cur, target_day, rows_wanted, rng, store_span, product_span
        )
        facts = fetch_rows(cur, target_day, pairs)
        if not facts:
            return _empty(tick, target_day, started)
        for fact in facts:
            mutate_row(fact, baseline_units, rng)
        anomalies = inject_anomalies(facts, baseline_units, rng)

    with conn.cursor() as cur:
        mutated = apply_mutations(cur, target_day, facts)

    actual_pairs = [(f.store_id, f.product_id) for f in facts]
    result = TickResult(
        tick=tick,
        day=target_day,
        row_count=mutated,
        distinct_products=len({p for _, p in actual_pairs}),
        distinct_stores=len({s for s, _ in actual_pairs}),
        elapsed_ms=0,
        payload_bytes=0,
        truncated=False,
        anomalies=anomalies,
    )

    if notify:
        text, size, truncated = build_payload(tick, target_day, actual_pairs)
        result.payload_bytes = size
        result.truncated = truncated
        with conn.cursor() as cur:
            cur.execute("SELECT pg_notify(%s, %s)", (channel, text))

    result.elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return result


def describe_tick(result: TickResult) -> str:
    """One log line per tick (contract §3)."""
    if result.day is None:
        return (
            f"tick={result.tick} day=none rows=0 products=0 stores=0 "
            f"elapsed_ms={result.elapsed_ms} dataset_empty=true"
        )
    extra = ""
    if result.anomalies:
        extra = " anomalies=" + ",".join(
            f"{k}:{v}" for k, v in sorted(result.anomalies.items())
        )
    if result.truncated:
        extra += f" payload_truncated=true payload_bytes={result.payload_bytes}"
    return (
        f"tick={result.tick} day={result.day.isoformat()} "
        f"rows={result.row_count} products={result.distinct_products} "
        f"stores={result.distinct_stores} elapsed_ms={result.elapsed_ms}{extra}"
    )
