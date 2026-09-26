#!/usr/bin/env bash
# End-to-end acceptance for the laya-research stack.
# Requires a running stack: `make up` (or run with --up to build+start).
#
#   scripts/smoke.sh            # verify the running stack
#   scripts/smoke.sh --up       # docker compose up --build -d first
#   scripts/smoke.sh --fast     # skip the realtime mutation window
set -uo pipefail

PORT="${PORT:-8090}"
BASE="http://localhost:${PORT}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
FAIL=0

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
info() { printf '\n\033[36m== %s\033[0m\n' "$1"; }

# jq-free json probe: prints a python-evaluated expression, or "" on failure
probe() { # probe <json> <python-expression-over-d>
  python3 -c '
import json,sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print(""); sys.exit(0)
try:
    v = eval(sys.argv[1], {"d": d, "len": len, "sum": sum, "round": round, "max": max, "min": min})
except Exception as e:
    print(f"__eval_error__:{e}"); sys.exit(0)
if isinstance(v,float): v = round(v,6)
print(v)
' "$2" <<<"$1"
}

get() { curl -fsS --max-time 30 "$1"; }

if [[ "${1:-}" == "--up" ]]; then
  info "docker compose up --build -d"
  (cd "$ROOT" && docker compose up --build -d)
fi
FAST=0
[[ "${1:-}" == "--fast" || "${2:-}" == "--fast" ]] && FAST=1

info "containers"
(cd "$ROOT" && docker compose ps --format 'table {{.Service}}\t{{.Status}}' 2>/dev/null) | sed 's/^/  /'

info "waiting for the edge"
for i in $(seq 1 90); do
  if curl -fsS --max-time 3 "$BASE/api/health" >/dev/null 2>&1; then ok "edge reachable after ${i}s"; break; fi
  sleep 1
  [[ $i -eq 90 ]] && { bad "edge never became reachable at $BASE"; exit 1; }
done

info "GET /api/health"
H=$(get "$BASE/api/health")
[[ "$(probe "$H" 'd["status"]')" == "ok" ]] && ok "status=ok" || bad "status != ok :: $H"
[[ "$(probe "$H" 'd["db"]')" == "True" ]] && ok "db=true" || bad "db probe failed :: $H"

info "GET /api/meta  (datasheet)"
M=$(get "$BASE/api/meta")
FACTS=$(probe "$M" 'd["dataset"]["facts"]')
PRODS=$(probe "$M" 'd["dataset"]["products"]')
STORES=$(probe "$M" 'd["dataset"]["stores"]')
DMIN=$(probe "$M" 'd["dataset"]["day_min"]')
DMAX=$(probe "$M" 'd["dataset"]["day_max"]')
CATS=$(probe "$M" 'len(d["categories"])')
BRANDS=$(probe "$M" 'len(d["brands"])')
echo "  facts=$FACTS products=$PRODS stores=$STORES days=$DMIN..$DMAX categories=$CATS brands=$BRANDS"
[[ "$FACTS" =~ ^[0-9]+$ && "$FACTS" -gt 1000000 ]] && ok "datasheet is >1M rows" || bad "datasheet too small: $FACTS"
[[ "$CATS" =~ ^[0-9]+$ && "$CATS" -ge 10 ]] && ok ">=10 categories" || bad "categories=$CATS"
[[ "$BRANDS" =~ ^[0-9]+$ && "$BRANDS" -ge 250 ]] && ok ">=250 brands" || bad "brands=$BRANDS"
[[ -n "$DMIN" && "$DMIN" != "None" ]] && ok "day range present" || bad "no day range"

info "GET /api/products?q=  (full-text search)"
P=$(get "$BASE/api/products?q=milk&limit=5")
TOT=$(probe "$P" 'd["total"]')
HITS=$(probe "$P" 'len(d["items"])')
[[ "$TOT" =~ ^[0-9]+$ && "$TOT" -gt 0 ]] && ok "tsquery matched $TOT products" || bad "no product matches for 'milk' :: $(head -c 200 <<<"$P")"
[[ "$HITS" -gt 0 ]] && ok "returned $HITS rows" || bad "empty items"

info "GET /api/products  (punctuation-only input must not 5xx)"
CODE=$(curl -s -o /tmp/laya_punct.json -w '%{http_code}' --max-time 20 --get --data-urlencode 'q=!@#$%^&*()' "$BASE/api/products")
[[ "$CODE" == "200" ]] && ok "status 200 for punctuation-only q" || bad "status $CODE :: $(head -c 200 /tmp/laya_punct.json)"

info "GET /api/search  (fact-grain search + totals)"
S=$(get "$BASE/api/search?category=Produce&promo=false&sort=revenue&limit=10")
STOT=$(probe "$S" 'd["total"]')
SROWS=$(probe "$S" 'len(d["items"])')
SREV=$(probe "$S" 'd["totals"]["revenue"]')
echo "  total=$STOT rows=$SROWS total_revenue=$SREV"
[[ "$STOT" =~ ^[0-9]+$ && "$STOT" -gt 0 ]] && ok "filtered fact rows: $STOT" || bad "no fact rows"
[[ "$SROWS" -le 10 ]] && ok "limit respected ($SROWS <= 10)" || bad "limit ignored: $SROWS"
FIRST=$(probe "$S" 'sorted(d["items"], key=lambda r: -r["revenue"])[0]["revenue"] == d["items"][0]["revenue"]')
[[ "$FIRST" == "True" ]] && ok "sort=revenue descending" || bad "sort not applied"
[[ -n "$(probe "$S" 'd["items"][0]["store_name"]')" ]] && ok "store joined" || bad "store_name missing"
[[ -n "$(probe "$S" 'd["items"][0]["margin_pct"]')" ]] && ok "margin_pct present" || bad "margin_pct missing"

