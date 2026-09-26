"""Entry point for the laya-research realtime mutation simulator (``python -m sim``).

Responsibilities:

* run the mutation tick loop on a single autocommit connection (contract §3);
* publish each tick as a ``pg_notify`` payload on ``LAYA_TICK_CHANNEL`` (§6.1);
* keep ``mv_product_day`` / ``mv_category_day`` fresh from a background daemon
  thread that never blocks or crashes the tick loop;
* shut down cleanly on SIGTERM/SIGINT, finishing the in-flight tick first.
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import psycopg

from . import mutate

LOG = logging.getLogger("sim")

DEFAULT_CHANNEL = "laya_events"
DEFAULT_TICK_SECONDS = 4.0
DEFAULT_TICK_ROWS = 250
DEFAULT_REFRESH_SECONDS = 30.0

#: minimum refresh interval after a failed refresh (contract §3: back off to
#: at least 60 s).
REFRESH_BACKOFF_SECONDS = 60.0

#: connect retry backoff for the tick loop.
CONNECT_RETRY_SECONDS = 3.0

#: rollup views refreshed by the background thread, in order.
ROLLUP_VIEWS = ("mv_product_day", "mv_category_day")


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        LOG.warning("invalid %s=%r, using default %s", name, raw, default)
        return default
    return value if value > 0 else default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(float(raw))
    except ValueError:
        LOG.warning("invalid %s=%r, using default %s", name, raw, default)
        return default
    return value if value > 0 else default


def env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def dataset_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is required")
    return url


def seed_from_env() -> int:
    raw = os.environ.get("LAYA_SEED", "").strip()
    if not raw:
        return int(time.time() * 1000) & 0x7FFFFFFF
    try:
        return int(raw)
    except ValueError:
        LOG.warning("invalid LAYA_SEED=%r, deriving a seed", raw)
        return abs(hash(raw)) & 0x7FFFFFFF


# --------------------------------------------------------------------------
# Rollup refresher
# --------------------------------------------------------------------------


class RollupRefresher:
    """Schedules rollup refreshes on a dedicated daemon thread.

    ``REFRESH MATERIALIZED VIEW CONCURRENTLY`` cannot run inside a transaction
    block, so the refresher holds its own autocommit connection. It is strictly
    advisory: any failure is logged, the connection is recycled and the next
    attempt is pushed out to at least :data:`REFRESH_BACKOFF_SECONDS`. The tick
    loop never waits on it.

    The scheduler thread only ever *schedules*; the SQL runs on a separate
    worker thread. That split is what makes the "skip a refresh if one is still
    running" guard real rather than vacuous: a refresh that outlives the
    interval is still in flight when the next slot comes round, and the slot is
    then skipped instead of piling a second refresh on top of the first.
    """

    def __init__(self, dsn: str, interval: float) -> None:
        self._dsn = dsn
        self._interval = interval
        self._stop = threading.Event()
        self._conn: psycopg.Connection | None = None
        #: held for the duration of a refresh; never acquired blocking by the
        #: scheduler, so a long refresh can never stall the schedule.
        self._running = threading.Lock()
        self._scheduler = threading.Thread(
            target=self._schedule, name="rollup-scheduler", daemon=True
        )
        self._worker: threading.Thread | None = None
        self._next_at = time.monotonic() + interval

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._scheduler.start()

    def stop(self) -> None:
        """Ask for shutdown, then wait briefly for any in-flight refresh."""
        self._stop.set()
        self._scheduler.join(timeout=5.0)
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=self._interval + 5.0)
        self._close()

    # -- scheduler ---------------------------------------------------------

    def _schedule(self) -> None:
        while not self._stop.is_set():
            delay = self._next_at - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                break
            if self._stop.is_set():
                break
            if self._running.locked():
                # Previous refresh still in flight: skip this slot entirely.
                LOG.info(
                    "rollup refresh still running; skipping this slot (next in %.0fs)",
                    self._interval,
                )
                self._next_at = time.monotonic() + self._interval
                continue
            worker = threading.Thread(
                target=self._run_refresh, name="rollup-refresh", daemon=True
            )
            self._worker = worker
            worker.start()
            self._next_at = time.monotonic() + self._interval

    def _run_refresh(self) -> None:
        if not self._running.acquire(blocking=False):
            return
        try:
            self._refresh_once()
        except Exception:  # noqa: BLE001 - never let the thread die
            LOG.exception(
                "rollup refresh failed; backing off %.0fs", REFRESH_BACKOFF_SECONDS
            )
            self._next_at = time.monotonic() + max(
                REFRESH_BACKOFF_SECONDS, self._interval
            )
            self._recycle()
        finally:
            self._running.release()

    # -- internals ---------------------------------------------------------

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._dsn, autocommit=True, application_name="laya-sim-refresh"
            )
        return self._conn

    def _recycle(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _refresh_once(self) -> None:
        started = time.monotonic()
        conn = self._connection()
        for view in ROLLUP_VIEWS:
            with conn.cursor() as cur:
                cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # Stamp the refresh so /api/health can report real rollup staleness
        # (now() - this instant) instead of the age of the dataset's newest day,
        # which is a different and misleading quantity.
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dataset_meta (key, value, updated_at)
                VALUES ('rollup_refresh', %s::jsonb, now())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = now()
                """,
                (
                    json.dumps(
                        {
                            "at": datetime.now(timezone.utc)
                            .isoformat(timespec="milliseconds")
                            .replace("+00:00", "Z"),
                            "views": list(ROLLUP_VIEWS),
                            "duration_ms": elapsed_ms,
                        }
                    ),
                ),
            )
        LOG.info(
            "rollups refreshed views=%s elapsed_ms=%d",
            ",".join(ROLLUP_VIEWS),
            elapsed_ms,
        )


