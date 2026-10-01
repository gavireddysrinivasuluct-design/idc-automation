#!/usr/bin/env bash
# Starts or stops the local Teleport forward and Host-rewrite proxy.
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/netbox-mcp"
tsh_port="${NETBOX_TSH_PORT:-8443}"
proxy_port="${NETBOX_PROXY_PORT:-8444}"
teleport_app="${NETBOX_TELEPORT_APP:-netbox-prod-europe-west2-netbox}"
python_bin="${NETBOX_PROXY_PYTHON:-/usr/bin/python3}"
mkdir -p "$state_dir"

is_running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
start() {
  is_running "$state_dir/tsh.pid" && { echo "Teleport forward is already running."; exit 1; }
  is_running "$state_dir/proxy.pid" && { echo "Host-rewrite proxy is already running."; exit 1; }
  tsh status >/dev/null
  nohup tsh proxy app "$teleport_app" --port "$tsh_port" > "$state_dir/tsh.log" 2>&1 & echo $! > "$state_dir/tsh.pid"
  [ -x "$python_bin" ] || { echo "Python interpreter not found: $python_bin" >&2; exit 2; }
  NETBOX_TSH_PORT="$tsh_port" NETBOX_PROXY_PORT="$proxy_port" nohup "$python_bin" "$script_dir/netbox_host_proxy.py" > "$state_dir/proxy.log" 2>&1 & echo $! > "$state_dir/proxy.pid"
  for _ in {1..20}; do
    if is_running "$state_dir/tsh.pid" && is_running "$state_dir/proxy.pid"; then
      echo "NetBox proxy is available at http://127.0.0.1:$proxy_port/."
      return
    fi
    sleep 0.25
  done
  echo "NetBox proxy did not stay running. Check $state_dir/tsh.log and $state_dir/proxy.log." >&2
  stop
  exit 1
}
stop() {
  for name in proxy tsh; do
    file="$state_dir/$name.pid"
    if is_running "$file"; then kill "$(cat "$file")"; fi
    rm -f "$file"
  done
}
status() {
  for name in tsh proxy; do
    if is_running "$state_dir/$name.pid"; then echo "$name: running (pid $(cat "$state_dir/$name.pid"))"; else echo "$name: stopped"; fi
  done
}
case "${1:-}" in start) start ;; stop) stop ;; status) status ;; *) echo "Usage: $0 {start|stop|status}" >&2; exit 2 ;; esac
