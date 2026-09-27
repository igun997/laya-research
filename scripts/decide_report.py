#!/usr/bin/env python3
"""Pretty-print one ``GET /api/decide/{product_id}`` response.

    curl -fsS localhost:8090/api/decide/2177 | python3 scripts/decide_report.py
    python3 scripts/decide_report.py 2177 --base http://localhost:8090
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def load(args: argparse.Namespace) -> dict:
    if args.product_id is None:
        raw = sys.stdin.read()
        if not raw.strip():
            raise SystemExit("no product id and nothing on stdin")
        return json.loads(raw)
    url = f"{args.base.rstrip('/')}/api/decide/{args.product_id}"
    if args.day:
        url += f"?day={args.day}"
    with urllib.request.urlopen(url, timeout=args.timeout) as response:
        return json.loads(response.read())


def bar(probability: float, width: int = 22) -> str:
    filled = int(round(probability * width))
    return "#" * filled + "." * (width - filled)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("product_id", nargs="?", type=int, default=None)
    ap.add_argument("--base", default="http://localhost:8090")
    ap.add_argument("--day", default=None)
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    data = load(args)
    state = data["state"]
    rules = data["rules"]
    agreement = data["agreement"]

    print(f"product      : {state['name']}  [{state['category']} / {state['brand']}]")
    print(f"day          : {data['day']}   private label={state['is_private_label']} perishable={state['is_perishable']}")
    print()
    print("state the model was given")
    print(f"  units        : {state['units']} vs baseline {state['units_baseline']}  (lift {state['unit_lift']})")
    print(f"  avg price    : {state['avg_price']} vs baseline {state['price_baseline']}  (lift {state['price_lift']})")
    print(f"  margin       : {state['margin_pct']:.2f}% vs baseline {state['margin_pct_baseline']}")
    print(f"  inventory    : {state['inventory']} units, cover {state['inventory_cover_days']} days")
    print(f"  stores       : {state['store_count']}, promoting {state['promo_stores']}")
    print(f"  observations : {state['observations']}")
    print()

    laya = data.get("laya") or {}
    if not laya.get("available"):
        print(f"laya         : UNAVAILABLE  {laya.get('detail')}")
        print()
    else:
        print(f"laya         : available, {round(laya.get('latency_ms') or 0)} ms, checkpoint "
              f"{(laya.get('routing') or {}).get('model')}")
        print()
        for name, answer in (laya.get("answers") or {}).items():
            probabilities = answer.get("probabilities") or {}
            confidence = answer.get("confidence")
            conf = f"{confidence:.3f}" if isinstance(confidence, (int, float)) else "n/a"
            print(f"  {name}")
            print(f"    answer      : {answer.get('answer')!r}   confidence {conf}")
            if answer.get("score_position") is not None:
                print(f"    score position: {answer['score_position']:.3f} on the rubric")
            for option, probability in sorted(probabilities.items(), key=lambda kv: -kv[1]):
                marker = " <-" if option == answer.get("answer") else ""
                print(f"      {option:22} {probability:6.3f}  {bar(probability)}{marker}")
            print()

    print("rule engine verdict for the identical state")
    print(f"  pattern  : {rules['pattern']}   (all hits: {rules['patterns'] or 'none'})")
    print(f"  severity : {rules['severity']}")
    print(f"  reorder  : {rules['reorder']}")
    print()
    print("agreement")
    for key in ("pattern", "severity", "reorder"):
        flag = agreement.get(key)
        mark = "agree" if flag else ("DIFFER" if flag is False else "n/a")
        print(f"  {key:9}: {mark}")
    print()
    print(f"  laya     : pattern={agreement['laya']['pattern']!r} severity={agreement['laya']['severity']!r} "
          f"P(reorder)={agreement['laya']['reorder_probability']}")
    print(f"  rules    : pattern={agreement['rules']['pattern']!r} severity={agreement['rules']['severity']!r} "
          f"reorder={agreement['rules']['reorder']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
