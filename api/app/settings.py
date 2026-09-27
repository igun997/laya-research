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
    # Laya is a separate container running `laya-serve`, which speaks TypeSafe
    # Jev's POST /v1/systemone shape. CPU inference on this host is slow (~1-2 s
    # per question), so the timeout is generous and the whole integration is
    # advisory: the dashboard must keep working when Laya is absent.
    laya_url: str
    laya_timeout: float
    laya_enabled: bool
    # Which Laya checkpoint should answer. Sent as the request-level `model` field,
    # which is the ONLY way to pin a checkpoint through `laya-serve`: it does not
    # read any LAYA_MODEL* variable for model selection, and `LAYA_MODELS` only
    # controls the preload list. Empty means "let the Router choose", which sends
    # English text to the `english` checkpoint.
    laya_checkpoint: str


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
        laya_url=_str("LAYA_URL", "http://laya:8000"),
        laya_timeout=_float("LAYA_TIMEOUT", 120.0),
        laya_enabled=_str("LAYA_ENABLED", "1").lower() not in ("0", "false", "no", "off"),
        laya_checkpoint=_str("LAYA_CHECKPOINT", ""),
    )


settings = _load()
