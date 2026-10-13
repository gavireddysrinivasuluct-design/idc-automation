# Backend fabric dashboard (one service, every region)

One local, read-only service on **http://127.0.0.1:8765/** for every backend region. Pick a
region and you get the ICE2-style fabric dashboard (spine · leaf · rail, cabling, inspector)
plus the region's live issues.

| Region | Fabric dashboard | Expected topology | Actual links | Metrics, logs, incidents |
| --- | --- | --- | --- | --- |
| **ICE2, LON14** | their own engines (`<site>-netbox-dashboard`), started by this service and served at `/ice2/`, `/lon14/` unchanged | NetBox + approved design | UFM (REST or scan) and switch SSH, as before | nsr |
| **OSL1, STA1, GLO1** | `/f/<site>`, same design | **NetBox first**, read directly | **UFM directly** (REST links through the site's jump host) | nsr |

- **NetBox first.** The expected topology for OSL1/STA1/GLO1 is read straight from NetBox
  (devices, then every leaf interface's cable and connected endpoint) through the same local
  NetBox proxy and read-only Keychain token as the ICE2 dashboard. **Refresh NetBox** takes a
  new snapshot.
- **UFM directly for the real cabling.** **Fetch from UFM** reads UFM's live link list over
  its REST API through the site's jump host (same mechanism as ICE2's *Fetch from UFM*) and
  compares it with NetBox cable by cable: *miscabled*, *not seen by UFM*, *UFM only*.
  Miscabled cables are drawn in magenta and flagged in the inspector.
- **nsr only for metrics and other data:** UFM telemetry (port state, link-downs, errors),
  gNMI, SNMP, blackbox ping and UFM servers from Grafana; the UFM event log and switch syslog
  from Loki; open incidents from PagerDuty.
- **Read-only.** NetBox and UFM get GET requests only; nothing is written to a device.
  Passwords stay in Keychain; the service listens on 127.0.0.1 only.

```
Browser ─► 127.0.0.1:8765  multi_region_dashboard.py
             ├─ /ice2/   ─► ICE2 engine  (127.0.0.1:18765, ice2-netbox-dashboard)
             ├─ /lon14/  ─► LON14 engine (127.0.0.1:18766, lon14-netbox-dashboard; SYS2 on 8767)
             ├─ /f/osl1  ─► NetBox proxy 127.0.0.1:8444 (expected) + tsh ─► jump host ─► UFM REST (actual)
             └─ metrics  ─► nsr ─► Grafana (Mimir · VictoriaMetrics · Loki) · PagerDuty
```

## Run it

Prerequisites are the ones you already have for the ICE2 dashboard (Teleport, the NetBox
token in Keychain, `device-access.ini`, switch host keys), plus `nsr` for metrics:

```bash
uv tool install "git+ssh://git@github.com/nscaledev/netops-misc.git#subdirectory=nsr"
nsr doctor
```

Every day: **`scripts/open_regions.command`** (double-click, or run it). It checks Teleport,
starts the NetBox proxy, offers to stop the old standalone dashboards (ICE2 on 8765, LON14 on
8766, regions on 8780, which this service replaces), starts the service and opens the browser.
The service starts the ICE2 and LON14 engines itself with your profiles
(`device-access.ini`, `device-access-lon14.ini`); their logs are in `cache/<site>-engine.log`.

By hand: `python3 multi_region_dashboard.py` (options: `--port`, `--netbox-url`,
`--device-profile`, `--nsr`, `--cache-seconds`, `--no-engines`). Stop everything with
`pkill -f multi_region_dashboard.py; pkill -f netbox_live_sync.py`.

**Once per NetBox-first region, for Fetch from UFM:**

```bash
./scripts/configure_site_ufm.sh osl1     # and sta1
```

It asks how to reach that UFM, then for a read-only UFM web user; it writes a `[ufm.<site>]`
section (no password) to `device-access.ini` and stores the password in Keychain item
`idc-automation-<site>-ufm-rest`. Two ways in:

- **Direct to the UFM node** (STA1): the UFM appliance is itself a Teleport node,
  `tsh ssh sta1-p-phy-ufm2` (pre-filled). Leave the jump host empty. The node asks its own
  REST API on 127.0.0.1, then the VIP 10.40.40.54 if it is the standby.
- **Through a jump host** (OSL1, until its UFM node is in Teleport): a node that reaches the
  UFM VIP on 443 (10.50.40.9 pre-filled).

The Teleport login is optional; empty uses your `tsh` default, as plain `tsh ssh <node>` does.

## Using it

- **All regions** (`/`): one card per region with status, switches answering ping, ports not
  Active, link-downs and errors in the last 15 min, UFM servers up, open incidents, and a dot
  per data source.
- **A region** (`/r/<site>`) opens on its **Fabric dashboard** tab. **Live issues (nsr)** is
  the second tab: issues ranked critical → major → minor with what to do, UFM event log and
  syslog lines, PagerDuty incidents, which data sources answered, and every query run.
- **ICE2 / LON14 fabric tab**: the engine's own page (Sync fabric, Refresh NetBox, Fetch from
  UFM, cabling check, incidents, inspector), exactly as when it ran on its own port.
