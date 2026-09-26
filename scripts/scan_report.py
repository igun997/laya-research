#!/usr/bin/env python3
"""Reads POST /api/patterns/scan output on stdin and prints a readable report.

    curl -fsS -X POST localhost:8090/api/patterns/scan \
      -H 'content-type: application/json' -d '{"persist":true}' \
      | python3 scripts/scan_report.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter


def main() -> int:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"not JSON ({len(raw)} bytes): {raw[:500]}", file=sys.stderr)
        return 1

    signals = data.get("signals", [])
    print(f"scanned {data.get('scanned')} subject-days -> {len(signals)} signals")

    by_pattern = Counter(s.get("pattern") for s in signals)
    by_severity = Counter(s.get("severity") for s in signals)
    if by_pattern:
        print("\nby pattern : " + ", ".join(f"{k}={v}" for k, v in by_pattern.most_common()))
        print("by severity: " + ", ".join(f"{k}={v}" for k, v in by_severity.most_common()))

    critical = [s for s in signals if s.get("severity") == "critical"]
    rest = [s for s in signals if s.get("severity") != "critical"]
    for title, group in (("critical", critical[:15]), ("warn/info", rest[:15])):
        if not group:
            continue
        print(f"\n-- {title} --")
        for s in group:
            print(
                f"{s.get('severity', '?'):8} {s.get('pattern', '?'):22} "
                f"{str(s.get('subject_label', ''))[:32]:32} score={s.get('score')}"
            )
            print(f"         day={s.get('day')} subject={s.get('subject_type')}:{s.get('subject_id')}")
            print(f"         evidence={json.dumps(s.get('evidence'), sort_keys=True)[:200]}")
            print(f"         action  ={s.get('action')}")
    if not signals:
        print("no signals fired", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
