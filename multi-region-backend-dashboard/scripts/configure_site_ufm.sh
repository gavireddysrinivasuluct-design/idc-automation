#!/bin/bash
# One-time setup of direct, read-only UFM access for a NetBox-first region (OSL1, STA1, ...).
# Writes a [ufm.<site>] section (names and addresses only) to your private profile and stores
# the UFM web password in macOS Keychain. Run again to change anything; Enter keeps a value.
#
#   ./scripts/configure_site_ufm.sh osl1
set -euo pipefail
site="${1:-}"; [[ -n "$site" ]] || { echo "usage: $0 <site>   e.g. $0 osl1"; exit 1; }
profile="${IDC_DEVICE_PROFILE:-${XDG_CONFIG_HOME:-$HOME/.config}/idc-automation/device-access.ini}"
mkdir -p "$(dirname "$profile")"; touch "$profile"; chmod 600 "$profile"
here="$(cd "$(dirname "$0")/.." && pwd)"
default_hosts=$(python3 -c "import json,sys;r=[x for x in json.load(open('$here/regions.json'))['regions'] if x['site']=='$site'];print((r[0].get('fabric_view') or {}).get('ufm_hosts','') if r else '')")
get() { python3 - "$profile" "ufm.$site" "$1" <<'PY'
import configparser, sys
p = configparser.ConfigParser(interpolation=None); p.read(sys.argv[1])
print(p.get(sys.argv[2], sys.argv[3], fallback=""))
PY
}
ask() { local cur; cur="$(get "$1")"; [[ -z "$cur" ]] && cur="$3"; read -r -p "$2 [${cur}]: " v; echo "${v:-$cur}"; }
jump_host=$(ask jump_host "Teleport jump host that reaches the $(echo "$site" | tr a-z A-Z) UFM (tsh ls)" "")
jump_user=$(ask jump_user "Your Teleport login on that host (tsh status)" "$(id -un)")
hosts=$(ask hosts "UFM address(es), VIP first" "$default_hosts")
rest_user=$(ask rest_user "UFM web (REST) user, read-only if possible" "")
[[ -n "$jump_host" && -n "$jump_user" && -n "$hosts" && -n "$rest_user" ]] || { echo "All four values are needed."; exit 1; }
service="idc-automation-${site}-ufm-rest"
read -r -s -p "UFM web password for $rest_user (Enter keeps the stored one): " pw; echo
if [[ -n "$pw" ]]; then
  security add-generic-password -U -s "$service" -a "$rest_user" -w "$pw" >/dev/null
  unset pw
fi
python3 - "$profile" "ufm.$site" "$jump_host" "$jump_user" "$hosts" "$rest_user" "$service" <<'PY'
import configparser, os, sys
path, section, jump_host, jump_user, hosts, user, service = sys.argv[1:]
p = configparser.ConfigParser(interpolation=None); p.read(path)
if not p.has_section(section): p.add_section(section)
for k, v in (("jump_host", jump_host), ("jump_user", jump_user), ("hosts", hosts), ("rest_user", user),
             ("rest_keychain_service", service), ("rest_keychain_account", user)):
    p.set(section, k, v)
with open(path, "w") as f: p.write(f)
os.chmod(path, 0o600)
PY
echo "Saved [ufm.$site] in $profile (no password in the file; it is in Keychain item $service)."
echo "Test from the jump host: curl -sk -u $rest_user https://${hosts%% *}/ufmRest/resources/links -o /dev/null -w '%{http_code}\n'  (expect 200)"
