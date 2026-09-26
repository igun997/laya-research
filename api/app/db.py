"""Connection management.

Two access paths:

* an :class:`psycopg_pool.AsyncConnectionPool` used by every request handler and by
  the in-process tick worker, opened once from the FastAPI lifespan;
* a synchronous :func:`connect_sync` helper used only by the single LISTEN thread.

This module also owns the one and only ``numeric`` -> ``float`` conversion for the
service: a global psycopg loader, so no query in this codebase needs a ``::float8``
cast and no response model ever sees a :class:`decimal.Decimal`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.postgres import adapters
from psycopg.rows import dict_row
from psycopg.types.numeric import NumericBinaryLoader, NumericLoader
from psycopg_pool import AsyncConnectionPool

from .settings import settings

log = logging.getLogger("laya.api.db")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """ISO-8601 UTC timestamp used by every ``at`` field."""
    return utcnow().isoformat()


# ---------------------------------------------------------------------------
# numeric -> float (single conversion point)
# ---------------------------------------------------------------------------


class _NumericAsFloat(NumericLoader):
    """Text-format ``numeric`` loader that yields ``float`` instead of ``Decimal``."""

    def load(self, data: Any) -> float:
        return float(super().load(data))


class _NumericBinaryAsFloat(NumericBinaryLoader):
    """Binary-format ``numeric`` loader that yields ``float``."""

    def load(self, data: Any) -> float:
        return float(super().load(data))


def _install_numeric_loaders() -> None:
    """Register the float loaders on psycopg's global adapter map.

    Registered once at import, so every connection (pooled, LISTEN, or ad hoc)
    converts ``numeric`` the same way and no response model ever sees a
    :class:`decimal.Decimal`.
    """
    for loader in (_NumericAsFloat, _NumericBinaryAsFloat):
        try:
            adapters.register_loader("numeric", loader)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("numeric loader %s not registered: %s", loader.__name__, exc)


_install_numeric_loaders()


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

pool: AsyncConnectionPool = AsyncConnectionPool(
    settings.database_url,
    min_size=settings.pool_min_size,
    max_size=settings.pool_max_size,
    timeout=settings.pool_timeout,
    kwargs={"row_factory": dict_row},
    open=False,
    name="laya-api",
)


async def open_pool() -> None:
    """Open the pool. A cold database is not fatal: the pool keeps retrying."""
    try:
        await pool.open(wait=True, timeout=settings.pool_timeout)
        log.info("connection pool open (min=%s max=%s)", settings.pool_min_size, settings.pool_max_size)
    except Exception as exc:
        log.warning("connection pool not ready yet (%s); serving degraded, retrying in background", exc)


async def close_pool() -> None:
    try:
        await pool.close()
    except Exception as exc:  # pragma: no cover - shutdown path
        log.debug("pool close: %s", exc)


def connect_sync() -> psycopg.Connection:
    """Autocommit connection for the LISTEN thread (never taken from the pool)."""
    return psycopg.connect(settings.database_url, autocommit=True)
