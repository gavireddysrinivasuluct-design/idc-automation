#!/usr/bin/env bash
# Starts or stops the local Teleport forward and Host-rewrite proxy.
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/netbox-mcp"
tsh_port="${NETBOX_TSH_PORT:-8443}"
proxy_port="${NETBOX_PROXY_PORT:-8444}"
# The Teleport app that fronts NetBox (the same global NetBox serves ICE2 and LON14). Override with NETBOX_TELEPORT_APP, or set it
# once in ~/.config/idc-automation/netbox.env (NETBOX_TELEPORT_APP=...). The Host header is
# the app's public address as Teleport reports it (tsh apps ls), unless NETBOX_HOST_HEADER is set.
env_file="${XDG_CONFIG_HOME:-$HOME/.config}/idc-automation/netbox.env"
[ -f "$env_file" ] && . "$env_file"
teleport_app="${NETBOX_TELEPORT_APP:-netbox-prod-europe-west2-netbox}"
app_address() {  # public address of a Teleport app, or nothing if Teleport does not list it
  tsh apps ls --format=json 2>/dev/null | "$python_bin" -c '
import json, sys
want = sys.argv[1]
try:
    apps = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for app in apps if isinstance(apps, list) else []:
    meta, spec = app.get("metadata") or {}, app.get("spec") or {}
    if (meta.get("name") or app.get("name")) == want:
        print(spec.get("public_addr") or app.get("public_addr") or ""); break
' "$teleport_app"
}
host_header="${NETBOX_HOST_HEADER:-}"
python_bin="${NETBOX_PROXY_PYTHON:-/usr/bin/python3}"
mkdir -p "$state_dir"

is_running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
start() {
  is_running "$state_dir/tsh.pid" && { echo "Teleport forward is already running."; exit 1; }
  is_running "$state_dir/proxy.pid" && { echo "Host-rewrite proxy is already running."; exit 1; }
  tsh status >/dev/null
  if [ -z "$host_header" ]; then
    host_header="$(app_address)"
    if [ -z "$host_header" ] && tsh apps ls 2>/dev/null | awk '{sub(/^>/, "", $1); print $1}' | grep -qx "$teleport_app"; then
      host_header="$teleport_app.${NETBOX_TELEPORT_CLUSTER:-nscale.teleport.sh}"  # older tsh without JSON output
    fi
    if [ -z "$host_header" ]; then
      echo "Teleport has no app named '$teleport_app'. NetBox apps you can use:" >&2
      tsh apps ls 2>/dev/null | grep -i netbox >&2 || true
      echo "Set NETBOX_TELEPORT_APP (and NETBOX_HOST_HEADER) in $env_file, then run start again." >&2
      exit 1
    fi
  fi
  nohup tsh proxy app "$teleport_app" --port "$tsh_port" > "$state_dir/tsh.log" 2>&1 & echo $! > "$state_dir/tsh.pid"
  [ -x "$python_bin" ] || { echo "Python interpreter not found: $python_bin" >&2; exit 2; }
  NETBOX_TSH_PORT="$tsh_port" NETBOX_PROXY_PORT="$proxy_port" NETBOX_HOST_HEADER="$host_header" nohup "$python_bin" "$script_dir/netbox_host_proxy.py" > "$state_dir/proxy.log" 2>&1 & echo $! > "$state_dir/proxy.pid"
  for _ in {1..20}; do
    if is_running "$state_dir/tsh.pid" && is_running "$state_dir/proxy.pid" && curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$tsh_port/"; then
      echo "NetBox proxy is available at http://127.0.0.1:$proxy_port/ (Teleport app $teleport_app, https://$host_header)."
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
  echo "app: $teleport_app (https://${host_header:-$(app_address)})"
  for name in tsh proxy; do
    if is_running "$state_dir/$name.pid"; then echo "$name: running (pid $(cat "$state_dir/$name.pid"))"; else echo "$name: stopped"; fi
  done
}
case "${1:-}" in start) start ;; stop) stop ;; status) status ;; *) echo "Usage: $0 {start|stop|status}" >&2; exit 2 ;; esac
