#!/usr/bin/env python3
"""Local, read-only ICE2 backend fabric reconciliation service.

The browser never receives the NetBox token.  It talks only to this loopback
service, which reads the token from macOS Keychain and queries NetBox through
the already authenticated local Teleport proxy.  Live switch collection is
also read-only (`nv show interface --output json`).

Endpoints
---------
GET  /                    the live backend GPU diagram
GET  /api/live            live link state for every designed link in use (switch side)
GET  /api/health          NetBox reachability + live-evidence summary (never fails hard)
GET  /api/verify/<id>     one cable: current NetBox record vs. live switch state
POST /api/refresh         start a read-only collection across the 100 backend switches
GET  /api/refresh/<run>   collection progress
POST /api/sync            Sync fabric: switch collection + UFM fetch (+ NetBox when its inventory is due)
POST /api/netbox/refresh  NetBox inventory and cable records only
GET  /api/sync/<run>      sync progress with per-phase timings
GET  /api/cabling         design topology vs. what UFM actually sees (NetBox alongside)
GET  /api/incidents       ranked fabric incidents
POST /api/cabling/fetch   read UFM's scan, master topology and compare report directly (via the jump host)
GET  /api/cabling/fetch/<run>  progress of that fetch
GET  /api/cabling/findings.csv       every non-OK cabling observation
GET  /api/cabling/netbox-import.csv  cables UFM sees but NetBox lacks, in NetBox import columns

Performance notes
-----------------
* /api/live is computed once per data change and served with an ETag (304 when
  unchanged) and gzip, so frequent dashboard polling is nearly free.
* Management IPs come from one bulk NetBox query per (site, role) group and are
  cached locally (default 24 h); per-device lookups are only a fallback.
* Cable sync trims the REST payload with `fields=` and, between periodic full
  syncs, is incremental: only cables named in the NetBox change log since the
  last sync are fetched (deleted cables are detected the same way).
* The NetBox phase and the device phase run concurrently; device results are
  applied to the live view as each switch finishes.
* `--fanout jump` replaces one Teleport session per switch with a single
  session that fans out from the jump host.
"""

from __future__ import annotations

import argparse
import sys as _sys
import collections
import csv
import getpass
import gzip
import hashlib
import os
import json
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
ASSETS_DIR = PROJECT_ROOT / "assets"
COLLECTOR = PROJECT_ROOT / "collector" / "run_ntp_audit.py"
STATE_DIR = PROJECT_ROOT / ".netbox-live-sync"
LATEST = STATE_DIR / "latest-live.json"
ADDRESS_CACHE = STATE_DIR / "management-addresses.csv"
CABLE_CACHE = STATE_DIR / "netbox-cables.json"
DEVICE_CACHE = STATE_DIR / "netbox-devices.json"
TRAY_HISTORY = STATE_DIR / "tray-history.json"
DEFAULT_UFM_SCAN = PROJECT_ROOT / "local-inputs" / "ufm" / "ibdiagnet2.lst.gz"
DEFAULT_EXPECTED = ASSETS_DIR / "expected_topology.csv"
DEFAULT_UFM_MASTER = PROJECT_ROOT / "local-inputs" / "ufm" / "master.topo.gz"
DEFAULT_DESIGN_TOPO = PROJECT_ROOT / "local-inputs" / "ufm" / "nscale_Compute.topo"
DEFAULT_UFM_REPORT = PROJECT_ROOT / "local-inputs" / "ufm" / "topology-compare.json.gz"
DEVICE_FIELDS = "name,primary_ip4,primary_ip,device_type,status"
CABLE_FIELDS = "id,a_terminations,b_terminations"
BASELINE_COLLECTED_AT = "not-collected"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def endpoint_from_termination(termination: dict) -> dict:
    obj = termination.get("object") or {}
    device = obj.get("device") or {}
    return {"device": device.get("name", "unknown"), "port": obj.get("name", "unknown")}


def parse_iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def age_hours(value: str | None) -> float:
    stamp = parse_iso(value)
    return float("inf") if not stamp else (datetime.now(timezone.utc) - stamp).total_seconds() / 3600


def next_path(page: dict) -> str | None:
    nxt = page.get("next")
    return nxt[nxt.find("/api/"):] if nxt and "/api/" in nxt else None


def termination_problem(cable: dict) -> str:
    """'' when the cable has exactly one interface termination with a device and port on each side."""
    for side in ("a", "b"):
        terms = cable.get(side + "_terminations") or []
        if len(terms) != 1:
            return "%d %s-side terminations" % (len(terms), side.upper())
        term = terms[0]
        obj = term.get("object") or {}
        if term.get("object_type") not in (None, "dcim.interface"):
            return "%s side ends on a %s, not an interface" % (side.upper(), term.get("object_type"))
        if not (obj.get("device") or {}).get("name") or not obj.get("name"):
            return "malformed %s-side termination" % side.upper()
    return ""


def cable_endpoints(cable: dict) -> list | None:
    """[a_dev, a_port, b_dev, b_port], or None plus nothing usable. A cable that is not exactly
    one interface on each side returns ["", "", "", "", "<problem>"] so it is never counted as matching."""
    problem = termination_problem(cable)
    if problem:
        return ["", "", "", "", problem]
    ea = endpoint_from_termination(cable["a_terminations"][0])
    eb = endpoint_from_termination(cable["b_terminations"][0])
    return [ea["device"], ea["port"], eb["device"], eb["port"]]


def device_details(device: dict) -> dict:
    """The NetBox facts shown in the inspector, from a full or `fields=`-trimmed device record."""
    device_type = device.get("device_type") or {}
    manufacturer = device_type.get("manufacturer") or {}
    primary = device.get("primary_ip4") or device.get("primary_ip") or {}
    status = device.get("status") or {}
    return {
        "hostname": device.get("name"),
        "primary_ip": primary.get("address") or "not assigned",
        "vendor": manufacturer.get("name") or manufacturer.get("display") or "not recorded",
        "model": device_type.get("model") or device_type.get("display") or "not recorded",
        "status": (status.get("value") or status.get("label") or "unknown") if isinstance(status, dict) else str(status),
    }


def link_status(states: list[str], required: int | None = None) -> str:
    """Collapse the endpoint states of one cable into a single status.

    `required` is how many ends must be collected and Active for "active": 2 for a
    leaf-spine cable, 1 for a leaf-GPU cable (the GPU side is never collected).
    A required end that was not collected in the latest run ("stale"/"not-collected")
    makes an otherwise healthy cable "unverified", never "active"."""
    collected = [s for s in states if s and s not in ("not-collected", "stale", "missing-port")]
    if any(s.startswith("Down/") for s in collected):
        return "down"
    if any(s.startswith("Initialize/") or s.startswith("Armed/") for s in collected):
        return "init"
    need = len(states) if required is None else required
    if len(collected) < need:
        return "unknown" if any(s == "missing-port" for s in states) else "unverified"
    if collected and all(s.startswith("Active/LinkUp/") for s in collected):
        return "active"
    return "unknown"


def file_fingerprint(*paths) -> str:
    """Short hash of the topology and inventory files the evidence was collected against."""
    digest = hashlib.sha256()
    for path in paths:
        try:
            digest.update(Path(path).read_bytes() if path else b"-")
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()[:16]


