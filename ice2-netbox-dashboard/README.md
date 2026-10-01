# NetBox dashboard launcher

This repository contains safe, reusable code and credential-setup scripts for a read-only NetBox/device dashboard. Internal topology, device inventories, dashboard HTML, host keys, tokens, and collected evidence are intentionally **not** stored here.

Every user obtains the operational inputs through the approved internal process and keeps them only on their own machine. Never add those files or any credentials to Git.

## Prerequisites

- macOS with Python 3, macOS Keychain, and the Teleport CLI (`tsh`).
- An approved personal read-only NetBox token.
- Approved device and Teleport access from the platform owner.

## Per-user credentials

Authenticate to Teleport using your approved account, then run:

```bash
tsh login
./scripts/configure_netbox_token.sh
./scripts/configure_device_access.sh
```

The scripts prompt for credentials and save them only in the logged-in user's macOS Keychain. The device script writes a mode-`600` metadata profile outside the repository, normally at `~/.config/idc-automation/device-access.ini`.

## Local operational inputs

Create a local directory that is outside Git, then obtain these approved files from your local machine or the authorized internal distribution location:

- dashboard HTML
- backend cable topology CSV
- device inventory CSV
- approved SSH host-key file
- read-only device-command file

The `.gitignore` protects the conventional `local-inputs/` directory. The files are not interchangeable: request the current approved package if any are missing or stale.

Management IPs are different: they are fetched read-only from NetBox during each device refresh. The generated address map is written only to the ignored runtime directory and is never committed.

## Run

Replace every placeholder with your local input path and approved NetBox URL:

```bash
python3 app/netbox_live_sync.py \
  --netbox-url '<approved-netbox-url-or-local-proxy>' \
  --diagram local-inputs/dashboard.html \
  --connections local-inputs/connections.csv \
  --device-profile "$HOME/.config/idc-automation/device-access.ini" \
  --devices local-inputs/devices.csv \
  --known-hosts local-inputs/known_hosts \
  --commands local-inputs/read_only_commands.txt
```

Open `http://127.0.0.1:8765/`. The browser only talks to the local service; the NetBox token and switch password never reach the browser. The service uses read-only NetBox GET requests, fetches each listed device's current primary management IP from NetBox during refresh, and executes only the command supplied in the approved local command file.

If an approved local proxy requires an HTTP Host header, add `--netbox-host-header '<approved-hostname>'` to the command. Do not guess this value; obtain it from the platform owner.

## Repository contents

- `app/netbox_live_sync.py` — local-only service.
- `collector/run_ntp_audit.py` — read-only collector.
- `scripts/` — personal Keychain setup.
- `config/` — non-secret profile example.

The collector and service write runtime evidence only to ignored local paths.
