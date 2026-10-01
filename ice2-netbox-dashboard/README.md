# NetBox dashboard launcher

This repository contains the shared dashboard HTML, topology snapshot, device inventory, and read-only command file for a read-only NetBox/device dashboard. SSH host keys, tokens, passwords, and collected runtime evidence are intentionally **not** stored here.

Every user keeps their own credentials and approved SSH host keys only on their own machine. Never add those files or collected evidence to Git.

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

## SSH host-key setup

Obtain the approved SSH `known_hosts` file from the platform owner, then install it locally:

```bash
./scripts/configure_known_hosts.sh
```

The script copies the supplied file to `local-inputs/known_hosts` with mode `600`. That directory is ignored by Git.

Management IPs are different: they are fetched read-only from NetBox during each device refresh. The generated address map is written only to the ignored runtime directory and is never committed.

## Run

Use the shared NetBox endpoint. All dashboard and device inputs are bundled; only the private device profile and local host-key file need setup:

```bash
python3 app/netbox_live_sync.py \
  --netbox-url 'https://netbox-prod-europe-west2-netbox.nscale.teleport.sh' \
  --device-profile "$HOME/.config/idc-automation/device-access.ini"
```

Open `http://127.0.0.1:8765/`. The browser only talks to the local service; the NetBox token and switch password never reach the browser. The service uses read-only NetBox GET requests, fetches each listed device's current primary management IP from NetBox during refresh, and executes only the command supplied in the approved local command file.

If an approved local proxy requires an HTTP Host header, add `--netbox-host-header '<approved-hostname>'` to the command. Do not guess this value; obtain it from the platform owner.

## Repository contents

- `assets/` — shared dashboard, topology, device inventory, and command file.
- `app/netbox_live_sync.py` — local-only service.
- `collector/run_ntp_audit.py` — read-only collector.
- `scripts/` — personal Keychain and local host-key setup.
- `config/` — non-secret profile example.

The collector and service write runtime evidence only to ignored local paths.
