# ICE2 NetBox + live device dashboard

A local, read-only dashboard for the ICE2 backend InfiniBand fabric: 36 spines, 64 leaves and the GPU trays. It compares two sources:

- **NetBox**, which says what *should* be cabled.
- **The switches**, which say what is actually up right now (`nv show interface --output json`).

Every link in the diagram is coloured by its live state, and one **Sync** button refreshes both sources.

- **Read-only.** Nothing is ever written to NetBox or to a device.
- **Secrets stay on your Mac.** Your NetBox token and switch password are kept in your own macOS Keychain. They never reach the browser or this repository.

```
 Browser ──► http://127.0.0.1:8765  (app/netbox_live_sync.py, runs on your Mac)
                 │                          │
                 │ read-only REST           │ one Teleport session (--fanout jump)
                 ▼                          ▼
   127.0.0.1:8444 → tsh app proxy      jmp0 ──ssh──► 100 backend switches
          → NetBox (prod)                     (read-only nv show interface)
```

This guide is written for a **new user setting up from nothing**. Follow sections 1–4 once, then use section 5 every day.

## Contents

1. [Onboarding checklist](#1-onboarding-checklist)
2. [Request access](#2-request-access)
3. [Install the tools on your Mac](#3-install-the-tools-on-your-mac)
4. [One-time setup](#4-one-time-setup)
5. [Daily use](#5-daily-use)
6. [Using the dashboard](#6-using-the-dashboard)
7. [Command-line options](#7-command-line-options)
8. [How sync works](#8-how-sync-works)
9. [Maintenance: passwords, tokens, host keys, updates](#9-maintenance)
10. [Troubleshooting](#10-troubleshooting)
11. [Security model](#11-security-model)
12. [Remove everything](#12-remove-everything)
13. [HTTP API and repository contents](#13-reference)

---

## 1. Onboarding checklist

Tick these off in order. Each item links to the step that explains it.

- [ ] Teleport account works: `tsh login` ([2](#2-request-access), [4.2](#42-log-in-to-teleport-and-check-your-access))
- [ ] NetBox Teleport app is visible: `tsh apps ls` shows `netbox-prod-europe-west2-netbox`
- [ ] Jump host is reachable: `tsh ssh <login>@jmp0 hostname`
- [ ] Switch login works from `jmp0` ([4.2](#42-log-in-to-teleport-and-check-your-access))
- [ ] Mac tools installed: `python3` 3.8+, `tsh`, `git` ([3](#3-install-the-tools-on-your-mac))
- [ ] Repository cloned ([4.1](#41-get-the-code))
- [ ] Read-only NetBox API token created and stored ([4.3](#43-create-a-read-only-netbox-api-token), [4.4](#44-store-the-netbox-token-in-keychain))
- [ ] Switch login stored, private profile created ([4.5](#45-store-your-switch-login-and-create-your-profile))
- [ ] NetBox proxy running and answering ([4.6](#46-start-the-local-netbox-proxy-and-test-it))
- [ ] Approved switch host keys installed ([4.7](#47-install-the-approved-switch-host-keys))
- [ ] First sync completed ([4.8](#48-first-run-and-verification))

---

## 2. Request access

Ask the platform/network owner, or raise your team's usual access request, for the following:

| Access | Used for |
| --- | --- |
| A **Teleport** account on `nscale.teleport.sh` | Everything goes through Teleport |
| Teleport **app** access to `netbox-prod-europe-west2-netbox` | Reading NetBox through a local proxy |
| A Teleport **login on the jump host `jmp0`** | Reaching the switch management network |
| A **switch SSH account** for the ICE2 backend switches; read-only is enough | Running `nv show interface` |
| NetBox **read** permission for devices, interfaces and cables | Cable and IP lookups |
| *(Recommended)* NetBox permission to **view the change log** (object changes) | Fast incremental sync. Without it every sync does a full pull, which still works but is slower. |
| An approved **switch `known_hosts` file**, or approved fingerprints | Strict SSH host-key checking ([4.7](#47-install-the-approved-switch-host-keys)) |

Some Teleport access is granted through **access requests** rather than permanently. If `tsh ssh …@jmp0` is denied, request the role your team uses for jump-host access:

```bash
tsh request search --kind node            # see what you can request
tsh request create --roles <role-name> --reason "ICE2 dashboard"
# after approval:
tsh login --request-id=<request-id>
```

Ask the platform owner which role name applies to you.

---

## 3. Install the tools on your Mac

| Tool | Install | Check |
| --- | --- | --- |
| **Xcode command-line tools** (provides `git` and `python3`) | `xcode-select --install` | `git --version` |
| **Python 3.8 or newer** | Included with the command-line tools, or `brew install python` | `python3 --version` |
| **Teleport CLI `tsh`** | Use the Teleport version your company standardises on: download from the Teleport downloads page, or `brew install teleport`. Its major version should match the cluster's (`tsh version`). | `tsh version` |
| **macOS Keychain** | Built in (`security` command) | `security -h >/dev/null && echo ok` |

No Python packages are needed; only the standard library is used.

**On the jump host,** the fast `--fanout jump` mode needs `python3` on `jmp0`. You'll check this in step 4.2. If it's missing, the service still works, just more slowly.

---

## 4. One-time setup

> Unless stated otherwise, run every command from the project folder `idc-automation/ice2-netbox-dashboard`.

### 4.1 Get the code

```bash
cd ~                                       # or wherever you keep projects
git clone https://github.com/gavireddysrinivasuluct-design/idc-automation.git
cd idc-automation/ice2-netbox-dashboard
chmod +x scripts/*.sh                      # only needed if the scripts are not executable
```

### 4.2 Log in to Teleport and check your access

```bash
tsh login --proxy=nscale.teleport.sh       # first time; later just `tsh login`
tsh status                                 # shows your logins and "Valid until"
tsh apps ls | grep -i netbox               # must list netbox-prod-europe-west2-netbox
tsh ls | grep -i jmp0                      # must list the jump host
```

Note the login name shown under **Logins** in `tsh status`. You'll use it as `<login>` below.

Check the jump host:

```bash
tsh ssh <login>@jmp0 'hostname; python3 --version; which ssh'
```

- If `python3` prints a version, `--fanout jump` will work.
- If it says *command not found*, use the default `--fanout local` or ask for `python3` on `jmp0`.

Check your switch login once by hand from `jmp0`. Replace the address with any backend switch's management IP from NetBox:

```bash
tsh ssh --tty <login>@jmp0
ssh <switch-user>@<switch-mgmt-ip> 'nv show interface --output json | head -c 200'
exit
```

A Teleport login lasts about **8 hours**. Renew it with `tsh login` when it expires.

### 4.3 Create a read-only NetBox API token

1. Open NetBox through Teleport. Either use the Teleport web UI (**Applications → netbox-prod-europe-west2-netbox**) or run `tsh apps login netbox-prod-europe-west2-netbox` and open the URL it prints.
2. In NetBox, click your user name (top right), then **API Tokens** (on some versions it's **Profile → API Tokens**), then **Add a token**.
3. Leave **Write enabled** *unticked*. That makes the token read-only.
4. Set an **expiry date** that follows your team's policy, and a description such as `ICE2 dashboard – <your name>`.
5. Save, then **copy the token now**. Newer NetBox versions show it only once.

### 4.4 Store the NetBox token in Keychain

```bash
./scripts/configure_netbox_token.sh
```

Paste the token when Keychain prompts. It is saved as Keychain item **`netbox-mcp-token`** for your macOS user. It is never written to disk, your shell profile or environment variables.

### 4.5 Store your switch login and create your profile

```bash
./scripts/configure_device_access.sh
```

The script asks for:

| Prompt | Enter |
| --- | --- |
| `Switch SSH username` | Your switch account |
| `Approved Teleport jump host [jmp0]` | Press Enter for `jmp0` |
| `Teleport username [...]` | Your Teleport **login** from `tsh status` |
| Keychain password prompt | Your **switch** password |

It stores the password in Keychain item **`idc-automation-ice2-switch`**. It also writes your private profile to **`~/.config/idc-automation/device-access.ini`** with mode `600`. The profile contains names and Keychain references only, never the password.

### 4.6 Start the local NetBox proxy and test it

```bash
./scripts/netbox_proxy.sh start
./scripts/netbox_proxy.sh status          # both lines must say "running"
```

The proxy listens on **`http://127.0.0.1:8444`**. Test it:

```bash
curl -s -H "Authorization: Token $(security find-generic-password -s netbox-mcp-token -a "$(id -un)" -w)" \
  http://127.0.0.1:8444/api/status/ | head -c 200; echo
```

You should see JSON that includes `"netbox-version"`. If not, see [Troubleshooting](#10-troubleshooting).

### 4.7 Install the approved switch host keys

SSH host keys are trust material. Choose **one** option.

**Option A (preferred).** Install a file issued by the platform owner. Get the approved `known_hosts` file through your team's secure onboarding channel, then run:

```bash
./scripts/configure_known_hosts.sh        # asks for the path to the file
```

**Option B.** Collect the keys live through `jmp0`, then verify them. This needs the proxy from step 4.6 running:

```bash
./scripts/configure_known_hosts.sh --collect-live
# prompts: jump host [jmp0], Teleport login
```

Option B reads the device list, looks up each management IP in NetBox, and runs `ssh-keyscan` from `jmp0`. **Before you trust the result**, compare the fingerprints with an approved baseline from the platform owner:

```bash
ssh-keygen -lf local-inputs/known_hosts
```

Either option installs **`local-inputs/known_hosts`** (mode `600`, ignored by Git). Any earlier file is kept as `known_hosts.previous`.

### 4.8 First run and verification

```bash
python3 app/netbox_live_sync.py \
  --netbox-url 'http://127.0.0.1:8444' \
  --device-profile "$HOME/.config/idc-automation/device-access.ini" \
  --fanout jump --device-parallel 15 --sync-every-minutes 15
```

The terminal prints `Open http://127.0.0.1:8765/`. In a **second** terminal you can check:

```bash
curl -s http://127.0.0.1:8765/api/health; echo        # expect "netbox": "reachable"
```

Open **http://127.0.0.1:8765/** in a browser and press **⟳ Sync NetBox + devices**. The first sync does a **full** NetBox pull, so it takes longer than later ones. A successful result looks like this:

```
NetBox  complete · full · 5,424 cables · 0 differ · 0 missing · 25 API calls
Devices complete · 100 switches · 14,500 IB ports · 33.4 s · IPs: netbox bulk
Sync complete in 34.2 s   NetBox … ‖ IPs … ‖ devices …
```

From the second sync onwards, the NetBox step should say **incremental** and need only about 2 API calls. If it still says **full**, your token cannot read the change log (see [section 2](#2-request-access)).

Setup is done.

---

## 5. Daily use

```bash
cd ~/idc-automation/ice2-netbox-dashboard
git pull                                   # get the latest dashboard and fixes
tsh status || tsh login                    # renew Teleport if it has expired
./scripts/netbox_proxy.sh status           # if "tsh: stopped": run stop, then start
./scripts/netbox_proxy.sh start            # skip if both already say running

python3 app/netbox_live_sync.py \
  --netbox-url 'http://127.0.0.1:8444' \
  --device-profile "$HOME/.config/idc-automation/device-access.ini" \
  --fanout jump --device-parallel 15 --sync-every-minutes 15
```

Then open **http://127.0.0.1:8765/** and press **Sync**. With `--sync-every-minutes 15`, the service also syncs by itself every 15 minutes. The first automatic sync runs 15 minutes after start.

Always open the dashboard at this local address. Opened as a `file://` page, it shows only its saved snapshot and Sync cannot run.

**To run it in the background,** so you can close the terminal:

```bash
mkdir -p ~/Library/Logs
nohup python3 app/netbox_live_sync.py --netbox-url 'http://127.0.0.1:8444' \
  --device-profile "$HOME/.config/idc-automation/device-access.ini" \
  --fanout jump --device-parallel 15 --sync-every-minutes 15 \
  > ~/Library/Logs/ice2-dashboard.log 2>&1 &
tail -f ~/Library/Logs/ice2-dashboard.log           # watch it; Ctrl+C stops watching only
pkill -f app/netbox_live_sync.py                     # stop it
```

**When you finish:**

1. Stop the service: `Ctrl+C`, or `pkill -f app/netbox_live_sync.py` if it runs in the background.
2. Stop the proxy: `./scripts/netbox_proxy.sh stop`.

### What a successful sync looks like

```
NetBox  complete · incremental · 0 changed in log · 5,424 cables · 0 differ · 0 missing · 2 API calls
Devices complete · 100 switches · 14,500 IB ports · 33.4 s · IPs: local cache
Sync complete in 34.2 s   NetBox 1.0 s ‖ IPs 0.0 s ‖ devices 34.2 s
```

- **NetBox.**
  - *incremental* means only cables listed in the NetBox change log were re-read.
  - *full* (the first sync, then every 6 hours) re-reads every backend cable.
  - *differ* counts cables whose NetBox endpoints no longer match the topology. *missing* counts cables that were deleted from NetBox.
- **Devices.**
  - The count rises (`37/100 switches · jump fan-out`) as switches return.
  - Each switch is shown on the diagram as soon as its result arrives.
  - If the text says *fell back to local fan-out*, the jump-host worker could not be used and the reason is shown next to it.

---

## 6. Using the dashboard

- **Status bar.** Shows LIVE or SNAPSHOT, when the devices and NetBox were last synced, and the Sync button. The page re-checks for new data every 15 seconds.
- **Canvas.**
  - Every spine and leaf has a status dot: green up, amber initializing, red down, orange changed in NetBox.
  - Leaf–spine links with a problem are drawn as dashed overlay lines.
  - A GPU port that is down is outlined, and its tray turns red.
  - The **Live status** chip turns this layer on or off.
- **Hover or click** a spine, leaf or tray to trace its cables. Use the search box to find a device by name (for example `bel12` or `gpu1300`).
- **Inspector (right panel).**
  - A live summary for the selected device.
  - A **Live** column in every cable table.
  - Click any **cable ID** to check that one cable against NetBox right now.
- **Live link state card.** Counts, the full problem list (down, initializing, NetBox changed or missing), and the UFM `fnm1` port states.

GPU-side RDMA ports are not collected; only the leaf side of each GPU link is checked.

---

## 7. Command-line options

| Option | Default | Use |
| --- | --- | --- |
| `--netbox-url` | *(required)* | `http://127.0.0.1:8444` (the local proxy) |
| `--device-profile` | — | `~/.config/idc-automation/device-access.ini`. Required for device collection. |
| `--fanout local\|jump` | `local` | `jump` runs one Teleport session to `jmp0`, which logs in to the switches in parallel. Recommended. Falls back to `local` automatically if it can't be used. |
| `--device-parallel N` | `10` | Concurrent switch logins (1–25). Raise gradually, for example 15 → 20 → 25, and watch for failures. |
| `--sync-every-minutes N` | `0` (off) | Background sync, so the dashboard is already fresh when you open it |
| `--full-netbox-every-hours H` | `6` | Do a full cable pull this often; between pulls the sync is incremental. `0` forces a full pull every time. |
| `--address-cache-hours H` | `24` | Reuse switch management IPs from NetBox for this long. `0` always re-queries. |
| `--netbox-page-size N` | `250` | Cables per NetBox page during a full pull |
| `--netbox-concurrency N` | `2` | NetBox pages fetched in parallel through the Teleport app proxy |
| `--netbox-host-header` | — | Only if the platform owner tells you a proxy needs it |
| `--port N` | `8765` | Local dashboard port |
| `--diagram`, `--connections`, `--devices`, `--commands`, `--known-hosts` | bundled | Override the bundled dashboard, topology, inventory, command file or host-key path |

---

## 8. How sync works

1. **Two phases run in parallel:**
   - **NetBox.**
     - The first run, and every 6 hours after that, reads every backend cable, with pages trimmed to the needed fields.
     - Other runs ask the NetBox **change log** which cables changed since the last sync: edits, deletions and re-terminations. They re-read only those, usually in 1–3 API calls.
   - **Devices.**
     - Switch management IPs come from 2 bulk NetBox queries, or from the 24-hour local cache.
     - Then `nv show interface --output json` is collected from all 100 switches.
2. **Jump-host fan-out.** With `--fanout jump`, one `tsh ssh` session starts a small worker on `jmp0`.
   - The switch password is passed only on the worker's input. It never appears in a command line, an environment variable or a file.
   - The worker logs in to each switch with strict host-key checking, using your approved `known_hosts`.
3. **Progressive results.** Each switch's result is shown as soon as it arrives. The latest evidence is saved locally, so a restart keeps it.

Typical timings from a production run: NetBox 1.0 s (incremental, 2 API calls), devices about 34 s for 100 switches at `--device-parallel 15`.

---

## 9. Maintenance

| Task | Command |
| --- | --- |
| Switch password changed | `./scripts/configure_device_access.sh` (overwrites the Keychain item and profile) |
| NetBox token expired or rotated | Create a new token ([4.3](#43-create-a-read-only-netbox-api-token)), then `./scripts/configure_netbox_token.sh` |
| Switch host keys changed (rebuild, RMA) | Get an updated approved file and rerun `./scripts/configure_known_hosts.sh` |
| Update the code | `git pull`, then restart the service |
| Roll back the code | `git log --oneline -5`, then `git checkout <commit>`. Return with `git checkout main`. |
| Reset local state (forces a full NetBox pull and fresh IPs) | Stop the service, then `rm -rf .netbox-live-sync` |

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `tsh: command not found` | Teleport CLI is not installed | See [section 3](#3-install-the-tools-on-your-mac) |
| `ERROR: Active profile expired.` | Teleport login expired | `tsh login`, then `./scripts/netbox_proxy.sh start` |
| `tsh apps ls` does not list NetBox | No app access | Request it ([section 2](#2-request-access)) |
| `access denied` on `tsh ssh …@jmp0` | No jump-host role, or the access request has expired | `tsh request create …`, then `tsh login --request-id=…` |
| `Host-rewrite proxy is already running.` but NetBox fails | The proxy is up but its Teleport forward stopped | `./scripts/netbox_proxy.sh status`. If it shows `tsh: stopped`: `stop`, then `start`. |
| `NetBox token was not found in macOS Keychain` | Step 4.4 was skipped, or done as a different macOS user | `./scripts/configure_netbox_token.sh` |
| NetBox step: `Cannot reach NetBox …` | Proxy not running, or Teleport expired | The two proxy and login rows above |
| NetBox step: `HTTP 403` | Token lacks read permission | Ask for read permission on devices, interfaces and cables |
| NetBox step always says `full` | Token cannot read the change log | Ask for change-log view permission, or accept full pulls |
| `unrecognized arguments: --fanout …` | Old copy of the code | `git pull` |
| `Local device-access inputs are missing: …` | Missing profile or host keys | Steps 4.5 and 4.7, and pass `--device-profile` |
| Devices: `fell back to local fan-out: worker unavailable` | No `python3` on `jmp0` | Ask for `python3` on the jump host, or drop `--fanout jump` |
| Devices: `Permission denied` | Wrong switch password, or the account is locked | `./scripts/configure_device_access.sh` |
| Devices: `Host key verification failed` | A switch's key changed, or is missing from `known_hosts` | Get an updated approved file ([4.7](#47-install-the-approved-switch-host-keys)). Never just accept a changed key. |
| Devices: some switches failed | The switch is unreachable or timed out | `ls -t .netbox-live-sync/` (newest first), then read `<run>/errors/<hostname>.txt` |
| Page shows `SNAPSHOT` and Sync says "needs the local service" | Opened as a file, or from a published copy | Open `http://127.0.0.1:8765/` |
| `Address already in use` | The service is already running | Use it, stop it with `pkill -f app/netbox_live_sync.py`, or pass `--port 8766` |

To see where the time goes, read the final Sync line (NetBox ‖ IPs ‖ devices). The terminal also prints one summary line per sync.

---

## 11. Security model

- **Read-only.** The service makes only NetBox `GET` requests, and runs only the commands in `assets/read_only_commands.txt` on switches.
- **Secrets live only in your Keychain:**
  - `netbox-mcp-token` for the NetBox token
  - `idc-automation-ice2-switch` for the switch password
- **Secrets never reach:**
  - the browser
  - this repository
  - log files
  - command lines
  - environment variables
- **Loopback only.** The service and proxy listen on `127.0.0.1` only, so other machines cannot reach them.
- **Strict host-key checking** is always on, against your approved `known_hosts`.
- **Local, Git-ignored state.** Runtime evidence stays on your Mac:
  - `.netbox-live-sync/` holds collections, the IP cache and the cable cache.
  - `local-inputs/` holds host keys.
- **Each user sets up their own credentials.** Never share a token, password, profile or host-key file, and never commit them.
- **Inventory changes are never automatic.** Changes such as re-terminating a cable in NetBox stay explicit, reviewed actions.

---

## 12. Remove everything

```bash
pkill -f app/netbox_live_sync.py; ./scripts/netbox_proxy.sh stop
security delete-generic-password -s netbox-mcp-token -a "$(id -un)"
security delete-generic-password -s idc-automation-ice2-switch -a <switch-user>
rm -f ~/.config/idc-automation/device-access.ini
rm -rf .netbox-live-sync local-inputs            # run inside ice2-netbox-dashboard
```

Also revoke the NetBox API token in NetBox (**API Tokens → delete**).

---

## 13. Reference

### HTTP API (local only)

| Method and path | Purpose |
| --- | --- |
| `GET /` | Dashboard |
| `GET /api/live` | Current live state (ETag and gzip; `304` when unchanged) |
| `POST /api/sync` · `GET /api/sync/<run>` | Start a sync, or check its progress and per-phase timings |
| `POST /api/refresh` · `GET /api/refresh/<run>` | Device collection only |
| `GET /api/verify/<cable_id>` | One cable: current NetBox record vs. live state |
| `GET /api/device/<hostname>` | NetBox details for one device |
| `GET /api/health` | NetBox reachability |

### Repository contents

| Path | Contents |
| --- | --- |
| `app/netbox_live_sync.py` | Local service: dashboard, sync and API |
| `collector/run_ntp_audit.py` | Read-only collector (local and jump-host fan-out) |
| `assets/dashboard.html` | Dashboard |
| `assets/connections.csv` | Backend topology baseline (5,424 cables) |
| `assets/devices.csv` | 100 backend switches with site and NetBox role |
| `assets/read_only_commands.txt` | The only command run on switches |
| `scripts/configure_netbox_token.sh` | Saves the NetBox token to Keychain |
| `scripts/configure_device_access.sh` | Saves the switch password to Keychain and writes your private profile |
| `scripts/configure_known_hosts.sh`, `scripts/collect_known_hosts.py` | Installs approved switch host keys |
| `scripts/netbox_proxy.sh`, `scripts/netbox_host_proxy.py` | Local NetBox proxy through Teleport |
| `config/device-access.example.ini` | Example profile (no secrets) |

### Files created on your Mac

| Location | Created by | Contents |
| --- | --- | --- |
| Keychain `netbox-mcp-token` | step 4.4 | NetBox token |
| Keychain `idc-automation-ice2-switch` | step 4.5 | Switch password |
| `~/.config/idc-automation/device-access.ini` | step 4.5 | Usernames, jump host, Keychain references |
| `local-inputs/known_hosts` | step 4.7 | Approved switch host keys |
| `~/.local/state/netbox-mcp/` | `netbox_proxy.sh` | Proxy PIDs and logs |
| `.netbox-live-sync/` | the service | Collected evidence, IP cache, cable cache |
