#!/usr/bin/env python3
"""Minimal dependency-free RFC6455 client for GET /api/stream.

Used as an end-to-end probe: it must receive `hello` first, then a `tick` frame
every LAYA_TICK_SECONDS, and a `signal` frame whenever the rule engine fires.

    python3 scripts/ws_probe.py [--url ws://localhost:8090/api/stream] [--seconds 20]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
from urllib.parse import urlparse


def handshake(host: str, port: int, path: str, timeout: float) -> tuple[socket.socket, bytes]:
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.sendall(
        (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("connection closed during handshake")
        buf += chunk
    head, rest = buf.split(b"\r\n\r\n", 1)
    status = head.split(b"\r\n", 1)[0].decode()
    if "101" not in status:
        raise RuntimeError(f"upgrade failed: {status}")
    return sock, rest


def frames(sock: socket.socket, carry: bytes):
    """Yield text payloads. Server sends unmasked frames; client must not mask either."""
    while True:
        while len(carry) < 2:
            carry += sock.recv(65536)
        fin_opcode, b2 = carry[0], carry[1]
        length, offset = b2 & 0x7F, 2
        if length == 126:
            while len(carry) < 4:
                carry += sock.recv(65536)
            (length,) = struct.unpack(">H", carry[2:4])
            offset = 4
        elif length == 127:
            while len(carry) < 10:
                carry += sock.recv(65536)
            (length,) = struct.unpack(">Q", carry[2:10])
            offset = 10
        while len(carry) < offset + length:
            carry += sock.recv(65536)
        payload, carry = carry[offset : offset + length], carry[offset + length :]
        opcode = fin_opcode & 0x0F
        if opcode == 0x1:
            yield payload.decode()
        elif opcode == 0x8:
            return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("LAYA_WS", "ws://localhost:8090/api/stream"))
    ap.add_argument("--seconds", type=float, default=20.0)
    args = ap.parse_args()

    parsed = urlparse(args.url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/api/stream"

    sock, carry = handshake(host, port, path, timeout=10)
    sock.settimeout(args.seconds)

    counts: dict[str, int] = {}
    saw_hello = False
    saw_tick = False
    signals: list[dict] = []
    deadline = time.monotonic() + args.seconds

    try:
        for raw in frames(sock, carry):
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                print(f"  !! non-json frame: {raw[:120]}", file=sys.stderr)
                continue
            kind = msg.get("type", "?")
            counts[kind] = counts.get(kind, 0) + 1

            if kind == "hello":
                saw_hello = True
                print(f"hello  dataset={json.dumps(msg.get('dataset'))[:200]}")
            elif kind == "tick":
                saw_tick = True
                s = msg.get("summary", {})
                print(
                    f"tick   #{s.get('tick')}  rows={s.get('rows')}  "
                    f"products={s.get('products')}  stores={s.get('stores')}"
                )
            elif kind == "signal":
                sig = msg.get("signal", {})
                signals.append(sig)
                print(
                    f"  SIG  {sig.get('severity','?'):8} {sig.get('pattern',''):22} "
                    f"{str(sig.get('subject_label',''))[:30]:30} score={sig.get('score')}"
                )
            elif kind == "heartbeat":
                pass
            else:
                print(f"{kind:9} {raw[:180]}")

            if time.monotonic() >= deadline:
                break
    except (socket.timeout, TimeoutError):
        pass
    finally:
        sock.close()

    print(f"\ncounts={counts} signals={len(signals)}")
    if not saw_hello:
        print("FAIL: no hello frame", file=sys.stderr)
        return 1
    if not saw_tick:
        print("FAIL: no tick frame (is sim publishing?)", file=sys.stderr)
        return 1
    print("OK: hello + tick observed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
