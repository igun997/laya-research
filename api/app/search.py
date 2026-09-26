"""Search endpoints: catalog lookup, fact-grain datasheet search, series.

Two rules hold for every statement in this module:

* parameters are always bound with psycopg placeholders (``%s`` / ``%(name)s``) —
  no user input ever reaches a string-formatted statement;
* ``sort`` is resolved through :data:`SORT_SQL`, an allowlist dict, so an unknown
  value falls back to the default instead of reaching the database.

The only f-strings in SQL interpolate module-level *constants* (column lists and
static WHERE fragments); every value is bound.

Literal ``%`` characters never appear in a statement: LIKE patterns are built in
Python and bound as parameters, and the ``pg_trgm`` similarity operator is spelled
``similarity(...) >= threshold`` because psycopg uses ``%`` as its escape character.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from . import rules
from .db import pool
from .schemas import CategorySeriesOut, ProductPage, ProductSeriesOut, SearchPage

log = logging.getLogger("laya.api.search")

router = APIRouter()

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
PRODUCTS_DEFAULT_LIMIT = 25
SERIES_MAX_DAYS = 180

# similarity() below this is noise for realistic partial input against long
# product names; `word_similarity` is the last resort for a single word.
TRIGRAM_THRESHOLD = 0.3
WORD_TRIGRAM_THRESHOLD = 0.4

# Allowlisted sort keys -> ORDER BY fragment. Never interpolate user input.
SORT_SQL: dict[str, str] = {
    "revenue": "revenue DESC, day DESC, store_id ASC, product_id ASC",
    "units": "units_sold DESC, day DESC, store_id ASC, product_id ASC",
    "price": "price DESC, day DESC, store_id ASC, product_id ASC",
    "margin": "margin_pct DESC, day DESC, store_id ASC, product_id ASC",
    "day": "day DESC, store_id ASC, product_id ASC",
}
DEFAULT_SORT = "day"

LIKE_ESCAPE = "ESCAPE '\\'"


def _like_pattern(value: str) -> str:
    """Escape LIKE metacharacters so a user query is matched literally."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def _tsquery_is_empty(cur: Any, q: str) -> bool:
    """True when ``q`` yields an empty tsquery (blank or punctuation-only input).

    Postgres does not raise for ``'!!!'``; it emits a NOTICE and returns an empty
    tsquery, and ``search_tsv @@ <empty>`` matches nothing. So emptiness — not a
    zero row count — is what routes us to the trigram fallback.
    """
    await cur.execute("SELECT (websearch_to_tsquery('english', %s) = ''::tsquery) AS empty", (q,))
    row = await cur.fetchone()
    return bool(row and row["empty"])


# ---------------------------------------------------------------------------
# /api/products
# ---------------------------------------------------------------------------

_PRODUCT_COLUMNS = """
    p.product_id,
    p.sku,
    p.name,
    p.brand,
    p.category,
    p.subcategory,
    p.uom,
    p.pack_size,
    p.is_private_label,
    p.is_perishable,
    p.list_price,
    l.day       AS latest_day,
    l.units     AS latest_units,
    l.avg_price AS latest_price,
    CASE WHEN l.revenue > 0 THEN (l.revenue - l.cogs) / l.revenue ELSE 0 END AS latest_margin_pct
"""

_PRODUCT_FROM = """
FROM products p
LEFT JOIN LATERAL (
    SELECT m.day, m.units, m.avg_price, m.revenue, m.cogs
    FROM mv_product_day m
    WHERE m.product_id = p.product_id
    ORDER BY m.day DESC
    LIMIT 1
) l ON true
"""


def _product_filters(
    category: str | None,
    brand: str | None,
    private_label: bool | None,
    perishable: bool | None,
) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if category:
        clauses.append("p.category = %(category)s")
        params["category"] = category
    if brand:
        clauses.append("p.brand = %(brand)s")
        params["brand"] = brand
    if private_label is not None:
        clauses.append("p.is_private_label = %(private_label)s")
        params["private_label"] = private_label
    if perishable is not None:
        clauses.append("p.is_perishable = %(perishable)s")
        params["perishable"] = perishable
    return clauses, params


