"""Dataset metadata and the dashboard overview.

Both endpoints follow the §4 freshness path: `sim` only mutates the target day, so
the target-day numbers are recomputed live from `market_facts` while earlier days
come from the materialised views.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Query

from . import rules
from .db import now_iso, pool
from .schemas import MetaOut, OverviewOut

log = logging.getLogger("laya.api.overview")

router = APIRouter()

MOVERS_LIMIT = 10
# A mover needs a baseline worth comparing against, mirroring §4's rule gate.
MOVERS_MIN_BASELINE_UNITS = 20.0
OVERVIEW_MAX_DAYS = 180


# ---------------------------------------------------------------------------
# shared: dataset summary
# ---------------------------------------------------------------------------


async def dataset_summary(cur: Any) -> dict[str, Any]:
    """Dataset provenance for ``/api/health`` and ``/api/meta``.

    ``facts`` prefers the generator's own row count from ``dataset_meta.generation``
    (exact and free); the fallback is a real ``count(*)`` for databases loaded by
    other means.
    """
    await cur.execute("SELECT value FROM dataset_meta WHERE key = 'generation'")
    row = await cur.fetchone()
    generation: dict[str, Any] = dict(row["value"]) if row and isinstance(row["value"], dict) else {}

    facts = generation.get("rows")
    if facts is None:
        await cur.execute("SELECT count(*) AS n FROM market_facts")
        facts = int((await cur.fetchone() or {"n": 0})["n"])

    await cur.execute("SELECT count(*) AS n FROM products")
    products = int((await cur.fetchone() or {"n": 0})["n"])
    await cur.execute("SELECT count(*) AS n FROM stores")
    stores = int((await cur.fetchone() or {"n": 0})["n"])

    day_min = generation.get("day_min")
    day_max = generation.get("day_max")
    if day_min is None or day_max is None:
        await cur.execute("SELECT min(day) AS lo, max(day) AS hi FROM market_facts")
        bounds = await cur.fetchone() or {}
        day_min = day_min or bounds.get("lo")
        day_max = day_max or bounds.get("hi")

    return {
        "facts": int(facts or 0),
        "products": products,
        "stores": stores,
        # ISO strings keep this payload directly JSON-serialisable for the
        # `hello` WebSocket frame as well as for the HTTP routes.
        "day_min": day_min.isoformat() if hasattr(day_min, "isoformat") else day_min,
        "day_max": day_max.isoformat() if hasattr(day_max, "isoformat") else day_max,
        "seed": generation.get("seed"),
    }


async def rollups_as_of(cur: Any) -> str | None:
    """When the rollup views were last refreshed, as an ISO timestamp.

    ``market_facts`` is mutated continuously by the simulator while the rollup
    views are only rebuilt every ``LAYA_REFRESH_SECONDS``, so the honest answer
    to "how stale are the rollups" is the wall-clock time of the last refresh
    (stamped by the simulator in ``dataset_meta.rollup_refresh``) — not the
    newest day present in the view, which is almost always the newest day in the
    dataset and therefore says nothing about staleness.
    """
    await cur.execute(
        "SELECT value ->> 'at' AS at FROM dataset_meta WHERE key = 'rollup_refresh'"
    )
    row = await cur.fetchone()
    if row and row.get("at"):
        return str(row["at"])
    # Simulator has not refreshed yet: fall back to the newest rolled-up day so
    # the field is still populated on a freshly generated dataset.
    await cur.execute("SELECT max(day) AS day FROM mv_product_day")
    row = await cur.fetchone()
    day = None if not row else row["day"]
    if day is None:
        return None
    return day.isoformat()


async def signal_counts(cur: Any) -> dict[str, int]:
    await cur.execute("SELECT severity, count(*) AS n FROM signals GROUP BY severity")
    counts = {"critical": 0, "warn": 0, "info": 0}
    for row in await cur.fetchall():
        severity = str(row["severity"])
        if severity in counts:
            counts[severity] = int(row["n"])
    return counts


# ---------------------------------------------------------------------------
# /api/meta
# ---------------------------------------------------------------------------


@router.get("/meta", response_model=MetaOut)
async def meta() -> dict[str, Any]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            dataset = await dataset_summary(cur)

            await cur.execute(
                "SELECT category, count(*) AS products FROM products GROUP BY category ORDER BY category"
            )
            categories = [
                {"category": str(r["category"]), "products": int(r["products"])}
                for r in await cur.fetchall()
            ]

            await cur.execute("SELECT DISTINCT brand FROM products ORDER BY brand")
            brands = [str(r["brand"]) for r in await cur.fetchall()]

            await cur.execute("SELECT DISTINCT format FROM stores ORDER BY format")
            formats = [str(r["format"]) for r in await cur.fetchall()]

            await cur.execute("SELECT DISTINCT region FROM stores ORDER BY region")
            regions = [str(r["region"]) for r in await cur.fetchall()]

    return {
        "dataset": dataset,
        "categories": categories,
        "brands": brands,
        "formats": formats,
        "regions": regions,
    }


# ---------------------------------------------------------------------------
# /api/overview
# ---------------------------------------------------------------------------

_MV_DAY_SERIES = """
SELECT
    day,
    sum(units)::bigint              AS units,
    sum(revenue)                    AS revenue,
    sum(cogs)                       AS cogs,
    sum(pl_units)::bigint           AS pl_units
