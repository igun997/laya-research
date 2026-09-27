#!/usr/bin/env python3
"""Latency and correctness probe for Laya on this host.

Three modes, because the first run of this script found the answer was 18 seconds
per call and the cause had to be isolated:

    --mode sdk          one checkpoint via laya.load, torch defaults
    --mode sdk-pinned   same, but intra-op = physical cores and inter-op = 1
    --mode router       Router(preload=True), all checkpoints, torch defaults
    --mode http         talk to a laya-serve already listening on --url

The upstream BENCHMARKS.md documents the pinning effect: torch's defaults (10
intra-op, 5 inter-op threads) gave a 9,396 ms p50 where pinning inter-op to 1 gave
783 ms, a 12x difference with no code change. Router(preload=True) also builds
every checkpoint, and upstream measured 9.3 GiB peak RSS with five resident, which
this host cannot afford.

    docker run --rm -v laya_hf:/models -v $PWD/scripts:/app/scripts:ro \
      --entrypoint python laya-research-laya:dev /app/scripts/laya_probe.py --mode sdk-pinned
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from typing import Any

CHECKPOINT_REPO = "convaiinnovations/laya"
CHECKPOINT_SUBFOLDER = {"english": None, "multilingual": "multilingual", "typed-decisions": "typed-decisions"}

PATTERN_OPTIONS: dict[str, str] = {
    "none": "no decision pattern applies, behaviour is in line with its own baseline",
    "DEMAND_SURGE": "units well above the trailing baseline",
    "DEMAND_COLLAPSE": "units well below the trailing baseline",
    "PRICE_SPIKE": "average price well above the trailing baseline",
    "PRICE_CUT_UNANSWERED": "price cut with no increase in units",
    "MARGIN_SQUEEZE": "margin fell below its baseline and is thin in absolute terms",
    "STOCKOUT_RISK": "inventory cover is below one and a half days of baseline demand",
    "PROMO_INEFFECTIVE": "most stores are promoting but units did not lift",
}
SEVERITY_RUBRIC = ["none", "info", "warn", "critical"]

# Two product-days with known correct answers. The rule engine labels these
# DEMAND_SURGE plus STOCKOUT_RISK at critical, and none/quiet respectively, so a
# disagreement here is a real signal rather than a synthetic edge case.
STATE_QUIET = (
    "Grocery product-day. Product: Cider Barn Fruit Cider 3L. Category: Beverages. "
    "Brand: Cider Barn. Private label: False. Perishable: False. "
    "Units today: 34 vs baseline median 31.00. Average price today: 14.55 vs baseline 14.59. "
    "Margin today: 28.4% vs baseline 28.1%. Inventory cover: 4.20 days. "
    "Stores carrying: 140, of which promoting: 3. Baseline computed from 14 days."
)
STATE_SURGE = (
    "Grocery product-day. Product: Cider Barn Fruit Cider 3L. Category: Beverages. "
    "Brand: Cider Barn. Private label: False. Perishable: False. "
    "Units today: 96 vs baseline median 33.00. Average price today: 14.59 vs baseline 14.58. "
    "Margin today: 27.9% vs baseline 28.2%. Inventory cover: 0.40 days. "
    "Stores carrying: 140, of which promoting: 12. Baseline computed from 14 days."
)
STATES = {"quiet_expected_none": STATE_QUIET, "surge_expected_surge_stockout": STATE_SURGE}


def questions() -> dict[str, Any]:
    return {
        "pattern": {
            "type": "choice",
            "instructions": (
                "Which single decision pattern best describes this grocery "
                "product-day, compared with its own trailing baseline?"
            ),
            "criteria": dict(PATTERN_OPTIONS),
        },
        "severity": {
            "type": "score",
            "instructions": "How severe is the situation for this product-day?",
            "criteria": list(SEVERITY_RUBRIC),
        },
        "reorder_now": {
            "type": "noul",
            "instructions": (
                "Should replenishment be raised for this product today to avoid "
                "running out within the next day?"
            ),
        },
    }


def physical_cores() -> int:
    try:
        import psutil  # not installed by default; fall back below
        return psutil.cpu_count(logical=False) or 4
    except Exception:  # noqa: BLE001
        pass
    try:
        with open("/proc/cpuinfo") as handle:
            pairs = {
                (line.split(":")[1].strip(), None)
                for line in handle
                if line.startswith(("physical id", "core id"))
            }
        # Not a reliable pairing across files; use the count of distinct core ids.
        with open("/proc/cpuinfo") as handle:
            core_ids = {
                line.split(":")[1].strip() for line in handle if line.startswith("core id")
            }
        if core_ids:
            return len(core_ids)
    except Exception:  # noqa: BLE001
        pass
    return max(1, (os.cpu_count() or 4) // 2)


def pin_threads() -> tuple[int, int]:
    import torch

    intra = physical_cores()
    torch.set_num_threads(intra)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError as exc:
        # Can only be set before the first parallel work; report rather than fail.
        print(f"  note: could not pin inter-op threads: {exc}")
    return intra, torch.get_num_interop_threads()


def load_single(checkpoint: str, device: str):
    import laya

    sub = CHECKPOINT_SUBFOLDER.get(checkpoint)
    started = time.monotonic()
    if sub:
        agent = laya.load(CHECKPOINT_REPO, subfolder=sub, device=device)
    else:
        agent = laya.load(CHECKPOINT_REPO, device=device)
    return agent, time.monotonic() - started


def timed_predict(agent, state: str, qs: dict[str, Any], repeat: int) -> list[float]:
    latencies: list[float] = []
    for attempt in range(repeat):
        started = time.monotonic()
        try:
            result = agent.predict(state, qs)
        except TypeError:
            result = agent.predict(state=state, questions=qs)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        latencies.append(elapsed_ms)
        if attempt == 0:
            print(f"  first call {elapsed_ms:.0f} ms")
            for name, answer in (result.get("answers") or {}).items():
                print(f"    {name}: {json.dumps(answer, default=str)}")
        else:
            print(f"  repeat {attempt + 1} {elapsed_ms:.0f} ms")
    return latencies


def http_predict(url: str, state: str, qs: dict[str, Any], repeat: int) -> list[float]:
    import urllib.request

    latencies: list[float] = []
    for attempt in range(repeat):
        payload = json.dumps({"state": state, "questions": qs}).encode()
        request = urllib.request.Request(
            f"{url.rstrip('/')}/v1/systemone",
            data=payload,
            headers={"content-type": "application/json"},
        )
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=600) as response:
            body = json.loads(response.read())
        elapsed_ms = (time.monotonic() - started) * 1000.0
        latencies.append(elapsed_ms)
        if attempt == 0:
            print(f"  first call {elapsed_ms:.0f} ms")
            print(f"    routing: {json.dumps(body.get('routing'), default=str)[:200]}")
            for name, answer in (body.get("answers") or {}).items():
                print(f"    {name}: {json.dumps(answer, default=str)}")
        else:
            print(f"  repeat {attempt + 1} {elapsed_ms:.0f} ms")
    return latencies


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="sdk-pinned", choices=["sdk", "sdk-pinned", "router", "http"])
    ap.add_argument("--checkpoint", default="english")
    ap.add_argument("--device", default=os.environ.get("LAYA_DEVICE", "cpu"))
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--states", type=int, default=2, help="how many states to run")
    args = ap.parse_args()

    import torch

    print(f"python      : {sys.version.split()[0]}")
    print(f"torch       : {torch.__version__}  cuda={torch.cuda.is_available()}")
    print(f"logical cpus: {os.cpu_count()}   OMP_NUM_THREADS={os.environ.get('OMP_NUM_THREADS')}")
    print(f"mode        : {args.mode}   checkpoint={args.checkpoint}   device={args.device}")

    qs = questions()
    latencies: list[float] = []
    agent = None

    if args.mode == "http":
        print(f"target      : {args.url}")
        try:
            import urllib.request

            with urllib.request.urlopen(f"{args.url.rstrip('/')}/health", timeout=30) as r:
                print(f"health      : {r.status} {r.read()[:200]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"health      : FAILED {type(exc).__name__}: {exc}")
            return 1
    elif args.mode == "router":
        from laya import Router

        started = time.monotonic()
        agent = Router(preload=True, device=args.device)
        print(f"load time   : {time.monotonic() - started:.1f}s  (preloads every checkpoint)")
    else:
        import torch as _torch

        print(f"torch intra : {_torch.get_num_threads()}  torch inter: {_torch.get_num_interop_threads()}")
        if args.mode == "sdk-pinned":
            intra, inter = pin_threads()
            print(f"pinned      : intra={intra} inter={inter}")
        agent, load_s = load_single(args.checkpoint, args.device)
        print(f"load time   : {load_s:.1f}s  (single checkpoint)")
        print(f"torch intra : {_torch.get_num_threads()}  torch inter: {_torch.get_num_interop_threads()}")

    for index, (name, state) in enumerate(list(STATES.items())[: args.states]):
        print("\n" + "=" * 78)
        print(f"state: {name}")
        print("-" * 78)
        if args.mode == "http":
            latencies += http_predict(args.url, state, qs, args.repeat)
        else:
            latencies += timed_predict(agent, state, qs, args.repeat)

    print("\n" + "=" * 78)
    if latencies:
        ordered = sorted(latencies)
        p50 = statistics.median(ordered)
        p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
        print(
            f"latency ms  : n={len(ordered)} min={ordered[0]:.0f} p50={p50:.0f} "
            f"p95={p95:.0f} max={ordered[-1]:.0f}"
        )
        print(f"per question: {p50 / 3:.0f} ms p50 (3 questions per call)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
