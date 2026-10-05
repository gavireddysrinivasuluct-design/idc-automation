#!/usr/bin/env bash
# Copies the current approved design and three read-only UFM files to local-inputs/ufm/ for the dashboard's
# "Cabling vs UFM" check, in one session:
#   - the current fabric scan        (fabric_analysis/ibdiagnet.out/ibdiagnet2.lst)
#   - UFM's master/reference topology (shared_config_files/periodicTopo/master.topo)
#   - UFM's latest Topology Compare report (reports/TopologyCompare/TopologyCompare.json)
#   - the approved design topology     (/root/nscale_Compute.topo on the UFM host)
# It only reads files UFM already writes; nothing is sent to the fabric and nothing
# on UFM is changed. The master keeps its original save date.
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
scan="opt/ufm/tmp/fabric_analysis/ibdiagnet.out/ibdiagnet2.lst"
master="opt/ufm/shared_config_files/periodicTopo/master.topo"
report="opt/ufm/shared_config_files/reports/TopologyCompare/TopologyCompare.json"
design="/root/nscale_Compute.topo"
remote_file="ice2-ufm-bundle.tgz"
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
       "ssh -o ConnectTimeout=10 $ufm_user@$host \"docker exec ufm test -s /$scan && test -s $design && { echo __ICE2_UFM_BUNDLE_BEGIN__; docker exec ufm sh -c 'cd / && tar czhf - --ignore-failed-read $scan $master $report 2>/dev/null' | base64 -w0; echo; echo __ICE2_UFM_DESIGN_BEGIN__; base64 -w0 $design; echo; echo __ICE2_UFM_BUNDLE_END__; }\" > ~/$remote_file.part && test -s ~/$remote_file.part && mv ~/$remote_file.part ~/$remote_file"; then
    got="$host"
    break
  fi
  echo "  $host did not provide a scan (standby UFM or unreachable); trying the next one."
done
[ -n "$got" ] || { echo "No UFM host returned a fabric scan." >&2; exit 1; }

umask 077
mkdir -p "$target_dir"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
tsh scp "$jump_user@$jump_host:$remote_file" "$work/bundle.tgz" >/dev/null
tsh ssh --login "$jump_user" "$jump_host" "rm -f ~/$remote_file" >/dev/null 2>&1 || true
awk '/^__ICE2_UFM_BUNDLE_BEGIN__$/{copy=1; next} /^__ICE2_UFM_DESIGN_BEGIN__$/{copy=0} copy {print}' "$work/bundle.tgz" | base64 -D > "$work/files.tgz"
awk '/^__ICE2_UFM_DESIGN_BEGIN__$/{copy=1; next} /^__ICE2_UFM_BUNDLE_END__$/{copy=0} copy {print}' "$work/bundle.tgz" | base64 -D > "$work/nscale_Compute.topo"
tar xzf "$work/files.tgz" -C "$work"
[ -s "$work/$scan" ] || { echo "The bundle from UFM has no fabric scan." >&2; exit 1; }
[ -s "$work/nscale_Compute.topo" ] || { echo "The bundle from UFM has no approved design topology." >&2; exit 1; }
save() {  # save <source> <target.gz>: gzip, keep UFM's timestamp, keep one previous copy
  [ -f "$2" ] && cp -p "$2" "${2%.gz}.previous.gz" 2>/dev/null || true
  gzip -c "$1" > "$2.part" && touch -r "$1" "$2.part" && mv "$2.part" "$2"
}
save "$work/$scan" "$target"
lanes=$(gzip -dc "$target" | grep -c '^{' || true)
echo "Saved $target ($lanes link lanes from UFM $got)."
design_target="$target_dir/nscale_Compute.topo"
[ -f "$design_target" ] && cp -p "$design_target" "$target_dir/nscale_Compute.previous.topo" 2>/dev/null || true
cp "$work/nscale_Compute.topo" "$design_target.part" && chmod 600 "$design_target.part" && mv "$design_target.part" "$design_target"
echo "Saved $design_target (approved design from UFM $got; sha256 $(shasum -a 256 "$design_target" | awk '{print $1}'))."
if [ -s "$work/$master" ]; then
  save "$work/$master" "$target_dir/master.topo.gz"
  echo "Saved $target_dir/master.topo.gz (UFM master topology, saved on UFM $(date -r "$work/$master" '+%Y-%m-%d %H:%M' 2>/dev/null || stat -c %y "$work/$master" | cut -c1-16))."
else
  echo "UFM has no master topology at /$master; the check runs against the design only."
fi
if [ -s "$work/$report" ]; then
  save "$work/$report" "$target_dir/topology-compare.json.gz"
  echo "Saved $target_dir/topology-compare.json.gz (UFM's latest Topology Compare report)."
fi
echo "The dashboard picks these up automatically; reload http://127.0.0.1:8765/ if it is open."