- **OSL1 / STA1 / GLO1 fabric tab**: first press *Read <site> from NetBox* (a minute or two),
  then *Fetch from UFM*. The page shows the expected/actual sources and times in its status
  line, the stat strip (including miscabled cables), one GPU node end to end, the rail × SU
  matrix, the canvas with live status and miscabling, the inspector, **Cabling vs UFM**, the
  scalable units and the design checks.

### nsr metrics inside every fabric page (teal)

Every fabric page (the ICE2 and LON14 engines too) carries the nsr live metrics directly
under its own stat strip, in the same design but always in **teal** and tagged **nsr**, so
they are never mistaken for NetBox / UFM / switch data: a teal stat strip (ping, switches in
UFM telemetry, ports not Active, link-downs and errors in 15 min, UFM servers, gNMI sessions,
PSU/fan problems, SNMP, open PagerDuty incidents), then **Live issues (nsr)** ranked with
the affected switches and ports, UFM event-log lines and the incidents. It refreshes every
60 s or with its own ⟳ Refresh. In the NetBox-first view the nsr-derived values on the page
(per-cable live state, *Live status (nsr)* chip, *UFM · nsr* and *Ping · nsr* in the
inspector) are teal as well.

### How the NetBox-first view reads the fabric

- Rails are the GPU NIC names used across the fabric (`ib0..ib7`, `mlx5_0..3`), in order; a
  leaf's rail is where most of its GPU cables arrive; a scalable unit is the GPU nodes that
  share the same leaves (a node with a NIC missing or misplaced stays in its SU and is flagged).
- UFM ports are matched to NetBox cables by port number: `swNpM` → 2·(N−1)+M, `IB1/N/M`, or
  `N`. The status line shows how many cables matched; if few do, set
  `"port_names": "swNpM" | "cage/split" | "auto"` in that region's `fabric_view`.
- UFM and NetBox spell hosts differently (`gpu012` vs `sys2-osl1-p-phy-gpu012`); they count
  as the same device when one name ends with the other.
- Design checks: full leaf–spine mesh and cables per pair, leaf-to-leaf cables, GPU nodes
  missing rails, NICs on a leaf of another rail, uneven port use, NetBox cable states,
  switches without UFM telemetry or ping.

## What each site has (checked 2026-10-08, netops-misc observability compendium)

