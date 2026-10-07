#!/usr/bin/env bash
# Stores each user's switch password in Keychain and emits a private profile.
set -euo pipefail

if ! command -v security >/dev/null; then
  echo "This setup script requires macOS Keychain (the security command)." >&2
  exit 1
fi

config_root="${XDG_CONFIG_HOME:-$HOME/.config}/idc-automation"
profile="$config_root/device-access-lon14.ini"

read -r -p "Switch SSH username: " ssh_user
read -r -p "Approved Teleport jump host [lon14deploy1]: " jump_host
jump_host="${jump_host:-lon14deploy1}"
read -r -p "Teleport username [$ssh_user]: " jump_user
jump_user="${jump_user:-$ssh_user}"

if [[ -z "$ssh_user" ]]; then
  echo "Switch SSH username is required." >&2
  exit 1
fi

service="idc-automation-lon14-switch"
echo "Enter your switch password when Keychain prompts. It is never saved in this repository."
security add-generic-password -U -a "$ssh_user" -s "$service" -l "IDC Automation LON14 switch password" -w

umask 077
mkdir -p "$config_root"
cat > "$profile" <<EOF
[lon14]
ssh_user = $ssh_user
jump_host = $jump_host
jump_user = $jump_user
keychain_service = $service
keychain_account = $ssh_user
EOF
chmod 600 "$profile"
echo "Created private device profile: $profile"
echo "Use it with: python3 app/netbox_live_sync.py --device-profile $profile"
