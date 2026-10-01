#!/usr/bin/env bash
# Lets the dashboard's "Fetch from UFM" button read UFM's fabric files by itself.
# Stores the UFM host password in your macOS Keychain (never in a file or in Git)
# and adds a [ufm] section to your private device profile. Run once; run again to
# change the login or password.
set -euo pipefail

if ! command -v security >/dev/null; then
  echo "This setup script requires macOS Keychain (the security command)." >&2
  exit 1
fi

config_root="${XDG_CONFIG_HOME:-$HOME/.config}/idc-automation"
profile="$config_root/device-access.ini"
[ -f "$profile" ] || { echo "Run ./scripts/configure_device_access.sh first (it creates $profile)." >&2; exit 1; }

echo "The dashboard reads three files UFM already writes (fabric scan, master topology, Topology Compare report)."
echo "Use a read-only login on the UFM host if one exists; it only needs to run 'docker exec ufm cat/tar'."
read -r -p "UFM host login [root]: " ufm_user
ufm_user="${ufm_user:-root}"
read -r -p "UFM host addresses, active first [10.1.67.190 10.1.67.191]: " ufm_hosts
ufm_hosts="${ufm_hosts:-10.1.67.190 10.1.67.191}"

service="idc-automation-ice2-ufm"
echo "Enter the UFM host password for $ufm_user when Keychain prompts. It is never saved in this repository."
security add-generic-password -U -a "$ufm_user" -s "$service" -l "IDC Automation ICE2 UFM host password" -w

umask 077
tmp="$(mktemp)"
# Drop any previous [ufm] section, then append the new one.
awk 'BEGIN{skip=0} /^\[ufm\]/{skip=1; next} /^\[/{skip=0} !skip' "$profile" > "$tmp"
cat >> "$tmp" <<EOF

[ufm]
ufm_user = $ufm_user
ufm_hosts = $ufm_hosts
keychain_service = $service
keychain_account = $ufm_user
EOF
mv "$tmp" "$profile"
chmod 600 "$profile"
echo "Saved UFM access in $profile (password in Keychain item '$service')."
echo "Restart netbox_live_sync.py, then use 'Fetch from UFM' in the Cabling vs UFM tab."
