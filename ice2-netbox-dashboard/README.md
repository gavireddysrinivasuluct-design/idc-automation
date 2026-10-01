# ICE2 backend fabric dashboard (switches · UFM · NetBox)

A local, read-only dashboard for the ICE2 backend InfiniBand fabric: 36 spines, 64 leaves and the GPU trays. It checks the fabric against the **design topology** (`assets/expected_topology.csv`) using:

- **The switches**, which say which ports are up right now (`nv show interface --output json`).
- **UFM**, which says where every cable actually lands (live links or its fabric scan).
- **NetBox**, as inventory (management IPs, models) and as a record to keep correct. It is shown for comparison, not used as the truth.

Everything is button-driven by default: **⟳ Sync fabric** collects the switches and fetches from UFM, and **Refresh NetBox** updates the NetBox inventory. Nothing runs on a timer unless you ask for it (`--sync-every-minutes`, `--netbox-every-hours`, `--ufm-fetch-every-minutes`).

What you get:

- **Incidents:** one ranked list (critical, major, minor, info) of what is wrong in the fabric, with impact and action.
- **Cabling vs UFM:** every miscabled cable, with its current and expected connection, and whether it changes the topology or is only a port swap.
- **Live link state:** every designed link, as the switches report it.
- **The diagram and inspector:** spines, leaves and GPU trays, with live state, NetBox details and each tray's RDMA, frontend and out-of-band ports.

- **Read-only.** Nothing is ever written to NetBox, UFM or a device.
- **Secrets stay on your Mac.** Your NetBox token, switch password and UFM passwords are kept in your own macOS Keychain. They never reach the browser or this repository.

```
 Browser ──► http://127.0.0.1:8765  (app/netbox_live_sync.py, runs on your Mac)
                 │                          │
                 │ read-only REST           │ one Teleport session per sync (--fanout jump)
                 ▼                          ▼
   127.0.0.1:8444 → tsh app proxy      jmp0 ──ssh──────► 100 backend switches (nv show interface)
          → NetBox (prod)                   ├─https GET─► UFM REST: live links          (optional)
          (inventory, ~daily)               └─ssh───────► UFM host: master topology,    (optional)
                                                          Topology Compare, scan file
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
- [ ] Recommended: UFM access stored, so Sync fabric and **Fetch from UFM** can read UFM ([4.9](#49-optional-let-the-dashboard-fetch-from-ufm))

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
| *(Recommended)* A **UFM web (REST) user**, read-only if possible, and HTTPS from `jmp0` to the UFM addresses | Live links for the miscabling check and incidents ([4.9](#49-optional-let-the-dashboard-fetch-from-ufm)) |
| *(Optional)* A **UFM host SSH login** (the UFM CLI entry in 1Password, or a read-only account) | UFM's master topology and Topology Compare report ([4.9](#49-optional-let-the-dashboard-fetch-from-ufm)) |

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
  --fanout jump --device-parallel 15
```

The terminal prints `Open http://127.0.0.1:8765/`. In a **second** terminal you can check:

```bash
curl -s http://127.0.0.1:8765/api/health; echo        # expect "netbox": "reachable"
```

Open **http://127.0.0.1:8765/** in a browser and press **⟳ Sync fabric**. The first sync also does a **full** NetBox pull, because there is no inventory yet. A successful result looks like this:

```
Switches complete · verified · 100/100 switches · 14,500 IB ports · 33.4 s · IPs: netbox bulk
UFM      complete · live links from 10.1.67.190 · 8 miscabled · 1,120 trays     (or "skipped" until 4.9 is done)
NetBox   complete · full · 5,424 cables · 0 differ · 0 missing · 25 API calls
Sync complete in 34.2 s   devices … ‖ IPs … ‖ UFM … ‖ NetBox …
```

