# NetBox dashboard launcher

This repository contains the shared dashboard HTML, topology snapshot, device inventory, and read-only command file for a read-only NetBox/device dashboard. SSH host keys, tokens, passwords, and collected runtime evidence are intentionally **not** stored here.

Every user keeps their own credentials and approved SSH host keys only on their own machine. Never add those files or collected evidence to Git.

## Quick start

From the project directory, complete these steps in order:

```bash
git pull
tsh login
./scripts/configure_netbox_token.sh
./scripts/configure_device_access.sh
./scripts/netbox_proxy.sh start
./scripts/configure_known_hosts.sh --collect-live
python3 app/netbox_live_sync.py \
  --netbox-url 'http://127.0.0.1:8444' \
  --device-profile "$HOME/.config/idc-automation/device-access.ini"
```

Then open `http://127.0.0.1:8765/` in a browser. The dashboard, topology, device list, and read-only command file are already included under `assets/`.

When you finish, stop the local NetBox proxy:

```bash
./scripts/netbox_proxy.sh stop
```

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

To collect a fresh candidate file through the authenticated Teleport jump host, run:

```bash
./scripts/configure_known_hosts.sh --collect-live
```

The script reads the bundled device inventory, fetches each current management IP from NetBox, then runs `ssh-keyscan` through the approved jump host. It installs the collected keys at `local-inputs/known_hosts` with mode `600`, while preserving an existing file as `local-inputs/known_hosts.previous`. That directory is ignored by Git.

Start the local NetBox proxy first with `./scripts/netbox_proxy.sh start`. It is the standard local NetBox endpoint for this project: `http://127.0.0.1:8444`. Stop it after use with `./scripts/netbox_proxy.sh stop`.

To install a file already issued by the platform owner instead, run `./scripts/configure_known_hosts.sh` without `--collect-live` and provide its path.

### How a new user obtains `known_hosts`

Host keys are trust material. Live collection is useful for onboarding, but each newly collected key must be independently verified against an approved platform fingerprint, host-CA, or trusted baseline before use. The platform/network owner can also issue a pre-verified file through the restricted onboarding download or internal secure channel.

The live collection command requires explicit acknowledgement and repeats this warning. Do not use unverified `ssh-keyscan` output as a trust source.

Management IPs are different: they are fetched read-only from NetBox during each device refresh. The generated address map is written only to the ignored runtime directory and is never committed.

## Run

Use the shared NetBox endpoint. All dashboard and device inputs are bundled; only the private device profile and local host-key file need setup:

```bash
python3 app/netbox_live_sync.py \
  --netbox-url 'http://127.0.0.1:8444' \
  --device-profile "$HOME/.config/idc-automation/device-access.ini"
```

Open `http://127.0.0.1:8765/`. The browser only talks to the local service; the NetBox token and switch password never reach the browser. The service uses read-only NetBox GET requests, fetches each listed device's current primary management IP from NetBox during refresh, and executes only the command supplied in the approved local command file.

If an approved local proxy requires an HTTP Host header, add `--netbox-host-header '<approved-hostname>'` to the command. Do not guess this value; obtain it from the platform owner.

## Repository contents

- `assets/` — shared dashboard, topology, device inventory, and command file.
- `app/netbox_live_sync.py` — local-only service.
- `collector/run_ntp_audit.py` — read-only collector.
- `scripts/` — Teleport NetBox proxy, personal Keychain, and local host-key setup.
- `config/` — non-secret profile example.

The collector and service write runtime evidence only to ignored local paths.
