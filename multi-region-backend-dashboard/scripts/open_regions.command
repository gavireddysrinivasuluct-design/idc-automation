#!/bin/zsh
# One-click start of the backend regions dashboard (double-click in Finder, or run in Terminal).
# Checks Teleport and nsr, starts the service from this repository if needed, opens the browser.
set -u
cd "$(dirname "$0")/.." || exit 1
PORT="${REGIONS_PORT:-8780}"
URL="http://127.0.0.1:${PORT}/"

NSR="$(command -v nsr || true)"
[[ -z "$NSR" && -x "$HOME/.local/bin/nsr" ]] && NSR="$HOME/.local/bin/nsr"
if [[ -z "$NSR" ]]; then
  echo "nsr is not installed. Install it once with:"
  echo '  uv tool install "git+ssh://git@github.com/nscaledev/netops-misc.git#subdirectory=nsr"'
  exit 1
fi

if command -v tsh >/dev/null 2>&1; then
  if ! tsh status >/dev/null 2>&1; then
    echo "Teleport login needed (nsr reaches Grafana through Teleport)."
    tsh login --proxy=nscale.teleport.sh || exit 1
  fi
else
  echo "tsh not found: install Teleport first (brew install teleport)."; exit 1
fi

echo "Checking Grafana through nsr…"
if ! "$NSR" --json -q grafana query 'vector(1)' >/dev/null 2>/tmp/regions-nsr-check.txt; then
  echo "nsr cannot query Grafana:"; tail -n 3 /tmp/regions-nsr-check.txt
  echo "Run: $NSR doctor"; exit 1
fi

if curl -fsS "${URL}api/health" >/dev/null 2>&1; then
  echo "Already running at ${URL}"
else
  mkdir -p "$HOME/Library/Logs"
  nohup python3 multi_region_dashboard.py --nsr "$NSR" --port "$PORT" --warm ${REGIONS_ARGS:-} \
    > "$HOME/Library/Logs/backend-regions.log" 2>&1 &
  for _ in {1..20}; do curl -fsS "${URL}api/health" >/dev/null 2>&1 && break; sleep 0.3; done
  echo "Started (log: ~/Library/Logs/backend-regions.log). Stop with: pkill -f multi_region_dashboard.py"
fi
open "$URL" 2>/dev/null || echo "Open $URL"
