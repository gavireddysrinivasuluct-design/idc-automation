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

## Faster sync

Recommended once `python3` is confirmed on the jump host:

```bash
python3 app/netbox_live_sync.py \
  --netbox-url 'http://127.0.0.1:8444' \
  --device-profile "$HOME/.config/idc-automation/device-access.ini" \
  --fanout jump --device-parallel 15 --sync-every-minutes 15
```

What each speed-up does:

- **Concurrent phases.** The NetBox cable check and the device collection run at the same time, so a sync takes about as long as its slowest phase rather than the sum of both.
- **Progressive results.** Each switch is applied to the dashboard as soon as it returns. The Sync bar shows `37/100 switches`, and the final line shows per-phase timings.
- **Management IPs.** These come from one bulk NetBox query per site and role, instead of 100 single lookups. They are cached for `--address-cache-hours` (default 24). The cache is dropped automatically when any switch fails, in case an IP moved.
- **Incremental cable sync.**
  - Between full syncs (`--full-netbox-every-hours`, default 6), only cables named in the NetBox change log since the last sync are re-read. That covers edits, deletions and re-terminations.
  - If the change log is unavailable to your token, or its time filter is not honoured, the sync falls back to a full pull.
  - Cable pages are trimmed with `fields=` on NetBox 4.x, which keeps the payload small.
- **`--fanout jump`.**
  - Opens one Teleport session to the jump host and runs a small fan-out worker there, instead of a new `tsh ssh` session per switch.
  - The switch password is sent only on the worker's stdin. It never appears in argv, the environment or a file. The commands run on each switch are unchanged.
  - If the worker cannot start (for example, `python3` is missing on the jump host), the service falls back to `--fanout local` automatically.
- **`--device-parallel`** sets the number of concurrent switch sessions (1–25, default 10). Raise it gradually and watch jump-host load.
- **`/api/live`** is rebuilt only when the data changes and is served with an ETag and gzip. Dashboard polling (every 15 s) is mostly `304 Not Modified`.
- **`--sync-every-minutes`** runs the sync in the background, so the dashboard opens on fresh evidence.

Measured in a simulated run (1.5 s Teleport setup, 1 s per switch command, 150 ms per NetBox request, 100 switches):

| Configuration | Sync time | NetBox requests |
| --- | --- | --- |
| Previous version | 39.4 s | 123 |
| New, `--fanout local`, first run | 32.7 s | 25 |
| New, `--fanout jump`, parallel 10, first run | 18.7 s | 25 |
| New, `--fanout jump`, parallel 20, repeat run | 13.4 s | 2 |

Real timings depend on Teleport and switch response times. Compare them using the per-phase timings shown after each sync.

## Repository contents

- `assets/` — shared dashboard, topology, device inventory, and command file.
- `app/netbox_live_sync.py` — local-only service.
- `collector/run_ntp_audit.py` — read-only collector.
- `scripts/` — Teleport NetBox proxy, personal Keychain, and local host-key setup.
- `config/` — non-secret profile example.

The collector and service write runtime evidence only to ignored local paths.
