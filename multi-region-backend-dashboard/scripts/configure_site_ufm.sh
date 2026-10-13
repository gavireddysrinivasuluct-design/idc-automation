#!/bin/bash
# One-time setup of direct, read-only UFM access for a NetBox-first region (OSL1, STA1, ...).
# Writes a [ufm.<site>] section (names and addresses only) to your private profile and stores
# the UFM web password in macOS Keychain. Run again to change anything; Enter keeps a value.
#
#   ./scripts/configure_site_ufm.sh sta1
#
# Two ways in, both read-only REST (GET /ufmRest/resources/links):
#   * direct:   the UFM appliance is a Teleport node (STA1: tsh ssh sta1-p-phy-ufm2).
#               Leave the jump host empty; the UFM node asks its own REST API.
#   * jump host: a Teleport node that reaches the UFM VIP on 443.
set -euo pipefail
site="${1:-}"; [[ -n "$site" ]] || { echo "usage: $0 <site>   e.g. $0 sta1"; exit 1; }
SITE=$(echo "$site" | tr a-z A-Z)
profile="${IDC_DEVICE_PROFILE:-${XDG_CONFIG_HOME:-$HOME/.config}/idc-automation/device-access.ini}"
mkdir -p "$(dirname "$profile")"; touch "$profile"; chmod 600 "$profile"
here="$(cd "$(dirname "$0")/.." && pwd)"
fv() { python3 -c "import json;r=[x for x in json.load(open('$here/regions.json'))['regions'] if x['site']=='$site'];print((r[0].get('fabric_view') or {}).get('$1','') if r else '')"; }
default_vip=$(fv ufm_hosts); default_node=$(fv ufm_node)
get() { python3 - "$profile" "ufm.$site" "$1" <<'PY'
import configparser, sys
p = configparser.ConfigParser(interpolation=None); p.read(sys.argv[1])
print(p.get(sys.argv[2], sys.argv[3], fallback=""))
PY
}
ask() { local cur; cur="$(get "$1")"; [[ -z "$cur" ]] && cur="$3"; read -r -p "$2 [${cur}]: " v; [[ "$v" == "-" ]] && v=""; echo "${v:-$cur}"; }

echo "UFM access for $SITE. Type - to clear a value."
ufm_node=$(ask ufm_node "UFM appliance as a Teleport node, for direct access (tsh ls)" "$default_node")
jump_host=$(ask jump_host "Jump host instead (leave empty to go to the UFM node directly)" "")
if [[ -z "$jump_host" && -z "$ufm_node" ]]; then echo "Need the UFM node or a jump host."; exit 1; fi
if [[ -z "$jump_host" ]]; then default_hosts="127.0.0.1 $default_vip"; via="$ufm_node"; else default_hosts="$default_vip"; via="$jump_host"; fi
jump_user=$(ask jump_user "Teleport login on $via (empty = your tsh default)" "")
hosts=$(ask hosts "UFM REST address(es) as seen from $via" "$default_hosts")
rest_user=$(ask rest_user "UFM web (REST) user, read-only if possible" "")
[[ -n "$hosts" && -n "$rest_user" ]] || { echo "UFM address and web user are needed."; exit 1; }
service="idc-automation-${site}-ufm-rest"
read -r -s -p "UFM web password for $rest_user (Enter keeps the stored one): " pw; echo
if [[ -n "$pw" ]]; then
  security add-generic-password -U -s "$service" -a "$rest_user" -w "$pw" >/dev/null
  unset pw
fi
python3 - "$profile" "ufm.$site" "$ufm_node" "$jump_host" "$jump_user" "$hosts" "$rest_user" "$service" <<'PY'
import configparser, os, sys
path, section, ufm_node, jump_host, jump_user, hosts, user, service = sys.argv[1:]
p = configparser.ConfigParser(interpolation=None); p.read(path)
if not p.has_section(section): p.add_section(section)
for k, v in (("ufm_node", ufm_node), ("jump_host", jump_host), ("jump_user", jump_user), ("hosts", hosts),
             ("rest_user", user), ("rest_keychain_service", service), ("rest_keychain_account", user)):
    p.set(section, k, v)
with open(path, "w") as f: p.write(f)
os.chmod(path, 0o600)
PY
echo "Saved [ufm.$site] in $profile (no password in the file; it is in Keychain item $service)."
login=""; [[ -n "$jump_user" ]] && login="--login $jump_user "
echo "Test:  tsh ssh ${login}$via \"curl -sk -u $rest_user https://${hosts%% *}/ufmRest/resources/links -o /dev/null -w '%{http_code}\\n'\"   (expect 200; it asks for the password)"
