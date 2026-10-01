# ICE2 NetBox + live device dashboard

A local, read-only dashboard for the ICE2 backend InfiniBand fabric: 36 spines, 64 leaves and the GPU trays. It compares two sources:

- **NetBox**, which says what *should* be cabled.
- **The switches**, which say what is actually up right now (`nv show interface --output json`).

Every link in the diagram is coloured by its live state. One **Sync** button refreshes both sources.

Nothing is ever written to NetBox or to a device. Your NetBox token and switch password stay in your own macOS Keychain and never reach the browser or this repository.

```
 Browser ──► http://127.0.0.1:8765  (app/netbox_live_sync.py, runs on your Mac)
                 │                          │
                 │ read-only REST           │ one Teleport session (--fanout jump)
                 ▼                          ▼
   127.0.0.1:8444 → tsh app proxy      jmp0 ──ssh──► 100 backend switches
          → NetBox (prod)                     (read-only nv show interface)
```

---

## 1. Requirements

### Access (request these from the platform/network owner)

| What | Why | How to check |
| --- | --- | --- |
| Teleport account on `nscale.teleport.sh` | Everything goes through Teleport | `tsh login` succeeds |
| Teleport app access to `netbox-prod-europe-west2-netbox` | NetBox API through the local proxy | `tsh apps ls` lists it |
| Teleport login on the jump host `jmp0` | Reaching switch management IPs | `tsh ssh <login>@jmp0 hostname` |
| Switch SSH account (read-only is enough) | Running `nv show interface` | Manual SSH from `jmp0` works |
| Personal **read-only** NetBox API token | Reading cables, devices and IPs | Created in NetBox → *Profile → API Tokens* |
| *(Recommended)* NetBox permission to view the change log | Fast incremental cable sync | Otherwise every sync does a full pull, which still works but is slower |
| Approved SSH `known_hosts` for the switches | Strict host-key checking | Issued by the platform owner (see step 6) |

### Software

| Where | Requirement |
| --- | --- |
| Your Mac | macOS with Keychain (`security`), **Python 3.8+** (`python3 --version`), Teleport CLI **`tsh`**, `git` |
| Jump host `jmp0` | `python3` is needed for the fast `--fanout jump` mode (`tsh ssh <login>@jmp0 python3 --version`). Without it, the service falls back to one Teleport session per switch automatically. |

No Python packages need to be installed; only the standard library is used.

---

## 2. One-time setup

Run everything from the project folder:

```bash
git clone https://github.com/gavireddysrinivasuluct-design/idc-automation.git
cd idc-automation/ice2-netbox-dashboard
```

**Step 1. Log in to Teleport.** A login lasts about 8 hours.

```bash
tsh login
```

**Step 2. Store your NetBox token in Keychain.** Paste the token when Keychain prompts for it. It is saved as Keychain item `netbox-mcp-token` for your macOS user.

```bash
./scripts/configure_netbox_token.sh
```

**Step 3. Store your switch login and create your private profile.** The script asks for:

- your switch SSH username
- the jump host (default `jmp0`)
- your Teleport username

Then Keychain asks for the switch password. The script writes `~/.config/idc-automation/device-access.ini` with mode `600`. That file contains no secrets, only names and Keychain references.

```bash
./scripts/configure_device_access.sh
```

**Step 4. Start the local NetBox proxy.** It runs at `http://127.0.0.1:8444`.

```bash
./scripts/netbox_proxy.sh start
./scripts/netbox_proxy.sh status      # both "tsh" and "proxy" must say running
```

**Step 5. Check that NetBox answers through the proxy.** This should print the NetBox version JSON.

```bash
curl -s -H "Authorization: Token $(security find-generic-password -s netbox-mcp-token -a "$(id -un)" -w)" \
  http://127.0.0.1:8444/api/status/ | head -c 200; echo
```

**Step 6. Install the approved switch host keys.** Use one of these two options:

```bash
# Option A (preferred): a file issued by the platform owner
./scripts/configure_known_hosts.sh                 # asks for the file path

# Option B: collect live through jmp0, then verify fingerprints independently
./scripts/configure_known_hosts.sh --collect-live
```

Both options install `local-inputs/known_hosts`, which Git ignores.

Host keys are trust material. Keys collected live with `ssh-keyscan` must be checked against an approved fingerprint or baseline before use.

---

## 3. Daily use

```bash
cd idc-automation/ice2-netbox-dashboard
git pull                                   # get the latest dashboard and fixes
tsh status || tsh login                    # renew the login if it has expired
./scripts/netbox_proxy.sh status           # if tsh shows "stopped": stop, then start
./scripts/netbox_proxy.sh start            # skip if both are already running

python3 app/netbox_live_sync.py \
  --netbox-url 'http://127.0.0.1:8444' \
  --device-profile "$HOME/.config/idc-automation/device-access.ini" \
  --fanout jump --device-parallel 15 --sync-every-minutes 15
```

