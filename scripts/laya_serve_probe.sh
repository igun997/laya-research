#!/bin/sh
# Start `laya-serve` inside the laya image and measure it over HTTP.
#
# Needed because the SDK probe pins torch threads by hand, and it is not obvious
# whether `laya-serve` does the same. If it does not, the service would run at
# roughly 18 s per call instead of 8 s, which decides whether the dashboard can
# offer a "Laya decides" panel at all.
#
# Usage (from the repo root):
#   docker run --rm -v laya_hf:/models -v "$PWD/scripts:/app/scripts:ro" \
#     --entrypoint sh laya-research-laya:dev /app/scripts/laya_serve_probe.sh english 3
set -eu

CHECKPOINT="${1:-english}"
REPEAT="${2:-3}"

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"
export LAYA_PRELOAD=1
export LAYA_MODELS="$CHECKPOINT"
export LAYA_LOG_LEVEL=warning

echo "starting laya-serve checkpoint=$CHECKPOINT OMP_NUM_THREADS=$OMP_NUM_THREADS"
laya-serve >/tmp/laya-serve.log 2>&1 &
SERVE_PID=$!

python - <<'PY'
import sys, time, urllib.request

deadline = time.monotonic() + 600
started = time.monotonic()
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3) as response:
            body = response.read().decode()[:200]
        print(f"healthy after {time.monotonic() - started:.0f}s: {response.status} {body}")
        sys.exit(0)
    except Exception:
        time.sleep(2)
print("FAIL: laya-serve never became healthy", file=sys.stderr)
sys.exit(1)
PY

python /app/scripts/laya_probe.py --mode http --repeat "$REPEAT"

echo "--- server log tail ---"
tail -n 15 /tmp/laya-serve.log
kill "$SERVE_PID" 2>/dev/null || true
