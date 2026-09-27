"""FastAPI application: search, decision patterns, realtime fan-out.

Every route is mounted under the real ``/api`` prefix — there is no ``root_path``
rewriting, so the paths below are the paths clients call (and the paths Caddy
proxies).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from . import decide, overview, rules, search, stream
from .db import close_pool, open_pool, pool
from .overview import dataset_summary
from .schemas import PatternsOut, ScanIn, ScanOut, SeenIn, SeenOut, SignalsOut
from .settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("laya.api")

API_PREFIX = "/api"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await open_pool()
    await stream.start_realtime()
    try:
        yield
    finally:
        await stream.stop_realtime()
        await close_pool()


app = FastAPI(
    title="laya-research api",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.origins),
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

api = APIRouter()


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------

_LAG_SQL = """
SELECT
    (SELECT max(day) FROM market_facts)      AS facts_day,
    (SELECT max(day) FROM mv_product_day)    AS rollup_day,
    (SELECT (value ->> 'at') FROM dataset_meta WHERE key = 'rollup_refresh')::timestamptz
        AS refreshed_at,
    EXTRACT(EPOCH FROM (
        now() - (SELECT (value ->> 'at') FROM dataset_meta WHERE key = 'rollup_refresh')::timestamptz
    )) AS lag_seconds
"""


@api.get("/health")
async def health() -> dict[str, Any]:
    """Liveness plus a real database probe.

    The route answers 200 even when Postgres is unreachable — the compose
    healthcheck gates on the API process, and the ``db`` flag carries the truth —
    so a transient database blip does not restart the container.
    """
    dataset: dict[str, Any] = {
        "facts": 0,
        "products": 0,
        "stores": 0,
        "day_min": None,
        "day_max": None,
        "seed": None,
    }
    lag_seconds: float | None = None
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 AS ok")
                probe = await cur.fetchone()
                db_ok = bool(probe and probe.get("ok") == 1)
                if db_ok:
                    dataset = await dataset_summary(cur)
                    await cur.execute(_LAG_SQL)
                    row = await cur.fetchone() or {}
                    lag = row.get("lag_seconds")
                    # Negative lag means the rollup day sits in the future
                    # relative to the clock; report null rather than a nonsense
                    # number.
                    lag_seconds = None if lag is None else round(float(lag), 3)
                    if lag_seconds is not None and lag_seconds < 0:
                        lag_seconds = None
    except Exception as exc:
        log.warning("health probe failed: %s", exc)
        return {"status": "degraded", "db": False, "rollup_lag_seconds": None, "dataset": dataset}

    return {
        "status": "ok" if db_ok else "degraded",
        "db": db_ok,
        "rollup_lag_seconds": lag_seconds if db_ok else None,
        "dataset": dataset,
    }


# ---------------------------------------------------------------------------
# /api/patterns
# ---------------------------------------------------------------------------


@api.get("/patterns", response_model=PatternsOut)
async def patterns() -> dict[str, Any]:
    """The declarative rule catalog, served verbatim from `rules.RULES`."""
    return rules.catalog()


@api.post("/patterns/scan", response_model=ScanOut)
async def patterns_scan(body: ScanIn) -> dict[str, Any]:
    """Evaluate the catalog for one day.

    ``day`` of ``null`` means the latest day in ``market_facts``. For
    ``product_ids`` / ``categories``, ``null`` means *every* subject and an empty
    list means *no* subject. ``persist: false`` computes without writing.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            result = await rules.scan(
                cur,
                day=body.day,
                product_ids=body.product_ids,
                categories=body.categories,
                persist=body.persist,
            )
    return {"scanned": result.scanned, "signals": result.signals}


# ---------------------------------------------------------------------------
# /api/signals
# ---------------------------------------------------------------------------


@api.get("/signals", response_model=SignalsOut)
async def signals(
    pattern: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    subject_id: int | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    only_unseen: bool = Query(default=False),
) -> dict[str, Any]:
    """Persisted decision patterns, newest first."""
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if pattern:
        clauses.append("pattern = %(pattern)s")
        params["pattern"] = pattern
    if severity:
        clauses.append("severity = %(severity)s")
        params["severity"] = severity
    if subject_type:
        clauses.append("subject_type = %(subject_type)s")
        params["subject_type"] = subject_type
    if subject_id is not None:
        clauses.append("subject_id = %(subject_id)s")
        params["subject_id"] = subject_id
    if since is not None:
        clauses.append("fired_at >= %(since)s")
        params["since"] = since
    if only_unseen:
        clauses.append("seen = false")

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT count(*) AS total, count(*) FILTER (WHERE severity = 'critical') AS critical, "  # noqa: S608
                f"count(*) FILTER (WHERE severity = 'warn') AS warn, "
                f"count(*) FILTER (WHERE severity = 'info') AS info "
                f"FROM signals{where}",
                params,
            )
            counts = await cur.fetchone() or {}
            total = int(counts.get("total") or 0)

            await cur.execute(
                f"SELECT signal_id, fired_at, day, pattern, severity, subject_type, subject_id, "  # noqa: S608
                f"subject_label, score, evidence, action FROM signals{where} "
                f"ORDER BY fired_at DESC, signal_id DESC LIMIT %(limit)s",
                params,
            )
            items = [
                {
                    "signal_id": int(r["signal_id"]),
                    "fired_at": r["fired_at"].isoformat()
                    if hasattr(r["fired_at"], "isoformat")
                    else str(r["fired_at"]),
                    "day": r["day"],
                    "pattern": r["pattern"],
                    "severity": r["severity"],
                    "subject_type": r["subject_type"],
                    "subject_id": int(r["subject_id"]),
                    "subject_label": r["subject_label"],
                    "score": float(r["score"]),
                    "evidence": r["evidence"],
                    "action": r["action"],
                }
                for r in await cur.fetchall()
            ]

    return {
        "items": items,
        "counts": {
            "critical": int(counts.get("critical") or 0),
            "warn": int(counts.get("warn") or 0),
            "info": int(counts.get("info") or 0),
        },
        "total": total,
    }


@api.post("/signals/seen", response_model=SeenOut)
async def signals_seen(body: SeenIn) -> dict[str, Any]:
    """Mark signals as seen; already-seen ids are not counted twice."""
    ids = sorted({int(i) for i in body.signal_ids})
    if not ids:
        return {"updated": 0}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE signals SET seen = true WHERE signal_id = ANY(%s) AND seen = false",
                (ids,),
            )
            updated = cur.rowcount
    return {"updated": int(updated or 0)}


# ---------------------------------------------------------------------------
# Mount
# ---------------------------------------------------------------------------

api.include_router(search.router)
api.include_router(overview.router)
api.include_router(stream.router)
api.include_router(decide.router)

app.include_router(api, prefix=API_PREFIX)