Open **http://127.0.0.1:8765/** and press **⟳ Sync NetBox + devices**.

Open it through this local address only. Opened as a `file://` page, the diagram shows its saved snapshot and Sync cannot run.

When you finish, stop the service with `Ctrl+C`, then stop the proxy:

```bash
./scripts/netbox_proxy.sh stop
```

### What a successful sync looks like

```
NetBox  complete · incremental · 0 changed in log · 5,424 cables · 0 differ · 0 missing · 2 API calls
Devices complete · 100 switches · 14,500 IB ports · 33.4 s · IPs: local cache
Sync complete in 34.2 s   NetBox 1.0 s ‖ IPs 0.0 s ‖ devices 34.2 s
```

- **NetBox.**
  - *incremental* means only cables in the NetBox change log were re-read.
  - *full* (the first sync, then every 6 h) re-reads every backend cable.
  - *differ* counts cables whose NetBox endpoints no longer match the topology. *missing* counts cables deleted from NetBox.
- **Devices.**
  - The count rises (`37/100 switches · jump fan-out`) as switches return.
  - Each switch is shown on the diagram as soon as its result arrives.
  - If the text says *fell back to local fan-out*, the jump-host worker could not be used and the reason is shown next to it.

---

## 4. Using the dashboard

- **Status bar.** Shows LIVE or SNAPSHOT, when the devices and NetBox were last synced, and the Sync button. The page re-checks for new data every 15 s.
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
- **Live link state card.** Counts plus the full problem list (down, initializing, NetBox changed or missing), and the UFM `fnm1` port states.

GPU-side RDMA ports are not collected; only the leaf side of each GPU link is checked.

---

## 5. Command-line options

| Option | Default | Use |
| --- | --- | --- |
| `--netbox-url` | *(required)* | `http://127.0.0.1:8444` (the local proxy) |
| `--device-profile` | — | `~/.config/idc-automation/device-access.ini`. Needed for device collection. |
| `--fanout local\|jump` | `local` | `jump` runs one Teleport session to `jmp0`, which then logs in to the switches in parallel. Recommended. It falls back to `local` automatically if it can't be used. |
| `--device-parallel N` | `10` | Concurrent switch logins (1–25). Raise gradually, for example 15 → 20 → 25, and watch for failures. |
| `--sync-every-minutes N` | `0` (off) | Background sync, so the dashboard is already fresh when opened. The first scheduled run starts N minutes after launch. |
| `--full-netbox-every-hours H` | `6` | Do a full cable pull this often; between full pulls the sync is incremental. Set `0` to force a full pull every time. |
| `--address-cache-hours H` | `24` | Reuse switch management IPs from NetBox for this long. Set `0` to always re-query. |
| `--netbox-page-size N` | `250` | Cables per NetBox page for a full pull |
| `--netbox-concurrency N` | `2` | NetBox pages fetched in parallel through the Teleport app proxy |
| `--netbox-host-header` | — | Only if the platform owner tells you a proxy needs it |
| `--port N` | `8765` | Local dashboard port |
| `--diagram`, `--connections`, `--devices`, `--commands`, `--known-hosts` | bundled | Override the bundled dashboard, topology, inventory, command file or host-key path |

---

## 6. How sync works

1. **In parallel:**
   - **NetBox phase.** On the first run, and every 6 h after that, it reads every backend cable, with pages trimmed to the needed fields. On other runs it asks the NetBox change log which cables changed since the last sync (edits, deletions and re-terminations) and re-reads only those, usually in 1–3 API calls.
   - **Device phase.** It gets switch management IPs from 2 bulk NetBox queries (or the 24 h local cache), then collects `nv show interface --output json` from all 100 switches.
2. With `--fanout jump`, one `tsh ssh` session starts a small worker on `jmp0`.
   - The switch password is passed only on the worker's input, never in a command line, environment variable or file.
   - The worker logs in to each switch with strict host-key checking, using your approved `known_hosts`.
3. Results are applied to the diagram as each switch returns. The latest evidence is saved locally, so a restart keeps it.

