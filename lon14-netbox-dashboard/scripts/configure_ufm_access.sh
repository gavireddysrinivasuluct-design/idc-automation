#!/usr/bin/env bash
# Lets the dashboard's "Fetch from UFM" button read UFM by itself. Two optional logins:
#
#   1. UFM web (REST) user  -> live links: UFM's current view of every cable (recommended)
#   2. UFM host SSH login    -> UFM's files: master topology, Topology Compare report,
#                               and the periodic scan (used when live links are not set up)
#
# Passwords go into your macOS Keychain (never a file, a command line or Git). The
# addresses and user names go into a [ufm] section of your private device profile.
# Run again to change a login; press Enter at a prompt to keep what is stored.
set -euo pipefail

if ! command -v security >/dev/null; then
  echo "This setup script requires macOS Keychain (the security command)." >&2
  exit 1
fi

config_root="${XDG_CONFIG_HOME:-$HOME/.config}/idc-automation"
profile="$config_root/device-access-lon14.ini"
[ -f "$profile" ] || { echo "Run ./scripts/configure_device_access.sh first (it creates $profile)." >&2; exit 1; }

current() { awk -F' *= *' -v k="$1" '/^\[ufm\]/{s=1; next} /^\[/{s=0} s && $1==k {print $2; exit}' "$profile"; }
ssh_service="idc-automation-lon14-ufm"
rest_service="idc-automation-lon14-ufm-rest"
ufm_hosts="$(current ufm_hosts)"; ufm_user="$(current ufm_user)"; rest_user="$(current rest_user)"

read -r -p "UFM addresses, active first [${ufm_hosts:-10.2.64.75 10.2.64.76}]: " answer
ufm_hosts="${answer:-${ufm_hosts:-10.2.64.75 10.2.64.76}}"
design_path="$(current design_path)"
read -r -p "Design file on the UFM host ('none' if LON14 has none) [${design_path:-/root/nscale_Compute.topo}]: " answer
design_path="${answer:-${design_path:-/root/nscale_Compute.topo}}"

echo
echo "1) UFM web (REST) user, for live links. Use a read-only UFM user if one exists (GET requests only)."
read -r -p "   UFM web user [${rest_user:-skip}] ('-' to remove): " answer
case "$answer" in
  -) rest_user="" ;;
  "") ;;
  *) rest_user="$answer"
     echo "   Enter the UFM web password for $rest_user when Keychain prompts."
     security add-generic-password -U -a "$rest_user" -s "$rest_service" -l "IDC Automation LON14 UFM web (REST) password" -w ;;
esac

echo
echo "2) UFM host SSH login, for the master topology and report (and the scan file fallback)."
echo "   Prefer a read-only login; it only needs to run 'docker exec ufm'."
read -r -p "   UFM host login [${ufm_user:-skip}] ('-' to remove): " answer
case "$answer" in
  -) ufm_user="" ;;
  "") ;;
  *) ufm_user="$answer"
     echo "   Enter the UFM host password for $ufm_user when Keychain prompts."
     security add-generic-password -U -a "$ufm_user" -s "$ssh_service" -l "IDC Automation LON14 UFM host password" -w ;;
esac

[ -n "$rest_user$ufm_user" ] || { echo "No UFM login set; nothing saved." >&2; exit 1; }

umask 077
tmp="$(mktemp)"
# Drop any previous [ufm] section, then append the new one.
awk '/^\[ufm\]/{skip=1; next} /^\[/{skip=0} !skip' "$profile" > "$tmp"
{
  echo
  echo "[ufm]"
  echo "ufm_hosts = $ufm_hosts"
  echo "design_path = $design_path"
  if [ -n "$rest_user" ]; then
    echo "rest_user = $rest_user"
    echo "rest_keychain_service = $rest_service"
    echo "rest_keychain_account = $rest_user"
  fi
  if [ -n "$ufm_user" ]; then
    echo "ufm_user = $ufm_user"
    echo "keychain_service = $ssh_service"
    echo "keychain_account = $ufm_user"
  fi
} >> "$tmp"
mv "$tmp" "$profile"
chmod 600 "$profile"
echo
echo "Saved UFM access in $profile:"
[ -n "$rest_user" ] && echo "  live links  : UFM web user $rest_user (Keychain item '$rest_service')"
[ -n "$ufm_user" ] && echo "  UFM files   : host login $ufm_user (Keychain item '$ssh_service')"
echo "Restart netbox_live_sync.py, then use 'Fetch from UFM' in the Cabling vs UFM tab."