Later syncs show **NetBox skipped · use Refresh NetBox to update the inventory**. Press **Refresh NetBox** after someone fixes NetBox records; it should say **incremental** and need only about 2 API calls. If it always says **full**, your token cannot read the change log (see [section 2](#2-request-access)).

The switch and NetBox setup is done. Do 4.9 as well: without UFM access, the miscabling check and most incidents have no data.

### 4.9 Optional: let the dashboard fetch from UFM

The miscabling check ([6.1](#61-cabling-vs-ufm-miscabling-check)) needs UFM's view of the fabric. To let the dashboard's **⟳ Fetch from UFM** button get it by itself, store one or both UFM logins once:

```bash
./scripts/configure_ufm_access.sh
# prompts: UFM addresses [10.1.67.190 10.1.67.191]
#          1) UFM web (REST) user   -> live links          (Enter = skip)
#          2) UFM host SSH login    -> master topology etc. (Enter = skip)
```

| Login | What the button gets with it | How fresh |
| --- | --- | --- |
| **1. UFM web (REST) user** (recommended) | UFM's live link list, `GET /ufmRest/resources/links` | Live: what UFM sees right now |
| **2. UFM host SSH login** | UFM's master topology and Topology Compare report, plus UFM's periodic scan file | The scan is as old as UFM's last scan (often hours) |

With both logins, the button uses live links for the comparison and refreshes the master topology and report over SSH at most every 6 hours. With only the web user, it keeps the master topology from your last SSH or terminal fetch.

- Passwords (from your 1Password vault) go into the Keychain items `idc-automation-ice2-ufm-rest` (web) and `idc-automation-ice2-ufm` (SSH). They are never written to a file, a command line or Git.
- The script adds a `[ufm]` section (users and addresses only) to your private profile `~/.config/idc-automation/device-access.ini`. Run it again to change a login; Enter keeps what is stored.
- Use read-only logins where they exist. The web user only needs to read resources (the button sends GET requests only). The SSH login only needs to run `docker exec ufm`.
- `jmp0` must reach the UFM web port (HTTPS 443). Test from `jmp0`: `curl -sk -u <web-user> https://10.1.67.190/ufmRest/resources/links -o /dev/null -w '%{http_code}\n'` should print `200`.
- UFM uses a self-signed certificate. The first successful fetch records its fingerprint in `local-inputs/ufm/tls-pins.json`, and later fetches refuse a different certificate.
- For the SSH login, your account on `jmp0` must already trust the UFM host keys. Run `ssh <login>@10.1.67.190 hostname` once from `jmp0` (and `10.1.67.191`) and check the fingerprint with the platform owner.

Restart `netbox_live_sync.py` after running the script. Without this step, use `./scripts/fetch_ufm_scan.sh` in a terminal instead.

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
  --fanout jump --device-parallel 15
```

Then open **http://127.0.0.1:8765/** and press **⟳ Sync fabric**. Or start everything with one double-click: **`scripts/open_dashboard.command`** (see below).

Nothing refreshes by itself unless you add one of these to the start command:

- `--sync-every-minutes 15`: sync the switches and UFM every 15 minutes (the first automatic sync runs 15 minutes after start).
- `--netbox-every-hours 24`: also refresh NetBox about once a day.

**One-click start.** `scripts/open_dashboard.command` (double-click it in Finder, or run it in Terminal) checks Teleport, starts the NetBox proxy if needed, starts the service from **this** repository with your profile, and opens the browser. If port 8765 is already used by an **older** copy of the dashboard (for example the earlier standalone `ice2-live-data` folder and its `Open ICE2 Live Dashboard.command`), it shows that process and asks before stopping it, so you never look at an old version by mistake. Add options with `ICE2_DASHBOARD_ARGS="--sync-every-minutes 15" ./scripts/open_dashboard.command`.

**If NetBox is down,** the fabric sync still works: switch IPs come from the last good copy (the step says `IPs: cached IPs from …`), and the NetBox step shows the error and keeps the previous inventory.

Always open the dashboard at this local address. Opened as a `file://` page, it shows only its saved snapshot and Sync cannot run.

**UFM data** (after [4.9](#49-optional-let-the-dashboard-fetch-from-ufm)) is read by every **⟳ Sync fabric**, including the automatic ones. **⟳ Fetch from UFM** in the *Cabling vs UFM* tab reads UFM alone. Without 4.9, run `./scripts/fetch_ufm_scan.sh` in a second terminal. See [6.1](#61-cabling-vs-ufm-miscabling-check).

**To run it in the background,** so you can close the terminal:

```bash
mkdir -p ~/Library/Logs
nohup python3 app/netbox_live_sync.py --netbox-url 'http://127.0.0.1:8444' \
  --device-profile "$HOME/.config/idc-automation/device-access.ini" \
  --fanout jump --device-parallel 15 \
  > ~/Library/Logs/ice2-dashboard.log 2>&1 &
tail -f ~/Library/Logs/ice2-dashboard.log           # watch it; Ctrl+C stops watching only
pkill -f app/netbox_live_sync.py                     # stop it
```

**When you finish:**

1. Stop the service: `Ctrl+C`, or `pkill -f app/netbox_live_sync.py` if it runs in the background.
2. Stop the proxy: `./scripts/netbox_proxy.sh stop`.

### What a successful sync looks like

```
Switches complete · verified · 100/100 switches · 14,500 IB ports · 33.4 s · IPs: local cache
UFM      complete · live links from 10.1.67.190 · 8 miscabled · 1,120 trays
NetBox   skipped · use Refresh NetBox to update the inventory · last Oct 02, 09:12
Sync complete in 34.2 s   devices 34.2 s ‖ IPs 0.0 s ‖ UFM 6.9 s
```

- **Switches.**
  - The count rises (`37/100 switches · jump fan-out`) as switches return, and each switch is shown on the diagram as soon as its result arrives.
  - *IPs: local cache* means the management IPs came from the 24-hour cache. *cached IPs from … (NetBox unavailable)* means NetBox could not be reached and the last known IPs were used.
  - If the text says *fell back to local fan-out*, the jump-host worker could not be used and the reason is shown next to it.
- **UFM.** *live links* (with a UFM web user) is what UFM sees right now; *scan file* is UFM's last periodic scan. *skipped* means [4.9](#49-optional-let-the-dashboard-fetch-from-ufm) is not set up.
- **NetBox** (when it runs, or after **Refresh NetBox**).
  - *incremental* means only cables listed in the NetBox change log were re-read; *full* (the first time, then every 6 hours) re-reads every backend cable.
  - *differ* counts NetBox cables whose endpoints changed since the export; *missing* counts cables deleted from NetBox.

---

## 6. Using the dashboard

- **Status bar.** Says how far the switch evidence can be trusted, when the switches, UFM and NetBox inventory were last read, and has the **Refresh NetBox** and **⟳ Sync fabric** buttons. The page re-checks for new data every 15 seconds.

  | Label | Meaning |
  | --- | --- |
  | **VERIFIED · 100/100 switches** | The last collection reached every inventory switch, and each one reported every designed port |
  | **PARTIAL · 98/100 switches** | Some switches were not reached, or did not report designed ports. Their links show as *not verified*, never as up. |
  | **STALE** | The last collection is older than `--stale-after-minutes` (60), or was made against a different topology or inventory file |
  | **NOT VERIFIED** | No collection yet since the service started with these files; press **⟳ Sync fabric** |
- **Canvas.**
  - Every spine and leaf has a status dot: green up, amber initializing, red down, orange changed in NetBox.
  - Leaf–spine links with a problem are drawn as dashed overlay lines.
  - A GPU port that is down is outlined, and its tray turns red.
  - The **Live status** chip turns this layer on or off.
- **Hover or click** a spine, leaf or tray to trace its cables. Use the search box to find a device by name (for example `bel12` or `gpu1300`).
- **Inspector (right panel).**
  - A live summary for the selected device.
  - The device's NetBox **vendor, model, management IP and status**, taken from the most recent NetBox refresh. Clicking a device makes no NetBox call, so this works even when the NetBox proxy is down. A device not seen by any sync yet is looked up once, then remembered.
  - A **Live** column in every cable table.
  - Click any **cable ID** to check that one cable against NetBox right now.
  - A **GPU tray** shows its four RDMA links as UFM sees them, and its frontend (eth0/eth1 to the SpectrumX leaves) and out-of-band ports (BMC, OS MGMT, BF MGMT) from NetBox. UFM sees only the RDMA fabric, and the frontend and OOB switches are not collected, so those have no live state.
- **Incident banner** under the status bar: how many critical, major, minor and info incidents there are, and the most severe one. **View incidents** opens the Incidents tab. See [6.2](#62-incidents).
- **Check tabs** below the diagram: **Incidents**, **Cabling vs UFM** and **Live link state**. Each tab shows a count of items to review, or ✓ when there are none. Click a tab or use the arrow keys to switch; the page remembers your last tab.
- **Live link state tab.** Every designed link in use, as the switches report it: counts, the problem list (down, initializing, port not reported, plus NetBox records that changed or are missing), the switches not reached by the last collection, and the UFM `fnm1` port states. A `—` in the cable column means NetBox has no cable record for that designed link.
  - A **leaf–spine** link is *up* only when **both** switches were reached and both ends are Active.
  - A **GPU** link is *up* when its leaf end is Active; the GPU side is never collected.
  - A link with an end on a switch that the last collection did not reach is **not verified**. Its last known state is listed for reference only.
  - **Port not reported** means the switch answered but did not list a designed port (renamed, breakout changed, or not an IB port).

GPU-side RDMA ports are not collected by the switch sync; only the leaf side of each GPU link is checked there. The UFM cabling check below covers both ends.

### 6.1 Cabling vs UFM (miscabling check)

The live sync tells you whether each port is *up*. This check tells you whether each cable goes *where the fabric design says it should go*. It compares the **expected** connection for every port with the **current** connection as UFM sees it, with both ends of every link: UFM's live links (REST) or its periodic fabric scan. Nothing is sent to the fabric.

**The reference is the design, not NetBox.** NetBox can be wrong too, so it is reported next to each link (*agrees with the design*, *records the current cabling*, *differs*, or *missing*) but is never used as the truth.

The design topology is the file `assets/expected_topology.csv`. It has one row per expected link: 4,608 leaf–spine and 4,608 leaf–GPU rows. It is generated from the design rules in `scripts/build_expected_topology.py`:

| Rule | Expected connection |
| --- | --- |
| L1 | Leaf *j* port `sw(36+i)pM` ⟷ spine *i* port `sw(j)pM` (two cables per leaf–spine pair) |
| G1 | 4 pods of 16 leaves. Scalable unit *k* of a pod takes the *k*-th leaf of each of its 4 rail blocks. |
| G2 | Leaf port `swNpM` (N ≤ 36) is tray slot 2·(N−1)+M of that SU. A tray uses the same slot on all four of its leaves. |
| G3 | Rail *r* reaches the tray's adapter `mlx5_(r−1)` (RDMA *r*) |

If the design changes, edit the rules and run `python3 scripts/build_expected_topology.py`, or edit the CSV directly for a one-off exception. The dashboard picks up the change automatically.

**UFM's master topology as a second reference.** UFM keeps its own reference, the *master topology* (`/opt/ufm/shared_config_files/periodicTopo/master.topo`). It copies the master to `/opt/ufm/data/fabric.topo` every night and runs its own Topology Compare against it. The master records how the fabric looked *on the day someone saved it*, which isn't necessarily how it was designed. So the check shows the master next to each cable, and that tells you *since when* a difference exists:

| Current vs design | Current vs master | Meaning |
| --- | --- | --- |
| ✓ | ✓ | Correct |
| ✗ | ✓ | Miscabled, and **already like this in the master**. UFM's own compare treats it as correct and never flags it. |
| ✗ | ✗ | Miscabled, and **changed since the master** (for example a recent move or RMA) |
| ✓ | ✗ | Changed since the master, and now as designed (fixed, or the master was wrong) |

The tab's *UFM master topology* section shows:

- when the master was saved and what it covers
- how many links agree in design, master and current
- which hostnames the master knows for adapters that are unnamed today
- a plain summary of UFM's own Topology Compare report

UFM's report compares only against its master, so it mostly lists trays added after the master was saved, not cabling errors. After fixing the miscabled cables, save a new master in UFM so that its nightly compare becomes meaningful again.

**Load or refresh from the dashboard.** Press **⟳ Fetch from UFM** at the top of the *Cabling vs UFM* tab. With a UFM web user ([4.9](#49-optional-let-the-dashboard-fetch-from-ufm)), it reads UFM's **live links**, so the comparison shows what is plugged in right now. Otherwise it reads UFM's latest periodic scan file.

- It needs the one-time setup in [4.9](#49-optional-let-the-dashboard-fetch-from-ufm). Until then, the button shows the command to run.
- Progress shows next to the button: connecting to `jmp0`, reading from UFM, saving, comparing. A fetch usually takes a few seconds.
- When it is done, the button line says what was read (*live links* or *scan file*), the UFM host, the time taken, the number of miscabled cables, the trays seen and the master date. The tab, GPU area and inspector update without a page reload.
- Live links: one HTTPS GET from `jmp0` to the UFM REST API. Scan file: the same three files as the script below. Either way it is one Teleport session, and passwords go from Keychain to the jump host on standard input only.
- If live links fail (for example a wrong web password) and an SSH login is stored, it falls back to the scan file and shows a ⚠ note.
- Live links list one record per cable when all four planes are up, and one record per plane otherwise; both become the same four-plane lanes as the scan file. GPU adapter names (`nvl72dXXX-TNN mlx5_N`) are matched by GUID from the last scan file, because the REST API shows host:interface names. If a live result names far fewer leaf–spine links than the last scan, it is refused and the previous data stays.
- Live links show which ports are connected, not link training states. A cable stuck in *Init* still appears in the **Live link state** tab, which reads the switches.
- If the first UFM address does not answer (for example it is the standby), it tries the next one.
- If the fetch fails, the reason is shown and the previous files stay in use.
- With `--ufm-fetch-every-minutes N`, the service also fetches by itself every N minutes.

**Or load it from a terminal.** This needs no stored UFM password. Run it in the project folder:

```bash
./scripts/fetch_ufm_scan.sh
```

The script:

- connects through `jmp0` to the active UFM, trying `10.1.67.190` and then `10.1.67.191`
- asks once for the UFM host password, which is typed into `ssh` on the jump host and never stored
- copies three files UFM already writes, in one session, into `local-inputs/ufm/` (ignored by Git):
  - the current fabric scan, saved as `ibdiagnet2.lst.gz`
  - UFM's master topology, saved as `master.topo.gz` with its original save date
  - UFM's latest Topology Compare report, saved as `topology-compare.json.gz`

The dashboard picks up the new file automatically; reload the page if it is open. If you have a read-only login on the UFM host, use it with `UFM_USER=<user> ./scripts/fetch_ufm_scan.sh` instead of the default `root`.

**What changes on the dashboard when a scan is loaded:**

- **GPU area, redrawn from UFM.** It shows 4 pods × 4 scalable units × 72 tray slots, with every GPU tray in the slot where UFM actually sees it. Tile colours:

  | Tile | Meaning |
  | --- | --- |
  | Solid | The tray matches NetBox |
  | Blue, dashed | UFM sees the tray, but it is not in NetBox |
  | Amber | A rail link is missing or not Active |
  | Red | Wiring error: wrong rail, slot or host |
  | Grey `?` | The adapter has no name, so the tray can't be identified |
  | Faint | Empty slot |

  The four small bars in each tile are the tray's rails 1–4. Click a tray to see its four links, adapter by adapter, with the NetBox cable for each.
- **Leaves** take their rail colour and show their real GPU downlink count from UFM.
- **Miscabled leaf–spine cables** are drawn in **magenta** on the mesh, and both switches get a ◆ marker. The **Miscabling** chip turns the layer on or off.
- **Cabling vs UFM tab**, in six sections:
  1. Miscabled cables, grouped per leaf. Each one shows the **Current** connection (UFM), the **Expected** connection (design) and the **Master** connection, end to end. It also gives the re-patch instruction, the impact (*port swap · no fabric impact* or *topology change*, see [6.2](#62-incidents)) and whether NetBox agrees with the design.
  2. GPU trays per scalable unit.
  3. Trays needing attention.
  4. Links not fully Active, or where NetBox differs from the design.
  5. Adapters without a name.
  6. UFM master topology.
- **Downloads** from the tab:
  - **Findings CSV**: every difference, one row each, for a ticket or a spreadsheet. Columns include `expected_connection`, `current_connection_ufm`, `netbox_connection`, `netbox_vs_expected` and the fix.
  - **NetBox import CSV**: every GPU cable UFM sees but NetBox lacks, in NetBox's cable bulk-import columns. The UFM tray name is in `label`. Fill in `side_b_device` (the tray's NetBox host) before importing in NetBox (*Cables → Import*).

**How it matches the two sources.** Each Q3400 switch is four chips, one per plane, so each 800G cable appears as four 200G lanes on the same port. UFM numbers ports in hex, and NVOS `swNpM` is port 2·(N−1)+M. UFM names GPU adapters by rack and tray (`nvl72d031-T14 mlx5_2`); live links are converted to the same lanes, as described above. The tray's NetBox host is learned from the NetBox cables that end on its adapters. Each switch's internal chip-to-chip links, the SHARP aggregation nodes and UFM's own links are left out.

### 6.2 Incidents

The **Incidents** tab turns everything the dashboard knows into one ranked list: what is broken, what it affects, and what to do. It uses only data already collected (the UFM data from **Fetch from UFM**, and the last device sync), so it adds no load on the fabric. It updates whenever new UFM data or a new sync arrives.

| Severity | Meaning | Examples it detects |
| --- | --- | --- |
| **Critical** | Fabric-wide, or many GPUs affected now | A spine or leaf with no links left; a quarter or more of leaf–spine capacity missing; 18 or more GPU trays gone offline; no UFM data at all |
| **Major** | Hurts jobs or routing | Miscabling that changes the topology (a leaf with more cables to one spine and fewer to another); planes of one cable on different far ends; leaf–leaf or spine–spine links; a GPU tray on the wrong rail, SU or slot; a tray running without all four rails; GPU trays that went offline; a leaf or spine losing 1/8 or more of its links; NetBox GPU hosts missing from the fabric; UFM's own fabric links degraded; switches the sync could not read; many down ports on one switch |
| **Minor** | Single links, or labels only | Individual leaf–spine cables not up, in *Init*, or missing planes; GPU adapters not fully active; **port swaps with no fabric impact** |
| **Info** | Documentation and data | NetBox differs from the design; unnamed adapters; trays missing from NetBox; miscabling baked into UFM's master; UFM data older than 6 hours; a failed UFM fetch |

Each incident shows its impact, the action to take, a link to the tab with the details, and the affected cables, ports or trays.

**Miscabling impact.** Each miscabled leaf–spine cable is also labelled in *Cabling vs UFM*:

- **Port swap · no fabric impact:** every leaf still has its designed number of cables to every spine; only port positions differ (for example the BEL21 sw49 ↔ sw50 cages). Routing and bandwidth are unaffected; labels, runbooks and port-based maintenance are wrong. Fix in a maintenance window.
- **Topology change · affects routing:** some leaf–spine pairs have more or fewer cables than designed. That means uneven bandwidth and hot spots, and the fat-tree routing may not hold. Fix soon.
- **Planes split:** the four planes of one cable land on different far ends.

**Offline GPU trays.** The service remembers when it last saw each GPU tray (`.netbox-live-sync/tray-history.json`, kept 7 days). A tray that was on the fabric and is missing from newer UFM data is reported as offline, with its last-seen time and leaf ports. Live links ([4.9](#49-optional-let-the-dashboard-fetch-from-ufm)) make this current to the minute.

What it cannot see: link errors, congestion and UFM alarms (not collected yet), GPU-side health, and *Init* states when using live links (the scan file and the Live link state tab still show those).

---

## 7. Command-line options

| Option | Default | Use |
| --- | --- | --- |
| `--netbox-url` | *(required)* | `http://127.0.0.1:8444` (the local proxy) |
| `--device-profile` | — | `~/.config/idc-automation/device-access.ini`. Required for device collection. |
| `--fanout local\|jump` | `local` | `jump` runs one Teleport session to `jmp0`, which logs in to the switches in parallel. Recommended. Falls back to `local` automatically if it can't be used. |
| `--device-parallel N` | `10` | Concurrent switch logins (1–25). Raise gradually, for example 15 → 20 → 25, and watch for failures. |
| `--sync-every-minutes N` | `0` (off) | Background fabric sync (switches and UFM), so the dashboard is already fresh when you open it |
| `--stale-after-minutes N` | `60` | Show the switch evidence as **STALE** when the last collection is older than this |
| `--netbox-every-hours H` | `0` (off) | Also refresh NetBox inventory this often, in the background and on **Sync fabric**. With `0`, NetBox is read only with **Refresh NetBox**, and on the first **Sync fabric** when there is no inventory yet. |
| `--full-netbox-every-hours H` | `6` | Do a full cable pull this often; between pulls the sync is incremental. `0` forces a full pull every time. |
| `--address-cache-hours H` | `24` | Reuse switch management IPs from NetBox for this long. `0` always re-queries. |
| `--netbox-page-size N` | `250` | Cables per NetBox page during a full pull |
| `--netbox-concurrency N` | `2` | NetBox pages fetched in parallel through the Teleport app proxy |
| `--netbox-host-header` | — | Only if the platform owner tells you a proxy needs it |
| `--expected-topology PATH` | `assets/expected_topology.csv` | Designed topology the cabling check compares against ([6.1](#61-cabling-vs-ufm-miscabling-check)) |
| `--ufm-master PATH` | `local-inputs/ufm/master.topo.gz` | UFM's master topology, the second reference ([6.1](#61-cabling-vs-ufm-miscabling-check)) |
| `--ufm-report PATH` | `local-inputs/ufm/topology-compare.json.gz` | UFM's latest Topology Compare report, summarized in the tab |
| `--ufm-fetch-every-minutes N` | `0` (off) | Fetch from UFM by itself every N minutes, in addition to the fabric sync. Only needed if you want UFM more often than `--sync-every-minutes`. Needs [4.9](#49-optional-let-the-dashboard-fetch-from-ufm). |
| `--ufm-scan PATH` | `local-inputs/ufm/ibdiagnet2.lst.gz` | UFM fabric scan used by the cabling check ([6.1](#61-cabling-vs-ufm-miscabling-check)). Plain or gzip-compressed. |
| `--port N` | `8765` | Local dashboard port |
| `--diagram`, `--connections`, `--devices`, `--commands`, `--known-hosts` | bundled | Override the bundled dashboard, NetBox cable export, switch inventory, command file or host-key path |

---

## 8. How sync works

1. **⟳ Sync fabric runs these phases in parallel:**
   - **Switches.**
     - Switch management IPs, vendor, model and status come from 2 bulk NetBox queries, or from the 24-hour local cache. If NetBox cannot be reached, the last known IPs are used.
     - Then `nv show interface --output json` is collected from all 100 switches.
   - **UFM**, when [4.9](#49-optional-let-the-dashboard-fetch-from-ufm) is set up: the same as **Fetch from UFM**.
   - **NetBox**, only on the first sync (no inventory yet), or when `--netbox-every-hours` is set and the inventory is older than that. **Refresh NetBox** runs this phase alone.
     - A full pull reads every backend cable, with pages trimmed to the needed fields; it runs the first time and every `--full-netbox-every-hours` (6).
     - Other runs ask the NetBox **change log** which cables changed since the last refresh, and re-read only those, usually in 1–3 API calls.
     - A NetBox failure never fails a fabric sync; the previous copy stays in use.
   - **Live link state** compares the switch port states with the **design topology**: every designed leaf–spine link, and every designed GPU port in use (seen by UFM now or before, or recorded in NetBox), so empty tray slots are not reported as down. NetBox cable IDs are shown where NetBox has the cable.
2. **Coverage and replacement.** When the collection finishes, it is checked against the 100 inventory switches and the designed ports of each:
   - Each reached switch's ports **replace** its previous ports entirely, so a port it no longer reports is not kept.
   - Switches not reached keep their last known states only for display; their links count as *not verified*.
   - The result is *verified* (every switch, every designed port) or *partial*, and is shown as such in the status bar, the Sync line and the Incidents tab.
   - The snapshot is saved **atomically** (temporary file, then rename) to `.netbox-live-sync/latest-live.json`, with the coverage, each switch's last-read time and a fingerprint of `connections.csv`, `expected_topology.csv` and `devices.csv`. After a restart it is trusted only if those files are unchanged; otherwise everything shows as not verified until the next sync.
3. **Strict NetBox validation.** A NetBox cable must have exactly one interface termination on each side, with a device and port. A cable with several terminations, a front/rear port, or a malformed termination is reported as differing, never as matching.
4. **Jump-host fan-out.** With `--fanout jump`, one `tsh ssh` session starts a small worker on `jmp0`.
   - The switch password is passed only on the worker's input. It never appears in a command line, an environment variable or a file.
   - The worker logs in to each switch with strict host-key checking, using your approved `known_hosts`.
5. **Progressive results.** Each switch's result is shown as soon as it arrives.

Typical timings: switches about 34 s for 100 switches at `--device-parallel 15`; UFM live links 3–10 s, in parallel; NetBox about 1 s when incremental (2 API calls).

---

## 9. Maintenance

| Task | Command |
| --- | --- |
| Switch password changed | `./scripts/configure_device_access.sh` (overwrites the Keychain item and profile) |
| UFM web or host password changed | `./scripts/configure_ufm_access.sh`, then restart the service |
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
| NetBox step: `Cannot reach NetBox …` | Proxy not running, or Teleport expired. The fabric sync still runs with the last known IPs. | The two proxy and login rows above, then **Refresh NetBox** |
| NetBox step: `HTTP 403` | Token lacks read permission | Ask for read permission on devices, interfaces and cables |
| NetBox step always says `full` | Token cannot read the change log | Ask for change-log view permission, or accept full pulls |
| `unrecognized arguments: --fanout …` | Old copy of the code | `git pull` |
| `Local device-access inputs are missing: …` | Missing profile or host keys | Steps 4.5 and 4.7, and pass `--device-profile` |
| Devices: `fell back to local fan-out: worker unavailable` | No `python3` on `jmp0` | Ask for `python3` on the jump host, or drop `--fanout jump` |
| Devices: `Permission denied` | Wrong switch password, or the account is locked | `./scripts/configure_device_access.sh` |
| Devices: `Host key verification failed` | A switch's key changed, or is missing from `known_hosts` | Get an updated approved file ([4.7](#47-install-the-approved-switch-host-keys)). Never just accept a changed key. |
| Devices: some switches failed | The switch is unreachable or timed out | `ls -t .netbox-live-sync/` (newest first), then read `<run>/errors/<hostname>.txt` |
| Status bar says **PARTIAL** | Switches not reached, or designed ports not reported | The Sync line and the Incidents tab name them; see the errors folder above. Their links show as *not verified* until a sync reaches them. |
| Status bar says **STALE** after a restart or `git pull` | The topology or inventory files changed since the saved evidence | Press **⟳ Sync fabric** |
| Cable check: "cannot be verified: 2 A-side terminations" | The NetBox cable has several terminations, or ends on a front/rear port | Fix the cable record in NetBox: one interface on each side |
| Cabling tab says "No UFM data yet" | Nothing fetched from UFM yet | Press **⟳ Fetch from UFM**, or run `./scripts/fetch_ufm_scan.sh` |
| Sync: `UFM skipped · UFM access not set up` | [4.9](#49-optional-let-the-dashboard-fetch-from-ufm) not done, or the service was not restarted after it | `./scripts/configure_ufm_access.sh`, then restart the service |
| Switches: `IPs: cached IPs from … (NetBox unavailable)` | NetBox or its proxy is down; the sync used the last known IPs | Nothing urgent. Fix the proxy (rows above) so IPs and inventory stay current. |
| Incidents: "UFM live links look incomplete" | UFM answered with far fewer links than the last scan (UFM restarting, or an unknown format) | The previous data is kept; fetch again later, and open an issue if it persists |
| Fetch from UFM: "UFM access is not set up yet" | No `[ufm]` section in your profile, or the service was not restarted | `./scripts/configure_ufm_access.sh`, then restart the service |
| Fetch from UFM: "The UFM password is not in Keychain" | Keychain item missing or renamed | `./scripts/configure_ufm_access.sh` |
| Fetch from UFM: "Teleport login expired" | Teleport session ended | `tsh login`, then press the button again |
| Fetch from UFM: `HTTP 401 Unauthorized` | Wrong UFM web user or password | `./scripts/configure_ufm_access.sh` with the current web password |
| Fetch from UFM: `ConnectionRefusedError` or `timed out` for every address | `jmp0` cannot reach the UFM web port | Check the `curl` test in [4.9](#49-optional-let-the-dashboard-fetch-from-ufm); ask for HTTPS from `jmp0` to UFM |
| Fetch from UFM: `TLS certificate changed` | UFM's certificate was replaced (or something is intercepting) | Confirm the new certificate with the UFM owner, then delete that address from `local-inputs/ufm/tls-pins.json` |
| Fetch from UFM: "none had node descriptions" | This UFM version names link fields differently | Open an issue with the field list from the message |
| Fetch from UFM: `Permission denied` | Wrong or changed UFM password | `./scripts/configure_ufm_access.sh` with the current password |
| Fetch from UFM: `Host key verification failed` | Your `jmp0` account does not yet trust the UFM host key, or the key changed | From `jmp0`, run `ssh <login>@10.1.67.190 hostname` once and check the fingerprint ([4.9](#49-optional-let-the-dashboard-fetch-from-ufm)) |
| `fetch_ufm_scan.sh`: "No UFM host returned a fabric scan" | Wrong UFM password, both UFM hosts unreachable from `jmp0`, or UFM not running | Check `ssh root@10.1.67.190` from `jmp0` works. Set `UFM_HOSTS` if the UFM addresses changed. |
| GPU area still shows the NetBox drawing (4 SUs) | No UFM scan loaded, or the page was opened as a file | Fetch a scan and open `http://127.0.0.1:8765/` |
| Page shows `SNAPSHOT` and Sync says "needs the local service" | Opened as a file, or from a published copy | Open `http://127.0.0.1:8765/` |
| `Address already in use` | The service is already running | Use it, stop it with `pkill -f app/netbox_live_sync.py`, or pass `--port 8766` |

To see where the time goes, read the final Sync line (devices ‖ IPs ‖ UFM ‖ NetBox). The terminal also prints one summary line per sync.

---

## 11. Security model

- **Read-only.** The service makes only NetBox `GET` requests, runs only the commands in `assets/read_only_commands.txt` on switches, and only reads from UFM: `GET /ufmRest/resources/links`, and `docker exec ufm` reading three files UFM already writes.
- **UFM's certificate is pinned** on first use (`local-inputs/ufm/tls-pins.json`), and UFM host keys are checked against your `jmp0` account's `known_hosts`.
- **Secrets live only in your Keychain:**
  - `netbox-mcp-token` for the NetBox token
  - `idc-automation-ice2-switch` for the switch password
  - `idc-automation-ice2-ufm-rest` and `idc-automation-ice2-ufm` for the UFM web and host passwords (optional, [4.9](#49-optional-let-the-dashboard-fetch-from-ufm))
- **Secrets never reach:**
  - the browser
  - this repository
  - log files
  - command lines
  - environment variables
- **Loopback only.** The service and proxy listen on `127.0.0.1` only, so other machines cannot reach them.
- **Strict host-key checking** is always on, against your approved `known_hosts`.
- **Local, Git-ignored state.** Runtime evidence stays on your Mac:
  - `.netbox-live-sync/` holds collections, the IP cache, the cable cache and the tray history.
  - `local-inputs/` holds host keys and the files read from UFM.
- **Each user sets up their own credentials.** Never share a token, password, profile or host-key file, and never commit them.
- **Inventory changes are never automatic.** Changes such as re-terminating a cable in NetBox stay explicit, reviewed actions.

---

## 12. Remove everything

```bash
pkill -f app/netbox_live_sync.py; ./scripts/netbox_proxy.sh stop
security delete-generic-password -s netbox-mcp-token -a "$(id -un)"
security delete-generic-password -s idc-automation-ice2-switch -a <switch-user>
security delete-generic-password -s idc-automation-ice2-ufm -a <ufm-login>        # only if you did 4.9
security delete-generic-password -s idc-automation-ice2-ufm-rest -a <ufm-web-user> # only if you did 4.9
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
| `GET /api/live` | Live link state for every designed link in use, plus sync, UFM-fetch and incident summaries (ETag and gzip; `304` when unchanged) |
| `POST /api/sync` · `GET /api/sync/<run>` | Start a fabric sync (switches, UFM, and NetBox when due), or check its progress and per-phase timings |
| `POST /api/netbox/refresh` | NetBox inventory only (progress at `GET /api/sync/<run>`) |
| `POST /api/refresh` · `GET /api/refresh/<run>` | Device collection only |
| `GET /api/verify/<cable_id>` | One cable: current NetBox record vs. live state |
| `GET /api/device/<hostname>` | NetBox details for one device, from the last sync (add `?live=1` to query NetBox now) |
| `GET /api/health` | NetBox reachability |
| `GET /api/incidents` | Ranked fabric incidents ([6.2](#62-incidents)); counts and the top three are also in `/api/live` |
| `GET /api/cabling` | Cabling vs UFM report (ETag and gzip) |
| `GET /api/cabling/findings.csv` | Every cabling difference, one row each |
| `POST /api/cabling/fetch` · `GET /api/cabling/fetch/<run>` | Fetch from UFM now (live links or files), or check that fetch's progress |
| `GET /api/cabling/netbox-import.csv` | GPU cables UFM sees but NetBox lacks, in NetBox import columns |

### Repository contents

| Path | Contents |
| --- | --- |
| `app/netbox_live_sync.py` | Local service: dashboard, sync and API |
| `assets/expected_topology.csv` | Designed topology: the reference for the cabling check |
| `scripts/build_expected_topology.py` | Design rules that generate `expected_topology.csv` |
| `app/ufm_cabling.py` | Cabling vs UFM check (also runs on its own: `python3 app/ufm_cabling.py <scan>`) |
| `app/ufm_fetch.py` | Fetch from UFM: live links over the UFM REST API, or UFM's files, through `jmp0` (read-only) |
| `app/incidents.py` | Incident rules: severity, impact and action for each problem found ([6.2](#62-incidents)) |
| `collector/run_ntp_audit.py` | Read-only collector (local and jump-host fan-out) |
| `assets/dashboard.html` | Dashboard |
| `assets/connections.csv` | NetBox cable export (5,424 cables): the diagram and the NetBox comparison |
| `assets/devices.csv` | 100 backend switches with site and NetBox role |
| `assets/read_only_commands.txt` | The only command run on switches |
| `scripts/configure_netbox_token.sh` | Saves the NetBox token to Keychain |
| `scripts/configure_device_access.sh` | Saves the switch password to Keychain and writes your private profile |
| `scripts/configure_known_hosts.sh`, `scripts/collect_known_hosts.py` | Installs approved switch host keys |
| `scripts/netbox_proxy.sh`, `scripts/netbox_host_proxy.py` | Local NetBox proxy through Teleport |
| `scripts/open_dashboard.command` | One-click start of this repository's dashboard (checks Teleport and the proxy; refuses to reuse an older copy) |
| `scripts/fetch_ufm_scan.sh` | Copies UFM's latest fabric scan to `local-inputs/ufm/` (read-only) |
| `scripts/configure_ufm_access.sh` | Saves the UFM web and/or host password to Keychain for the Fetch from UFM button |
| `config/device-access.example.ini` | Example profile (no secrets) |

### Files created on your Mac

| Location | Created by | Contents |
| --- | --- | --- |
| Keychain `netbox-mcp-token` | step 4.4 | NetBox token |
| Keychain `idc-automation-ice2-switch` | step 4.5 | Switch password |
| Keychain `idc-automation-ice2-ufm-rest`, `idc-automation-ice2-ufm` | step 4.9 | UFM web and host passwords |
| `~/.config/idc-automation/device-access.ini` | steps 4.5, 4.9 | Usernames, jump host, UFM addresses, Keychain references |
| `local-inputs/known_hosts` | step 4.7 | Approved switch host keys |
| `~/.local/state/netbox-mcp/` | `netbox_proxy.sh` | Proxy PIDs and logs |
| `.netbox-live-sync/` | the service | Collected evidence, IP cache, cable cache, device details (`netbox-devices.json`) |
| `local-inputs/ufm/ibdiagnet2.lst.gz` | Fetch from UFM or `fetch_ufm_scan.sh` | UFM fabric scan for the cabling check. The previous copy is kept as `ibdiagnet2.lst.previous.gz`. |
| `local-inputs/ufm/master.topo.gz`, `topology-compare.json.gz` | Fetch from UFM or `fetch_ufm_scan.sh` | UFM's master topology and its latest Topology Compare report |
| `.netbox-live-sync/tray-history.json` | The service | When each GPU tray was last seen, to report trays that go offline |
| `local-inputs/ufm/links.json.gz`, `tls-pins.json` | Fetch from UFM (live links) | UFM's raw live link list, and the pinned UFM certificate fingerprints |
