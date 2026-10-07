#!/bin/zsh
# Start the LON14 dashboard from THIS repository and open it. Double-click in Finder,
# or run ./scripts/open_dashboard.command. Safe to run again: it reuses a running
# service from this repository and refuses to silently reuse an older copy.
#
# Extra service options: LON14_DASHBOARD_ARGS="--sync-every-minutes 15" ./scripts/open_dashboard.command
# It also starts the SYS2 (Ethernet) service on port 8767; its extra options: LON14_SYS2_ARGS="..."
set -euo pipefail

dashboard_dir=${0:A:h:h}
url="http://127.0.0.1:8766"
profile="${IDC_DEVICE_PROFILE:-${XDG_CONFIG_HOME:-$HOME/.config}/idc-automation/device-access-lon14.ini}"
log_file="$HOME/Library/Logs/lon14-dashboard.log"
cd "$dashboard_dir"

running_here() {  # a service answers and it is this repository's version (it reports evidence freshness)
  curl --silent --fail --max-time 2 "$url/api/live" 2>/dev/null | grep -q '"freshness"'
}

stop_port() {
  local pids; pids=$(lsof -nP -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
  [ -n "$pids" ] && kill ${(f)pids} 2>/dev/null || true
  for _ in {1..20}; do curl --silent --max-time 1 -o /dev/null "$url/" 2>/dev/null || break; sleep 0.25; done
}

start_sys2() {
  # The SYS2 tab (Ethernet backend, SN5610) is served by a second service on 8767 from the same code.
  sys2_url="http://127.0.0.1:8767"
  sys2_log="$HOME/Library/Logs/lon14-sys2-dashboard.log"
  local want2 have2 pids2; want2=$(python3 app/netbox_live_sync.py --code-version 2>/dev/null || echo unknown)
  have2=$(curl --silent --max-time 2 "$sys2_url/api/live" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("code_version","none"))' 2>/dev/null || echo none)
  if [ "$have2" != "$want2" ]; then
    pids2=$(lsof -nP -tiTCP:8767 -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "$pids2" ]; then
      if ps -o command= -p ${(f)pids2} 2>/dev/null | grep -q -- "--fabric sys2"; then
        echo "Restarting the SYS2 service (code $have2, now $want2)."; kill ${(f)pids2} 2>/dev/null || true; sleep 1
      else
        echo "Port 8767 is used by another program; the SYS2 tab will show the static NetBox view."; pids2="busy"
      fi
    fi
    if [ "$pids2" != "busy" ]; then
      echo "Starting the SYS2 service (log: $sys2_log)…"
      nohup python3 app/netbox_live_sync.py --fabric sys2 --netbox-url 'http://127.0.0.1:8444' --device-profile "$profile" \
        --fanout jump --device-parallel 20 ${=LON14_SYS2_ARGS:-} >>"$sys2_log" 2>&1 &
      for _ in {1..40}; do curl --silent --fail --max-time 1 -o /dev/null "$sys2_url/api/health" 2>/dev/null && break; sleep 0.25; done
      curl --silent --fail --max-time 1 -o /dev/null "$sys2_url/api/health" 2>/dev/null || { echo "The SYS2 service did not start (the SYS1 dashboard is unaffected). Last log lines:"; tail -n 8 "$sys2_log"; }
    fi
  fi
  [ -f local-inputs/sys2/known_hosts ] || echo "SYS2 switch SSH sync needs host keys: ./scripts/configure_known_hosts.sh --fabric sys2 --collect-live (see README section 15)."
}

if curl --silent --max-time 1 -o /dev/null "$url/" 2>/dev/null; then
  if running_here; then
    want=$(python3 app/netbox_live_sync.py --code-version 2>/dev/null || echo unknown)
    have=$(curl --silent --max-time 2 "$url/api/live" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("code_version","none"))' 2>/dev/null || echo none)
    if [ "$want" = "$have" ]; then
      echo "The dashboard from this repository is already running (code $have)."
      start_sys2
      open "$url/"
      exit 0
    fi
    echo "The running dashboard is an older version of this repository (code $have, now $want): restarting it."
    stop_port
  fi
fi
if curl --silent --max-time 1 -o /dev/null "$url/" 2>/dev/null; then
  pids=$(lsof -nP -tiTCP:8766 -sTCP:LISTEN 2>/dev/null || true)
  echo "Port 8766 is used by an OLDER dashboard (not this repository):"
  [ -n "$pids" ] && ps -o pid=,command= -p ${(f)pids} 2>/dev/null | sed 's/^/  /'
  if read -q "?Stop it and start this repository's version? [y/N] "; then
    echo
    stop_port
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
  --fanout jump --device-parallel 15 ${=LON14_DASHBOARD_ARGS:-} >>"$log_file" 2>&1 &
for _ in {1..40}; do running_here && break; sleep 0.25; done
running_here || { echo "The dashboard did not start. Last log lines:"; tail -n 15 "$log_file"; exit 1; }
echo "Running from $dashboard_dir ($(git -C "$dashboard_dir" log -1 --format='%h %s' 2>/dev/null || echo 'no git')), code $(python3 app/netbox_live_sync.py --code-version 2>/dev/null)."

start_sys2
open "$url/"
