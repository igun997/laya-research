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

info "GET /api/dataset/fingerprint  (frozen-experiment guard)"
FP=$(get "$BASE/api/dataset/fingerprint")
FPDAY=$(probe "$FP" 'd["day"]')
FPN=$(probe "$FP" 'd["facts"]')
FPHASH=$(probe "$FP" 'd["fingerprint"]')
echo "  $FPDAY facts=$FPN md5=$FPHASH"
[[ "${#FPHASH}" == "32" ]] && ok "md5 fingerprint issued" || bad "fingerprint malformed :: $FP"
[[ "$FPN" =~ ^[0-9]+$ && "$FPN" -gt 0 ]] && ok "fingerprint covers $FPN facts" || bad "no facts hashed"

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

info "GET /api/laya/health  (decision model)"
LH=$(get "$BASE/api/laya/health")
[[ "$(probe "$LH" 'd["enabled"]')" == "True" ]] && ok "laya integration enabled" || bad "laya disabled :: $LH"
if [[ "$(probe "$LH" 'd["available"]')" == "True" ]]; then
  ok "model loaded: $(probe "$LH" 'd["detail"]["loaded"]') on $(probe "$LH" 'd["detail"]["device"]')"
  LAYA_UP=1
else
  printf '  \033[33mNOTE\033[0m model not warm yet, skipping the decide checks: %s\n' "$(head -c 160 <<<"$LH")"
  LAYA_UP=0
fi

info "GET /api/laya/questions  (frozen question schema)"
Q=$(get "$BASE/api/laya/questions")
NO=$(probe "$Q" 'len(d["pattern_options"])')
[[ "$NO" == "8" ]] && ok "8 choice options (none + 7 product-scope patterns)" || bad "expected 8 choice options, got $NO"
[[ "$(probe "$Q" 'd["category_scope_patterns"] == ["CATEGORY_DRIFT", "PRIVATE_LABEL_GAIN"]')" == "True" ]] \
  && ok "category-scope patterns correctly excluded from a product question" \
  || bad "category-scope patterns leaked into the option set"
[[ "$(probe "$Q" 'd["severity_rubric"] == ["none", "info", "warn", "critical"]')" == "True" ]] \
  && ok "severity rubric is ordinal and ordered" || bad "severity rubric wrong"
[[ "$(probe "$Q" 'sorted(d["questions"])')" == "['pattern', 'reorder_now', 'severity']" ]] \
  && ok "one question per primitive: choice, score, noul" || bad "question set incomplete"

if [[ "$LAYA_UP" == "1" ]]; then
  info "GET /api/decide/{id}  (one CPU forward pass, ~6-15s)"
  D=$(curl -fsS --max-time 180 "$BASE/api/decide/$PID")
  [[ "$(probe "$D" 'd["laya"]["available"]')" == "True" ]] && ok "model answered" || bad "model reported unavailable :: $(head -c 200 <<<"$D")"
  [[ "$(probe "$D" 'sorted(d["laya"]["answers"])')" == "['pattern', 'reorder_now', 'severity']" ]] \
    && ok "all three primitives answered in one pass" || bad "missing primitives"
  [[ "$(probe "$D" 'all(a.get("probabilities") for k,a in d["laya"]["answers"].items() if k != "reorder_now")')" == "True" ]] \
    && ok "choice and score returned probability distributions" || bad "distributions missing"
  [[ "$(probe "$D" '0.0 <= d["laya"]["answers"]["reorder_now"]["answer"] <= 1.0')" == "True" ]] \
    && ok "noul returned a probability in [0,1]" || bad "noul out of range"
  [[ "$(probe "$D" '{"pattern","severity","reorder"} <= set(d["agreement"])')" == "True" ]] \
    && ok "agreement reported for all three questions" || bad "agreement block incomplete: $(probe "$D" 'sorted(d["agreement"])')"
  [[ "$(probe "$D" 'len(d["state_text"]) > 100')" == "True" ]] \
    && ok "state rendering returned for inspection" || bad "state_text missing"
  echo "  laya=$(probe "$D" 'd["agreement"]["laya"]')"
  echo "  rules=$(probe "$D" 'd["agreement"]["rules"]')"
fi

info "web through the edge"
WCODE=$(curl -s -o /tmp/laya_web.html -w '%{http_code}' --max-time 20 "$BASE/")
[[ "$WCODE" == "200" ]] && ok "GET / -> 200" || bad "GET / -> $WCODE"
grep -qi 'sveltekit' /tmp/laya_web.html && ok "SvelteKit app shell served" || bad "no SvelteKit markers in /"

if [[ "$FAST" == "0" ]]; then
  info "realtime: websocket + live mutation"
  # Proof that the data actually moved, chosen so it cannot pass by luck. The
  # simulator rewrites LAYA_TICK_ROWS rows every LAYA_TICK_SECONDS, so the
  # aggregate over the newest day must change. An earlier version watched a single
  # fact row, which failed to change on some runs purely because 250 of 336,000
  # rows per tick rarely include one particular row.
  DAY=$(probe "$M" 'd["dataset"]["day_max"]')
  SCOPE="date_from=$DAY&date_to=$DAY&limit=1"
  U0=$(probe "$(curl -fsS --max-time 30 "$BASE/api/search?$SCOPE")" 'd["totals"]["units"]')
  WS=$(LAYA_WS="ws://localhost:${PORT}/api/stream" python3 "$ROOT/scripts/ws_probe.py" --seconds 22 2>&1)
  echo "$WS" | grep -q '^hello' && ok "received hello frame" || bad "no hello frame"
  echo "$WS" | grep -q '^tick' && ok "received tick frame(s): $(grep -c '^tick' <<<"$WS")" || bad "no tick frame — sim not publishing"
  if echo "$WS" | grep -q '  SIG'; then ok "received live signal frame(s): $(grep -c '  SIG' <<<"$WS")"; else
    echo "  \033[33mNOTE\033[0m no signal frame in the 22s window (tick rate/thresholds); scan still proved the engine"
  fi
  U1=$(probe "$(curl -fsS --max-time 30 "$BASE/api/search?$SCOPE")" 'd["totals"]["units"]')
  echo "  units on $DAY: $U0 -> $U1"
  if [[ "$U0" =~ ^[0-9]+$ && "$U1" =~ ^[0-9]+$ && "$U0" != "$U1" ]]; then
    ok "facts actually changed while watching (not just frames received)"
  else
    bad "aggregate over $DAY did not move: $U0 -> $U1"
  fi
fi

info "results"
printf '  \033[32m%d passed\033[0m  \033[31m%d failed\033[0m\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
