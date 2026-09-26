"""Realtime transport: one LISTEN thread, in-process tick worker, WS fan-out.

Ownership follows ``docs/CONTRACT.md`` §6.2: the API holds **exactly one** LISTEN
connection, on its own supervised thread with exponential-backoff reconnect, and
fans every frame out to all WebSocket clients. The thread never touches the
database pool and never blocks: payloads are handed to the event loop through a
bounded queue, and a consumer task does the rule work.

On each tick the worker runs the §4 freshness path — recompute the ``target_day``
rollup live from ``market_facts``, take earlier days from the materialised views —
then evaluates the catalog, upserts new or changed signals and broadcasts them.
Unchanged signals are neither re-persisted nor re-emitted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import date
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from psycopg import sql

from . import rules
from .db import connect_sync, now_iso, pool
from .overview import dataset_summary
from .settings import settings

log = logging.getLogger("laya.api.stream")

router = APIRouter()

SEVERITIES = ("info", "warn", "critical")
# A slow client is dropped rather than allowed to grow unbounded.
# A slow client is dropped rather than allowed to grow unbounded.
CLIENT_QUEUE_SIZE = 256


# ---------------------------------------------------------------------------
# Client registry and fan-out
# ---------------------------------------------------------------------------


class Client:
    """One WebSocket, its subscription filter and its outbound queue."""

    __slots__ = ("ws", "queue", "patterns", "severities", "alive", "_writer")

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
        self.patterns: set[str] = set()  # empty = all patterns
        self.severities: set[str] = set()  # empty = all severities
        self.alive = True
        self._writer: asyncio.Task[None] | None = None

    def offer(self, frame: dict[str, Any]) -> None:
        """Non-blocking enqueue; the slowest clients lose frames, never the server."""
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            log.warning("client queue full, dropping %s frame", frame.get("type"))

    def accepts(self, signal: dict[str, Any]) -> bool:
        if self.patterns and signal.get("pattern") not in self.patterns:
            return False
        if self.severities and signal.get("severity") not in self.severities:
            return False
        return True

    def start_writer(self) -> None:
        self._writer = asyncio.create_task(self._drain(), name=f"ws-writer-{id(self)}")

    async def _drain(self) -> None:
        try:
            while self.alive:
                frame = await self.queue.get()
                await self.ws.send_json(frame)
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception as exc:
            log.debug("websocket writer stopped: %s", exc)
        finally:
            self.alive = False

    async def stop_writer(self) -> None:
        self.alive = False
        if self._writer is not None and not self._writer.done():
            self._writer.cancel()
            try:
                await self._writer
            except (asyncio.CancelledError, Exception):  # pragma: no cover - shutdown
                pass


class Hub:
    """Fan-out to every live client, with per-connection signal filtering."""

    def __init__(self) -> None:
        self._clients: set[Client] = set()

    @property
    def size(self) -> int:
        return len(self._clients)

    def add(self, client: Client) -> None:
        self._clients.add(client)

    async def remove(self, client: Client) -> None:
        self._clients.discard(client)
        await client.stop_writer()

    def broadcast(self, frame: dict[str, Any], signal: dict[str, Any] | None = None) -> int:
        """Queue ``frame`` for every client; ``signal`` is the per-client filter input."""
        sent = 0
        dead: list[Client] = []
        for client in list(self._clients):
            if not client.alive:
                dead.append(client)
                continue
            if signal is not None and not client.accepts(signal):
                continue
            client.offer(frame)
            sent += 1
        for client in dead:
            self._clients.discard(client)
        return sent


hub = Hub()


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


def _frame(kind: str, **payload: Any) -> dict[str, Any]:
    return {"type": kind, "at": now_iso(), **payload}


def tick_frame(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("rows") or []
    product_ids = payload.get("product_ids") or []
    stores = payload.get("stores") or []
    return _frame(
        "tick",
        day=payload.get("day"),
        mutations=rows,
        summary={
            "rows": int(payload.get("row_count") or len(rows)),
            "products": len(product_ids),
            "stores": len(stores),
            "tick": payload.get("tick"),
        },
    )


def signal_frame(signal: dict[str, Any]) -> dict[str, Any]:
    return _frame("signal", signal=_jsonable_signal(signal))


def _jsonable_signal(signal: dict[str, Any]) -> dict[str, Any]:
    day = signal.get("day")
    if isinstance(day, date):
        signal = dict(signal, day=day.isoformat())
    return signal


# ---------------------------------------------------------------------------
# LISTEN thread (exactly one connection)
# ---------------------------------------------------------------------------


class Listener:
    """Supervised thread holding the single LISTEN connection."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._loop = loop
        self._queue = queue
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="laya-listen", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)

    # -- thread body ------------------------------------------------------

    def _offer(self, payload: str) -> None:
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("tick queue full, dropping payload (%d bytes)", len(payload))

    def _run(self) -> None:
        backoff = 1.0
        listen_sql = sql.SQL("LISTEN {}").format(sql.Identifier(settings.tick_channel))
        while not self._stop.is_set():
            conn = None
            try:
                conn = connect_sync()
                conn.execute(listen_sql)
                backoff = 1.0
                log.info("listening on channel %s", settings.tick_channel)
                while not self._stop.is_set():
                    for notify in conn.notifies(timeout=1.0):
                        if self._stop.is_set():
                            break
                        if notify.payload:
                            self._loop.call_soon_threadsafe(self._offer, notify.payload)
            except Exception as exc:
                if self._stop.is_set():
                    break
                log.warning(
                    "LISTEN connection lost (%s); reconnecting in %.1fs", exc, backoff
                )
                self._stop.wait(backoff)
                backoff = min(backoff * 2.0, rules.LISTEN_BACKOFF_MAX_SECONDS)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # pragma: no cover - teardown
                        pass
        log.info("listener thread stopped")