| Site | gNMI | UFM telemetry | SNMP | Ping | UFM servers | Logs (Loki) |
| --- | --- | --- | --- | --- | --- | --- |
| ICE2 | ✓ 100 | ✓ alert-primary-hf (Mimir) | ✓ | ✓ | ✓ + HA role | UFM event log |
| LON14 | ✓ 100 | ✓ **lon14-victoriametrics only** | ✓ | ✓ | ✓ + HA role | UFM event log |
| OSL1 | ✗ | ✓ alert-primary-hf, 48 switches | ✓ mlnx_health 48/48 | ✓ 48/48 | ufm0 up, ufm1 down | UFM event log, switch syslog |
| STA1 | ✗ | ✓ **job `ufm-mgmt`** (cset `nscale`), 97 switches, 8,328 ports (verified 12 Oct) | 24 Nokia sys1 (incl. chassis health); QM9700 pending (PR #1041) | ✓ 120 (96 QM9700 + 24 Nokia) | down since 24 Sep (ITSM-3588) | UFM event log, switch syslog |
| GLO1 | ✗ | ✗ | ✗ | ✗ | ✗ | — (OBS-681) |

So for **STA1 and OSL1** the live sources beyond gNMI are: UFM telemetry, blackbox ping,
UFM server monitoring, the UFM event log and switch syslog in Loki, SNMP (OSL1 fully,
STA1 Nokia only) and PagerDuty. STA1's UFM data comes from a different job (`ufm-mgmt`)
but carries the same labels, verified with `discover.py` on 12 Oct 2026. STA1 also has
per-switch exporter jobs (`scrape_swi638..705`), PDU, cooler and IPMI/Redfish telemetry,
not used here. Re-check any site with:

```bash
python3 discover.py sta1        # or osl1, or --all
```

`discover.py` asks every Prometheus store (infra-mimir, promxy, europe-west and LON14
VictoriaMetrics) which jobs, UFM metric families and labels exist for the site, samples
its Loki streams, runs the dashboard's own checks, and saves the report to
`discovery/<site>-<time>.txt` (git-ignored). If a source has series but a check says
*no data*, fix that site's `selector` in `regions.json`.

## Adding or changing a site

Everything site-specific is in `regions.json`: `cloud_regions` (all aliases), the label
selector for each source (`ufm`, `gnmi`, `snmp`, `ping`, `ufm_server`), the LogQL for
`ufm_events` and `syslog`, the Grafana dashboards to link, the cabling dashboard URL and
notes. A source that is left out is shown as *not collected*. Restart the service after
editing.

## Files

| Path | Purpose |
| --- | --- |
| `multi_region_dashboard.py` | The single service: region pages, HTTP API, engine proxy |
| `engines.py` | Starts the ICE2 / LON14 engines and serves them under /ice2/, /lon14/ |
| `netbox_source.py` | NetBox first: devices and leaf cabling, read directly (GET only) |
| `ufm_direct.py` | UFM REST link list through the site's jump host; UFM-vs-NetBox matching |
| `scripts/configure_site_ufm.sh` | One-time direct UFM access per NetBox-first region |
| `checks.py` | Every query, and how answers become issues |
| `nsr_client.py` | Runs the allowed nsr verbs (metrics, logs, incidents only), limits concurrency |
| `discover.py` | Read-only data-source discovery per site |
| `regions.json` | Sites, selectors, links, notes (no secrets) |
| `assets/regions.html` | Region picker and region pages |
| `assets/fabric.html` | Spine · leaf · rail view for regions without their own dashboard |
| `fabric.py` | NetBox snapshot, inferred rails/SUs, design checks, UFM live overlay |
| `scripts/open_regions.command` | One-click start of the single service |
| `tests/` | Offline tests with `fake_nsr.py` and `fake_netbox.py` (invented data) |

HTTP API (127.0.0.1 only): `GET /api/config`, `GET /api/overview`,
`GET /api/region/<site>/<fabric|switches|reach|events|incidents>`, `GET /api/topology/<site>`;
add `?refresh=1` to bypass the cache.

Tests: `python3 -m unittest discover -s tests -v`.
