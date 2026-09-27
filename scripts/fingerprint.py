#!/usr/bin/env python3
"""Print, or compare, the dataset fingerprint.

    python3 scripts/fingerprint.py                       # current
    python3 scripts/fingerprint.py --watch 5             # five samples, 5s apart
    python3 scripts/fingerprint.py --expect <md5>        # exit 1 on mismatch
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request


def fingerprint(base: str, day: str | None = None, timeout: float = 120.0) -> dict:
    url = f"{base.rstrip('/')}/api/dataset/fingerprint"
    if day:
        url += f"?day={day}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("LAYA_BASE", "http://localhost:8090"))
    ap.add_argument("--day", default=None)
    ap.add_argument("--expect", default=None, help="exit non-zero unless the fingerprint matches")
    ap.add_argument("--watch", type=int, default=0, help="sample N times to show it moving")
    args = ap.parse_args()

    samples = []
    rounds = max(1, args.watch)
    for index in range(rounds):
        try:
            data = fingerprint(args.base, args.day)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        line = f"{data['day']}  facts={data['facts']:,}  md5={data['fingerprint']}"
        marker = ""
        if samples and data["fingerprint"] != samples[-1]["fingerprint"]:
            marker = "   <- changed"
        print(f"  {line}{marker}")
        samples.append(data)
        if index + 1 < rounds:
            time.sleep(5)

    if args.watch and len({s["fingerprint"] for s in samples}) == 1:
        print(f"  (stable across {rounds} samples)")
    if args.watch and len({s["fingerprint"] for s in samples}) > 1:
        print(f"  ({len({s['fingerprint'] for s in samples})} distinct fingerprints: the data moved)")

    if args.expect:
        actual = samples[-1]["fingerprint"]
        if actual != args.expect:
            print(f"FAIL: expected {args.expect}, got {actual}", file=sys.stderr)
            return 1
        print("  fingerprint matches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
