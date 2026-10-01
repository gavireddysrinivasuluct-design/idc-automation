#!/usr/bin/env bash
# Installs an already approved SSH host-key file locally. Host keys are never
# included in this repository or uploaded by this script.
set -euo pipefail

cat <<'GUIDE'
Before continuing, obtain the approved SSH known_hosts file through one of the
platform team's trusted onboarding channels:

  1. Download the current file from the restricted team onboarding location.
  2. Request the approved file from the platform/network owner.
  3. Use the documented file location or fingerprint supplied by that owner.

Do not generate the file with ssh-keyscan unless the platform owner verifies
the resulting fingerprint through an independent channel.
GUIDE

read -r -p "Path to the approved SSH known_hosts file: " source_file
if [[ -z "$source_file" || ! -f "$source_file" ]]; then
  echo "A readable approved known_hosts file is required." >&2
  exit 1
fi

project_root="$(cd "$(dirname "$0")/.." && pwd)"
target_dir="$project_root/local-inputs"
target_file="$target_dir/known_hosts"
mkdir -p "$target_dir"
umask 077
cp "$source_file" "$target_file"
chmod 600 "$target_file"
echo "Installed approved host keys at $target_file"
