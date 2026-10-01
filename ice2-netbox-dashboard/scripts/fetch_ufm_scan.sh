#!/usr/bin/env bash
# Copies UFM's latest fabric scan (ibdiagnet2.lst) to local-inputs/ufm/ for the
# dashboard's "Cabling vs UFM" check. Read-only: it only reads a file UFM already
# writes; nothing is sent to the fabric and nothing on UFM is changed.
#
# Path: Mac -> tsh -> jump host -> ssh -> active UFM host -> docker exec ufm cat
# You are asked for the UFM host password once (typed into ssh on the jump host;
# it is never stored). Override defaults with environment variables:
#   UFM_HOSTS   active/standby UFM addresses   (default "10.1.67.190 10.1.67.191")
#   UFM_USER    login on the UFM host          (default "root"; prefer a read-only account)
#   JUMP_HOST / JUMP_USER                      (default: from your device profile)
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_root="$(cd "$script_dir/.." && pwd)"
profile="${IDC_DEVICE_PROFILE:-${XDG_CONFIG_HOME:-$HOME/.config}/idc-automation/device-access.ini}"
read_profile() { [ -f "$profile" ] && awk -F' *= *' -v k="$1" '$1==k {print $2; exit}' "$profile" || true; }

jump_host="${JUMP_HOST:-$(read_profile jump_host)}"; jump_host="${jump_host:-jmp0}"
jump_user="${JUMP_USER:-$(read_profile jump_user)}"
ufm_hosts="${UFM_HOSTS:-10.1.67.190 10.1.67.191}"
ufm_user="${UFM_USER:-root}"
scan="/opt/ufm/tmp/fabric_analysis/ibdiagnet.out/ibdiagnet2.lst"
remote_file="ice2-ufm-scan.lst.gz"
target_dir="$project_root/local-inputs/ufm"
target="$target_dir/ibdiagnet2.lst.gz"

if [ -z "$jump_user" ]; then
  read -r -p "Teleport login for $jump_host: " jump_user
fi
command -v tsh >/dev/null || { echo "Teleport CLI (tsh) is required." >&2; exit 1; }
tsh status >/dev/null 2>&1 || { echo "Teleport login expired. Run: tsh login" >&2; exit 1; }

got=""
for host in $ufm_hosts; do
  echo "Reading the UFM fabric scan from $host via $jump_host (enter the $ufm_user@$host password when asked)…"
  if tsh ssh --tty --login "$jump_user" "$jump_host" \
       "ssh -o ConnectTimeout=10 $ufm_user@$host 'docker exec ufm test -s $scan && docker exec ufm cat $scan | gzip -c' > ~/$remote_file.part && test -s ~/$remote_file.part && mv ~/$remote_file.part ~/$remote_file"; then
    got="$host"
    break
  fi
  echo "  $host did not provide a scan (standby UFM or unreachable); trying the next one."
done
[ -n "$got" ] || { echo "No UFM host returned a fabric scan." >&2; exit 1; }

umask 077
mkdir -p "$target_dir"
tsh scp "$jump_user@$jump_host:$remote_file" "$target.part" >/dev/null
tsh ssh --login "$jump_user" "$jump_host" "rm -f ~/$remote_file" >/dev/null 2>&1 || true
gzip -t "$target.part"
[ -f "$target" ] && cp -p "$target" "$target_dir/ibdiagnet2.previous.lst.gz"
mv "$target.part" "$target"
lanes=$(gzip -dc "$target" | grep -c '^{' || true)
echo "Saved $target ($lanes link lanes from UFM $got)."
echo "The dashboard picks it up automatically; reload http://127.0.0.1:8765/ if it is open."