# --------------------------------------------------------------------------
# Simulator
# --------------------------------------------------------------------------


class Simulator:
    """Owns the tick connection, the RNG and the refresh thread."""

    def __init__(self) -> None:
        self.dsn = dataset_url()
        self.channel = env_str("LAYA_TICK_CHANNEL", DEFAULT_CHANNEL)
        self.tick_seconds = env_float("LAYA_TICK_SECONDS", DEFAULT_TICK_SECONDS)
        self.tick_rows = env_int("LAYA_TICK_ROWS", DEFAULT_TICK_ROWS)
        self.refresh_seconds = env_float(
            "LAYA_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS
        )
        self.seed = seed_from_env()
        self.rng = random.Random(self.seed)
        self.conn: psycopg.Connection | None = None
        self.refresher: RollupRefresher | None = None
        self._stopping = threading.Event()
        self._empty_logged = False
        self.baseline_units = mutate.FALLBACK_UNITS_BASELINE
        self.store_span = 0
        self.product_span = 0
        self._tick = 0

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        self.conn = psycopg.connect(
            self.dsn, autocommit=True, application_name="laya-sim"
        )

    def request_stop(self, signum: int | None = None, _frame=None) -> None:
        if signum is not None:
            LOG.info("received signal %s, shutting down after the in-flight tick", signum)
        self._stopping.set()

    def stop(self) -> None:
        if self.refresher is not None:
            self.refresher.stop()
            self.refresher = None
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                LOG.warning("error closing tick connection", exc_info=True)
            self.conn = None

    # -- tick loop ---------------------------------------------------------

    def load_baseline(self) -> None:
        try:
            params = mutate.load_dataset_params(self.conn)
        except Exception:  # noqa: BLE001 - baseline is a nicety, not required
            LOG.warning("could not read dataset_meta.generation", exc_info=True)
            params = {}
        self.baseline_units = mutate.load_units_baseline(self.conn, params)
        self.store_span, self.product_span = mutate.discover_spans(
            self.conn, self.store_span, self.product_span
        )
        LOG.info(
            "baseline units/row=%.2f stores=%d products=%d dataset=%s",
            self.baseline_units,
            self.store_span,
            self.product_span,
            params or "unknown",
        )

    def run(self) -> int:
        LOG.info(
            "sim starting seed=%s channel=%s tick_seconds=%s tick_rows=%s refresh_seconds=%s",
            self.seed,
            self.channel,
            self.tick_seconds,
            self.tick_rows,
            self.refresh_seconds,
        )
        self.connect_with_retry()
        if self.conn is None:
            LOG.info("stopped before the database connection was established")
            return 0
        self.load_baseline()
        self.refresher = RollupRefresher(self.dsn, self.refresh_seconds)
        self.refresher.start()

        while not self._stopping.is_set():
            started = time.monotonic()
            if self.conn is None:
                self.connect_with_retry()
                if self.conn is None:
                    break
                self.load_baseline()
            try:
                result = mutate.run_tick(
                    self.conn,
                    tick=self._tick + 1,
                    rows_wanted=self.tick_rows,
                    channel=self.channel,
                    rng=self.rng,
                    baseline_units=self.baseline_units,
                    store_span=self.store_span,
                    product_span=self.product_span,
                )
            except psycopg.OperationalError:
                LOG.exception("database connection lost; reconnecting")
                self._reconnect()
                if self.conn is not None and not self._stopping.is_set():
                    self.load_baseline()
                self._sleep(self.tick_seconds, started)
                continue
            except psycopg.Error:
                LOG.exception("tick failed; skipping")
                self._sleep(self.tick_seconds, started)
                continue
            except Exception:  # noqa: BLE001 - the loop must survive anything
                LOG.exception("unexpected tick failure; skipping")
                self._sleep(self.tick_seconds, started)
                continue

            self._tick = result.tick

            if result.day is None:
                if not self._empty_logged:
                    LOG.warning(
                        "market_facts is empty; idling without publishing "
                        "(is the generator done?)"
                    )
                    self._empty_logged = True
            else:
                self._empty_logged = False
                if result.truncated:
                    LOG.warning(
                        "notify payload truncated to fit %d bytes (actual %d bytes, "
                        "row_count=%d)",
                        mutate.PAYLOAD_BYTE_BUDGET,
                        result.payload_bytes,
                        result.row_count,
                    )
                LOG.info("%s", mutate.describe_tick(result))

            self._sleep(self.tick_seconds, started)

        LOG.info("sim stopped after %d ticks", self._tick)
        self.stop()
        return 0

    def _reconnect(self) -> None:
        self.connect_with_retry()

    def connect_with_retry(self) -> None:
        """Connect, retrying until success or shutdown is requested."""
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None
        while not self._stopping.is_set():
            try:
                self.connect()
                return
            except psycopg.Error:
                LOG.warning(
                    "database connection failed; retrying in %.0fs",
                    CONNECT_RETRY_SECONDS,
                )
                self._stopping.wait(CONNECT_RETRY_SECONDS)

    def _sleep(self, seconds: float, started: float) -> None:
        """Sleep out the remainder of the tick interval, waking early on stop."""
        remaining = seconds - (time.monotonic() - started)
        if remaining > 0:
            self._stopping.wait(remaining)


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LAYA_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )


def main() -> int:
    configure_logging()
    sim = Simulator()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, sim.request_stop)
    try:
        return sim.run()
    except KeyboardInterrupt:
        LOG.info("interrupted")
        sim.stop()
        return 0
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        LOG.exception("fatal error")
        sim.stop()
        return 1


if __name__ == "__main__":
    sys.exit(main())
