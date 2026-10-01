#!/usr/bin/env python3
"""Local, read-only ICE2 backend fabric reconciliation service.

The browser never receives the NetBox token.  It talks only to this loopback
service, which reads the token from macOS Keychain and queries NetBox through
the already authenticated local Teleport proxy.  Live switch collection is
also read-only (`nv show interface --output json`).

Endpoints
---------
GET  /                    the live backend GPU diagram
GET  /api/live            full live link state for every backend cable
GET  /api/health          NetBox reachability + live-evidence summary (never fails hard)
GET  /api/verify/<id>     one cable: current NetBox record vs. live switch state
POST /api/refresh         start a read-only collection across the 100 backend switches
GET  /api/refresh/<run>   collection progress
POST /api/sync            one-click sync: NetBox cables and device collection, run concurrently
GET  /api/sync/<run>      sync progress with per-phase timings
GET  /api/cabling         NetBox cabling vs. what UFM actually sees (from a local UFM fabric scan)
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
import csv
import getpass
import gzip
import hashlib
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
DEFAULT_UFM_SCAN = PROJECT_ROOT / "local-inputs" / "ufm" / "ibdiagnet2.lst.gz"
DEFAULT_EXPECTED = ASSETS_DIR / "expected_topology.csv"
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


def cable_endpoints(cable: dict) -> list | None:
    terms_a, terms_b = cable.get("a_terminations") or [], cable.get("b_terminations") or []
    if not terms_a or not terms_b:
        return None
    ea, eb = endpoint_from_termination(terms_a[0]), endpoint_from_termination(terms_b[0])
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


def link_status(states: list[str]) -> str:
    """Collapse the collected endpoint states of one cable into a single status."""
    collected = [s for s in states if s and s != "not-collected"]
    if not collected:
        return "unknown"
    if any(s.startswith("Down/") for s in collected):
        return "down"
    if any(s.startswith("Initialize/") or s.startswith("Armed/") for s in collected):
        return "init"
    if all(s.startswith("Active/LinkUp/") for s in collected):
        return "active"
    return "unknown"


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
        self.fields_ok: bool | None = None          # NetBox `fields=` support, learned on first use
        self.nb_cables: dict[str, list | None] = {}  # cable id -> [a_dev, a_port, b_dev, b_port]
        self.nb_full_at: str | None = None
        self.nb_synced_at: str | None = None        # start time of the last successful cable sync
        self.version = 0
        self._live_cache: tuple | None = None
        self.load_baseline()
        self.load_latest()
        self.load_cable_cache()
        self.ufm_scan = Path(getattr(opts, "ufm_scan", None) or DEFAULT_UFM_SCAN)
        self.expected_topology = Path(getattr(opts, "expected_topology", None) or DEFAULT_EXPECTED)
        self._cabling: tuple | None = None
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

    def load_latest(self) -> None:
        """Re-apply the most recent successful collection so a restart keeps the newest evidence."""
        if not LATEST.is_file():
            return
        try:
            saved = json.loads(LATEST.read_text(encoding="utf-8"))
            for key, state in saved.get("ports", {}).items():
                device, port = key.split("|", 1)
                self.live[(device, port)] = state
            self.collected_at = saved.get("collected_at", self.collected_at)
            self.switches = saved.get("switches", [])
            self.source = "device collection %s (%d switches)" % (self.collected_at, len(self.switches))
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
        headers = {"Authorization": "Token " + self.netbox_token(), "Accept": "application/json"}
        if self.netbox_host_header:
            headers["Host"] = self.netbox_host_header
        request = Request(self.netbox_url + path, headers=headers, method="GET")
        self.netbox_requests = getattr(self, "netbox_requests", 0) + 1
        for attempt in range(3):
            try:
                with urlopen(request, timeout=20) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
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
                print("[netbox-live-sync] bulk address query %s/%s failed, falling back per device: %s" % (site, role, error))
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
    def live_view(self) -> dict:
        with self.lock:
            live = dict(self.live)
            collected_at, source, switches = self.collected_at, self.source, list(self.switches)
        counts = {"active": 0, "init": 0, "down": 0, "other": 0, "not_collected": 0}
        cable_counts = {"active": 0, "init": 0, "down": 0, "unknown": 0}
        exceptions = []
        for cable_id, row in self.baseline.items():
            a = live.get((row["endpoint_a_device"], row["endpoint_a_port"]), "not-collected")
            b = live.get((row["endpoint_b_device"], row["endpoint_b_port"]), "not-collected")
            for state in (a, b):
                if state == "not-collected":
                    counts["not_collected"] += 1
                elif state.startswith("Active/LinkUp/"):
                    counts["active"] += 1
                elif state.startswith("Initialize/") or state.startswith("Armed/"):
                    counts["init"] += 1
                elif state.startswith("Down/"):
                    counts["down"] += 1
                else:
                    counts["other"] += 1
            status = link_status([a, b])
            cable_counts[status] += 1
            if status != "active":
                exceptions.append([
                    int(cable_id), row["connection_type"], status,
                    row["endpoint_a_device"], row["endpoint_a_port"], a,
                    row["endpoint_b_device"], row["endpoint_b_port"], b,
                ])
        exceptions.sort(key=lambda r: ({"down": 0, "init": 1, "unknown": 2}.get(r[2], 3), r[0]))
        return {
            "mode": "service",
            "source": source,
            "collected_at": collected_at,
            "served_at": now_iso(),
            "switches_collected": len(switches) or 100,
            "endpoints": {"compared": sum(counts.values()) - counts["not_collected"], **counts},
            "cables": {"total": len(self.baseline), **cable_counts},
            "exceptions": exceptions,
            "ufm": [],
            "devices": self.known_devices(),
            "devices_synced_at": self.device_info_at,
            "netbox": self.netbox_sync,
            "cabling": self.cabling_summary(),
            "refresh_running": any(item.get("state") == "running" for item in self.refreshes.values()),
            "sync_running": any(item.get("state") == "running" for item in self.syncs.values()),
        }

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
        design = self.expected_topology if self.expected_topology.is_file() else None
        key = (str(path), path.stat().st_mtime_ns, path.stat().st_size, design.stat().st_mtime_ns if design else 0)
        with self.lock:
            cached = self._cabling
        if cached and cached[0] == key:
            return cached[1]
        _sys.path.insert(0, str(APP_DIR))
        import ufm_cabling  # noqa: E402  (local module next to this file)
        report = ufm_cabling.analyse(path, ufm_cabling.load_baseline(self.connections),
                                     ufm_cabling.load_expected(design) if design else None)
        body = json.dumps(report, separators=(",", ":")).encode("utf-8")
        payload = ('"c%d-%s"' % (key[1] % 10**9, hashlib.sha1(body).hexdigest()[:10]), body, gzip.compress(body, compresslevel=5))
        with self.lock:
            self._cabling = (key, report, payload)
            self.bump()
        return report

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
        terms_a = cable.get("a_terminations") or []
        terms_b = cable.get("b_terminations") or []
        if not terms_a or not terms_b:
            raise RuntimeError("Cable %s does not have two interface terminations in NetBox." % cable_id)
        endpoints = [endpoint_from_termination(terms_a[0]), endpoint_from_termination(terms_b[0])]
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

    def start_sync(self) -> dict:
        """One-click sync: NetBox cable records first, then the read-only device collection."""
        with self.lock:
            if any(item.get("state") == "running" for item in self.syncs.values()):
                raise RuntimeError("A sync is already running.")
            run_id = secrets.token_hex(6)
            self.syncs[run_id] = {"state": "running", "started_at": now_iso(),
                                  "netbox": {"state": "running"}, "devices": {"state": "pending"}, "timings": {}}
            self.bump()
        threading.Thread(target=self.run_sync, args=(run_id,), daemon=True).start()
        return {"run_id": run_id, "state": "running"}

    def run_sync(self, run_id: str) -> None:
        """NetBox cable sync and the device phase (address lookup -> collection) run concurrently."""
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
                record["netbox"] = {"state": "failed", "error": str(error)}
            timings["netbox_s"] = round(time.monotonic() - start, 1)

        def device_phase() -> None:
            start = time.monotonic()
            try:
                STATE_DIR.mkdir(exist_ok=True)
                address_file = STATE_DIR / (run_id + "-management-addresses.csv")
                count = self.fetch_management_addresses(address_file)
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

        phases = [threading.Thread(target=netbox_phase, daemon=True), threading.Thread(target=device_phase, daemon=True)]
        for phase in phases:
            phase.start()
        for phase in phases:
            phase.join()
        ok = [record["netbox"]["state"], record["devices"]["state"]]
        record["state"] = "complete" if all(s == "complete" for s in ok) else "partial" if any(s in ("complete", "partial") for s in ok) else "failed"
        timings["total_s"] = round(time.monotonic() - t0, 1)
        record["finished_at"] = now_iso()
        with self.lock:
            self.bump()
        print("[netbox-live-sync] sync %s %s in %.1fs %s" % (run_id, record["state"], timings["total_s"], timings))

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
        failed = sum(1 for f in err_dir.glob("*.txt") if f.stat().st_size) if err_dir.is_dir() else 0
        failed += len(record.get("unparsable", []))
        record.update(done=len(list(raw_dir.glob("*.txt"))) if raw_dir.is_dir() else 0, failed=failed)
        if fresh:
            parsed.update(fresh)
            with self.lock:
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
            state = "complete" if not record.get("failed") else "partial"
            with self.lock:
                self.collected_at = stamp
                self.switches = switches
                self.source = "device collection %s (%d switches)" % (stamp, len(switches))
                record.update(state=state, updated_interfaces=len(parsed), switches=len(switches), finished_at=stamp,
                              seconds=round(time.monotonic() - t0, 1))
                self.bump()
            if record.get("failed"):
                ADDRESS_CACHE.unlink(missing_ok=True)  # an IP may have moved; re-resolve next time
            previous = {}
            if LATEST.is_file():
                try:
                    previous = json.loads(LATEST.read_text(encoding="utf-8")).get("ports", {})
                except (OSError, ValueError):
                    previous = {}
            previous.update({"%s|%s" % key: value for key, value in parsed.items()})
            LATEST.write_text(json.dumps({"collected_at": stamp, "switches": switches, "ports": previous}), encoding="utf-8")
        except (RuntimeError, OSError) as error:
            if not log_handle.closed:
                log_handle.close()
            record.update(state="failed", error=str(error), exit_code=record["process"].returncode)
            with self.lock:
                self.bump()

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
        return {key: value for key, value in item.items() if key not in {"process", "output_dir", "log", "command", "switches_ok"}}

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
                if payload is None:
                    self.respond(HTTPStatus.NOT_FOUND, {"error": "No UFM fabric scan found at %s. Run scripts/fetch_ufm_scan.sh." % self.state.ufm_scan})
                    return
                self.respond_cached(*payload)
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
            elif path.startswith("/api/sync/"):
                self.respond(HTTPStatus.OK, self.state.sync_status(unquote(path.rsplit("/", 1)[-1])))
            else:
                self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except RuntimeError as error:
            self.respond(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
        except (OSError, ValueError) as error:
            where = "UFM scan" if path.startswith("/api/cabling") else "request"
            self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Could not complete the %s: %s" % (where, error)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        actions = {"/api/refresh": self.state.start_refresh, "/api/sync": self.state.start_sync}
        if path not in actions:
            self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            self.respond(HTTPStatus.ACCEPTED, actions[path]())
        except RuntimeError as error:
            self.respond(HTTPStatus.CONFLICT, {"error": str(error)})


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
    tuning.add_argument("--sync-every-minutes", type=float, default=0, help="Run the full sync in the background on this interval (0 = on demand only).")
    args = parser.parse_args()
    if not 1 <= args.device_parallel <= 25:
        raise SystemExit("--device-parallel must be between 1 and 25")
    if not args.diagram.is_file() or not args.connections.is_file():
        raise SystemExit("Diagram or backend connection CSV is missing.")
    Handler.state = SyncState(args.netbox_url, args.netbox_host_header, args.connections, args.device_profile, args.devices, args.known_hosts, args.commands, args)
    if args.sync_every_minutes > 0:
        threading.Thread(target=Handler.state.schedule, args=(args.sync_every_minutes,), daemon=True).start()
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
