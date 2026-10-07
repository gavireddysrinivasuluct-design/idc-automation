#!/usr/bin/env bash
# Stores a personal, read-only NetBox token in macOS Keychain. It never writes
# the token to this repository, a shell profile, or an environment variable.
set -euo pipefail

if ! command -v security >/dev/null; then
  echo "This setup script requires macOS Keychain (the security command)." >&2
  exit 1
fi

account="$(id -un)"
echo "Paste your personal read-only NetBox API token when prompted by Keychain."
security add-generic-password -U \
  -a "$account" \
  -s "netbox-mcp-token" \
  -l "NetBox API token for IDC Automation" \
  -w
echo "Saved the NetBox token in Keychain for $account."
