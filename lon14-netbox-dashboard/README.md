# LON14 backend fabric dashboard (switches · UFM · NetBox)

> **This is the LON14 copy of the ICE2 dashboard** (`../ice2-netbox-dashboard`). It runs side by side with it: its own port (**8766**), its own profile (`~/.config/idc-automation/device-access-lon14.ini`, section `[lon14]`), its own Keychain items (`idc-automation-lon14-*`) and its own local state. NetBox and its Teleport proxy (127.0.0.1:8444) are shared.
>
> | | LON14 (this dashboard) | ICE2 |
> | --- | --- | --- |
> | Fabric | `sys1-lon14` InfiniBand: 64 BEL + 36 BES (Q3400), 1,152 GB300 NVL72 trays, all 16 SUs cabled | 64 BEL + 36 BES, 4 SUs populated |
> | SU → leaves | SU *k* = BEL4*k*−3 … 4*k* (consecutive); rail *r* = BEL4(*k*−1)+*r* | SU *k* = *k*-th leaf of each rail block |
> | Leaf uplink | leaf *j* `sw(73−i)p(3−M)` ⟷ spine *i* `sw(j)pM` | leaf *j* `sw(36+i)pM` ⟷ spine *i* `sw(j)pM` |
> | UFM | `sys1-lon14-p-phy-ufm1/2` 10.2.64.75 / .76 (ib0/ib1 → BEL16/32 and BEL12/28 `fnm1`) | 10.1.67.190 / .191 |
> | Jump host | `lon14deploy1` (Teleport node) | `jmp0` |
> | Design file | optional (`design_path`, default `/root/nscale_Compute.topo`; `none` = inferred rules) | required |
>
> **Two fabrics, two tabs.** LON14 has two separate GPU backends: **SYS1** (`sys1-lon14`, InfiniBand, this page) and **SYS2** (`sys2-lon14`, Ethernet, Spectrum-X SN5610: 256 BEL + 72 BES, 1,152 hosts). The **SYS2 · ETHERNET** tab at the top shows SYS2; see [15](#15-sys2-tab-ethernet-backend). Known NetBox anomaly (SYS1): gpu1153 is recorded in gpu118's slot (SU2 slot 46, `sw23p2` on BEL5–8) and gpu118 has no backend cables.
>
> **Refreshing the NetBox-derived files** (`assets/devices.csv`, `assets/connections.csv` and the data embedded in `assets/dashboard.html`): with the NetBox proxy running, run `python3 scripts/lon14_discover.py` (switches and every switch cable at site lon14) and optionally `python3 scripts/lon14_inventory.py` (racks, U, serials), then `python3 scripts/build_site_data.py`. All three are read-only and write only to `local-inputs/netbox/` (not tracked). The live service also re-reads NetBox itself (**Refresh NetBox**).

A local, read-only dashboard for the LON14 backend InfiniBand fabric: 36 spines, 64 leaves and the GPU trays. It checks the fabric against the **design topology** (`assets/expected_topology.csv`) using:

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
 Browser ──► http://127.0.0.1:8766  (app/netbox_live_sync.py, runs on your Mac)
                 │                          │
                 │ read-only REST           │ one Teleport session per sync (--fanout jump)
                 ▼                          ▼
   127.0.0.1:8444 → tsh app proxy      lon14deploy1 ──ssh──────► 100 backend switches (nv show interface)
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
14. [Tests and CI](#14-tests-and-ci)
15. [SYS2 tab: Ethernet backend](#15-sys2-tab-ethernet-backend)

---

## 1. Onboarding checklist

Tick these off in order. Each item links to the step that explains it.

- [ ] Teleport account works: `tsh login` ([2](#2-request-access), [4.2](#42-log-in-to-teleport-and-check-your-access))
- [ ] NetBox Teleport app is visible: `tsh apps ls` shows `netbox-prod-europe-west2-netbox`
- [ ] Jump host is reachable: `tsh ssh <login>@lon14deploy1 hostname`
- [ ] Switch login works from `lon14deploy1` ([4.2](#42-log-in-to-teleport-and-check-your-access))
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
| Teleport **app** access to `netbox-prod-europe-west2-netbox` (the LON14 NetBox) | Reading NetBox through a local proxy |
| A Teleport **login on the jump host `lon14deploy1`** | Reaching the switch management network |
| A **switch SSH account** for the LON14 backend switches; read-only is enough | Running `nv show interface` |
| NetBox **read** permission for devices, interfaces and cables | Cable and IP lookups |
| *(Recommended)* NetBox permission to **view the change log** (object changes) | Fast incremental sync. Without it every sync does a full pull, which still works but is slower. |
| An approved **switch `known_hosts` file**, or approved fingerprints | Strict SSH host-key checking ([4.7](#47-install-the-approved-switch-host-keys)) |
| *(Recommended)* A **UFM web (REST) user**, read-only if possible, and HTTPS from `lon14deploy1` to the UFM addresses | Live links for the miscabling check and incidents ([4.9](#49-optional-let-the-dashboard-fetch-from-ufm)) |
| *(Optional)* A **UFM host SSH login** (the UFM CLI entry in 1Password, or a read-only account) | UFM's master topology and Topology Compare report ([4.9](#49-optional-let-the-dashboard-fetch-from-ufm)) |

Some Teleport access is granted through **access requests** rather than permanently. If `tsh ssh …@lon14deploy1` is denied, request the role your team uses for jump-host access:

```bash
tsh request search --kind node            # see what you can request
tsh request create --roles <role-name> --reason "LON14 dashboard"
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

**On the jump host,** the fast `--fanout jump` mode needs `python3` on `lon14deploy1`. You'll check this in step 4.2. If it's missing, the service still works, just more slowly.

---

## 4. One-time setup

> Unless stated otherwise, run every command from the project folder `idc-automation/lon14-netbox-dashboard`.

### 4.1 Get the code

```bash
cd ~                                       # or wherever you keep projects
git clone https://github.com/gavireddysrinivasuluct-design/idc-automation.git
cd idc-automation/lon14-netbox-dashboard
chmod +x scripts/*.sh                      # only needed if the scripts are not executable
```

### 4.2 Log in to Teleport and check your access

```bash
tsh login --proxy=nscale.teleport.sh       # first time; later just `tsh login`
tsh status                                 # shows your logins and "Valid until"
tsh apps ls | grep -i netbox               # must list netbox-prod-europe-west2-netbox
tsh ls | grep -i lon14deploy1                      # must list the jump host
```

Note the login name shown under **Logins** in `tsh status`. You'll use it as `<login>` below.

Check the jump host:

```bash
tsh ssh <login>@lon14deploy1 'hostname; python3 --version; which ssh'
```

- If `python3` prints a version, `--fanout jump` will work.
- If it says *command not found*, use the default `--fanout local` or ask for `python3` on `lon14deploy1`.

Check your switch login once by hand from `lon14deploy1`. Replace the address with any backend switch's management IP from NetBox:

```bash
tsh ssh --tty <login>@lon14deploy1
ssh <switch-user>@<switch-mgmt-ip> 'nv show interface --output json | head -c 200'
exit
```

A Teleport login lasts about **8 hours**. Renew it with `tsh login` when it expires.

### 4.3 Create a read-only NetBox API token

1. Open NetBox through Teleport. Either use the Teleport web UI (**Applications → netbox-prod-europe-west2-netbox**) or run `tsh apps login netbox-prod-europe-west2-netbox` and open the URL it prints.
2. In NetBox, click your user name (top right), then **API Tokens** (on some versions it's **Profile → API Tokens**), then **Add a token**.
3. Leave **Write enabled** *unticked*. That makes the token read-only.
4. Set an **expiry date** that follows your team's policy, and a description such as `LON14 dashboard – <your name>`.
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
| `Approved Teleport jump host [lon14deploy1]` | Press Enter for `lon14deploy1` |
| `Teleport username [...]` | Your Teleport **login** from `tsh status` |
| Keychain password prompt | Your **switch** password |

It stores the password in Keychain item **`idc-automation-lon14-switch`**. It also writes your private profile to **`~/.config/idc-automation/device-access-lon14.ini`** with mode `600`. The profile contains names and Keychain references only, never the password.

### 4.6 Start the local NetBox proxy and test it

```bash
./scripts/netbox_proxy.sh start
./scripts/netbox_proxy.sh status          # both lines must say "running"
```

The proxy listens on **`http://127.0.0.1:8444`**. It forwards to the Teleport app **`netbox-prod-europe-west2-netbox`**, using the address Teleport reports for it (`./scripts/netbox_proxy.sh status` shows both). To use a different NetBox app, put `NETBOX_TELEPORT_APP=<app name from tsh apps ls>` in `~/.config/idc-automation/netbox.env`, then run `./scripts/netbox_proxy.sh stop` and `start`. If the app does not exist, `start` says so and lists the NetBox apps you can use. Check that another NetBox actually holds LON14 before switching (for example `/api/dcim/devices/?name=sys1-lon14-p-swi-bel1` returns `"count": 1`). Test it:

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

**Option B.** Collect the keys live through `lon14deploy1`, then verify them. This needs the proxy from step 4.6 running:

```bash
./scripts/configure_known_hosts.sh --collect-live
# prompts: jump host [lon14deploy1], Teleport login
```

Option B reads the device list, looks up each management IP in NetBox, and runs `ssh-keyscan` from `lon14deploy1`. **Before you trust the result**, compare the fingerprints with an approved baseline from the platform owner:

```bash
ssh-keygen -lf local-inputs/known_hosts
```

Either option installs **`local-inputs/known_hosts`** (mode `600`, ignored by Git). Any earlier file is kept as `known_hosts.previous`.

### 4.8 First run and verification

```bash
python3 app/netbox_live_sync.py \
  --netbox-url 'http://127.0.0.1:8444' \
  --device-profile "$HOME/.config/idc-automation/device-access-lon14.ini" \
  --fanout jump --device-parallel 15
```

The terminal prints `Open http://127.0.0.1:8766/`. In a **second** terminal you can check:

```bash
curl -s http://127.0.0.1:8766/api/health; echo        # expect "netbox": "reachable"
```

Open **http://127.0.0.1:8766/** in a browser and press **⟳ Sync fabric**. The first sync also does a **full** NetBox pull, because there is no inventory yet. A successful result looks like this:

```
Switches complete · verified · 100/100 switches · 14,500 IB ports · 33.4 s · IPs: netbox bulk
UFM      complete · live links from 10.2.64.75 · 4 crossed pairs · 1,120 trays     (or "skipped" until 4.9 is done)
NetBox   complete · full · 9,216 cables · 0 differ · 0 missing · 25 API calls
Sync complete in 34.2 s   devices … ‖ IPs … ‖ UFM … ‖ NetBox …
```

Later syncs show **NetBox skipped · use Refresh NetBox to update the inventory**. Press **Refresh NetBox** after someone fixes NetBox records; it should say **incremental** and need only about 2 API calls. If it always says **full**, your token cannot read the change log (see [section 2](#2-request-access)).

The switch and NetBox setup is done. Do 4.9 as well: without UFM access, the miscabling check and most incidents have no data.

### 4.9 Optional: let the dashboard fetch from UFM

The miscabling check ([6.1](#61-cabling-vs-ufm-miscabling-check)) needs UFM's view of the fabric. To let the dashboard's **⟳ Fetch from UFM** button get it by itself, store one or both UFM logins once:

```bash
./scripts/configure_ufm_access.sh
# prompts: UFM addresses [10.2.64.75 10.2.64.76]
#          1) UFM web (REST) user   -> live links          (Enter = skip)
#          2) UFM host SSH login    -> master topology etc. (Enter = skip)
```

| Login | What the button gets with it | How fresh |
| --- | --- | --- |
| **1. UFM web (REST) user** (recommended) | UFM's live link list, `GET /ufmRest/resources/links` | Live: what UFM sees right now |
| **2. UFM host SSH login** | UFM's master topology, Topology Compare report, periodic scan file, and approved `/root/nscale_Compute.topo` | The scan is as old as UFM's last scan (often hours); the design is copied on every fetch |

With both logins, the button uses live links for the comparison and refreshes the approved design topology, master topology and report over SSH on every fetch. The SSH login is required: the dashboard will not compare new UFM data with an older local design file.

- Passwords (from your 1Password vault) go into the Keychain items `idc-automation-lon14-ufm-rest` (web) and `idc-automation-lon14-ufm` (SSH). They are never written to a file, a command line or Git.
- The script adds a `[ufm]` section (users and addresses only) to your private profile `~/.config/idc-automation/device-access-lon14.ini`. Run it again to change a login; Enter keeps what is stored.
- Use read-only logins where they exist. The web user only needs to read resources (the button sends GET requests only). The SSH login only needs to run `docker exec ufm`.
- `lon14deploy1` must reach the UFM web port (HTTPS 443). Test from `lon14deploy1`: `curl -sk -u <web-user> https://10.2.64.75/ufmRest/resources/links -o /dev/null -w '%{http_code}\n'` should print `200`.
- UFM uses a self-signed certificate. The first successful fetch records its fingerprint in `local-inputs/ufm/tls-pins.json`, and later fetches refuse a different certificate.
- For the SSH login, your account on `lon14deploy1` must already trust the UFM host keys. Run `ssh <login>@10.2.64.75 hostname` once from `lon14deploy1` (and `10.2.64.76`) and check the fingerprint with the platform owner.

Restart `netbox_live_sync.py` after running the script. Without this step, use `./scripts/fetch_ufm_scan.sh` in a terminal instead.

---

## 5. Daily use

```bash
cd ~/idc-automation/lon14-netbox-dashboard
git pull                                   # get the latest dashboard and fixes
tsh status || tsh login                    # renew Teleport if it has expired
./scripts/netbox_proxy.sh status           # if "tsh: stopped": run stop, then start
./scripts/netbox_proxy.sh start            # skip if both already say running

python3 app/netbox_live_sync.py \
  --netbox-url 'http://127.0.0.1:8444' \
  --device-profile "$HOME/.config/idc-automation/device-access-lon14.ini" \
  --fanout jump --device-parallel 15
```

Then open **http://127.0.0.1:8766/** and press **⟳ Sync fabric**. Or start everything with one double-click: **`scripts/open_dashboard.command`** (see below).

Nothing refreshes by itself unless you add one of these to the start command:

- `--sync-every-minutes 15`: sync the switches and UFM every 15 minutes (the first automatic sync runs 15 minutes after start).
- `--netbox-background`: also refresh NetBox in the background when it is due. Without it, **Sync fabric** refreshes NetBox (incrementally, from its change log) whenever the copy is older than `--netbox-every-hours` (24).

**One-click start.** `scripts/open_dashboard.command` (double-click it in Finder, or run it in Terminal) checks Teleport, starts the NetBox proxy if needed, starts the service from **this** repository with your profile, and opens the browser. If port 8766 is already used by an **older** copy of the dashboard (for example an older copy of this folder), it shows that process and asks before stopping it, so you never look at an old version by mistake. If the running service is from this repository but older than its code (after `git am` or `git pull`), it restarts it automatically: the service reports a code fingerprint in `/api/live` (`code_version`), and the launcher compares it with `python3 app/netbox_live_sync.py --code-version`. Add options with `LON14_DASHBOARD_ARGS="--sync-every-minutes 15" ./scripts/open_dashboard.command`.

**If NetBox is down,** the fabric sync still works: switch IPs come from the last good copy (the step says `IPs: cached IPs from …`), and the NetBox step shows the error and keeps the previous inventory.

Always open the dashboard at this local address. Opened as a `file://` page, it shows only its saved snapshot and Sync cannot run.

**UFM data** (after [4.9](#49-optional-let-the-dashboard-fetch-from-ufm)) is read by every **⟳ Sync fabric**, including the automatic ones. **⟳ Fetch from UFM** in the *Cabling vs UFM* tab reads UFM alone. Without 4.9, run `./scripts/fetch_ufm_scan.sh` in a second terminal. See [6.1](#61-cabling-vs-ufm-miscabling-check).

**To run it in the background,** so you can close the terminal:

```bash
mkdir -p ~/Library/Logs
nohup python3 app/netbox_live_sync.py --netbox-url 'http://127.0.0.1:8444' \
  --device-profile "$HOME/.config/idc-automation/device-access-lon14.ini" \
  --fanout jump --device-parallel 15 \
  > ~/Library/Logs/lon14-dashboard.log 2>&1 &
tail -f ~/Library/Logs/lon14-dashboard.log           # watch it; Ctrl+C stops watching only
pkill -f app/netbox_live_sync.py                     # stop it
```

**When you finish:**

1. Stop the service: `Ctrl+C`, or `pkill -f app/netbox_live_sync.py` if it runs in the background.
2. Stop the proxy: `./scripts/netbox_proxy.sh stop`.

### What a successful sync looks like

```
Switches complete · verified · 100/100 switches · 14,500 IB ports · 33.4 s · IPs: local cache
UFM      complete · live links from 10.2.64.75 · 4 crossed pairs · 1,120 trays
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
- **Hover or click** a spine, leaf or tray to trace its cables.
- **Search** (top of the page): type part of a hostname, short name, management IP, designed GPU host, NetBox host or UFM tray name, for example `bel21`, `bes13`, `10.2.97.60`, `gpu345` or a UFM tray name. Results list the device type, where it is (SU, slot, leaf ports) and its state; use the arrow keys and Enter, or click. Choosing a GPU tray opens its pod and selects it.
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

The live sync tells you whether each port is *up*. This check tells you whether each cable goes *where NetBox and the approved design say it goes*. It compares the **expected** connection for every port with the **current** connection as UFM sees it, with both ends of every link: UFM's live links (REST) or its periodic fabric scan. Nothing is sent to the fabric.

**Source of truth: NetBox + approved design (expected), UFM live links (actual).**

- **Expected = NetBox and the approved design together.**
  - NetBox: the leaf–spine and leaf–GPU cables NetBox records, as read by the last **Refresh NetBox** (`.netbox-live-sync/netbox-cables.json`). Until NetBox has been read once, the bundled export `assets/connections.csv` is used, and the tab and the Incidents tab say so. A NetBox cable that is not exactly one interface on each side is left out and listed in Incidents.
  - The approved design: `local-inputs/ufm/nscale_Compute.topo`, refreshed from the UFM host with each fetch (below).
- **Actual = UFM.** Live links (REST), or the periodic fabric scan.

For every leaf–spine port:

| NetBox vs design | UFM sees | Result |
| --- | --- | --- |
| agree | the same far end | as expected |
| agree | a different far end | **miscabled**: re-patch (section 1) |
| differ | the design's far end | **NetBox record to correct** (section 4, minor documentation incident) |
| differ | NetBox's far end | **NetBox and design disagree**: confirm which is intended (section 4, minor documentation incident) |
| differ | neither | **miscabled**, flagged that NetBox and the design disagree |
| only one has the port | — | that one is the expected far end |

GPU trays are checked against the design's port and adapter for every slot, and against their NetBox cables (host and RDMA port) where NetBox has them. NetBox holds all 4,608 GPU RDMA cables at LON14.

As of 5 Oct 2026, NetBox and the approved design agree on all 4,608 leaf–spine cables, so the 8 crossed cables on BEL21 and BEL55 are confirmed by both.

Other modes, with `--cabling-reference` (for example `LON14_DASHBOARD_ARGS="--cabling-reference design" ./scripts/open_dashboard.command`): `netbox` makes NetBox the reference with the design as a cross-check only; `design` makes the approved design the reference with NetBox shown alongside.

From a terminal: `python3 app/ufm_cabling.py local-inputs/ufm/ibdiagnet2.lst.gz` prints the same comparison (`--reference netbox` or `--reference design` for the other modes).

**The approved design and the rules:**

1. **The approved design: `local-inputs/ufm/nscale_Compute.topo`.** This is the signed-off compute-fabric topology kept on the UFM host as `/root/nscale_Compute.topo` (IBDM `.topo` format, physical ports `P1`–`P144`, where `swNpM` = 2·(N−1)+M). It lists every leaf–spine cable and the designed host and adapter on every GPU port. **LON14 may have no such file:** set `design_path = none` in the `[ufm]` profile section (or answer `none` in `configure_ufm_access.sh`) and the inferred rules below are the cross-check. Copy it once, keeping its date:

   ```bash
   # on lon14deploy1
   scp -p root@10.2.64.75:/root/nscale_Compute.topo ~/
   # on your Mac, in lon14-netbox-dashboard
   tsh scp <login>@lon14deploy1:nscale_Compute.topo local-inputs/ufm/nscale_Compute.topo
   shasum -a 256 local-inputs/ufm/nscale_Compute.topo   # compare with sha256sum on the UFM host
   ```

   Every UFM fetch copies `/root/nscale_Compute.topo` from the UFM host, but that copy is only a **candidate**: the comparison uses the **approved** copy, so an edit on the operational UFM host can never silently redefine the expected cabling (see [6.3](#63-evidence-design-approval-and-validation)). Each tray's **Design host** is shown in the inspector. Entries that cannot be physically right are not trusted: for example the current file lists `gpu1345 mlx5_0` (and three similar hosts) on four leaves at once. Those ports use the inferred rule instead, and the evidence bar's validation warnings list them so the design owner can correct the file.
2. **The inferred rules: `assets/expected_topology.csv`**, used for ports the design file doesn't cover and when the file is missing. The tab then says *inferred design*. The CSV has one row per expected link (4,608 leaf–spine, 4,608 leaf–GPU) and is generated from these rules by `scripts/build_expected_topology.py`. Checked against NetBox (site lon14), the rules agree on all 4,608 leaf–spine cables and on every GPU port apart from the 16 ports of those 4 suspect hosts.

| Rule | Expected connection |
| --- | --- |
| L1 | Leaf *j* port `sw(73−i)p(3−M)` ⟷ spine *i* port `sw(j)pM` (two cables per leaf–spine pair; all 4,608 NetBox cables follow it) |
| G1 | Scalable unit *k* owns four consecutive leaves BEL4*k*−3 … BEL4*k*; rail *r* is BEL4(*k*−1)+*r*. Four SUs make a pod (pod *p* = SU4*p*−3 … 4*p* = BEL16(*p*−1)+1 … 16*p*). |
| G2 | Leaf port `swNpM` (N ≤ 36) is tray slot 2·(N−1)+M of that SU. A tray uses the same slot on all four of its leaves. |
| G3 | Rail *r* reaches the tray's adapter `mlx5_(r−1)` (RDMA *r*) — assumed as on ICE2; confirm on a LON14 host |

If the design changes, replace `nscale_Compute.topo` with the new approved file. For the rules, edit them and run `python3 scripts/build_expected_topology.py`. The dashboard picks up either change automatically.

**Not a reference:** `topo.topo`, `ibnetdiscover` or `iblinkinfo` output, and UFM's master all record what *was* connected when they were written, so they would accept an existing miscabling as correct.

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
- Progress shows next to the button: connecting to `lon14deploy1`, reading from UFM, saving, comparing. A fetch usually takes a few seconds.
- When it is done, the button line says what was read (*live links* or *scan file*), the UFM host, the time taken, the number of crossed pairs or miscabled cables, the trays seen and the master date. The tab, GPU area and inspector update without a page reload.
- Live links: one HTTPS GET from `lon14deploy1` to the UFM REST API, followed by a host SSH read of the approved design, master topology and report. Scan file: the same four files as the script below. Either way it is one Teleport session per source, and passwords go from Keychain to the jump host on standard input only.
- If live links fail (for example a wrong web password) and an SSH login is stored, it falls back to the scan file and shows a ⚠ note.
- Live links list one record per cable when all four planes are up, and one record per plane otherwise; both become the same four-plane lanes as the scan file. GPU adapter names (`nvl72dXXX-TNN mlx5_N`) are matched by GUID from the last scan file, because the REST API shows host:interface names. If a live result names far fewer leaf–spine links than the last scan, it is refused and the previous data stays.
- Live links show which ports are connected, not link training states. A cable stuck in *Init* still appears in the **Live link state** tab, which reads the switches.
- If the first UFM address does not answer (for example it is the standby), it tries the next one.
- If the design refresh fails, the fetch fails and the previous dashboard result stays in use; it never compares a new UFM snapshot with an old design file.
- With `--ufm-fetch-every-minutes N`, the service also fetches by itself every N minutes.

**Or load it from a terminal.** This needs no stored UFM password. Run it in the project folder:

```bash
./scripts/fetch_ufm_scan.sh
```

The script:

- connects through `lon14deploy1` to the active UFM, trying `10.2.64.75` and then `10.2.64.76`
- asks once for the UFM host password, which is typed into `ssh` on the jump host and never stored
- copies the approved design plus three files UFM already writes, in one session, into `local-inputs/ufm/` (ignored by Git):
  - the current fabric scan, saved as `ibdiagnet2.lst.gz`
  - the approved design, saved as `nscale_Compute.topo`
  - UFM's master topology, saved as `master.topo.gz` with its original save date
  - UFM's latest Topology Compare report, saved as `topology-compare.json.gz`

The dashboard picks up the new file automatically; reload the page if it is open. If you have a read-only login on the UFM host, use it with `UFM_USER=<user> ./scripts/fetch_ufm_scan.sh` instead of the default `root`.

**Run the comparison from a terminal.** After fetching, this read-only command compares UFM's saved snapshot directly with the approved `nscale_Compute.topo` design (and shows the UFM master beside each difference when it is available):

```bash
python3 app/ufm_cabling.py local-inputs/ufm/ibdiagnet2.lst.gz
```

The command uses `local-inputs/ufm/nscale_Compute.topo` automatically when the file exists. `assets/expected_topology.csv` is used only to fill design-file gaps or as the fallback when the approved file is unavailable. To compare another approved file, pass `--design-topo /path/to/design.topo`.

**What changes on the dashboard when a scan is loaded:**

- **GPU area, redrawn from UFM, one pod at a time.** The **POD 1–4** buttons switch pods and show how many trays are on the fabric, designed but off, and needing attention. Each pod shows its 4 scalable units with 72 tray slots each, and the rail-coloured bundles from its 16 leaves: a bundle is thicker the more trays on that rail are Active, and dashed when none are. Each tile is labelled with the last two digits of the slot's **designed host** from `nscale_Compute.topo` (`56` = gpu1256), and sits in the slot where UFM actually sees a tray. Tile colours:

  | Tile | Meaning |
  | --- | --- |
  | Rail-coloured solid | The tray and its leaf-to-GPU cables are documented in NetBox |
  | Grey, dashed | UFM sees the tray, but its leaf-to-GPU cables are not documented in NetBox |
  | Amber | A rail link is missing or not Active |
  | Red | Wiring error: wrong rail, slot or host |
  | Grey `?` | The adapter has no name, so the tray can't be identified |
  | Dark, dotted outline, dim number | **Designed but not on the fabric**: the approved design has a host here, but UFM sees no adapter. The tray is probably powered off, unplugged or not installed; it is not counted as a cabling error. |
  | Faint | Empty slot (no design host, nothing seen) |

  The four small bars in each tile are the tray's rails 1–4. Click a tray to see its four links, adapter by adapter, with the NetBox cable for each. UFM-only details stay grey so they are clearly distinct from the NetBox cabling record.
- **NVL72 racks.** Each SU's 72 slots are 4 NVL72 racks of 18 trays: rack 1 is slots 1–18, rack 2 is 19–36, rack 3 is 37–54 and rack 4 is 55–72. UFM's tray names (`<rack>-T<n>`) confirm it: each of the 63 named racks sits in exactly one rack position. In the pod view each SU's tiles are grouped into its 4 racks (3 × 6 tiles each), headed with the rack name and how many of its 18 trays are on the fabric (`d031 · 18/18`; `+18?` counts unnamed adapters). Click a rack header, or search for a rack (`nvl72d031`, `d031`, `su8 rack 1`), to see all 18 trays with their design host, NetBox host and state. The tray inspector names its rack, its position in the rack and its NVLink peers.

  A rack is one NVLink domain: its 72 GPUs talk over NVSwitch and never use InfiniBand. Racks in the same SU reach each other through the SU's four leaves (one switch hop, rail to rail). Racks in different SUs or pods go leaf → spine → leaf. The Incidents tab flags a rack position that holds trays of more than one rack, or a rack whose trays sit in two positions (major), a rack with 9 or more designed trays off the fabric (major), and a rack UFM cannot name (info; today SU8 rack 1, design hosts gpu1760–gpu1777, whose adapters have no node description).
- **Traffic paths tab** (also the **How traffic flows** button in the header, or `/traffic-paths.html` on its own): an interactive page that animates the path between any two GPUs (same rack over NVLink, same SU and rail through one leaf, other SUs and pods through a spine, and NCCL PXN changing rail over NVLink first). It also works offline: open `assets/traffic-paths.html` in a browser.
- **Leaves** take their rail colour; documented GPU downlinks are rail-coloured, and UFM-only GPU discoveries are grey.
- **Miscabled leaf–spine cables** are drawn in **magenta** on the mesh, and both switches get a ◆ marker. The **Miscabling** chip turns the layer on or off.
- **Cabling vs UFM tab**, in six sections:
  1. Miscabled cables, grouped per leaf. Each one shows the **Current** connection (UFM), the **Expected** connection (design) and the **Master** connection, end to end. It also gives the re-patch instruction, the impact (*port swap · no fabric impact* or *topology change*, see [6.2](#62-incidents)) and whether NetBox agrees with the design.
  2. GPU trays per scalable unit, with each SU's four NVL72 racks (click one to open it).
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


### 6.3 Evidence, design approval and validation

A comparison is only as good as its inputs, so the dashboard shows them all, above the tabs, in the **evidence bar**:

| Source | What it answers | Shown as stale after |
| --- | --- | --- |
| **Approved design** (`nscale_Compute.topo`) | With NetBox: which leaf port should reach which spine port. Shown as *approved*, *not approved yet*, *changed on UFM · review pending* or *approved copy altered locally*. | — (never current until approved) |
| **UFM live links** | What is plugged in now | `--ufm-stale-minutes` (60) |
| **Switch states** | Is each port up (UFM ports, or the switches over SSH) | `--stale-after-minutes` (60) |
| **NetBox** | Expected cabling with the design, and the documented GPU allocation | `--netbox-stale-hours` (24) |
| **Inputs** | Whether every source passed validation | errors stop the comparison |

The bar says **CURRENT** only when the design is approved, every source is fresh and the inputs are valid; otherwise **NOT CURRENT**, with the reasons. Links and port states come from **one UFM snapshot**: the same REST session to the same UFM host returns both, a few seconds apart. "Sources and who decides what" opens the source-of-truth table:

| Question | Source of truth |
| --- | --- |
| Which leaf port should connect to which spine port? | NetBox **and** the approved design; both must agree, and where they differ the port is flagged, not judged |
| What is plugged in now, and is it up? | UFM (live links and port states) |
| Which GPU host and RDMA port is on a leaf port? | NetBox (documented allocation); the design names the slot |
| Switch port state, independently of UFM | the switches over SSH (**Sync with switch SSH**) |
| UFM master topology | history only: what UFM accepted when it was saved; never the expected cabling |

**Design approval.** Files in `local-inputs/ufm/`:

- `nscale_Compute.fetched.topo`: the latest copy from the UFM host (a candidate).
- `nscale_Compute.topo`: the **approved** copy the comparison uses.
- `design-approved.json`: its SHA-256, size, link counts, who approved it, when, a note, and the history of earlier approvals.
- `design-status.json`: the last fetch, and how the candidate differs from the approved copy.

A fetch identical to the approved copy changes nothing. A different one is kept as *review pending* and listed port by port (approved far end vs. fetched far end); the comparison keeps using the approved copy. To approve:

- in the dashboard: **Review…** in the evidence bar, then **Approve** (it records your user name and the time); or
- in a terminal, with the full diff: `./scripts/approve_design.py approve --note "CHG-1234"`.

On first use, the existing file shows as *not approved yet*: review it once and approve it (`./scripts/approve_design.py approve`). If the approved copy is edited by hand, its hash no longer matches the manifest and the bar shows *approved copy altered locally*.

**Validation.** Before every comparison, the sources are checked. **Errors** stop the comparison, and the Cabling tab shows the validation report instead of findings:

- missing columns, duplicate NetBox cable IDs;
- one switch port with two leaf–spine cables in NetBox (the expected far end would be ambiguous);
- switch ports that are not NVOS names (`swNpM`, `fnmN`), leaf–spine cables that do not join a leaf to a spine;
- design ports with two different far ends; an empty UFM snapshot.

**Warnings** are reported but handled safely: NetBox cables with unusable terminations (left out and named), suspect design entries, cable counts that differ from the 4,608 designed, and ports only one of NetBox and the design has.

**Comparison bundles.** Each comparison with new inputs is archived once in `.netbox-live-sync/bundles/<time>-<id>/`, read-only: `metadata.json` (the SHA-256 of the UFM snapshot, design, NetBox cable copy, NetBox export, inferred rules and code; the design approval; the NetBox sync time; validation; summary; miscabled cables) and `findings.csv.gz`. The id is a hash of the inputs, so the same inputs never produce a second bundle. No credentials or raw state are archived. The last 200 are kept; `GET /api/bundles` lists them.

**Offline file.** Opening `assets/dashboard.html` directly (not through the service) shows a banner: it is an embedded snapshot from the date it names, not live data.

---

## 7. Command-line options

| Option | Default | Use |
| --- | --- | --- |
| `--netbox-url` | *(required)* | `http://127.0.0.1:8444` (the local proxy) |
| `--device-profile` | — | `~/.config/idc-automation/device-access-lon14.ini`. Required for device collection. |
| `--switch-source auto\|ufm\|ssh` | `auto` | Where Sync fabric reads switch port states: `ufm` = one UFM REST call (needs the UFM web user), `ssh` = log in to every switch. `auto` uses UFM when its web user is set up ([8](#8-how-sync-works)). |
| `--fanout local\|jump` | `local` | `jump` runs one Teleport session to `lon14deploy1`, which logs in to the switches in parallel. Recommended. Falls back to `local` automatically if it can't be used. |
| `--device-parallel N` | `10` | Concurrent switch logins (1–25). Raise gradually, for example 15 → 20 → 25, and watch for failures. |
| `--sync-every-minutes N` | `0` (off) | Background fabric sync (switches and UFM), so the dashboard is already fresh when you open it |
| `--stale-after-minutes N` | `60` | Show the switch evidence as **STALE** when the last collection is older than this |
| `--netbox-every-hours H` | `24` | **Sync fabric** also refreshes NetBox (incrementally, from its change log) when its copy is older than this. `0`: only with **Refresh NetBox** (and on the first sync). |
| `--netbox-background` | off | Also refresh NetBox in the background when it is due, without a Sync. |
| `--netbox-stale-hours H` | `24` | The evidence bar shows NetBox as stale (comparison not current) when its copy is older than this. |
| `--ufm-stale-minutes M` | `60` | The evidence bar shows the UFM links as stale when they are older than this. |
| `--full-netbox-every-hours H` | `6` | Do a full cable pull this often; between pulls the sync is incremental. `0` forces a full pull every time. |
| `--address-cache-hours H` | `24` | Reuse switch management IPs from NetBox for this long. `0` always re-queries. |
| `--netbox-page-size N` | `250` | Cables per NetBox page during a full pull |
| `--netbox-concurrency N` | `2` | NetBox pages fetched in parallel through the Teleport app proxy |
| `--netbox-host-header` | — | Only if the platform owner tells you a proxy needs it |
| `--cabling-reference both\|netbox\|design` | `both` | Expected cabling for the miscabling check: NetBox and the approved design together; NetBox with the design as a cross-check; or the approved design alone ([6.1](#61-cabling-vs-ufm-miscabling-check)) |
| `--design-topo PATH` | `local-inputs/ufm/nscale_Compute.topo` | Approved design topology (`.topo`): with NetBox, the expected cabling for the cabling check ([6.1](#61-cabling-vs-ufm-miscabling-check)) |
| `--expected-topology PATH` | `assets/expected_topology.csv` | Inferred rules, used where the approved design has no (or a suspect) entry ([6.1](#61-cabling-vs-ufm-miscabling-check)) |
| `--ufm-master PATH` | `local-inputs/ufm/master.topo.gz` | UFM's master topology, the second reference ([6.1](#61-cabling-vs-ufm-miscabling-check)) |
| `--ufm-report PATH` | `local-inputs/ufm/topology-compare.json.gz` | UFM's latest Topology Compare report, summarized in the tab |
| `--ufm-fetch-every-minutes N` | `0` (off) | Fetch from UFM by itself every N minutes, in addition to the fabric sync. Only needed if you want UFM more often than `--sync-every-minutes`. Needs [4.9](#49-optional-let-the-dashboard-fetch-from-ufm). |
| `--ufm-scan PATH` | `local-inputs/ufm/ibdiagnet2.lst.gz` | UFM fabric scan used by the cabling check ([6.1](#61-cabling-vs-ufm-miscabling-check)). Plain or gzip-compressed. |
| `--port N` | `8766` | Local dashboard port |
| `--diagram`, `--connections`, `--devices`, `--commands`, `--known-hosts` | bundled | Override the bundled dashboard, NetBox cable export, switch inventory, command file or host-key path |

---

## 8. How sync works

1. **⟳ Sync fabric runs these phases in parallel:**
   - **Switches.** Where the switch port states come from (`--switch-source`):
     - **UFM (default when the UFM web user from [4.9](#49-optional-let-the-dashboard-fetch-from-ufm) is set up).** One read-only call to UFM REST `GET /ufmRest/resources/ports`, over the same `tsh ssh` session to `lon14deploy1` that the UFM fetch uses. UFM returns about 19,000 port objects; the worker on `lon14deploy1` keeps only the switch ports and a few fields (switch, port, logical and physical state, speed, BER severity), so well under 1 MB crosses Teleport. Each port is turned into the switch's own wording (`Active/LinkUp/800G`), so Live link state works exactly as before. No switch login, password or host key is needed, and it takes seconds instead of about half a minute.
     - **SSH** (the **Sync with switch SSH** button next to Sync fabric for a single run, `--switch-source ssh` for every run, or automatically when no UFM web user is set up). Switch management IPs come from NetBox (2 bulk queries, or the 24-hour cache), then `nv show interface --output json` is collected from all 100 switches. Use it to check the switches' own view independently of UFM.
   - **UFM**, when [4.9](#49-optional-let-the-dashboard-fetch-from-ufm) is set up: the same as **Fetch from UFM**.
   - **NetBox**, on the first sync (no inventory yet), and when its copy is older than `--netbox-every-hours` (24 h): an incremental refresh from the change log, usually 1–3 API calls. **Refresh NetBox** runs this phase alone.
     - A full pull reads every backend cable, with pages trimmed to the needed fields; it runs the first time and every `--full-netbox-every-hours` (6).
     - Other runs ask the NetBox **change log** which cables changed since the last refresh, and re-read only those, usually in 1–3 API calls.
     - A NetBox failure never fails a fabric sync; the previous copy stays in use.
   - **Live link state** compares the switch port states with the **design topology**: every designed leaf–spine link, and every designed GPU port in use (seen by UFM now or before, or recorded in NetBox), so empty tray slots are not reported as down. NetBox cable IDs are shown where NetBox has the cable.
2. **Coverage and replacement.** When the collection finishes (from UFM or SSH alike), it is checked against the 100 inventory switches and the designed ports of each:
   - Each reached switch's ports **replace** its previous ports entirely, so a port it no longer reports is not kept.
   - Switches not reached keep their last known states only for display; their links count as *not verified*.
   - The result is *verified* (every switch, every designed port) or *partial*, and is shown as such in the status bar, the Sync line and the Incidents tab.
   - The snapshot is saved **atomically** (temporary file, then rename) to `.netbox-live-sync/latest-live.json`, with the coverage, each switch's last-read time and a fingerprint of `connections.csv`, `expected_topology.csv` and `devices.csv`. After a restart it is trusted only if those files are unchanged; otherwise everything shows as not verified until the next sync.
3. **Strict NetBox validation.** A NetBox cable must have exactly one interface termination on each side, with a device and port. A cable with several terminations, a front/rear port, or a malformed termination is reported as differing, never as matching.
4. **Jump-host fan-out** (SSH source only). With `--fanout jump`, one `tsh ssh` session starts a small worker on `lon14deploy1`.
   - The switch password is passed only on the worker's input. It never appears in a command line, an environment variable or a file.
   - The worker logs in to each switch with strict host-key checking, using your approved `known_hosts`.
5. **Progressive results** (SSH source). Each switch's result is shown as soon as it arrives.

Typical timings: switches a few seconds from UFM, or about 34 s for 100 switches over SSH at `--device-parallel 15`; UFM live links 3–10 s, in parallel; NetBox about 1 s when incremental (2 API calls).

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
| `access denied` on `tsh ssh …@lon14deploy1` | No jump-host role, or the access request has expired | `tsh request create …`, then `tsh login --request-id=…` |
| `Host-rewrite proxy is already running.` but NetBox fails | The proxy is up but its Teleport forward stopped | `./scripts/netbox_proxy.sh status`. If it shows `tsh: stopped`: `stop`, then `start`. |
| `NetBox token was not found in macOS Keychain` | Step 4.4 was skipped, or done as a different macOS user | `./scripts/configure_netbox_token.sh` |
| NetBox step: `Cannot reach NetBox …` | Proxy not running, or Teleport expired. The fabric sync still runs with the last known IPs. | The two proxy and login rows above, then **Refresh NetBox** |
| NetBox step: `HTTP 403` | Token lacks read permission | Ask for read permission on devices, interfaces and cables |
| NetBox step always says `full` | Token cannot read the change log | Ask for change-log view permission, or accept full pulls |
| `unrecognized arguments: --fanout …` | Old copy of the code | `git pull` |
| `Local device-access inputs are missing: …` | Missing profile or host keys | Steps 4.5 and 4.7, and pass `--device-profile` |
| Devices: `fell back to local fan-out: worker unavailable` | No `python3` on `lon14deploy1` | Ask for `python3` on the jump host, or drop `--fanout jump` |
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
| Fetch from UFM: `ConnectionRefusedError` or `timed out` for every address | `lon14deploy1` cannot reach the UFM web port | Check the `curl` test in [4.9](#49-optional-let-the-dashboard-fetch-from-ufm); ask for HTTPS from `lon14deploy1` to UFM |
| Fetch from UFM: `TLS certificate changed` | UFM's certificate was replaced (or something is intercepting) | Confirm the new certificate with the UFM owner, then delete that address from `local-inputs/ufm/tls-pins.json` |
| Fetch from UFM: "none had node descriptions" | This UFM version names link fields differently | Open an issue with the field list from the message |
| Fetch from UFM: `Permission denied` | Wrong or changed UFM password | `./scripts/configure_ufm_access.sh` with the current password |
| Fetch from UFM: `Host key verification failed` | Your `lon14deploy1` account does not yet trust the UFM host key, or the key changed | From `lon14deploy1`, run `ssh <login>@10.2.64.75 hostname` once and check the fingerprint ([4.9](#49-optional-let-the-dashboard-fetch-from-ufm)) |
| `fetch_ufm_scan.sh`: "No UFM host returned a fabric scan" | Wrong UFM password, both UFM hosts unreachable from `lon14deploy1`, or UFM not running | Check `ssh root@10.2.64.75` from `lon14deploy1` works. Set `UFM_HOSTS` if the UFM addresses changed. |
| GPU area still shows the NetBox drawing (4 SUs) | No UFM scan loaded, or the page was opened as a file | Fetch a scan and open `http://127.0.0.1:8766/` |
| Page shows `SNAPSHOT` and Sync says "needs the local service" | Opened as a file, or from a published copy | Open `http://127.0.0.1:8766/` |
| `Address already in use` | The service is already running | Use it, stop it with `pkill -f app/netbox_live_sync.py`, or pass `--port 8766` |

To see where the time goes, read the final Sync line (devices ‖ IPs ‖ UFM ‖ NetBox). The terminal also prints one summary line per sync.

---

## 11. Security model

- **Read-only.** The service makes only NetBox `GET` requests, runs only the commands in `assets/read_only_commands.txt` on switches, and only reads from UFM: `GET /ufmRest/resources/links`, and `docker exec ufm` reading three files UFM already writes.
- **UFM's certificate is pinned** on first use (`local-inputs/ufm/tls-pins.json`) by every REST call (live links and port states share the pin), and UFM host keys are checked against your `lon14deploy1` account's `known_hosts`.
- **Secrets live only in your Keychain:**
  - `netbox-mcp-token` for the NetBox token
  - `idc-automation-lon14-switch` for the switch password
  - `idc-automation-lon14-ufm-rest` and `idc-automation-lon14-ufm` for the UFM web and host passwords (optional, [4.9](#49-optional-let-the-dashboard-fetch-from-ufm))
- **Secrets never reach:**
  - the browser
  - this repository
  - log files
  - command lines
  - environment variables
- **Loopback only.** The service and proxy listen on `127.0.0.1` only, so other machines cannot reach them.
- **Requests only from its own page.** Every `POST` (which starts a sync, a fetch or a design approval) must carry the per-process token embedded in the dashboard page (`X-LON14-CSRF`), come from `http://127.0.0.1:<port>` or `localhost` (a cross-site `Origin` is refused) and name this service in its `Host` header. Any request with a foreign `Host` header is refused, so another web page cannot use DNS rebinding to reach the service.
- **The expected design is reviewed.** A design file fetched from the UFM host becomes the expected design only after an approval recorded with its SHA-256, user and time ([6.3](#63-evidence-design-approval-and-validation)).
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
security delete-generic-password -s idc-automation-lon14-switch -a <switch-user>
security delete-generic-password -s idc-automation-lon14-ufm -a <ufm-login>        # only if you did 4.9
security delete-generic-password -s idc-automation-lon14-ufm-rest -a <ufm-web-user> # only if you did 4.9
rm -f ~/.config/idc-automation/device-access-lon14.ini
rm -rf .netbox-live-sync local-inputs            # run inside lon14-netbox-dashboard
```

Also revoke the NetBox API token in NetBox (**API Tokens → delete**).

---

## 13. Reference

### HTTP API (local only)

| Method and path | Purpose |
| --- | --- |
| `GET /` | Dashboard |
| `GET /traffic-paths.html` | How traffic flows (rack, rail, SU and pod paths) |
| `GET /api/live` | Live link state for every designed link in use, plus sync, UFM-fetch and incident summaries (ETag and gzip; `304` when unchanged) |
| `POST /api/sync` · `GET /api/sync/<run>` | Start a fabric sync (switches, UFM, and NetBox when due; `?switches=ssh` logs in to every switch for this run), or check its progress and per-phase timings |
| `POST /api/netbox/refresh` | NetBox inventory only (progress at `GET /api/sync/<run>`) |
| `POST /api/refresh` · `GET /api/refresh/<run>` | Device collection only |
| `GET /api/verify/<cable_id>` | One cable: current NetBox record vs. live state |
| `GET /api/device/<hostname>` | NetBox details for one device, from the last sync (add `?live=1` to query NetBox now) |
| `GET /api/health` | NetBox reachability |
| `GET /api/incidents` | Ranked fabric incidents ([6.2](#62-incidents)); counts and the top three are also in `/api/live` |
| `GET /api/cabling` | Cabling vs UFM report (ETag and gzip), with its validation and bundle; when validation fails, `{"blocked": true, "validation": …}` instead |
| `GET /api/evidence` | Every source behind the comparison: age, design approval, validation, bundle |
| `GET /api/design` · `POST /api/design/approve?sha256=…` | Design approval state (and the diff of a pending change) · approve a reviewed file |
| `GET /api/bundles` | The latest immutable comparison bundles |
| `GET /api/cabling/findings.csv` | Every cabling difference, one row each |
| `POST /api/cabling/fetch` · `GET /api/cabling/fetch/<run>` | Fetch from UFM now (live links or files), or check that fetch's progress |
| `GET /api/cabling/netbox-import.csv` | GPU cables UFM sees but NetBox lacks, in NetBox import columns |

### Repository contents

| Path | Contents |
| --- | --- |
| `app/netbox_live_sync.py` | Local service: dashboard, sync and API |
| `assets/expected_topology.csv` | Inferred design rules: fill gaps in the approved design; rail rules for GPU trays |
| `scripts/build_expected_topology.py` | Design rules that generate `expected_topology.csv` |
| `app/design_gate.py` | Approval of the expected design: candidate vs. approved copy, manifest, diff ([6.3](#63-evidence-design-approval-and-validation)) |
| `app/validate_inputs.py` | Validation of NetBox, design and UFM inputs before every comparison |
| `scripts/approve_design.py` | Review the design diff and approve it from a terminal |
| `tests/`, `.github/workflows/lon14-dashboard.yml` | Offline unit tests with fixtures, and the CI that runs them ([14](#14-tests-and-ci)) |
| `app/ufm_cabling.py` | Cabling vs UFM check (also runs on its own: `python3 app/ufm_cabling.py <scan>`) |
| `app/ufm_fetch.py` | Fetch from UFM: live links over the UFM REST API, or UFM's files, through `lon14deploy1` (read-only) |
| `app/incidents.py` | Incident rules: severity, impact and action for each problem found ([6.2](#62-incidents)) |
| `collector/run_ntp_audit.py` | Read-only collector (local and jump-host fan-out) |
| `assets/dashboard.html` | Dashboard |
| `assets/traffic-paths.html` | How traffic flows: interactive rack / rail / SU / pod path explainer (served at `/traffic-paths.html`) |
| `assets/connections.csv` | NetBox cable export (9,216 cables): the diagram and the NetBox comparison. Regenerate with `scripts/build_site_data.py` |
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
| Keychain `idc-automation-lon14-switch` | step 4.5 | Switch password |
| Keychain `idc-automation-lon14-ufm-rest`, `idc-automation-lon14-ufm` | step 4.9 | UFM web and host passwords |
| `~/.config/idc-automation/device-access-lon14.ini` | steps 4.5, 4.9 | Usernames, jump host, UFM addresses, Keychain references |
| `local-inputs/known_hosts` | step 4.7 | Approved switch host keys |
| `~/.local/state/netbox-mcp/` | `netbox_proxy.sh` | Proxy PIDs and logs |
| `.netbox-live-sync/` | the service | Collected evidence, IP cache, cable cache, device details (`netbox-devices.json`) |
| `local-inputs/ufm/ibdiagnet2.lst.gz` | Fetch from UFM or `fetch_ufm_scan.sh` | UFM fabric scan for the cabling check. The previous copy is kept as `ibdiagnet2.lst.previous.gz`. |
| `local-inputs/ufm/master.topo.gz`, `topology-compare.json.gz` | Fetch from UFM or `fetch_ufm_scan.sh` | UFM's master topology and its latest Topology Compare report |
| `.netbox-live-sync/tray-history.json` | The service | When each GPU tray was last seen, to report trays that go offline |
| `local-inputs/ufm/nscale_Compute.topo` | Approval ([6.3](#63-evidence-design-approval-and-validation)) | The approved design topology: with NetBox, the expected cabling |
| `local-inputs/ufm/nscale_Compute.fetched.topo`, `design-approved.json`, `design-status.json` | Fetch from UFM, approval | The latest design copy from UFM, the approval manifest and history, and the last fetch's diff |
| `.netbox-live-sync/bundles/` | Every comparison with new inputs | Immutable results: `metadata.json` (source hashes) and `findings.csv.gz` |
| `local-inputs/ufm/links.json.gz`, `tls-pins.json` | Fetch from UFM (live links) | UFM's raw live link list, and the pinned UFM certificate fingerprints |

---

## 14. Tests and CI

Offline tests use small fixtures in `tests/fixtures/` (UFM `/links` and `/ports` answers, NetBox cables, a design topology for a 2-leaf, 2-spine fabric with one crossed pair). They cover the UFM link and port conversion, NetBox and design parsing, the swap detection and its impact, the NetBox-vs-design verdicts, down and unverified links, every validation error, the design approval gate, the TLS pin, and the request protection.

```bash
cd lon14-netbox-dashboard
python3 -m unittest discover -s tests -v
```

GitHub Actions (`.github/workflows/lon14-dashboard.yml`) runs them on every push and pull request that touches the dashboard, and also checks that every Python file compiles, the shell scripts parse (`bash -n`, `zsh -n`), and the JavaScript in both pages parses.

---

## 15. SYS2 tab: Ethernet backend

LON14's second GPU backend, `sys2-lon14`, is an Ethernet fabric (NVIDIA Spectrum-X SN5610, Cumulus/NVUE). It has no UFM, so the tab is NetBox + switch SSH only.

| | SYS2 |
| --- | --- |
| Switches | 256 leaves BEL1–256, 72 spines BES1–72 (SN5610) |
| Hosts | gpu1–1152 (`sys2-lon14-p-phy-gpu*`), 8 backend ports each, plus eth0/eth1 (FEL), BF MGMT and iDRAC (OBL) |
| NetBox | 9,216 leaf–spine + 9,216 host cables; **no management IPs recorded for any SYS2 switch** |

**Wiring rules** (`scripts/build_sys2_data.py`; all 18,432 NetBox cables follow them, and the tab re-checks on every build):

| Rule | Expected connection |
| --- | --- |
| P1 | Two planes, never joined: plane A = BEL1–128 + BES1–36, plane B = BEL129–256 + BES37–72 |
| L1 | Leaf uplinks swp47s0 … swp64s1 (index *u* = 0–35). Leaf *j* uplink *u* → spine 36*p*+36−*u*, on spine port index *j*−1−128*p* (swp1s0, swp1s1, swp2s0 …) |
| G1 | Host port swp*r*s*p* = rail *r* (1–4), plane *p* (s0 = A, s1 = B). Pod *b* = 144 hosts on 16 leaves per plane; SU *k* (36 hosts) takes the *k*-th leaf of each 4-leaf rail block: gpu1–36 → BEL1/5/9/13 and BEL129/133/137/141. Host *i* of an SU uses leaf port swp(⌊*i*/2⌋+1)s(*i* mod 2) |

**How it runs.** The tab frames `assets/sys2.html`. `scripts/open_dashboard.command` also starts a second service from the same code, `app/netbox_live_sync.py --fabric sys2`, on **127.0.0.1:8767**. It has its own inventory and cables (`assets/sys2/`), its own state folder (`.netbox-live-sync-sys2/`) and host keys (`local-inputs/sys2/known_hosts`), never touches UFM, and reads switch states only over SSH (`nv show interface --output json`, read-only). When that service is not running the tab shows the static NetBox view.

**One-time setup for SYS2 live state:**

1. **Host keys.** NetBox has no IPs for the SYS2 switches, so names are resolved with DNS on the jump host (`lon14deploy1`):

   ```bash
   ./scripts/configure_known_hosts.sh --fabric sys2 --collect-live
   ```

   (or install an approved file: `./scripts/configure_known_hosts.sh --fabric sys2`). Verify fingerprints as for SYS1.
2. **Login.** By default SYS2 uses the same profile as SYS1 (`device-access-lon14.ini`). If the SYS2 switches use a different account, make a second profile and pass it: `LON14_SYS2_ARGS="--device-profile ~/.config/idc-automation/device-access-lon14-sys2.ini" ./scripts/open_dashboard.command`.
3. Open the dashboard, choose **SYS2 · ETHERNET**, press **Sync with switch SSH**.

Live link states use the same wording as SYS1: an Ethernet port that is up shows as `Active/LinkUp/<speed>`, admin-down as `Down/Disabled/`, otherwise `Down/LinkDown/`. Host-side ports are not collected.

**Refreshing SYS2 data from NetBox:** `python3 scripts/lon14_discover.py`, then `python3 scripts/build_sys2_data.py`.

**Known gaps:** the switch login for SYS2 is not confirmed (Cumulus switches often use a different account); DNS names for the SYS2 switches must resolve on the jump host until NetBox has their IPs.