# ---------------------------------------------------------------------------
# Tick worker
# ---------------------------------------------------------------------------


class TickWorker:
    """Consumes NOTIFY payloads, runs the rule engine, persists and broadcasts.

    ``_emitted`` holds one fingerprint per ``(pattern, subject_type, subject_id)``
    that last fired, so an unchanged dataset produces no repeat frames. The cache
    is bounded by the catalog size times the subject count (9 patterns x products
    plus categories), and entries for subjects that stop firing are evicted as
    soon as they are seen again — it cannot grow without limit.
    """

    def __init__(self, queue: asyncio.Queue[str]) -> None:
        self._queue = queue
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # (pattern, subject_type, subject_id) -> fingerprint of the last emission.
        self._emitted: dict[tuple[str, str, int], tuple] = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="laya-tick-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # pragma: no cover - shutdown
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except (asyncio.TimeoutError, TimeoutError):
                continue
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            try:
                await self.handle(raw)
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            except Exception:
                # A bad tick must never take the worker (or the API) down.
                log.exception("tick handling failed")

    async def handle(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("ignoring non-JSON NOTIFY payload")
            return
        if not isinstance(payload, dict) or payload.get("kind") != "tick":
            return

        day_raw = payload.get("day")
        try:
            day = date.fromisoformat(str(day_raw))
        except (ValueError, TypeError):
            log.warning("tick without a usable day (%r)", day_raw)
            return

        product_ids = [int(p) for p in (payload.get("product_ids") or []) if p is not None]
        # The tick frame is informative even when no rule can be evaluated.
        hub.broadcast(tick_frame(payload))

        if not product_ids:
            return

        stored: list[dict[str, Any]] = []
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                labels = await rules.product_labels(cur, product_ids)
                live = await rules.live_product_day(cur, day, product_ids)
                baselines = await rules.product_baselines(cur, product_ids, day)
                hits = rules.evaluate_products(live.values(), baselines, labels)

                categories = await rules.categories_for_products(cur, product_ids)
                if categories:
                    live_cats = await rules.live_category_day(cur, day, categories)
                    cat_baselines = await rules.category_baselines(cur, categories, day)
                    hits.extend(rules.evaluate_categories(live_cats.values(), cat_baselines))

                changed = self._filter_changed(hits, product_ids, categories)
                if changed:
                    stored = await rules.persist_hits(cur, changed)

        for signal in stored:
            hub.broadcast(signal_frame(signal), signal=signal)

    def _filter_changed(
        self,
        hits: list[rules.RuleHit],
        product_ids: list[int],
        categories: list[str],
    ) -> list[rules.RuleHit]:
        """Drop hits whose frame would repeat the previous emission for that subject."""
        changed: list[rules.RuleHit] = []
        seen: set[tuple[str, str, int]] = set()
        for hit in hits:
            key = (hit.pattern, hit.subject_type, hit.subject_id)
            seen.add(key)
            if self._emitted.get(key) == hit.fingerprint():
                continue
            self._emitted[key] = hit.fingerprint()
            changed.append(hit)

        # Subjects that stopped firing forget their fingerprint, so a later
        # recovery followed by a fresh break emits again.
        affected_products = set(product_ids)
        affected_categories = {rules.category_key(c) for c in categories}
        for key in list(self._emitted):
            pattern, subject_type, subject_id = key
            if subject_type == rules.PRODUCT and subject_id in affected_products:
                if key not in seen:
                    del self._emitted[key]
            elif subject_type == rules.CATEGORY and subject_id in affected_categories:
                if key not in seen:
                    del self._emitted[key]
        return changed


# ---------------------------------------------------------------------------
# Service lifecycle
# ---------------------------------------------------------------------------

_listener: Listener | None = None
_worker: TickWorker | None = None
_heartbeat: asyncio.Task[None] | None = None
_tick_queue: asyncio.Queue[str] | None = None


async def _heartbeat_loop() -> None:
    interval = max(1.0, settings.heartbeat_seconds)
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        try:
            hub.broadcast(_frame("heartbeat"))
        except Exception:  # pragma: no cover - defensive
            log.exception("heartbeat broadcast failed")


async def start_realtime() -> None:
    """Start the LISTEN thread, the tick worker and the heartbeat."""
    global _listener, _worker, _heartbeat, _tick_queue
    loop = asyncio.get_running_loop()
    _tick_queue = asyncio.Queue(maxsize=rules.TICK_QUEUE_SIZE)
    _worker = TickWorker(_tick_queue)
    _worker.start()
    _listener = Listener(loop, _tick_queue)
    _listener.start()
    _heartbeat = asyncio.create_task(_heartbeat_loop(), name="laya-heartbeat")
    log.info("realtime started (channel=%s heartbeat=%.0fs)", settings.tick_channel, settings.heartbeat_seconds)


async def stop_realtime() -> None:
    global _listener, _worker, _heartbeat, _tick_queue
    if _heartbeat is not None:
        _heartbeat.cancel()
        try:
            await _heartbeat
        except (asyncio.CancelledError, Exception):  # pragma: no cover - shutdown
            pass
        _heartbeat = None
    if _listener is not None:
        # Joining a blocking socket read happens off the event loop.
        await asyncio.to_thread(_listener.stop)
        _listener = None
    if _worker is not None:
        await _worker.stop()
        _worker = None
    _tick_queue = None


# ---------------------------------------------------------------------------
# /api/stream
# ---------------------------------------------------------------------------


@router.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    """Server -> client frames: hello, tick, signal, heartbeat, error."""
    await ws.accept()
    client = Client(ws)

    dataset: dict[str, Any] = {}
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                dataset = await dataset_summary(cur)
    except Exception as exc:
        log.warning("hello dataset unavailable: %s", exc)
        dataset = {"facts": 0, "products": 0, "stores": 0, "day_min": None, "day_max": None, "seed": None}

    # `hello` is the first frame on the wire: queued before the client is
    # registered for broadcasts, and the writer drains the queue in order.
    client.offer(_frame("hello", dataset=dataset))
    client.start_writer()
    hub.add(client)
    log.info("websocket client connected (clients=%d)", hub.size)

    try:
        while client.alive:
            raw = await ws.receive_text()
            await _handle_client_frame(client, raw)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("websocket closed: %s", exc)
    finally:
        await hub.remove(client)
        log.info("websocket client disconnected (clients=%d)", hub.size)


async def _handle_client_frame(client: Client, raw: str) -> None:
    try:
        message = json.loads(raw)
    except (ValueError, TypeError):
        client.offer(_frame("error", detail="invalid JSON frame"))
        return
    if not isinstance(message, dict):
        client.offer(_frame("error", detail="frame must be a JSON object"))
        return

    kind = message.get("type")
    if kind == "ping":
        client.offer(_frame("pong"))
        return

    if kind == "subscribe":
        if "patterns" in message:
            patterns = message.get("patterns") or []
            client.patterns = {str(p) for p in patterns if p}
        if "severities" in message:
            severities = message.get("severities") or []
            client.severities = {str(s) for s in severities if s in SEVERITIES}
        log.debug(
            "client subscribed patterns=%s severities=%s",
            sorted(client.patterns),
            sorted(client.severities),
        )
        return

    client.offer(_frame("error", detail=f"unknown frame type: {kind!r}"))