def _where(clauses: list[str]) -> str:
    return (" WHERE " + " AND ".join(clauses)) if clauses else ""


def _product_row(row: Any) -> dict[str, Any]:
    return {
        "product_id": int(row["product_id"]),
        "sku": row["sku"],
        "name": row["name"],
        "brand": row["brand"],
        "category": row["category"],
        "subcategory": row["subcategory"],
        "uom": row["uom"],
        "pack_size": float(row["pack_size"]),
        "is_private_label": bool(row["is_private_label"]),
        "is_perishable": bool(row["is_perishable"]),
        "list_price": float(row["list_price"]),
        "latest_day": row["latest_day"],
        "latest_units": None if row["latest_units"] is None else int(row["latest_units"]),
        "latest_price": None if row["latest_price"] is None else float(row["latest_price"]),
        "latest_margin_pct": None if row["latest_margin_pct"] is None else float(row["latest_margin_pct"]),
    }


async def _product_page(
    cur: Any,
    clauses: list[str],
    params: dict[str, Any],
    order: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    where = _where(clauses)
    await cur.execute(f"SELECT count(*) AS total FROM products p{where}", params)  # noqa: S608
    total = int((await cur.fetchone() or {"total": 0})["total"])
    items: list[dict[str, Any]] = []
    if total:
        await cur.execute(
            f"SELECT {_PRODUCT_COLUMNS} {_PRODUCT_FROM}{where} ORDER BY {order} "  # noqa: S608
            "LIMIT %(limit)s OFFSET %(offset)s",
            params,
        )
        items = [_product_row(row) for row in await cur.fetchall()]
        await _refresh_latest_live(cur, items)
    return {"total": total, "limit": limit, "offset": offset, "items": items}


async def _refresh_latest_live(cur: Any, items: list[dict[str, Any]]) -> None:
    """Overwrite the page's ``latest_*`` with the live §4 rollup for the newest day.

    ``sim`` mutates only the target day, so the materialised row for it can be up
    to one refresh interval stale. The list is small (one page), so one extra
    index-friendly aggregate keeps it consistent with ``/series``.
    """
    if not items:
        return
    target = await _latest_day(cur)
    if target is None:
        return
    page_ids = [item["product_id"] for item in items if item["latest_day"] == target]
    if not page_ids:
        return
    live = await rules.live_product_day(cur, target, page_ids)
    for item in items:
        row = live.get(item["product_id"])
        if row is None:
            continue
        item["latest_units"] = row.units
        item["latest_price"] = round(row.avg_price, 4)
        item["latest_margin_pct"] = round(row.margin, 6)


@router.get("/products", response_model=ProductPage)
async def list_products(
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    brand: str | None = Query(default=None),
    private_label: bool | None = Query(default=None),
    perishable: bool | None = Query(default=None),
    limit: int = Query(default=PRODUCTS_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Catalog search: full-text first, trigram fallback. Never 5xx on odd input."""
    term = (q or "").strip()
    clauses, params = _product_filters(category, brand, private_label, perishable)
    params.update({"limit": limit, "offset": offset})

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if not term:
                # No query text: a plain filtered listing by name.
                return await _product_page(cur, clauses, params, "p.name ASC", limit, offset)

            if not await _tsquery_is_empty(cur, term):
                text_params = dict(params, q=term)
                page = await _product_page(
                    cur,
                    clauses + ["p.search_tsv @@ websearch_to_tsquery('english', %(q)s)"],
                    text_params,
                    "ts_rank(p.search_tsv, websearch_to_tsquery('english', %(q)s)) DESC, p.name ASC",
                    limit,
                    offset,
                )
                if page["total"]:
                    return page

            # --- trigram fallback: ILIKE OR-ed in, ordered by similarity -----
            fb_params = dict(params, q=term, like=_like_pattern(term), thr=TRIGRAM_THRESHOLD)
            fb_clauses = clauses + [
                f"(similarity(p.name, %(q)s) >= %(thr)s OR p.name ILIKE %(like)s {LIKE_ESCAPE})"
            ]
            page = await _product_page(
                cur, fb_clauses, fb_params, "similarity(p.name, %(q)s) DESC, p.name ASC", limit, offset
            )
            if page["total"]:
                return page

            # --- last resort: best matching word inside the name -------------
            ws_params = dict(params, q=term, wthr=WORD_TRIGRAM_THRESHOLD)
            ws_clauses = clauses + ["word_similarity(%(q)s, p.name) > %(wthr)s"]
            return await _product_page(
                cur,
                ws_clauses,
                ws_params,
                "word_similarity(%(q)s, p.name) DESC, p.name ASC",
                limit,
                offset,
            )


# ---------------------------------------------------------------------------
# /api/search  (fact grain)
# ---------------------------------------------------------------------------

_FACT_SELECT = """
SELECT
    f.day,
    f.store_id,
    s.name  AS store_name,
    s.region,
    s.format,
    f.product_id,
    p.sku,
    p.name  AS product,
    p.brand,
    p.category,
    f.price,
    f.unit_cost,
    f.units_sold,
    (f.units_sold * f.price)                                                AS revenue,
    CASE WHEN f.price > 0 THEN (f.price - f.unit_cost) / f.price ELSE 0 END AS margin_pct,
    f.promo_flag,
    f.inventory,
    f.on_order
FROM market_facts f
JOIN stores   s ON s.store_id   = f.store_id
JOIN products p ON p.product_id = f.product_id
"""

_FACT_TOTALS = """
SELECT
    coalesce(sum(f.units_sold * f.price), 0)  AS revenue,
    coalesce(sum(f.units_sold), 0)            AS units,
    CASE
        WHEN coalesce(sum(f.units_sold * f.price), 0) > 0
        THEN (sum(f.units_sold * f.price) - sum(f.units_sold * f.unit_cost))
             / sum(f.units_sold * f.price)
        ELSE 0
    END                                       AS margin_pct,
    count(*)                                  AS rows
FROM market_facts f
JOIN stores   s ON s.store_id   = f.store_id
JOIN products p ON p.product_id = f.product_id
"""


def _fact_row(row: Any) -> dict[str, Any]:
    return {
        "day": row["day"],
        "store_id": int(row["store_id"]),
        "store_name": row["store_name"],
        "region": row["region"],
        "format": row["format"],
        "product_id": int(row["product_id"]),
        "sku": row["sku"],
        "product": row["product"],
        "brand": row["brand"],
        "category": row["category"],
        "price": float(row["price"]),
        "unit_cost": float(row["unit_cost"]),
        "units_sold": int(row["units_sold"]),
        "revenue": round(float(row["revenue"]), 4),
        "margin_pct": round(float(row["margin_pct"]), 6),
        "promo_flag": bool(row["promo_flag"]),
        "inventory": int(row["inventory"]),
        "on_order": int(row["on_order"]),
    }


async def _fact_page(
    cur: Any,
    clauses: list[str],
    params: dict[str, Any],
    order: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Page + totals over the *whole* filtered set (two independent aggregates)."""
    where = _where(clauses)
    await cur.execute(
        f"{_FACT_SELECT}{where} ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s",  # noqa: S608
        params,
    )
    items = [_fact_row(row) for row in await cur.fetchall()]
    await cur.execute(f"{_FACT_TOTALS}{where}", params)  # noqa: S608 - static SQL
    totals = await cur.fetchone() or {}
    rows = int(totals.get("rows") or 0)
    return {
        "total": rows,
        "limit": limit,
        "offset": offset,
        "items": items,
        "totals": {
            "revenue": round(float(totals.get("revenue") or 0.0), 4),
            "units": int(totals.get("units") or 0),
            "margin_pct": round(float(totals.get("margin_pct") or 0.0), 6),
            "rows": rows,
        },
    }


@router.get("/search", response_model=SearchPage)
async def search_facts(
    q: str | None = Query(default=None),
    product_id: int | None = Query(default=None),
    store_id: int | None = Query(default=None),
    category: str | None = Query(default=None),
    brand: str | None = Query(default=None),
    format: str | None = Query(default=None),
    region: str | None = Query(default=None),
    promo: bool | None = Query(default=None),
    min_price: float | None = Query(default=None),
    max_price: float | None = Query(default=None),
    min_units: int | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    sort: str = Query(default=DEFAULT_SORT),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Fact-grain datasheet search; `totals` covers the full filtered set."""
    order = SORT_SQL.get(sort) or SORT_SQL[DEFAULT_SORT]
    term = (q or "").strip()

    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if product_id is not None:
        clauses.append("f.product_id = %(product_id)s")
        params["product_id"] = product_id
    if store_id is not None:
        clauses.append("f.store_id = %(store_id)s")
        params["store_id"] = store_id
    if category:
        clauses.append("p.category = %(category)s")
        params["category"] = category
    if brand:
        clauses.append("p.brand = %(brand)s")
        params["brand"] = brand
    if format:
        clauses.append("s.format = %(format)s")
        params["format"] = format
    if region:
        clauses.append("s.region = %(region)s")
        params["region"] = region
    if promo is not None:
        clauses.append("f.promo_flag = %(promo)s")
        params["promo"] = promo
    if min_price is not None:
        clauses.append("f.price >= %(min_price)s")
        params["min_price"] = min_price
    if max_price is not None:
        clauses.append("f.price <= %(max_price)s")
        params["max_price"] = max_price
    if min_units is not None:
        clauses.append("f.units_sold >= %(min_units)s")
        params["min_units"] = min_units
    if date_from is not None:
        clauses.append("f.day >= %(date_from)s")
        params["date_from"] = date_from
    if date_to is not None:
        clauses.append("f.day <= %(date_to)s")
        params["date_to"] = date_to

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if not term:
                return await _fact_page(cur, clauses, params, order, limit, offset)

            if not await _tsquery_is_empty(cur, term):
                page = await _fact_page(
                    cur,
                    clauses + ["p.search_tsv @@ websearch_to_tsquery('english', %(q)s)"],
                    dict(params, q=term),
                    order,
                    limit,
                    offset,
                )
                if page["total"]:
                    return page

            # Text fallback over the dimension attributes a shopper would type.
            like = _like_pattern(term)
            like_clause = (
                f"(p.name ILIKE %(like)s {LIKE_ESCAPE}"
                f" OR p.brand ILIKE %(like)s {LIKE_ESCAPE}"
                f" OR p.sku ILIKE %(like)s {LIKE_ESCAPE})"
            )
            return await _fact_page(
                cur, clauses + [like_clause], dict(params, like=like), order, limit, offset
            )


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------

_MV_PRODUCT_SERIES = """
SELECT day, units, revenue, cogs, avg_price, inventory, promo_stores, store_count
FROM mv_product_day
WHERE product_id = %(pid)s AND day >= %(from_day)s AND day <= %(to_day)s
ORDER BY day ASC
"""

_MV_CATEGORY_SERIES = """
SELECT day, units, revenue, cogs, pl_units, product_count
FROM mv_category_day
WHERE category = %(category)s AND day >= %(from_day)s AND day <= %(to_day)s
ORDER BY day ASC
"""


async def _latest_day(cur: Any) -> date | None:
    await cur.execute("SELECT max(day) AS day FROM market_facts")
    row = await cur.fetchone()
    return None if not row else row["day"]


def _product_point(
    day: date,
    units: Any,
    revenue: Any,
    cogs: Any,
    avg_price: Any,
    inventory: Any,
    promo_stores: Any,
    store_count: Any,
) -> dict[str, Any]:
    revenue = float(revenue or 0.0)
    cogs = float(cogs or 0.0)
    return {
        "day": day,
        "units": int(units or 0),
        "avg_price": round(float(avg_price or 0.0), 4),
        "revenue": round(revenue, 4),
        "margin_pct": round((revenue - cogs) / revenue, 6) if revenue else 0.0,
        "inventory": int(inventory or 0),
        "promo_stores": int(promo_stores or 0),
        "store_count": int(store_count or 0),
    }


def _category_point(
    day: date,
    units: Any,
    revenue: Any,
    cogs: Any,
    pl_units: Any,
    product_count: Any,
) -> dict[str, Any]:
    revenue = float(revenue or 0.0)
    cogs = float(cogs or 0.0)
    units = int(units or 0)
    pl_units = int(pl_units or 0)
    return {
        "day": day,
        "units": units,
        "revenue": round(revenue, 4),
        "cogs": round(cogs, 4),
        "margin_pct": round((revenue - cogs) / revenue, 6) if revenue else 0.0,
        "pl_units": pl_units,
        "pl_share": round(pl_units / units, 6) if units else 0.0,
        "product_count": int(product_count or 0),
    }


@router.get("/products/{product_id}/series", response_model=ProductSeriesOut)
async def product_series(
    product_id: int,
    days: int = Query(default=30, ge=1, le=SERIES_MAX_DAYS),
) -> dict[str, Any]:
    """Product rollup series; the current day is recomputed live from `market_facts`."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_PRODUCT_COLUMNS} {_PRODUCT_FROM} WHERE p.product_id = %(pid)s",  # noqa: S608
                {"pid": product_id},
            )
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"product {product_id} not found")
            product = _product_row(row)
            await _refresh_latest_live(cur, [product])

            series: dict[date, dict[str, Any]] = {}
            target = await _latest_day(cur)
            if target is not None:
                await cur.execute(
                    _MV_PRODUCT_SERIES,
                    {"pid": product_id, "from_day": target - timedelta(days=days - 1), "to_day": target},
                )
                for mv in await cur.fetchall():
                    series[mv["day"]] = _product_point(
                        mv["day"], mv["units"], mv["revenue"], mv["cogs"],
                        mv["avg_price"], mv["inventory"], mv["promo_stores"], mv["store_count"],
                    )

                # Freshness path (§4): sim only mutates the target day, so the
                # materialised row for it may be one refresh interval stale.
                live = await rules.live_product_day(cur, target, [product_id])
                row_live = live.get(product_id)
                if row_live is not None:
                    series[target] = _product_point(
                        row_live.day, row_live.units, row_live.revenue, row_live.cogs,
                        row_live.avg_price, row_live.inventory, row_live.promo_stores, row_live.store_count,
                    )

    return {"product": product, "series": [series[day] for day in sorted(series)]}


@router.get("/categories/{category}/series", response_model=CategorySeriesOut)
async def category_series(
    category: str,
    days: int = Query(default=30, ge=1, le=SERIES_MAX_DAYS),
) -> dict[str, Any]:
    """Category rollup series; the current day is recomputed live from `market_facts`."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 AS ok FROM products WHERE category = %s LIMIT 1", (category,))
            if await cur.fetchone() is None:
                raise HTTPException(status_code=404, detail=f"category {category!r} not found")

            series: dict[date, dict[str, Any]] = {}
            target = await _latest_day(cur)
            if target is not None:
                await cur.execute(
                    _MV_CATEGORY_SERIES,
                    {
                        "category": category,
                        "from_day": target - timedelta(days=days - 1),
                        "to_day": target,
                    },
                )
                for mv in await cur.fetchall():
                    series[mv["day"]] = _category_point(
                        mv["day"], mv["units"], mv["revenue"], mv["cogs"], mv["pl_units"], mv["product_count"]
                    )

                live = await rules.live_category_day(cur, target, [category])
                row_live = live.get(category)
                if row_live is not None:
                    series[target] = _category_point(
                        row_live.day, row_live.units, row_live.revenue, row_live.cogs,
                        row_live.pl_units, row_live.product_count,
                    )

    return {"category": category, "series": [series[day] for day in sorted(series)]}
