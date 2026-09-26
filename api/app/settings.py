"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Matches the defaults declared in docker-compose.yml so a local run without
# DATABASE_URL still points somewhere sensible.
_DEFAULT_DATABASE_URL = "postgresql://laya:laya@db:5432/laya"


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str
    tick_channel: str
    origins: tuple[str, ...]
    cors_allow_credentials: bool
    # Deliberately conservative: the host has ~2 GB free RAM and Postgres
    # caps max_connections at 80 for the whole stack.
    pool_min_size: int
    pool_max_size: int
    pool_timeout: float
    heartbeat_seconds: float


def _load() -> Settings:
    origins_raw = _str("LAYA_ORIGINS", "*")
    origins = tuple(part.strip() for part in origins_raw.split(",") if part.strip())
    if not origins:
        origins = ("*",)
    wildcard = "*" in origins
    return Settings(
        database_url=_str("DATABASE_URL", _DEFAULT_DATABASE_URL),
        tick_channel=_str("LAYA_TICK_CHANNEL", "laya_events"),
        origins=origins,
        # Browsers reject credentialed CORS responses with a wildcard origin.
        cors_allow_credentials=not wildcard,
        pool_min_size=_int("LAYA_POOL_MIN", 1),
        pool_max_size=_int("LAYA_POOL_MAX", 6),
        pool_timeout=_float("LAYA_POOL_TIMEOUT", 15.0),
        heartbeat_seconds=_float("LAYA_WS_HEARTBEAT_SECONDS", 20.0),
    )


settings = _load()
