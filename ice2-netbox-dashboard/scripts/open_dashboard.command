#!/bin/zsh
# Start the ICE2 dashboard from THIS repository and open it. Double-click in Finder,
# or run ./scripts/open_dashboard.command. Safe to run again: it reuses a running
# service from this repository and refuses to silently reuse an older copy.
#
# Extra service options: ICE2_DASHBOARD_ARGS="--sync-every-minutes 15" ./scripts/open_dashboard.command
set -euo pipefail

dashboard_dir=${0:A:h:h}
url="http://127.0.0.1:8765"
profile="${IDC_DEVICE_PROFILE:-${XDG_CONFIG_HOME:-$HOME/.config}/idc-automation/device-access.ini}"
log_file="$HOME/Library/Logs/ice2-dashboard.log"
cd "$dashboard_dir"

running_here() {  # a service answers and it is this repository's version (it reports evidence freshness)
  curl --silent --fail --max-time 2 "$url/api/live" 2>/dev/null | grep -q '"freshness"'
}

if curl --silent --max-time 1 -o /dev/null "$url/" 2>/dev/null; then
  if running_here; then
    echo "The dashboard from this repository is already running."
    open "$url/"
    exit 0
  fi
  pids=$(lsof -nP -tiTCP:8765 -sTCP:LISTEN 2>/dev/null || true)
  echo "Port 8765 is used by an OLDER dashboard (not this repository):"
  [ -n "$pids" ] && ps -o pid=,command= -p ${(f)pids} 2>/dev/null | sed 's/^/  /'
  if read -q "?Stop it and start this repository's version? [y/N] "; then
    echo
    [ -n "$pids" ] && kill ${(f)pids} 2>/dev/null || true
    for _ in {1..20}; do curl --silent --max-time 1 -o /dev/null "$url/" 2>/dev/null || break; sleep 0.25; done
  else
    echo; echo "Left the older dashboard running. Stop it, then run this again."; exit 1
  fi
fi

tsh status >/dev/null 2>&1 || { echo "Teleport login expired. Run: tsh login"; exit 1; }
if ! curl --silent --max-time 2 -o /dev/null "http://127.0.0.1:8444/" 2>/dev/null; then
  echo "Starting the local NetBox proxy…"
  ./scripts/netbox_proxy.sh stop >/dev/null 2>&1 || true
  ./scripts/netbox_proxy.sh start
fi
[ -f "$profile" ] || { echo "No device profile at $profile. Run ./scripts/configure_device_access.sh first."; exit 1; }

mkdir -p "${log_file:h}"
echo "Starting the dashboard (log: $log_file)…"
nohup python3 app/netbox_live_sync.py --netbox-url 'http://127.0.0.1:8444' --device-profile "$profile" \
  --fanout jump --device-parallel 15 ${=ICE2_DASHBOARD_ARGS:-} >>"$log_file" 2>&1 &
for _ in {1..40}; do running_here && break; sleep 0.25; done
running_here || { echo "The dashboard did not start. Last log lines:"; tail -n 15 "$log_file"; exit 1; }
echo "Running from $dashboard_dir ($(git -C "$dashboard_dir" log -1 --format='%h %s' 2>/dev/null || echo 'no git'))."
open "$url/"