class SyncState:
    def __init__(self, netbox_url: str, netbox_host_header: str | None, connections: Path, device_profile: Path | None, devices: Path | None, known_hosts: Path | None, commands: Path | None, options: argparse.Namespace | None = None) -> None:
        self.netbox_url = netbox_url.rstrip("/")
        self.netbox_host_header = netbox_host_header
        self.connections = connections
        self.device_profile = device_profile
        self.devices = devices
        self.known_hosts, self.commands = known_hosts, commands
        self.token: str | None = None
        self.baseline: dict[str, dict] = {}
        self.live: dict[tuple[str, str], str] = {}
        self.refreshes: dict[str, dict] = {}
        self.lock = threading.RLock()  # re-entrant: address lookup may run inside start_refresh
        self.collected_at = BASELINE_COLLECTED_AT
        self.source = "local topology snapshot"
        self.switches: list[str] = []
        self.syncs: dict[str, dict] = {}
        self.netbox_sync: dict | None = None
        opts = options or argparse.Namespace()
        self.page_size = getattr(opts, "netbox_page_size", 250)
        self.nb_concurrency = getattr(opts, "netbox_concurrency", 2)
        self.address_cache_hours = getattr(opts, "address_cache_hours", 24.0)
        self.full_every_hours = getattr(opts, "full_netbox_every_hours", 6.0)
        self.device_parallel = getattr(opts, "device_parallel", 10)
        self.fanout = getattr(opts, "fanout", "local")
        self.netbox_every_hours = getattr(opts, "netbox_every_hours", 0.0)
        self.stale_after_minutes = getattr(opts, "stale_after_minutes", 60.0)
        # Evidence freshness: which switches the latest finished collection reached, when each
        # switch was last read, and the coverage of that collection.
        self.last_ok: set[str] = set()
        self.run_ok: set[str] = set()
        self.switch_at: dict[str, str] = {}
        self.coverage: dict | None = None
        self.evidence_note = ""
        self.fields_ok: bool | None = None          # NetBox `fields=` support, learned on first use
        self.nb_cables: dict[str, list | None] = {}  # cable id -> [a_dev, a_port, b_dev, b_port]
        self.nb_full_at: str | None = None
        self.nb_synced_at: str | None = None        # start time of the last successful cable sync
        self.version = 0
        self._live_cache: tuple | None = None
        self.load_baseline()
        self.load_cable_cache()
        self.ufm_scan = Path(getattr(opts, "ufm_scan", None) or DEFAULT_UFM_SCAN)
        self.expected_topology = Path(getattr(opts, "expected_topology", None) or DEFAULT_EXPECTED)
        self.design_topo = Path(getattr(opts, "design_topo", None) or DEFAULT_DESIGN_TOPO)
        self.cabling_reference = getattr(opts, "cabling_reference", None) or "netbox"
        self.load_latest()
        self.ufm_master = Path(getattr(opts, "ufm_master", None) or DEFAULT_UFM_MASTER)
        self.ufm_report = Path(getattr(opts, "ufm_report", None) or DEFAULT_UFM_REPORT)
        self._cabling: tuple | None = None
        self.ufm_fetches: dict[str, dict] = {}
        self.device_info: dict[str, dict] = {}
        self.device_info_at: str | None = None
        self.load_device_cache()

    def bump(self) -> None:
        """Invalidate the cached /api/live payload (call after any state change)."""
        self.version += 1

    # ---------- evidence loading ----------
    def load_baseline(self) -> None:
        with self.connections.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                self.baseline[row["netbox_cable_id"]] = row
                self.live[(row["endpoint_a_device"], row["endpoint_a_port"])] = row["endpoint_a_live_state"]
                self.live[(row["endpoint_b_device"], row["endpoint_b_port"])] = row["endpoint_b_live_state"]

    def evidence_fingerprint(self) -> str:
        return file_fingerprint(self.connections, self.expected_topology, self.devices,
                                self.design_topo if self.design_topo.is_file() else None)

    def expected_rows(self) -> tuple[list[dict] | None, dict | None]:
        """The reference: the approved design .topo when present (rules fill its gaps),
        otherwise the inferred rules in expected_topology.csv."""
        csv_file = self.expected_topology if self.expected_topology.is_file() else None
        topo = self.design_topo if self.design_topo.is_file() else None
        key = (csv_file.stat().st_mtime_ns if csv_file else 0, topo.stat().st_mtime_ns if topo else 0, topo.stat().st_size if topo else 0)
        cached = getattr(self, "_expected", None)
        if cached and cached[0] == key:
            return cached[1]
        _sys.path.insert(0, str(APP_DIR))
        import ufm_cabling  # noqa: E402
        rules = ufm_cabling.load_expected(csv_file) if csv_file else None
        result = (rules, {"kind": "inferred design", "file": csv_file.name} if csv_file else None)
        if topo:
            try:
                result = ufm_cabling.load_design_topo(topo, rules)
            except (OSError, ValueError) as error:
                print("[netbox-live-sync] approved design %s unreadable, using the inferred rules: %s" % (topo, error))
        self._expected = (key, result)
        return result

    def netbox_reference(self) -> tuple[list[dict], dict]:
        """NetBox as the expected cabling: the cables from the last NetBox sync, or the
        bundled export (assets/connections.csv) until NetBox has been read once."""
        key = (self.nb_synced_at, len(self.nb_cables), len(self.baseline))
        cached = getattr(self, "_nb_reference", None)
        if cached and cached[0] == key:
            return cached[1]
        _sys.path.insert(0, str(APP_DIR))
        import ufm_cabling  # noqa: E402
        if self.nb_cables:
            rows, info = ufm_cabling.netbox_rows(self.nb_cables)
            info.update(kind="NetBox", source="NetBox sync", synced_at=self.nb_synced_at, full_at=self.nb_full_at)
        else:
            rows = list(self.baseline.values())
            info = {"kind": "NetBox", "source": "bundled export", "file": self.connections.name, "synced_at": None, "cables": len(rows),
                    "leaf_spine": sum(r["connection_type"] == "leaf-spine" for r in rows),
                    "gpu": sum(r["connection_type"] == "leaf-gpu-rdma" for r in rows), "unusable": 0}
        self._nb_reference = (key, (rows, info))
        return rows, info

    def load_latest(self) -> None:
        """Re-apply the most recent collection so a restart keeps the newest evidence.

        The saved evidence is trusted as current only if it was collected against the same
        topology and inventory files (fingerprint); otherwise it is kept for display but
        every switch counts as not verified until the next sync."""
        if not LATEST.is_file():
            return
        try:
            saved = json.loads(LATEST.read_text(encoding="utf-8"))
            if saved.get("version") == 2:
                # the saved snapshot is complete per switch: drop the bundled export's states for those switches
                read = set(saved.get("switch_at", {}))
                for key in [k for k in self.live if k[0] in read]:
                    del self.live[key]
            for key, state in saved.get("ports", {}).items():
                device, port = key.split("|", 1)
                self.live[(device, port)] = state
            self.collected_at = saved.get("collected_at", self.collected_at)
            self.switches = saved.get("switches", [])
            self.switch_at = saved.get("switch_at", {})
            self.coverage = saved.get("coverage")
            self.source = "device collection %s (%d switches)" % (self.collected_at, len(self.switches))
            if saved.get("version") == 2 and saved.get("fingerprint") == self.evidence_fingerprint():
                self.last_ok = set(saved.get("last_ok", []))
            else:
                self.evidence_note = ("saved evidence was collected against a different topology or inventory; sync to verify"
                                      if saved.get("version") == 2 else "saved evidence predates coverage tracking; sync to verify")
        except (OSError, ValueError) as error:
            print("[netbox-live-sync] ignoring unreadable %s: %s" % (LATEST, error))

    def load_cable_cache(self) -> None:
        if not CABLE_CACHE.is_file():
            return
        try:
            saved = json.loads(CABLE_CACHE.read_text(encoding="utf-8"))
            self.nb_cables = saved.get("cables", {})
            self.nb_full_at, self.nb_synced_at = saved.get("full_at"), saved.get("synced_at")
            self.netbox_sync = saved.get("result")
        except (OSError, ValueError) as error:
            print("[netbox-live-sync] ignoring unreadable %s: %s" % (CABLE_CACHE, error))

    def load_device_cache(self) -> None:
        if not DEVICE_CACHE.is_file():
            return
        try:
            saved = json.loads(DEVICE_CACHE.read_text(encoding="utf-8"))
            self.device_info, self.device_info_at = saved.get("devices", {}), saved.get("synced_at")
        except (OSError, ValueError) as error:
            print("[netbox-live-sync] ignoring unreadable %s: %s" % (DEVICE_CACHE, error))

    def address_book(self) -> dict[str, str]:
        """Management IPs saved by earlier syncs (works with NetBox down)."""
        if not ADDRESS_CACHE.is_file():
            return {}
        try:
            with ADDRESS_CACHE.open(newline="", encoding="utf-8") as handle:
                return {r["hostname"]: r["management_address"] for r in csv.DictReader(handle) if r.get("management_address")}
        except (OSError, KeyError, ValueError):
            return {}

    def known_devices(self) -> dict[str, list]:
        """[ip, vendor, model, status] per switch from local data only: full details saved by
        the last sync, else just the saved management IP. Never calls NetBox."""
        known = {name: [d.get("primary_ip"), d.get("vendor"), d.get("model"), d.get("status")] for name, d in self.device_info.items()}
        for name, address in self.address_book().items():
            known.setdefault(name, [address, "", "not synced yet", "unknown"])
        return known

    def remember_devices(self, details: dict[str, dict], replace: bool = False) -> None:
        """Keep the NetBox device facts from the latest sync so the inspector never waits on NetBox."""
        with self.lock:
            if replace:
                self.device_info = dict(details)
                self.device_info_at = now_iso()
            else:
                self.device_info.update(details)
                self.device_info_at = self.device_info_at or now_iso()
            snapshot = {"synced_at": self.device_info_at, "devices": self.device_info}
            self.bump()
        STATE_DIR.mkdir(exist_ok=True)
        partial = DEVICE_CACHE.with_suffix(".part")
        partial.write_text(json.dumps(snapshot), encoding="utf-8")
        partial.replace(DEVICE_CACHE)

    def save_cable_cache(self) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        partial = CABLE_CACHE.with_suffix(".part")
        partial.write_text(json.dumps({"full_at": self.nb_full_at, "synced_at": self.nb_synced_at,
                                       "cables": self.nb_cables, "result": self.netbox_sync}), encoding="utf-8")
        partial.replace(CABLE_CACHE)

    # ---------- NetBox ----------
    def netbox_token(self) -> str:
        if self.token:
            return self.token
        command = ["security", "find-generic-password", "-s", "netbox-mcp-token", "-a", getpass.getuser(), "-w"]
        completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if completed.returncode or not completed.stdout.strip():
            raise RuntimeError("NetBox token was not found in macOS Keychain. Run configure-netbox-token.sh first.")
        self.token = completed.stdout.strip()
        return self.token

    def get_netbox(self, path: str) -> dict:
        reread = False
        self.netbox_requests = getattr(self, "netbox_requests", 0) + 1
        for attempt in range(3):
            headers = {"Authorization": "Token " + self.netbox_token(), "Accept": "application/json"}
            if self.netbox_host_header:
                headers["Host"] = self.netbox_host_header
            request = Request(self.netbox_url + path, headers=headers, method="GET")
            try:
                with urlopen(request, timeout=20) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
                if error.code in (401, 403) and not reread:
                    # The token may have been replaced in Keychain since start: read it again once.
                    reread, self.token = True, None
                    continue
                if error.code in (401, 403):
                    raise RuntimeError("NetBox rejected the token (HTTP %s: %s). Store a valid read-only token for this NetBox with "
                                       "./scripts/configure_netbox_token.sh." % (error.code, body[:120])) from error
                raise RuntimeError("NetBox returned HTTP %s: %s" % (error.code, body[:240])) from error
            except (URLError, TimeoutError, socket.timeout) as error:
                if attempt == 2:
                    raise RuntimeError("Cannot reach NetBox at %s after 3 attempts: %s" % (self.netbox_url, error)) from error
                time.sleep(0.5 * (attempt + 1))
        raise AssertionError("NetBox request retry loop did not return")

    def get_list(self, base: str, fields: str | None, required: tuple[str, ...] = ()) -> dict:
        """GET a NetBox list page, trimming the payload with `fields=` when NetBox supports it.

        NetBox 4.x honours `fields`; older versions ignore it or reject it, in which
        case the full payload is requested and the feature is switched off.
        """
        if fields and self.fields_ok is not False:
            try:
                page = self.get_netbox(base + "&fields=" + fields)
                results = page.get("results") or []
                if results and any(key not in results[0] for key in required):
                    self.fields_ok = False
                else:
                    if results:
                        self.fields_ok = True
                    return page
            except RuntimeError as error:
                if "HTTP 400" not in str(error):
                    raise
                self.fields_ok = False
        return self.get_netbox(base)

    def write_addresses(self, output: Path, names: list[str], addresses: dict[str, str]) -> None:
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["hostname", "management_address"])
            writer.writerows((name, addresses[name]) for name in names)

    def fetch_management_addresses(self, output: Path) -> int:
        """Build an ephemeral device-IP map; never persist it in Git.

        Order of preference: fresh local cache (no NetBox calls), one bulk query per
        (site, role) group from the inventory, then per-device lookups for any gaps.
        """
        if not self.devices:
            raise RuntimeError("A local device inventory is required to fetch management addresses.")
        with self.devices.open(newline="", encoding="utf-8-sig") as handle:
            inventory = [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(handle)]
        names = [row.get("hostname", "") for row in inventory if row.get("hostname")]
        if not names or len(names) != len(set(names)):
            raise RuntimeError("The device inventory must contain unique non-empty hostname values.")
        self.address_source = "netbox"
        if ADDRESS_CACHE.is_file() and (time.time() - ADDRESS_CACHE.stat().st_mtime) < self.address_cache_hours * 3600:
            with ADDRESS_CACHE.open(newline="", encoding="utf-8") as handle:
                cached = {r["hostname"]: r["management_address"] for r in csv.DictReader(handle) if r.get("management_address")}
            if all(name in cached for name in names) and all(name in self.device_info for name in names):
                self.write_addresses(output, names, cached)
                self.address_source = "local cache"
                return len(names)
        wanted = set(names)
        addresses: dict[str, str] = {}
        details: dict[str, dict] = {}
        groups = sorted({(row.get("site", ""), row.get("netbox_role", "")) for row in inventory if row.get("site") and row.get("netbox_role")})
        bulk_errors: list[str] = []
        for site, role in groups:
            try:
                path: str | None = "/api/dcim/devices/?site=%s&role=%s&limit=1000" % (quote(site, safe=""), quote(role, safe=""))
                first = True
                while path:
                    page = self.get_list(path, DEVICE_FIELDS, ("name", "device_type")) if first else self.get_netbox(path)
                    first = False
                    for device in page.get("results") or []:
                        name = device.get("name")
                        if name in wanted:
                            details[name] = device_details(device)
                        primary = device.get("primary_ip4") or device.get("primary_ip") or {}
                        address = (primary.get("address") or "").split("/", 1)[0]
                        if name in wanted and address:
                            addresses[name] = address
                    path = next_path(page)
            except RuntimeError as error:
                bulk_errors.append(str(error))
                print("[netbox-live-sync] bulk address query %s/%s failed, falling back per device: %s" % (site, role, error))
        if bulk_errors and not addresses:
            # NetBox is unreachable: do not try 100 per-device lookups first.
            raise RuntimeError(bulk_errors[-1])
        if addresses:
            self.address_source = "netbox bulk"
        if details:
            self.remember_devices(details, replace=len(details) == len(wanted))
        names_left = [name for name in names if name not in addresses]
        if not names_left:
            self.write_addresses(output, names, addresses)
            STATE_DIR.mkdir(exist_ok=True)
            self.write_addresses(ADDRESS_CACHE, names, addresses)
            return len(addresses)
        names_bulk = addresses
        names = names_left
        def address_from_device(device: dict, name: str) -> str:
            primary = device.get("primary_ip4") or device.get("primary_ip") or {}
            address = (primary.get("address") or "").split("/", 1)[0]
            if not address:
                raise RuntimeError("NetBox has no primary management IP for %s." % name)
            return address

        addresses = dict(names_bulk)
        def lookup(name: str) -> tuple[str, str]:
            payload = self.get_netbox("/api/dcim/devices/?limit=2&name=" + quote(name, safe=""))
            matches = payload.get("results") or []
            if len(matches) != 1:
                raise RuntimeError("NetBox returned %d devices for %s." % (len(matches), name))
            details[name] = device_details(matches[0])
            return name, address_from_device(matches[0], name)

        # The Teleport app proxy reliably handles exact device lookups, but
        # can stall on a long `name__in` filter. Three in-flight requests keep
        # it responsive while still cutting this phase substantially.
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(lookup, name): name for name in names}
            for future in as_completed(futures):
                name, address = future.result()
                addresses[name] = address
        all_names = [row.get("hostname", "") for row in inventory if row.get("hostname")]
        if details:
            self.remember_devices(details, replace=len(details) == len(wanted))
        self.write_addresses(output, all_names, addresses)
        STATE_DIR.mkdir(exist_ok=True)
        self.write_addresses(ADDRESS_CACHE, all_names, addresses)
        if names_bulk:
            self.address_source = "netbox bulk + %d per-device" % len(names)
        else:
            self.address_source = "netbox per-device"
        return len(addresses)

    # ---------- live view ----------
    def live_links(self) -> list[tuple]:
        """The links Live link state checks: the design topology, with NetBox cable IDs.

        leaf-spine: every designed cable. leaf-GPU: designed GPU ports that are in use
        (seen by UFM now or before, or documented in NetBox), so empty slots are not
        reported as down. Falls back to the NetBox cable list without a design file.
        Rows: (cable_id, type, a_dev, a_port, b_dev, b_port, source)."""
        rows, _info = self.expected_rows()
        try:
            report = self.cabling()
        except Exception:
            report = None
        key = (id(rows), self._cabling[0] if self._cabling else None, len(self.baseline),
               TRAY_HISTORY.stat().st_mtime_ns if TRAY_HISTORY.is_file() else 0)
        cached = getattr(self, "_live_links", None)
        if cached and cached[0] == key:
            return cached[1]
        by_port = {}
        for cid, row in self.baseline.items():
            for dev, port in ((row["endpoint_a_device"], row["endpoint_a_port"]), (row["endpoint_b_device"], row["endpoint_b_port"])):
                if "-swi-" in dev:
                    by_port.setdefault((dev, port), (cid, row))
        if not rows:
            links = [(cid, r["connection_type"], r["endpoint_a_device"], r["endpoint_a_port"], r["endpoint_b_device"], r["endpoint_b_port"], "netbox")
                     for cid, r in self.baseline.items()]
            self._live_links = (key, links)
            return links
        in_use = {}
        for leaf, port, code in (report or {}).get("gpu_ports_seen", []):
            in_use[(leaf, port)] = code
        try:
            for code, h in json.loads(TRAY_HISTORY.read_text()).items():
                for leaf, port in h.get("ports", []):
                    in_use.setdefault((leaf, port), code)
        except (OSError, ValueError):
            pass
        links = []
        for row in rows:
            a = (row["a_device"], row["a_port"])
            nb = by_port.get(a)
            cid = nb[0] if nb else ""
            if row["link_type"] == "leaf-spine":
                links.append((cid, "leaf-spine", a[0], a[1], row["b_device"], row["b_port"], "design"))
            elif row["link_type"] == "leaf-gpu":
                if a not in in_use and not (nb and nb[1]["connection_type"] == "leaf-gpu-rdma"):
                    continue
                far = in_use.get(a) or (nb[1]["endpoint_b_device"] if nb and nb[1]["endpoint_a_device"] == a[0] else nb[1]["endpoint_a_device"] if nb else "")
                links.append((cid, "leaf-gpu-rdma", a[0], a[1], far or row["b_device"], row["b_port"], "design"))
        self._live_links = (key, links)
        return links

    def live_view(self, with_incidents: bool = True) -> dict:
        with self.lock:
            live = dict(self.live)
            collected_at, source, switches = self.collected_at, self.source, list(self.switches)
        with self.lock:
            fresh = set(self.last_ok) | set(self.run_ok)
            switch_at = dict(self.switch_at)
        counts = {"active": 0, "init": 0, "down": 0, "other": 0, "not_collected": 0, "stale": 0}
        cable_counts = {"active": 0, "init": 0, "down": 0, "unverified": 0, "unknown": 0}
        exceptions = []
        unverified = collections.defaultdict(lambda: {"links": 0, "last_seen": None, "last_states": collections.Counter()})
        links = self.live_links()

        def endpoint(dev: str, port: str) -> str:
            """The state used for status: only switches reached by the latest collection count."""
            if "-swi-" not in dev:
                return "not-collected"            # GPU side: never collected
            if dev not in fresh:
                return "stale"
            return live.get((dev, port), "missing-port")

        for cable_id, ctype, a_dev, a_port, b_dev, b_port, _src in links:
            a, b = endpoint(a_dev, a_port), endpoint(b_dev, b_port)
            for state in (a, b):
                if state == "not-collected":
                    counts["not_collected"] += 1
                elif state in ("stale", "missing-port"):
                    counts["stale"] += 1
                elif state.startswith("Active/LinkUp/"):
                    counts["active"] += 1
                elif state.startswith("Initialize/") or state.startswith("Armed/"):
                    counts["init"] += 1
                elif state.startswith("Down/"):
                    counts["down"] += 1
                else:
                    counts["other"] += 1
            status = link_status([a, b], required=2 if ctype == "leaf-spine" else 1)
            cable_counts[status] += 1
            if status == "unverified":
                for dev, port, state in ((a_dev, a_port, a), (b_dev, b_port, b)):
                    if state == "stale":
                        item = unverified[dev]
                        item["links"] += 1
                        item["last_seen"] = switch_at.get(dev)
                        item["last_states"][(live.get((dev, port)) or "never collected").split("/")[0] or "?"] += 1
            elif status != "active":
                shown = [live.get((a_dev, a_port), "not-collected") if a in ("stale",) else a, live.get((b_dev, b_port), "not-collected") if b in ("stale",) else b]
                exceptions.append([int(cable_id) if cable_id else 0, ctype, status, a_dev, a_port, shown[0], b_dev, b_port, shown[1]])
        exceptions.sort(key=lambda r: ({"down": 0, "init": 1, "unknown": 2}.get(r[2], 3), r[0]))
        unverified_rows = sorted([[dev, v["links"], v["last_seen"], dict(v["last_states"])] for dev, v in unverified.items()], key=lambda r: -r[1])
        view = {
            "mode": "service",
            "source": source,
            "collected_at": collected_at,
            "served_at": now_iso(),
            "switches_collected": len(switches),
            "endpoints": {"compared": sum(counts.values()) - counts["not_collected"] - counts["stale"], **counts},
            "cables": {"total": len(links), **cable_counts},
            "unverified": unverified_rows,
            "freshness": self.freshness(),
            "coverage": self.coverage,
            "reference": "design topology" if links and links[0][6] == "design" else "NetBox",
            "exceptions": exceptions,
            "ufm": [],
            "devices": self.known_devices(),
            "devices_synced_at": self.device_info_at,
            "netbox": self.netbox_sync,
            "cabling": self.cabling_summary(),
            "ufm_fetch": self.ufm_fetch_state(),
            "incidents": None,
            "refresh_running": any(item.get("state") == "running" for item in self.refreshes.values()),
            "sync_running": any(item.get("state") == "running" for item in self.syncs.values()),
        }
        if with_incidents:
            try:
                found = self.incidents(view)
                view["incidents"] = {"counts": found["counts"], "generated_at": found["generated_at"],
                                     "top": [{k: i[k] for k in ("severity", "title", "id")} for i in found["incidents"][:3]]}
            except Exception as error:  # never break /api/live
                view["incidents"] = {"error": str(error)}
        return view

    def live_payload(self) -> tuple[str, bytes, bytes]:
        """(etag, json, gzipped json) for /api/live, rebuilt only when state changed."""
        try:
            self.cabling()  # a new or removed UFM scan bumps the version first
        except Exception:
            pass
        version = self.version
        cached = self._live_cache
        if cached and cached[0] == version:
            return cached[1], cached[2], cached[3]
        body = json.dumps(self.live_view(), separators=(",", ":")).encode("utf-8")
        etag = '"%d-%s"' % (version, hashlib.sha1(body).hexdigest()[:12])
        packed = gzip.compress(body, compresslevel=5)
        self._live_cache = (version, etag, body, packed)
        return etag, body, packed

    def schedule(self, minutes: float) -> None:
        """Background sync so the dashboard opens on fresh evidence."""
        while True:
            time.sleep(minutes * 60)
            try:
                print("[netbox-live-sync] scheduled sync:", self.start_sync())
            except RuntimeError as error:
                print("[netbox-live-sync] scheduled sync skipped:", error)

    # ---------- cabling vs. UFM ----------
    def ufm_scan_path(self) -> Path | None:
        for candidate in (self.ufm_scan, self.ufm_scan.with_suffix("") if self.ufm_scan.suffix == ".gz" else self.ufm_scan.with_name(self.ufm_scan.name + ".gz")):
            if candidate.is_file():
                return candidate
        return None

    def cabling(self) -> dict | None:
        """The UFM-vs-NetBox cabling report, rebuilt only when the scan file changes."""
        path = self.ufm_scan_path()
        if not path:
            if self._cabling is not None:  # the scan was removed: forget it
                with self.lock:
                    self._cabling = None
                    self.bump()
            return None
        rows, reference = self.expected_rows()
        nb_rows, nb_info = self.netbox_reference()
        def pick(p: Path) -> Path | None:
            for c in (p, p.with_suffix("") if p.suffix == ".gz" else p.with_name(p.name + ".gz")):
                if c.is_file():
                    return c
            return None
        master_file, report_file = pick(self.ufm_master), pick(self.ufm_report)
        key = (str(path), path.stat().st_mtime_ns, path.stat().st_size, id(rows), id(nb_rows), self.cabling_reference,
               master_file.stat().st_mtime_ns if master_file else 0, report_file.stat().st_mtime_ns if report_file else 0)
        with self.lock:
            cached = self._cabling
        if cached and cached[0] == key:
            return cached[1]
        _sys.path.insert(0, str(APP_DIR))
        import ufm_cabling  # noqa: E402  (local module next to this file)
        master = ufm_report = None
        try:
            master = ufm_cabling.read_master(master_file) if master_file else None
        except (OSError, ValueError) as error:
            print("[netbox-live-sync] ignoring unreadable UFM master %s: %s" % (master_file, error))
        try:
            ufm_report = ufm_cabling.read_ufm_compare(report_file) if report_file else None
        except (OSError, ValueError) as error:
            print("[netbox-live-sync] ignoring unreadable UFM compare report %s: %s" % (report_file, error))
        if self.cabling_reference == "netbox":
            # expected = NetBox, actual = UFM; the approved design cross-checks each difference
            report = ufm_cabling.analyse(path, nb_rows, None, master, ufm_report, nb_info, rows, reference)
        else:
            report = ufm_cabling.analyse(path, nb_rows, rows, master, ufm_report, reference)
        body = json.dumps(report, separators=(",", ":")).encode("utf-8")
        payload = ('"c%d-%s"' % (key[1] % 10**9, hashlib.sha1(body).hexdigest()[:10]), body, gzip.compress(body, compresslevel=5))
        try:
            import incidents  # noqa: E402
            self._tray_history = incidents.update_tray_history(TRAY_HISTORY, report)
        except Exception as error:  # history is a nice-to-have
            print("[netbox-live-sync] tray history not updated:", error)
        with self.lock:
            self._cabling = (key, report, payload)
            self.bump()
        return report

    def incidents(self, live: dict | None = None) -> dict:
        """Ranked fabric incidents from the cabling report and the live switch view."""
        live = live if live is not None else self.live_view(with_incidents=False)
        try:
            report = self.cabling()
        except Exception as error:
            report = None
            print("[netbox-live-sync] cabling report unavailable for incidents:", error)
        key = (self._cabling[0] if self._cabling else None, live.get("collected_at"), len(live.get("exceptions") or []), json.dumps(live.get("freshness"), sort_keys=True),
               json.dumps((self.ufm_fetch_state().get("last") or {}).get("state")), int(time.time() // 900))
        cached = getattr(self, "_incidents", None)
        if cached and cached[0] == key:
            return cached[1]
        _sys.path.insert(0, str(APP_DIR))
        import incidents  # noqa: E402
        history = getattr(self, "_tray_history", None)
        if history is None:
            try:
                history = json.loads(TRAY_HISTORY.read_text())
            except (OSError, ValueError):
                history = {}
        result = incidents.detect(report, live, history, self.ufm_fetch_state())
        self._incidents = (key, result)
        return result

    def ufm_fetch_configured(self) -> bool:
        if not self.device_profile or not self.device_profile.is_file():
            return False
        import configparser
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(self.device_profile, encoding="utf-8")
        except configparser.Error:
            return False
        return parser.has_section("ufm")

    def ufm_fetch_state(self) -> dict:
        runs = sorted(self.ufm_fetches.values(), key=lambda r: r["started_at"])
        last = {k: v for k, v in runs[-1].items() if k != "thread"} if runs else None
        return {"configured": self.ufm_fetch_configured(), "running": any(r["state"] == "running" for r in runs), "last": last}

    def start_ufm_fetch(self) -> dict:
        """Read UFM directly (button "Fetch from UFM"): live links over REST, or UFM's files."""
        if not self.ufm_fetch_configured():
            raise RuntimeError("UFM access is not set up yet. Run ./scripts/configure_ufm_access.sh, then restart the service.")
        with self.lock:
            if any(r["state"] == "running" for r in self.ufm_fetches.values()):
                raise RuntimeError("A UFM fetch is already running.")
            run_id = secrets.token_hex(6)
            record = {"run_id": run_id, "state": "running", "step": "starting", "detail": "", "started_at": now_iso()}
            self.ufm_fetches[run_id] = record
            self.bump()
        threading.Thread(target=self.run_ufm_fetch, args=(run_id,), daemon=True).start()
        return {"run_id": run_id, "state": "running"}

    def run_ufm_fetch(self, run_id: str) -> None:
        record = self.ufm_fetches[run_id]
        t0 = time.monotonic()
        try:
            _sys.path.insert(0, str(APP_DIR))
            import ufm_fetch  # noqa: E402
            result = ufm_fetch.fetch(self.device_profile, self.ufm_scan.parent, record)
            record.update(step="analysing", detail="comparing with the design topology")
            report = self.cabling()
            summary = report["summary"] if report else {}
            record.update(state="complete", step="done", host=result["host"], files=result["files"], skipped=result["skipped"],
                          source=result.get("source", "files"), links=result.get("links"),
                          scanned_at=report["source"]["scanned_at"] if report else None,
                          miscabled=summary.get("switch_miscabled"), swaps=summary.get("swaps"),
                          trays=summary.get("trays"))
        except Exception as error:  # surface every failure on the dashboard
            record.update(state="failed", step="failed", error=str(error))
        record["seconds"] = round(time.monotonic() - t0, 1)
        record["finished_at"] = now_iso()
        with self.lock:
            self.bump()
        print("[netbox-live-sync] UFM fetch %s %s in %.1fs %s" % (run_id, record["state"], record["seconds"], record.get("error", "")))

    def ufm_fetch_status(self, run_id: str) -> dict:
        item = self.ufm_fetches.get(run_id)
        if not item:
            raise RuntimeError("Unknown UFM fetch run.")
        return dict(item)

    def ufm_schedule(self, minutes: float) -> None:
        while True:
            time.sleep(minutes * 60)
            try:
                print("[netbox-live-sync] scheduled UFM fetch:", self.start_ufm_fetch())
            except RuntimeError as error:
                print("[netbox-live-sync] scheduled UFM fetch skipped:", error)

    def cabling_payload(self) -> tuple[str, bytes, bytes] | None:
        if self.cabling() is None:
            return None
        return self._cabling[2]

    def cabling_summary(self) -> dict | None:
        try:
            report = self.cabling()
        except Exception as error:  # a corrupt scan must not break /api/live
            return {"error": str(error)}
        if not report:
            return None
        return {"scanned_at": report["source"]["scanned_at"], "summary": report["summary"]}

    def health(self) -> dict:
        view = self.live_view()
        result = {"baseline_cables": len(self.baseline), "live_source": view["source"], "collected_at": view["collected_at"]}
        try:
            result["status"] = self.get_netbox("/api/status/")
            result["netbox"] = "reachable"
        except RuntimeError as error:
            result["netbox"] = "unreachable"
            result["netbox_error"] = str(error)
        return result

    def verify(self, cable_id: str) -> dict:
        if not cable_id.isdigit():
            raise RuntimeError("Cable ID must be numeric.")
        cable = self.get_netbox("/api/dcim/cables/%s/" % cable_id)
        problem = termination_problem(cable)
        if problem:
            raise RuntimeError("Cable %s cannot be verified: %s in NetBox (exactly one interface per side is required)." % (cable_id, problem))
        endpoints = [endpoint_from_termination(cable["a_terminations"][0]), endpoint_from_termination(cable["b_terminations"][0])]
        baseline = self.baseline.get(cable_id)
        with self.lock:
            live = [{**ep, "live_state": self.live.get((ep["device"], ep["port"]), "not-collected")} for ep in endpoints]
        netbox_matches_snapshot = None
        if baseline:
            snapshot = sorted([
                (baseline["endpoint_a_device"], baseline["endpoint_a_port"]),
                (baseline["endpoint_b_device"], baseline["endpoint_b_port"]),
            ])
            netbox_matches_snapshot = snapshot == sorted((e["device"], e["port"]) for e in endpoints)
        status = link_status([item["live_state"] for item in live])
        if netbox_matches_snapshot is False:
            verdict = "netbox-endpoint-changed"
        else:
            verdict = {"down": "live-link-down", "init": "live-link-initializing", "active": "verified-active"}.get(status, "requires-remote-endpoint-check")
        return {
            "cable_id": cable_id,
            "netbox": {"label": cable.get("label", ""), "description": cable.get("description", ""), "status": (cable.get("status") or {}).get("value"), "endpoints": endpoints},
            "live": live,
            "baseline_connection_type": baseline.get("connection_type") if baseline else None,
            "netbox_matches_2026_10_01_snapshot": netbox_matches_snapshot,
            "live_source": self.source,
            "collected_at": self.collected_at,
            "verdict": verdict,
        }

    def device(self, hostname: str, live: bool = False) -> dict:
        """Device facts for the inspector: from the latest sync, or NetBox live if not known yet."""
        cached = self.device_info.get(hostname)
        if cached and not live:
            return {**cached, "source": "last sync", "synced_at": self.device_info_at}
        try:
            payload = self.get_list("/api/dcim/devices/?limit=2&name=" + quote(hostname, safe=""), DEVICE_FIELDS, ("name", "device_type"))
        except RuntimeError:
            if cached:  # NetBox unreachable: the last sync is still the best answer
                return {**cached, "source": "last sync", "synced_at": self.device_info_at}
            address = self.address_book().get(hostname)
            if address:
                return {"hostname": hostname, "primary_ip": address, "vendor": "", "model": "not synced yet", "status": "unknown", "source": "address cache"}
            raise
        devices = payload.get("results") or []
        if len(devices) != 1:
            raise RuntimeError("NetBox returned %d devices for %s." % (len(devices), hostname))
        details = device_details(devices[0])
        self.remember_devices({hostname: details})
        return {**details, "source": "netbox live", "synced_at": now_iso()}

    # ---------- NetBox cable sync ----------
    def baseline_ranges(self) -> list[tuple[int, int]]:
        ids = sorted(int(k) for k in self.baseline)
        ranges, lo, prev = [], ids[0], ids[0]
        for cid in ids[1:]:
            if cid - prev > 50:
                ranges.append((lo, prev))
                lo = cid
            prev = cid
        ranges.append((lo, prev))
        return ranges

    def fetch_cables(self, progress: dict | None = None) -> dict[str, dict]:
        """Pull every backend cable (IDs in the baseline CSV ranges) from NetBox, paginated."""
        cables: dict[str, dict] = {}
        pages: list[str] = []
        ranges = self.baseline_ranges()
        for lo, hi in ranges:
            base = "/api/dcim/cables/?id__gte=%d&id__lte=%d&limit=%d" % (lo, hi, self.page_size)
            first = self.get_list(base + "&offset=0", CABLE_FIELDS, ("a_terminations", "b_terminations"))
            for cable in first.get("results", []):
                cables[str(cable["id"])] = cable
            suffix = ("&fields=" + CABLE_FIELDS) if self.fields_ok else ""
            pages.extend(base + "&offset=%d%s" % (offset, suffix) for offset in range(self.page_size, first.get("count", 0), self.page_size))
        if progress is not None:
            progress.update(state="running", mode="full", pages_completed=len(ranges), pages_total=len(ranges) + len(pages))
        # Teleport's local app forward has limited upstream capacity; keep the
        # number of in-flight expanded cable pages small (configurable).
        with ThreadPoolExecutor(max_workers=self.nb_concurrency) as pool:
            futures = [pool.submit(self.get_netbox, path) for path in pages]
            for future in as_completed(futures):
                page = future.result()
                for cable in page.get("results", []):
                    cables[str(cable["id"])] = cable
                if progress is not None:
                    progress["pages_completed"] += 1
        return cables

    def changed_cable_ids(self, since: str) -> set[str] | None:
        """Cable IDs touched in the NetBox change log since `since` (None = cannot tell; do a full sync).

        Covers edits, deletions and re-terminations (dcim.cabletermination rows carry
        the cable ID). Any doubt about the filter being honoured falls back to full.
        """
        cutoff = parse_iso(since)
        if not cutoff:
            return None
        for base in ("/api/core/object-changes/", "/api/extras/object-changes/"):
            try:
                ids: set[str] = set()
                for object_type in ("dcim.cable", "dcim.cabletermination"):
                    path: str | None = "%s?changed_object_type=%s&time_after=%s&limit=1000" % (base, object_type, quote(since, safe=""))
                    pages = 0
                    while path:
                        page = self.get_netbox(path)
                        pages += 1
                        if page.get("count", 0) > 20000 or pages > 25:
                            return None
                        for change in page.get("results") or []:
                            stamp = parse_iso((change.get("time") or "").replace("Z", "+00:00"))
                            if stamp and stamp < cutoff:
                                return None  # time filter not honoured by this NetBox
                            if object_type == "dcim.cable":
                                ids.add(str(change.get("changed_object_id")))
                            else:
                                for data in (change.get("postchange_data") or {}, change.get("prechange_data") or {}):
                                    if data.get("cable"):
                                        ids.add(str(data["cable"]))
                        path = next_path(page)
                return ids
            except RuntimeError as error:
                if "HTTP 404" in str(error):
                    continue
                print("[netbox-live-sync] change log unavailable, using a full cable sync: %s" % error)
                return None
        return None

    def fetch_cable_ids(self, ids: list[str]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for start in range(0, len(ids), 50):
            chunk = ids[start:start + 50]
            base = "/api/dcim/cables/?limit=%d&%s" % (len(chunk), "&".join("id=%s" % quote(i) for i in chunk))
            for cable in self.get_list(base, CABLE_FIELDS, ("a_terminations", "b_terminations")).get("results", []):
                found[str(cable["id"])] = cable
        return found

    def sync_netbox(self, progress: dict | None = None) -> dict:
        started = now_iso()
        t0 = time.monotonic()
        requests_before = getattr(self, "netbox_requests", 0)
        mode, changed = "full", None
        if self.nb_cables and self.nb_synced_at and age_hours(self.nb_full_at) < self.full_every_hours:
            since = (parse_iso(self.nb_synced_at) - timedelta(seconds=120)).isoformat(timespec="seconds")
            ids = self.changed_cable_ids(since)
            if ids is not None:
                mode = "incremental"
                relevant = sorted(i for i in ids if i in self.baseline)
                if progress is not None:
                    progress.update(state="running", mode=mode, changed=len(relevant))
                found = self.fetch_cable_ids(relevant)
                for cid in relevant:
                    if cid in found:
                        self.nb_cables[cid] = cable_endpoints(found[cid])
                    else:
                        self.nb_cables.pop(cid, None)
                changed = len(relevant)
        if mode == "full":
            cables = self.fetch_cables(progress)
            self.nb_cables = {cid: cable_endpoints(c) for cid, c in cables.items()}
            self.nb_full_at = started
        mismatches, missing = [], []
        for cable_id, row in self.baseline.items():
            if cable_id not in self.nb_cables:
                missing.append(int(cable_id))
                continue
            ends = self.nb_cables[cable_id]
            if not ends:
                mismatches.append([int(cable_id), "", "", "", "", "missing termination"])
                continue
            if len(ends) > 4:
                mismatches.append([int(cable_id), "", "", "", "", ends[4]])
                continue
            expected = sorted([(row["endpoint_a_device"], row["endpoint_a_port"]), (row["endpoint_b_device"], row["endpoint_b_port"])])
            if sorted([(ends[0], ends[1]), (ends[2], ends[3])]) != expected:
                mismatches.append([int(cable_id), ends[0], ends[1], ends[2], ends[3], "endpoint changed"])
        result = {"synced_at": now_iso(), "checked": len([c for c in self.baseline if c in self.nb_cables]), "missing": missing,
                  "mismatches": mismatches, "extra_in_range": len([c for c in self.nb_cables if c not in self.baseline]),
                  "mode": mode, "changed": changed, "full_at": self.nb_full_at,
                  "seconds": round(time.monotonic() - t0, 2), "requests": getattr(self, "netbox_requests", 0) - requests_before,
                  "fields_trimmed": bool(self.fields_ok)}
        with self.lock:
            self.netbox_sync = result
            self.nb_synced_at = started
            self.bump()
        self.save_cable_cache()
        return result

    def netbox_due(self) -> bool:
        """Whether a fabric sync (a button press) should also refresh NetBox.

        Always when NetBox was never read (there is no inventory to compare with yet);
        otherwise only with --netbox-every-hours > 0 and the inventory older than that.
        With the default 0, NetBox otherwise refreshes only with Refresh NetBox."""
        if not self.nb_synced_at:
            return True
        if self.netbox_every_hours <= 0:
            return False
        return age_hours(self.nb_synced_at) >= self.netbox_every_hours

    def start_sync(self, kind: str = "fabric") -> dict:
        """kind="fabric": switch states + UFM (+ NetBox when due). kind="netbox": NetBox only."""
        with self.lock:
            if any(item.get("state") == "running" for item in self.syncs.values()):
                raise RuntimeError("A sync is already running.")
            run_id = secrets.token_hex(6)
            if kind == "netbox":
                phases = {"netbox"}
            else:
                phases = {"devices"} | ({"ufm"} if self.ufm_fetch_configured() else set()) | ({"netbox"} if self.netbox_due() else set())
            skipped = lambda name, why: {"state": "skipped", "reason": why}
            self.syncs[run_id] = {
                "state": "running", "kind": kind, "started_at": now_iso(), "timings": {},
                "devices": {"state": "pending"} if "devices" in phases else skipped("devices", "NetBox-only refresh"),
                "ufm": {"state": "pending"} if "ufm" in phases else skipped("ufm", "UFM access not set up (scripts/configure_ufm_access.sh)" if kind != "netbox" else "NetBox-only refresh"),
                "netbox": {"state": "running"} if "netbox" in phases else {"state": "skipped", "reason": ("inventory refreshes every %g h" % self.netbox_every_hours) if self.netbox_every_hours > 0 else "use Refresh NetBox to update the inventory",
                                                                            "synced_at": self.nb_synced_at},
            }
            self.bump()
        threading.Thread(target=self.run_sync, args=(run_id, phases), daemon=True).start()
        return {"run_id": run_id, "state": "running", "phases": sorted(phases)}

    def management_addresses(self, output: Path) -> int:
        """IPs from NetBox (or its fresh cache); if NetBox is unreachable, the last known IPs."""
        try:
            return self.fetch_management_addresses(output)
        except Exception as error:
            if not ADDRESS_CACHE.is_file():
                raise
            with ADDRESS_CACHE.open(newline="", encoding="utf-8") as handle:
                cached = {r["hostname"]: r["management_address"] for r in csv.DictReader(handle) if r.get("management_address")}
            with self.devices.open(newline="", encoding="utf-8-sig") as handle:
                names = [r["hostname"].strip() for r in csv.DictReader(handle) if (r.get("hostname") or "").strip()]
            if not all(n in cached for n in names):
                raise
            self.write_addresses(output, names, cached)
            when = datetime.fromtimestamp(ADDRESS_CACHE.stat().st_mtime, timezone.utc).isoformat(timespec="minutes")
            self.address_source = "cached IPs from %s (NetBox unavailable: %s)" % (when, str(error).split(" after ")[0][:80])
            return len(names)

    def run_sync(self, run_id: str, phases: set | None = None) -> None:
        """Phases run concurrently: switch collection, UFM fetch, NetBox inventory."""
        phases = phases if phases is not None else {"devices", "netbox"}
        record = self.syncs[run_id]
        timings = record.setdefault("timings", {})
        t0 = time.monotonic()

        def netbox_phase() -> None:
            start = time.monotonic()
            try:
                nb = self.sync_netbox(record["netbox"])
                record["netbox"] = {"state": "complete", "checked": nb["checked"], "mismatches": len(nb["mismatches"]),
                                    "missing": len(nb["missing"]), "mode": nb["mode"], "changed": nb["changed"],
                                    "requests": nb["requests"], "fields_trimmed": nb["fields_trimmed"]}
            except Exception as error:
                record["netbox"] = {"state": "failed", "error": str(error), "synced_at": self.nb_synced_at}
            timings["netbox_s"] = round(time.monotonic() - start, 1)

        def device_phase() -> None:
            start = time.monotonic()
            record["devices"] = {"state": "running"}
            try:
                STATE_DIR.mkdir(exist_ok=True)
                address_file = STATE_DIR / (run_id + "-management-addresses.csv")
                count = self.management_addresses(address_file)
                timings["addresses_s"] = round(time.monotonic() - start, 1)
                record["devices"] = {"state": "running", "management_addresses": count, "address_source": self.address_source}
            except Exception as error:
                record["devices"] = {"state": "failed", "error": "Management-IP lookup failed: " + str(error)}
                return
            try:
                refresh = self.start_refresh(address_file)
                while self.refreshes[refresh["run_id"]]["state"] == "running":
                    record["devices"] = {**self.refresh_status(refresh["run_id"]), "address_source": self.address_source}
                    time.sleep(1)
                record["devices"] = {**self.refresh_status(refresh["run_id"]), "address_source": self.address_source}
            except Exception as error:
                record["devices"] = {"state": "failed", "error": str(error)}
            finally:
                address_file.unlink(missing_ok=True)
                timings["devices_s"] = round(time.monotonic() - start, 1)

        def ufm_phase() -> None:
            start = time.monotonic()
            try:
                running = [k for k, r in self.ufm_fetches.items() if r["state"] == "running"]
                fetch_id = running[0] if running else self.start_ufm_fetch()["run_id"]
                while self.ufm_fetches[fetch_id]["state"] == "running":
                    record["ufm"] = {k: v for k, v in self.ufm_fetches[fetch_id].items() if k != "thread"}
                    time.sleep(0.5)
                record["ufm"] = {k: v for k, v in self.ufm_fetches[fetch_id].items() if k != "thread"}
            except Exception as error:
                record["ufm"] = {"state": "failed", "error": str(error)}
            timings["ufm_s"] = round(time.monotonic() - start, 1)

        work = {"netbox": netbox_phase, "devices": device_phase, "ufm": ufm_phase}
        threads = [threading.Thread(target=work[name], daemon=True) for name in sorted(phases)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # NetBox is inventory: its failure does not fail a fabric sync (the last copy stays in use).
        core = [name for name in ("devices", "ufm") if name in phases] or ["netbox"]
        states = [record[name]["state"] for name in core]
        record["state"] = "complete" if all(x == "complete" for x in states) else "partial" if any(x in ("complete", "partial") for x in states) else "failed"
        timings["total_s"] = round(time.monotonic() - t0, 1)
        record["finished_at"] = now_iso()
        with self.lock:
            self.bump()
        print("[netbox-live-sync] %s sync %s %s in %.1fs %s" % (record.get("kind", "fabric"), run_id, record["state"], timings["total_s"], timings))

    def netbox_schedule(self) -> None:
        """Keep NetBox inventory fresh in the background (first check one minute after start)."""
        time.sleep(60)
        while True:
            if self.netbox_due() and not any(r.get("state") == "running" for r in self.syncs.values()):
                try:
                    print("[netbox-live-sync] background NetBox refresh:", self.start_sync("netbox"))
                except RuntimeError as error:
                    print("[netbox-live-sync] background NetBox refresh skipped:", error)
            time.sleep(1800)

    def sync_status(self, run_id: str) -> dict:
        item = self.syncs.get(run_id)
        if not item:
            raise RuntimeError("Unknown sync run.")
        return item

    # ---------- collection ----------
    def start_refresh(self, prepared_addresses: Path | None = None) -> dict:
        required = {"bundled collector": COLLECTOR, "device profile": self.device_profile, "device inventory": self.devices, "approved host-key file": self.known_hosts, "read-only command file": self.commands}
        missing = [label for label, path in required.items() if not path or not path.is_file()]
        if prepared_addresses and not prepared_addresses.is_file():
            missing.append("prepared management-address map")
        if missing:
            raise RuntimeError("Local device-access inputs are missing: %s. See the README setup section." % ", ".join(missing))
        with self.lock:
            if any(item.get("state") == "running" for item in self.refreshes.values()):
                raise RuntimeError("A live device collection is already running.")
            run_id = secrets.token_hex(6)
            output_dir = STATE_DIR / run_id
            STATE_DIR.mkdir(exist_ok=True)
            output_dir.mkdir()
            generated_addresses = prepared_addresses or (output_dir / "management_addresses.csv")
            address_count = None if prepared_addresses else self.fetch_management_addresses(generated_addresses)
            command = [
                sys.executable, str(COLLECTOR), "--profile", str(self.device_profile),
                "--devices", str(self.devices), "--addresses", str(generated_addresses),
                "--known-hosts", str(self.known_hosts), "--commands-file", str(self.commands),
                "--report-format", "none", "--parallel", str(self.device_parallel), "--fanout", self.fanout,
                "--output-dir", str(output_dir),
            ]
            log_file = STATE_DIR / (run_id + ".log")
            log_handle = log_file.open("w", encoding="utf-8")
            process = subprocess.Popen(command, stdout=log_handle, stderr=subprocess.STDOUT, text=True)
            self.run_ok = set()
            self.refreshes[run_id] = {"state": "running", "process": process, "command": command, "output_dir": output_dir, "log": str(log_file),
                                      "management_addresses_from_netbox": address_count, "started_at": now_iso(), "fanout": self.fanout,
                                      "done": 0, "failed": 0, "total": self.device_count()}
            self.bump()
        threading.Thread(target=self.finish_refresh, args=(run_id, log_handle), daemon=True).start()
        return {"run_id": run_id, "state": "running"}

    def device_count(self) -> int:
        try:
            with self.devices.open(newline="", encoding="utf-8-sig") as handle:  # type: ignore[union-attr]
                return sum(1 for row in csv.DictReader(handle) if (row.get("hostname") or "").strip())
        except (OSError, AttributeError):
            return 0

    def ingest(self, record: dict, parsed: dict[tuple[str, str], str], seen: set[str], final: bool = False) -> None:
        """Apply every newly finished switch to the live view (progressive results)."""
        raw_dir, err_dir = record["output_dir"] / "raw", record["output_dir"] / "errors"
        fresh: dict[tuple[str, str], str] = {}
        for raw_file in sorted(raw_dir.glob("*.txt")) if raw_dir.is_dir() else []:
            host = raw_file.stem
            if host in seen:
                continue
            ports = self.parse_raw_file(raw_file)
            if ports is None and not final:
                continue  # empty (failed) or unparsable yet; checked again at the end
            seen.add(host)
            if ports is None and raw_file.stat().st_size:
                record.setdefault("unparsable", []).append(host)
            if ports:
                fresh.update(ports)
                record.setdefault("switches_ok", []).append(host)
                record.setdefault("ports_by_switch", {})[host] = ports
        failed = sum(1 for f in err_dir.glob("*.txt") if f.stat().st_size) if err_dir.is_dir() else 0
        failed += len(record.get("unparsable", []))
        record.update(done=len(list(raw_dir.glob("*.txt"))) if raw_dir.is_dir() else 0, failed=failed)
        if fresh:
            parsed.update(fresh)
            stamp = now_iso()
            with self.lock:
                for host in {dev for dev, _ in fresh}:
                    # replace the switch's ports entirely: a port it no longer reports is not kept
                    for key in [k for k in self.live if k[0] == host and k not in fresh]:
                        del self.live[key]
                    self.switch_at[host] = stamp
                    self.run_ok.add(host)
                self.live.update(fresh)
                self.source = "device collection in progress (%d/%d switches)" % (record["done"], record.get("total") or record["done"])
                self.bump()

    def finish_refresh(self, run_id: str, log_handle) -> None:
        record = self.refreshes[run_id]
        parsed: dict[tuple[str, str], str] = {}
        seen: set[str] = set()
        t0 = time.monotonic()
        try:
            while True:
                while record["process"].poll() is None:
                    self.ingest(record, parsed, seen)
                    time.sleep(1.0)
                code = record["process"].returncode
                if code != 0 and record.get("fanout") == "jump" and not parsed:
                    # The jump-host worker could not start (exit 3, e.g. no python3 there) or
                    # not a single switch succeeded through it: retry this run, and use for
                    # later runs, one Teleport session per device (the proven path).
                    reason = "worker unavailable" if code == 3 else "no switch succeeded (exit %s)" % code
                    errors = sorted((record["output_dir"] / "errors").glob("*.txt")) if (record["output_dir"] / "errors").is_dir() else []
                    sample = errors[0].read_text(encoding="utf-8", errors="replace").strip()[-200:] if errors else ""
                    record["fanout_fallback"] = "%s%s" % (reason, (": " + sample) if sample else "")
                    print("[netbox-live-sync] jump fan-out %s; falling back to --fanout local" % record["fanout_fallback"])
                    self.fanout = record["fanout"] = "local"
                    command = [("local" if part == "jump" else part) for part in record["command"]]
                    shutil.rmtree(record["output_dir"], ignore_errors=True)  # the collector creates it afresh
                    record["process"] = subprocess.Popen(command, stdout=log_handle, stderr=subprocess.STDOUT, text=True)
                    continue
                break
            self.ingest(record, parsed, seen, final=True)
            log_handle.close()
            if not parsed:
                raise RuntimeError("collector exited with %s and produced no parsable interface output" % code)
            stamp = now_iso()
            switches = sorted(record.get("switches_ok", []))
            coverage = self.collection_coverage(record, switches, stamp)
            record.pop("ports_by_switch", None)
            state = "complete" if coverage["state"] == "verified" else "partial"
            with self.lock:
                self.collected_at = stamp
                self.switches = switches
                self.last_ok = set(switches)
                self.coverage = coverage
                self.evidence_note = ""
                self.source = "device collection %s (%d of %d switches)" % (stamp, len(switches), coverage["expected"])
                record.update(state=state, updated_interfaces=len(parsed), switches=len(switches), finished_at=stamp,
                              seconds=round(time.monotonic() - t0, 1), coverage={k: coverage[k] for k in ("state", "expected", "reached", "missing", "missing_ports")})
                snapshot = {"%s|%s" % key: value for key, value in self.live.items() if "-swi-" in key[0]}
                self.bump()
            if coverage["missing"]:
                ADDRESS_CACHE.unlink(missing_ok=True)  # an IP may have moved; re-resolve next time
            self.write_json_atomic(LATEST, {"version": 2, "fingerprint": self.evidence_fingerprint(), "collected_at": stamp,
                                            "switches": switches, "last_ok": switches, "switch_at": self.switch_at,
                                            "coverage": coverage, "ports": snapshot})
        except (RuntimeError, OSError) as error:
            if not log_handle.closed:
                log_handle.close()
            record.update(state="failed", error=str(error), exit_code=record["process"].returncode)
            with self.lock:
                self.bump()

    def expected_switches(self) -> list[str]:
        try:
            with self.devices.open(newline="", encoding="utf-8-sig") as handle:  # type: ignore[union-attr]
                return [row["hostname"].strip() for row in csv.DictReader(handle) if (row.get("hostname") or "").strip()]
        except (OSError, AttributeError, KeyError):
            return []

    def collection_coverage(self, record: dict, switches: list[str], stamp: str) -> dict:
        """How complete a collection is: switches reached vs. the inventory, and designed
        ports each reached switch did not report."""
        expected = self.expected_switches()
        reached = set(switches)
        err_dir = record["output_dir"] / "errors"
        failed = sorted(f.stem for f in err_dir.glob("*.txt") if f.stat().st_size) if err_dir.is_dir() else []
        unparsable = sorted(record.get("unparsable", []))
        missing = sorted(set(expected) - reached)
        designed = collections.defaultdict(set)
        for _cid, _t, a_dev, a_port, b_dev, b_port, _src in self.live_links():
            for dev, port in ((a_dev, a_port), (b_dev, b_port)):
                if "-swi-" in dev:
                    designed[dev].add(port)
        by_switch = record.get("ports_by_switch", {})
        missing_ports, missing_port_count = {}, 0
        for host in switches:
            seen = {port for _dev, port in by_switch.get(host, {})}
            gone = sorted(designed.get(host, set()) - seen)
            if gone:
                missing_port_count += len(gone)  # counted before the display list is shortened
                missing_ports[host] = gone[:20] + (["… %d more" % (len(gone) - 20)] if len(gone) > 20 else [])
        state = "verified" if not missing and not missing_ports and expected else "partial"
        return {"state": state, "collected_at": stamp, "expected": len(expected), "reached": len(reached & set(expected)) if expected else len(reached),
                "missing": missing, "failed": failed, "unparsable": unparsable, "missing_ports": missing_ports,
                "missing_port_count": missing_port_count,
                "missing_port_counts": {h: len(designed.get(h, set()) - {port for _d, port in by_switch.get(h, {})}) for h in missing_ports}}

    @staticmethod
    def write_json_atomic(path: Path, payload: dict) -> None:
        """Write to a temporary file in the same folder, then rename: never a half-written file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def freshness(self) -> dict:
        """verified / partial / stale / snapshot, for the status bar and the API."""
        cov = self.coverage
        age = age_hours(self.collected_at) * 60 if self.collected_at else None
        if age is not None and age == float("inf"):
            age = None
        if not cov or not self.last_ok:
            return {"state": "snapshot" if not cov else "stale", "note": self.evidence_note or "no verified collection yet; press Sync fabric",
                    "collected_at": self.collected_at, "age_minutes": round(age) if age is not None else None}
        if age is not None and age > self.stale_after_minutes:
            return {"state": "stale", "note": "last collection is %d min old (stale after %g min)" % (age, self.stale_after_minutes),
                    "collected_at": self.collected_at, "age_minutes": round(age), "reached": cov["reached"], "expected": cov["expected"]}
        return {"state": cov["state"], "collected_at": self.collected_at, "age_minutes": round(age) if age is not None else None,
                "reached": cov["reached"], "expected": cov["expected"], "missing": cov["missing"],
                "missing_port_count": cov.get("missing_port_count", 0),
                "note": "" if cov["state"] == "verified" else "%d of %d switches reached%s" % (
                    cov["reached"], cov["expected"], ", %d designed ports not reported" % cov["missing_port_count"] if cov.get("missing_port_count") else "")}

    def parse_raw_file(self, raw_file: Path) -> dict[tuple[str, str], str] | None:
        text = raw_file.read_text(encoding="utf-8", errors="replace")
        start = text.find("__ICE2_COMMAND_001_START__")
        end = text.find("__ICE2_COMMAND_001_END__")
        if start < 0 or end < 0:
            return None
        try:
            interfaces = json.loads(text[start + len("__ICE2_COMMAND_001_START__"):end].strip())
        except json.JSONDecodeError:
            return None
        hostname = raw_file.stem
        data: dict[tuple[str, str], str] = {}
        for port, info in interfaces.items():
            if not isinstance(info, dict) or (info.get("type") != "ib" and not port.startswith("fnm")):
                continue
            link = info.get("link") or {}
            data[(hostname, port)] = "%s/%s/%s" % (link.get("logical-state", ""), link.get("physical-state", ""), link.get("speed", ""))
        return data

    def parse_live_directory(self, output_dir: Path) -> tuple[dict[tuple[str, str], str], list[str]]:
        data: dict[tuple[str, str], str] = {}
        switches: list[str] = []
        for raw_file in sorted((output_dir / "raw").glob("*.txt")):
            ports = self.parse_raw_file(raw_file)
            if ports is not None:
                switches.append(raw_file.stem)
                data.update(ports)
        return data, switches

    def refresh_status(self, run_id: str) -> dict:
        item = self.refreshes.get(run_id)
        if not item:
            raise RuntimeError("Unknown refresh run.")
        return {key: value for key, value in item.items() if key not in {"process", "output_dir", "log", "command", "switches_ok", "ports_by_switch"}}

class Handler(BaseHTTPRequestHandler):
    state: SyncState
    diagram: Path

    def log_message(self, format: str, *args) -> None:
        if "/api/live" not in (args[0] if args else ""):
            print("[netbox-live-sync] " + format % args)

    def respond(self, code: int, payload: dict | str, content_type: str = "application/json; charset=utf-8") -> None:
        body = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def respond_cached(self, etag: str, body: bytes, packed: bytes) -> None:
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        use_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "")
        data = packed if use_gzip else body
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in {"/", "/index.html"}:
                self.respond(HTTPStatus.OK, self.diagram.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            elif path == "/api/cabling":
                payload = self.state.cabling_payload()
                if payload is None:  # not an error: nothing fetched yet
                    self.respond(HTTPStatus.OK, {"available": False, "error": "No UFM data yet. Press Fetch from UFM (or run scripts/fetch_ufm_scan.sh)."})
                    return
                self.respond_cached(*payload)
            elif path == "/api/incidents":
                self.respond(HTTPStatus.OK, self.state.incidents())
            elif path in ("/api/cabling/findings.csv", "/api/cabling/netbox-import.csv"):
                report = self.state.cabling()
                if report is None:
                    self.respond(HTTPStatus.NOT_FOUND, {"error": "No UFM fabric scan found."})
                    return
                _sys.path.insert(0, str(APP_DIR))
                import ufm_cabling  # noqa: E402
                findings = path.endswith("findings.csv")
                body = (ufm_cabling.findings_csv if findings else ufm_cabling.netbox_import_csv)(report)
                stamp = report["source"]["scanned_at"][:16].replace(":", "").replace("-", "")
                name = "ice2-cabling-%s-%s.csv" % ("findings" if findings else "netbox-import", stamp)
                data = body.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/live":
                etag, body, packed = self.state.live_payload()
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(HTTPStatus.NOT_MODIFIED)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    return
                use_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "")
                data = packed if use_gzip else body
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                if use_gzip:
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/health":
                self.respond(HTTPStatus.OK, self.state.health())
            elif path.startswith("/api/verify/"):
                self.respond(HTTPStatus.OK, self.state.verify(unquote(path.rsplit("/", 1)[-1])))
            elif path.startswith("/api/device/"):
                live = "live=1" in (urlparse(self.path).query or "")
                self.respond(HTTPStatus.OK, self.state.device(unquote(path.rsplit("/", 1)[-1]), live))
            elif path.startswith("/api/refresh/"):
                self.respond(HTTPStatus.OK, self.state.refresh_status(unquote(path.rsplit("/", 1)[-1])))
            elif path.startswith("/api/cabling/fetch/"):
                self.respond(HTTPStatus.OK, self.state.ufm_fetch_status(unquote(path.rsplit("/", 1)[-1])))
            elif path.startswith("/api/sync/"):
                self.respond(HTTPStatus.OK, self.state.sync_status(unquote(path.rsplit("/", 1)[-1])))
            else:
                self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except RuntimeError as error:
            self.respond(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
        except (OSError, ValueError) as error:
            where = "UFM scan" if path.startswith("/api/cabling") else "dashboard file" if path in {"/", "/index.html"} else "request"
            self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Could not complete the %s: %s" % (where, error)})
        except Exception as error:  # never an empty reply: report any other failure as JSON
            self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "%s: %s" % (type(error).__name__, error)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        actions = {"/api/refresh": self.state.start_refresh, "/api/sync": self.state.start_sync, "/api/cabling/fetch": self.state.start_ufm_fetch,
                   "/api/netbox/refresh": lambda: self.state.start_sync("netbox")}
        if path not in actions:
            self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            self.respond(HTTPStatus.ACCEPTED, actions[path]())
        except RuntimeError as error:
            self.respond(HTTPStatus.CONFLICT, {"error": str(error)})
        except Exception as error:  # never an empty reply
            self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "%s: %s" % (type(error).__name__, error)})


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the ICE2 backend diagram with NetBox/live reconciliation.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--netbox-url", required=True, help="Approved NetBox URL or local proxy URL.")
    parser.add_argument("--netbox-host-header", help="Host header required by an approved local proxy.")
    parser.add_argument("--diagram", type=Path, default=ASSETS_DIR / "dashboard.html", help="Dashboard HTML (bundled by default).")
    parser.add_argument("--connections", type=Path, default=ASSETS_DIR / "connections.csv", help="Topology CSV (bundled by default).")
    parser.add_argument("--device-profile", type=Path, help="Private per-user device profile created by scripts/configure_device_access.sh.")
    parser.add_argument("--devices", type=Path, default=ASSETS_DIR / "devices.csv", help="Device inventory CSV (bundled by default).")
    parser.add_argument("--known-hosts", type=Path, default=PROJECT_ROOT / "local-inputs" / "known_hosts", help="Local approved SSH host-key file installed by scripts/configure_known_hosts.sh.")
    parser.add_argument("--commands", type=Path, default=ASSETS_DIR / "read_only_commands.txt", help="Read-only command file (bundled by default).")
    parser.add_argument("--expected-topology", type=Path, default=DEFAULT_EXPECTED,
                        help="Designed topology the cabling check compares against (scripts/build_expected_topology.py writes it).")
    parser.add_argument("--cabling-reference", choices=["netbox", "design"], default="netbox",
                        help="Expected cabling for the miscabling check: NetBox cable records (default; the approved design "
                             "cross-checks each difference) or the approved design topology (NetBox shown alongside).")
    parser.add_argument("--ufm-master", type=Path, default=DEFAULT_UFM_MASTER,
                        help="Local copy of UFM's master (reference) topology, periodicTopo/master.topo; fetch_ufm_scan.sh copies it.")
    parser.add_argument("--ufm-report", type=Path, default=DEFAULT_UFM_REPORT,
                        help="Local copy of UFM's latest Topology Compare report (JSON); fetch_ufm_scan.sh copies it.")
    parser.add_argument("--ufm-scan", type=Path, default=DEFAULT_UFM_SCAN,
                        help="Local copy of UFM's fabric scan (ibdiagnet2.lst[.gz]) for the cabling check; scripts/fetch_ufm_scan.sh puts it here.")
    tuning = parser.add_argument_group("performance")
    tuning.add_argument("--fanout", choices=["local", "jump"], default="local",
                        help="jump: one Teleport session fans out from the jump host (needs python3 there; falls back to local automatically).")
    tuning.add_argument("--device-parallel", type=int, default=10, help="Concurrent switch sessions (1-25). Raise gradually.")
    tuning.add_argument("--netbox-page-size", type=int, default=250, help="Cable page size (NetBox max_page_size permitting).")
    tuning.add_argument("--netbox-concurrency", type=int, default=2, help="In-flight NetBox cable pages through the Teleport forward.")
    tuning.add_argument("--address-cache-hours", type=float, default=24.0, help="Reuse NetBox management IPs for this long (0 = always re-query).")
    tuning.add_argument("--full-netbox-every-hours", type=float, default=6.0, help="Between full cable syncs, sync only cables in the NetBox change log.")
    tuning.add_argument("--ufm-fetch-every-minutes", type=float, default=0,
                        help="Fetch UFM's fabric files in the background on this interval (0 = only with the Fetch from UFM button).")
    tuning.add_argument("--sync-every-minutes", type=float, default=0, help="Run the fabric sync (switches + UFM) in the background on this interval (0 = on demand only).")
    tuning.add_argument("--design-topo", type=Path, default=None,
                        help="Approved design topology (.topo), the reference for the cabling check; default local-inputs/ufm/nscale_Compute.topo. "
                             "Without it, the inferred rules in --expected-topology are used.")
    tuning.add_argument("--stale-after-minutes", type=float, default=60.0,
                        help="Show switch evidence as STALE when the last collection is older than this.")
    tuning.add_argument("--netbox-every-hours", type=float, default=0.0,
                        help="Also refresh NetBox inventory (IPs, models, cable records) this often, in the background and on Sync fabric. "
                             "Default 0: only with Refresh NetBox (and on the first Sync fabric, when there is no inventory yet).")
    args = parser.parse_args()
    if not 1 <= args.device_parallel <= 25:
        raise SystemExit("--device-parallel must be between 1 and 25")
    if not args.diagram.is_file() or not args.connections.is_file():
        raise SystemExit("Missing file: %s" % (args.diagram if not args.diagram.is_file() else args.connections))
    Handler.state = SyncState(args.netbox_url, args.netbox_host_header, args.connections, args.device_profile, args.devices, args.known_hosts, args.commands, args)
    if args.ufm_fetch_every_minutes > 0:
        threading.Thread(target=Handler.state.ufm_schedule, args=(args.ufm_fetch_every_minutes,), daemon=True).start()
    if args.sync_every_minutes > 0:
        threading.Thread(target=Handler.state.schedule, args=(args.sync_every_minutes,), daemon=True).start()
    if args.netbox_every_hours > 0:
        threading.Thread(target=Handler.state.netbox_schedule, daemon=True).start()
    Handler.diagram = args.diagram
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Live evidence: %s" % Handler.state.source)
    scan = Handler.state.ufm_scan_path()
    print("UFM cabling scan: %s" % (scan if scan else "none yet (run scripts/fetch_ufm_scan.sh)"))
    print("Open http://127.0.0.1:%d/" % args.port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