info "GET /api/overview"
O=$(get "$BASE/api/overview?days=14")
DAYS=$(probe "$O" 'len(d["days"])')
MOVERS=$(probe "$O" 'len(d["movers"])')
echo "  days=$DAYS categories=$(probe "$O" 'len(d["categories"])') movers=$MOVERS rollups_as_of=$(probe "$O" 'd["rollups_as_of"]')"
[[ "$DAYS" -gt 0 ]] && ok "series returned" || bad "empty day series"
[[ "$MOVERS" -gt 0 ]] && ok "movers returned" || bad "no movers"

info "GET /api/patterns  (rule catalog)"
R=$(get "$BASE/api/patterns")
N=$(probe "$R" 'len(d["patterns"])')
echo "  patterns: $(probe "$R" '",".join(p["id"] for p in d["patterns"])')"
[[ "$N" == "9" ]] && ok "9 rules exposed" || bad "expected 9 rules, got $N"
[[ "$(probe "$R" 'all("thresholds" in p and "severity_bands" in p and "action_template" in p for p in d["patterns"])')" == "True" ]] \
  && ok "every rule exposes thresholds + severity_bands + action_template" \
  || bad "rule catalog incomplete"

info "POST /api/patterns/scan  (persist)"
SC=$(curl -fsS --max-time 180 -X POST "$BASE/api/patterns/scan" \
      -H 'content-type: application/json' -d '{"persist":true}')
NSC=$(probe "$SC" 'd["scanned"]')
NSIG=$(probe "$SC" 'len(d["signals"])')
echo "  scanned=$NSC signals=$NSIG"
[[ "$NSC" =~ ^[0-9]+$ && "$NSC" -gt 0 ]] && ok "scanned subjects" || bad "scan produced nothing :: $(head -c 300 <<<"$SC")"
[[ "$NSIG" -gt 0 ]] && ok "$NSIG signals fired" || bad "no signals fired — rule thresholds unreachable on generated data"
[[ "$(probe "$SC" 'all(s.get("action") and s.get("evidence") and s.get("severity") in ("info","warn","critical") for s in d["signals"])')" == "True" ]] \
  && ok "every signal has action + evidence + valid severity" || bad "signal payload incomplete"

info "GET /api/signals"
G=$(get "$BASE/api/signals?limit=20")
echo "  items=$(probe "$G" 'len(d["items"])') counts=$(probe "$G" 'd["counts"]')"
[[ "$(probe "$G" 'len(d["items"])')" -gt 0 ]] && ok "signals history readable" || bad "signals table empty after scan"

info "GET /api/products/{id}/series"
PID=$(probe "$P" 'd["items"][0]["product_id"]')
SER=$(get "$BASE/api/products/$PID/series?days=30")
[[ "$(probe "$SER" 'len(d["series"])')" -gt 0 ]] && ok "product $PID series has $(probe "$SER" 'len(d["series"])') points" || bad "empty product series"

info "web through the edge"
WCODE=$(curl -s -o /tmp/laya_web.html -w '%{http_code}' --max-time 20 "$BASE/")
[[ "$WCODE" == "200" ]] && ok "GET / -> 200" || bad "GET / -> $WCODE"
grep -qi 'sveltekit' /tmp/laya_web.html && ok "SvelteKit app shell served" || bad "no SvelteKit markers in /"

if [[ "$FAST" == "0" ]]; then
  info "realtime: websocket + live mutation"
  SNIPE=$(curl -fsS --max-time 20 "$BASE/api/search?limit=1&sort=revenue" | python3 -c '
import json,sys
d=json.load(sys.stdin)["items"][0]
print(d["store_id"], d["product_id"], d["units_sold"], d["price"])')
  read -r SID PID2 UNITS0 PRICE0 <<<"$SNIPE"
  echo "  watching store=$SID product=$PID2 units=$UNITS0 price=$PRICE0"
  WS=$(LAYA_WS="ws://localhost:${PORT}/api/stream" python3 "$ROOT/scripts/ws_probe.py" --seconds 22 2>&1)
  echo "$WS" | grep -q '^hello' && ok "received hello frame" || bad "no hello frame"
  echo "$WS" | grep -q '^tick' && ok "received tick frame(s): $(grep -c '^tick' <<<"$WS")" || bad "no tick frame — sim not publishing"
  if echo "$WS" | grep -q '  SIG'; then ok "received live signal frame(s): $(grep -c '  SIG' <<<"$WS")"; else
    echo "  \033[33mNOTE\033[0m no signal frame in the 22s window (tick rate/thresholds); scan still proved the engine"
  fi
  AFTER=$(curl -fsS --max-time 20 "$BASE/api/search?store_id=$SID&product_id=$PID2&limit=1" | python3 -c '
import json,sys
i=json.load(sys.stdin)["items"]
print((i[0]["units_sold"], i[0]["price"]) if i else ("?","?"))')
  echo "  after: units/price = $AFTER  (before: $UNITS0 $PRICE0)"
  [[ "$AFTER" != "('?'"* ]] && ok "row still readable" || bad "row vanished"
fi

info "results"
printf '  \033[32m%d passed\033[0m  \033[31m%d failed\033[0m\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