FROM mv_category_day
WHERE day >= %(from_day)s AND day <= %(to_day)s
GROUP BY day
ORDER BY day ASC
"""

# One scan of the target day yields both the day totals and the category table:
# every product belongs to exactly one category, so the category rows sum to the
# day totals exactly (the §1 rollup definitions are additive).
_LIVE_TARGET_BY_CATEGORY = """
SELECT
    p.category,
    sum(f.units_sold)::bigint                                     AS units,
    sum(f.units_sold * f.price)                                   AS revenue,
    sum(f.units_sold * f.unit_cost)                               AS cogs,
    sum(f.units_sold) FILTER (WHERE p.is_private_label)::bigint   AS pl_units,
    count(DISTINCT f.product_id)::bigint                          AS product_count
FROM market_facts f
JOIN products p USING (product_id)
WHERE f.day = %(day)s
GROUP BY p.category
"""

_PREV_DAY_BY_CATEGORY = """
SELECT category, units::bigint AS units
FROM mv_category_day
WHERE day = %(day)s
"""

_MOVER_BASELINE = """
SELECT
    h.product_id,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY h.units) AS med_units,
    count(*)::bigint                                     AS observations
FROM mv_product_day h
WHERE h.day >= %(from_day)s AND h.day <= %(to_day)s
GROUP BY h.product_id
"""


@router.get("/overview", response_model=OverviewOut)
async def overview(days: int = Query(default=14, ge=1, le=OVERVIEW_MAX_DAYS)) -> dict[str, Any]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            target = await rules.resolve_target_day(cur)
            counts = await signal_counts(cur)
            as_of = now_iso()

            if target is None:
                return {
                    "days": [],
                    "categories": [],
                    "movers": [],
                    "signal_counts": counts,
                    "as_of": as_of,
                    "rollups_as_of": await rollups_as_of(cur),
                }

            from_day = target - timedelta(days=days - 1)

            # --- target day, live from market_facts -------------------------
            await cur.execute(_LIVE_TARGET_BY_CATEGORY, {"day": target})
            live_rows = await cur.fetchall()

            live_by_category: list[dict[str, Any]] = []
            day_units = day_revenue = day_cogs = 0.0
            day_pl_units = 0
            for row in live_rows:
                units = int(row["units"] or 0)
                revenue = float(row["revenue"] or 0.0)
                cogs = float(row["cogs"] or 0.0)
                pl_units = int(row["pl_units"] or 0)
                day_units += units
                day_revenue += revenue
                day_cogs += cogs
                day_pl_units += pl_units
                live_by_category.append(
                    {
                        "category": str(row["category"]),
                        "units": units,
                        "revenue": revenue,
                        "margin_pct": (revenue - cogs) / revenue if revenue else 0.0,
                        "pl_share": pl_units / units if units else 0.0,
                        "units_dod_pct": None,
                    }
                )

            # --- earlier days, from the rollup ------------------------------
            await cur.execute(
                _MV_DAY_SERIES,
                {"from_day": from_day, "to_day": target - timedelta(days=1)},
            )
            day_points: list[dict[str, Any]] = [
                _day_point(
                    row["day"], row["units"], row["revenue"], row["cogs"], row["pl_units"]
                )
                for row in await cur.fetchall()
            ]
            day_points.append(
                _day_point(target, day_units, day_revenue, day_cogs, day_pl_units)
            )

            # --- day-over-day on the category table -------------------------
            await cur.execute(_PREV_DAY_BY_CATEGORY, {"day": target - timedelta(days=1)})
            prev_units = {str(r["category"]): int(r["units"] or 0) for r in await cur.fetchall()}
            for entry in live_by_category:
                before = prev_units.get(entry["category"])
                if before:
                    entry["units_dod_pct"] = (entry["units"] - before) / before
            live_by_category.sort(key=lambda e: e["units"], reverse=True)

            movers = await _movers(cur, target)
            # Must be read while `cur` is still open: the cursor is closed when
            # the `async with` blocks exit, and this return statement sits outside
            # them.
            rollups_stamp = await rollups_as_of(cur)

    return {
        "days": day_points,
        "categories": live_by_category,
        "movers": movers,
        "signal_counts": counts,
        "as_of": as_of,
        "rollups_as_of": rollups_stamp,
    }


def _day_point(day: date, units: Any, revenue: Any, cogs: Any, pl_units: Any) -> dict[str, Any]:
    units = int(units or 0)
    revenue = float(revenue or 0.0)
    cogs = float(cogs or 0.0)
    pl_units = int(pl_units or 0)
    return {
        "day": day,
        "units": units,
        "revenue": round(revenue, 4),
        "cogs": round(cogs, 4),
        "margin_pct": round((revenue - cogs) / revenue, 6) if revenue else 0.0,
        "pl_units": pl_units,
        "pl_share": round(pl_units / units, 6) if units else 0.0,
    }


async def _movers(cur: Any, target: date) -> list[dict[str, Any]]:
    """Top movers by lift for the target day, using the live rollup + MV baseline."""
    await cur.execute("SELECT product_id, name, category FROM products")
    labels = {
        int(r["product_id"]): (str(r["name"]), str(r["category"])) for r in await cur.fetchall()
    }
    if not labels:
        return []

    live = await rules.live_product_day(cur, target, list(labels))
    if not live:
        return []

    await cur.execute(
        _MOVER_BASELINE,
        {"from_day": target - timedelta(days=rules.BASELINE_DAYS), "to_day": target - timedelta(days=1)},
    )
    baselines = {
        int(r["product_id"]): (float(r["med_units"]), int(r["observations"]))
        for r in await cur.fetchall()
        if r["med_units"] is not None
    }

    candidates: list[tuple[float, dict[str, Any]]] = []
    for product_id, row in live.items():
        base = baselines.get(product_id)
        if base is None:
            continue
        med_units, observations = base
        if observations < rules.MIN_OBS or med_units < MOVERS_MIN_BASELINE_UNITS:
            continue
        lift = row.units / med_units
        name, category = labels.get(product_id, (f"product {product_id}", ""))
        candidates.append(
            (
                lift,
                {
                    "product_id": product_id,
                    "name": name,
                    "category": category,
                    "units": int(row.units),
                    "baseline": round(med_units, 4),
                    "lift": round(lift, 4),
                    "revenue": round(row.revenue, 4),
                },
            )
        )

    candidates.sort(key=lambda item: (-item[0], item[1]["product_id"]))
    return [payload for _lift, payload in candidates[:MOVERS_LIMIT]]