Typical timings from a production run: NetBox 1.0 s (incremental, 2 API calls), devices about 34 s for 100 switches at `--device-parallel 15`.

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ERROR: Active profile expired.` | Teleport login expired | `tsh login`, then `./scripts/netbox_proxy.sh start` |
| `Host-rewrite proxy is already running.`, then NetBox errors | The proxy is up but its Teleport forward (`tsh`) stopped | `./scripts/netbox_proxy.sh status`. If it shows `tsh: stopped`: `stop`, then `start`. |
| NetBox step: `Cannot reach NetBox …` | Proxy not running, or Teleport login expired | Same as the two rows above |
| NetBox step: `HTTP 403` | Token lacks read permission | Ask for a read-only token with the needed object permissions |
| NetBox step always says `full` | Token cannot read the change log | Ask for change-log view permission, or accept full pulls |
| `unrecognized arguments: --fanout …` | Old copy of the repository | `git pull` |
| Devices: `fell back to local fan-out: worker unavailable` | `python3` is missing on `jmp0` | Ask for `python3` on the jump host, or run without `--fanout jump` |
| Devices: `Permission denied` | Wrong switch password, or the account is locked | Re-run `./scripts/configure_device_access.sh` |
| Devices: `Host key verification failed` | A switch's host key changed or is missing | Get an updated approved `known_hosts` (setup step 6). Never just accept a changed key. |
| Devices: some switches failed | The switch is unreachable or timed out | Check `.netbox-live-sync/<newest run>/errors/<hostname>.txt` (`ls -t .netbox-live-sync/` lists newest first) |
| Page says `SNAPSHOT` and Sync shows "needs the local service" | Page opened as a file or from the published copy | Open `http://127.0.0.1:8765/` |
| `Address already in use` | The service is already running | Use the running one, or stop it (`Ctrl+C`) or pass `--port 8766` |

To see exactly where time goes, read the final Sync line (NetBox ‖ IPs ‖ devices). The terminal also prints one summary line per sync.

---

## 8. Security model

- **Read-only.** The service makes only NetBox `GET` requests and runs only the commands in `assets/read_only_commands.txt` on switches.
- **Secrets stay in Keychain:**
  - Keychain item `netbox-mcp-token`, for the NetBox token.
  - Keychain item `idc-automation-ice2-switch`, for the switch password.
- **Neither secret ever reaches:**
  - the browser
  - this repository
  - log files
  - command lines
  - environment variables
- **Loopback only.** The service and proxy listen on `127.0.0.1` only.
- **Strict host-key checking** is always on, against your approved `known_hosts`.
- **Local, Git-ignored state.** Runtime evidence stays on your Mac in Git-ignored paths:
  - `.netbox-live-sync/` holds collections, the IP cache and the cable cache.
  - `local-inputs/` holds host keys.
- **Inventory changes are never automatic.** Changes such as re-terminating a cable in NetBox remain explicit, reviewed actions.

---

## 9. Updating and rolling back

```bash
git pull                     # update
git log --oneline -5         # see what changed
git checkout <commit>        # roll back temporarily; `git checkout main` to return
```

To start from a clean local state (this forces a full NetBox pull and fresh IPs on the next sync):

```bash
rm -rf .netbox-live-sync
```

---

## 10. HTTP API (local only)

| Method and path | Purpose |
| --- | --- |
| `GET /` | Dashboard |
| `GET /api/live` | Current live state (ETag and gzip; `304` when unchanged) |
| `POST /api/sync` · `GET /api/sync/<run>` | Start a sync, or check its progress and per-phase timings |
| `POST /api/refresh` · `GET /api/refresh/<run>` | Device collection only |
| `GET /api/verify/<cable_id>` | One cable: current NetBox record vs. live state |
| `GET /api/device/<hostname>` | NetBox details for one device |
| `GET /api/health` | NetBox reachability |

---

## 11. Repository contents

| Path | Contents |
| --- | --- |
| `app/netbox_live_sync.py` | Local service: dashboard, sync, API |
| `collector/run_ntp_audit.py` | Read-only collector (local and jump-host fan-out) |
| `assets/dashboard.html` | Dashboard |
| `assets/connections.csv` | Backend topology baseline: 5,424 cables |
| `assets/devices.csv` | 100 backend switches with site and NetBox role |
| `assets/read_only_commands.txt` | The only command run on switches |
| `scripts/configure_netbox_token.sh` | Saves the NetBox token to Keychain |
| `scripts/configure_device_access.sh` | Saves the switch password to Keychain and writes your private profile |
| `scripts/configure_known_hosts.sh`, `scripts/collect_known_hosts.py` | Installs approved switch host keys |
| `scripts/netbox_proxy.sh`, `scripts/netbox_host_proxy.py` | Local NetBox proxy through Teleport |
| `config/device-access.example.ini` | Example profile (no secrets) |

Never commit credentials, tokens, host keys or collected evidence. `.gitignore` already excludes the local paths above.
