#!/bin/zsh
# One-click start of the single backend fabric dashboard: every region on http://127.0.0.1:8765/
#   * ICE2 and LON14 run their own engines behind it (/ice2/, /lon14/); they are started by it.
#   * OSL1, STA1, GLO1: NetBox first (direct), UFM direct, metrics through nsr.
# Extra options: FABRIC_ARGS="--cache-seconds 120" ./scripts/open_regions.command
set -u
cd "$(dirname "$0")/.." || exit 1
repo="$(cd .. && pwd)"
PORT="${FABRIC_PORT:-8765}"
URL="http://127.0.0.1:${PORT}/"
profile="${IDC_DEVICE_PROFILE:-${XDG_CONFIG_HOME:-$HOME/.config}/idc-automation/device-access.ini}"

command -v tsh >/dev/null 2>&1 || { echo "tsh not found: brew install teleport"; exit 1; }
tsh status >/dev/null 2>&1 || { echo "Teleport login needed."; tsh login --proxy=nscale.teleport.sh || exit 1; }

# NetBox first: the same local proxy and Keychain token as the ICE2 dashboard.
if ! "$repo/ice2-netbox-dashboard/scripts/netbox_proxy.sh" status 2>/dev/null | grep -q "running"; then
  "$repo/ice2-netbox-dashboard/scripts/netbox_proxy.sh" start || echo "NetBox proxy did not start: NetBox snapshots will fail until it does."
fi

NSR="$(command -v nsr || true)"; [[ -z "$NSR" && -x "$HOME/.local/bin/nsr" ]] && NSR="$HOME/.local/bin/nsr"
if [[ -z "$NSR" ]]; then
  echo "nsr not found: metrics, logs and incidents will be empty (topology still works)."
  echo '  install: uv tool install "git+ssh://git@github.com/nscaledev/netops-misc.git#subdirectory=nsr"'
  NSR=nsr
fi

# Free the ports the single service and its engines use. The old standalone dashboards
# (ICE2 on 8765, LON14 on 8766/8767, regions on 8780) are replaced by this one service.
for p in $PORT 8780 8766; do
  pids=$(lsof -nP -tiTCP:$p -sTCP:LISTEN 2>/dev/null || true)
  if [[ -n "$pids" ]]; then
    if curl -fsS "http://127.0.0.1:$p/api/config" 2>/dev/null | grep -q '"regions"' && [[ "$p" == "$PORT" ]]; then
      echo "Restarting the running fabric dashboard on $p."
    else
      echo "Port $p is used by: $(ps -o command= -p ${pids%%$'\n'*} 2>/dev/null | cut -c1-120)"
      read -q "ans?Stop it so the single service can take over? [y/N] " || { echo; echo "Left running; not starting."; exit 1; }
      echo
    fi
    kill ${=pids} 2>/dev/null; sleep 1
  fi
done

# Engines from an earlier run of this service (internal ports): restart them with the current code.
for p in 18765 18766 8767; do
  pids=$(lsof -nP -tiTCP:$p -sTCP:LISTEN 2>/dev/null || true)
  [[ -n "$pids" ]] && ps -o command= -p ${pids%%$'\n'*} 2>/dev/null | grep -q netbox_live_sync.py && kill ${=pids} 2>/dev/null
done
sleep 0.5

mkdir -p "$HOME/Library/Logs"
nohup python3 multi_region_dashboard.py --port "$PORT" --nsr "$NSR" --device-profile "$profile" --warm ${=FABRIC_ARGS:-} \
  > "$HOME/Library/Logs/backend-fabric-dashboard.log" 2>&1 &
for _ in {1..30}; do curl -fsS "${URL}api/health" >/dev/null 2>&1 && break; sleep 0.3; done
echo "Backend fabric dashboard: $URL  (log: ~/Library/Logs/backend-fabric-dashboard.log; engine logs in cache/)"
echo "Stop everything: pkill -f multi_region_dashboard.py; pkill -f netbox_live_sync.py"
open "$URL" 2>/dev/null || true
