# Backend regions dashboard (all sites, live issues via nsr)

Pick a region, see what is wrong in its backend fabric right now. One small local service
covers every backend site (ICE2, LON14, OSL1, STA1, GLO1) using the observability stack
that already exists, read through [`nsr`](https://github.com/nscaledev/netops-misc/tree/main/nsr):

| Source (through nsr) | What the dashboard shows |
| --- | --- |
| **UFM telemetry** (Grafana: infra-mimir, LON14 VictoriaMetrics) | Ports not Active per switch, link-down events, symbol/receive errors, discards, congestion |
| **gNMI** (ICE2, LON14) | PSUs and fans not ACTIVE, PSUs without input, hot optics, ASIC temperature, switch-side link-downs |
| **Blackbox ping** | Switches not answering on management |
| **SNMP** | SNMP targets not answering |
| **UFM server monitoring** | UFM appliances up/down, CPU and memory, HA role where command-exporter runs |
| **Loki** | UFM event log (critical, errors, switch/node down) and backend switch syslog |
| **PagerDuty** (BackEnd Network, PK6NAFF) | Open incidents that name the site |

The ICE2 and LON14 NetBox + UFM dashboards (cabling, miscabling, diagram) stay as they
are; each region page links to them and can show them inline when they are running.

- **Read-only.** Only three nsr verbs are allowed: `grafana query`, `grafana loki` and
  `pd incidents`. Nothing logs in to a switch, UFM or NetBox.
- **No secrets here.** nsr owns Teleport mTLS and the PagerDuty token.
- **Light on Grafana.** The all-regions page is 18 grouped queries (`by (cloud_region)`),
  not one per site; every query aggregates or returns only offenders; answers are cached
  60 s; at most 6 nsr processes run at once.
- **Absent is not zero.** When a source has no data for a site, the page shows “—” or
  “no data”, never a healthy 0.

```
Browser ─► 127.0.0.1:8780  multi_region_dashboard.py ─► nsr ─► Teleport ─► Grafana (Mimir · VictoriaMetrics · Loki)
                                                          └──────────────► PagerDuty
```

## Run it

Once:

```bash
brew install uv teleport
uv tool install "git+ssh://git@github.com/nscaledev/netops-misc.git#subdirectory=nsr"
tsh login --proxy=nscale.teleport.sh --auth=google
nsr doctor                 # Grafana must be reachable; PagerDuty optional (nsr setup pd)
```

Every day: double-click **`scripts/open_regions.command`**, or

```bash
cd idc-automation/multi-region-backend-dashboard
python3 multi_region_dashboard.py          # http://127.0.0.1:8780/
```

Options: `--nsr PATH`, `--port N`, `--cache-seconds N` (default 60), `--max-parallel N`
(default 6), `--warm` (load the overview at start). Try it without any access:
`python3 multi_region_dashboard.py --nsr tests/fake_nsr.py` serves invented demo data.

For cabling and topology, also start the site dashboards: ICE2 on 8765
(`ice2-netbox-dashboard/scripts/open_dashboard.command`) and LON14 on 8766.

## Using it

- **All regions** (`/`): one card per site with status (*Healthy*, *Issues*, *Critical*,
  *No telemetry*), switches answering ping, ports not Active, ports with link-downs and
  errors in the last 15 min, UFM servers up, open incidents, and a dot per data source.
- **A region with its own fabric dashboard** (ICE2, LON14) opens that dashboard first,
  full size and unchanged: the same topology, cabling check, Sync fabric, Refresh NetBox
  and inspector as running `netbox_live_sync.py` on its own (ICE2 on 8765, LON14 on 8766).
  **Live issues (nsr)** is the second tab; the tab shows how many issues there are.
- **Live issues** (the only view for OSL1, STA1, GLO1; `#issues` for ICE2/LON14): ranked critical → major → minor, each with the
  affected switches/ports and what to do; UFM event log and syslog lines; open PagerDuty
  incidents; the data sources at that site and whether they answered; every check with
  its PromQL; links to that site's Grafana dashboards and cabling dashboard.
- **⟳ Refresh** asks nsr again (bypasses the cache). Auto-refresh runs every 60 s.

## The spine · leaf · rail view for every InfiniBand region

ICE2 and LON14 keep their own NetBox + UFM dashboards. Every other InfiniBand region
(OSL1, STA1, GLO1, and any site added to `regions.json` with a `fabric_view`) gets the
same design at `/f/<site>` (also the **Fabric dashboard** tab of its region page):
header and status line, stat strip, *How the rail groups are wired* (one GPU node end to
end, rail × SU matrix), the spine/leaf/SU canvas with live status, the inspector, the
scalable units and design checks.

It is built only from nsr, so a new site needs no jump host, switch login or UFM password:

- **Topology from NetBox** (`nsr netbox devices` + `nsr netbox connections`, read-only):
  press **Refresh NetBox** (or *Read <site> from NetBox* the first time). The snapshot is
  saved under `cache/` (git-ignored) and reused until the next refresh.
- **Structure is inferred**: rails are the GPU NIC names used across the fabric
  (`ib0..ib7`, `mlx5_0..3`); a leaf's rail is where most of its GPU cables arrive; an SU is
  the GPU nodes that share the same leaves. UFM servers and other hosts are listed apart.
- **Live state from UFM telemetry** (port state, link-downs, errors) and blackbox ping,
  matched to each cable's leaf port by number (`swNpM` → 2·(N−1)+M, `IB1/N/M`, or `N`).
  The status line shows how many cables matched; if few do, set
  `"port_names": "swNpM" | "cage/split" | "auto"` in that site's `fabric_view`.
- **Design checks**: full leaf–spine mesh and cables per pair, leaf-to-leaf cables, GPU
  nodes missing rails, NICs on a leaf of another rail, uneven port use, NetBox cable states,
  switches without UFM telemetry or ping.

`fabric_view` per site: `{"netbox_site": "osl1", "cluster": "sys2-osl1"}`; add
`"fiber_graph": true` for shuffle-cabled sites (GSO1-style) whose cables live in the
NetBox datacenter plugin.

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
| `multi_region_dashboard.py` | Local service and HTTP API |
| `checks.py` | Every query, and how answers become issues |
| `nsr_client.py` | Runs the allowed nsr verbs, parses `--json`, limits concurrency |
| `discover.py` | Read-only data-source discovery per site |
| `regions.json` | Sites, selectors, links, notes (no secrets) |
| `assets/regions.html` | Region picker and region pages |
| `assets/fabric.html` | Spine · leaf · rail view for regions without their own dashboard |
| `fabric.py` | NetBox snapshot, inferred rails/SUs, design checks, UFM live overlay |
| `scripts/open_regions.command` | One-click start |
| `tests/` | Offline tests and `fake_nsr.py` (invented data) |

HTTP API (127.0.0.1 only): `GET /api/config`, `GET /api/overview`,
`GET /api/region/<site>/<fabric|switches|reach|events|incidents>`, `GET /api/topology/<site>`;
add `?refresh=1` to bypass the cache.

Tests: `python3 -m unittest discover -s tests -v`.
