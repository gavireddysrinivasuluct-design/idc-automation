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
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
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
BASELINE_COLLECTED_AT = "not-collected"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def endpoint_from_termination(termination: dict) -> dict:
    obj = termination.get("object") or {}
    device = obj.get("device") or {}
    return {"device": device.get("name", "unknown"), "port": obj.get("name", "unknown")}


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
    def __init__(self, netbox_url: str, netbox_host_header: str | None, connections: Path, device_profile: Path | None, devices: Path | None, known_hosts: Path | None, commands: Path | None) -> None:
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
        self.lock = threading.Lock()
        self.collected_at = BASELINE_COLLECTED_AT
        self.source = "local topology snapshot"
        self.switches: list[str] = []
        self.syncs: dict[str, dict] = {}
        self.netbox_sync: dict | None = None
        self.load_baseline()
        self.load_latest()

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
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError("NetBox returned HTTP %s: %s" % (error.code, body[:240])) from error
        except URLError as error:
            raise RuntimeError("Cannot reach the local NetBox proxy at %s: %s" % (self.netbox_url, error.reason)) from error

    def fetch_management_addresses(self, output: Path) -> int:
        """Build an ephemeral device-IP map from NetBox; never persist it in Git."""
        if not self.devices:
            raise RuntimeError("A local device inventory is required to fetch management addresses.")
        with self.devices.open(newline="", encoding="utf-8-sig") as handle:
            names = [(row.get("hostname") or "").strip() for row in csv.DictReader(handle)]
        names = [name for name in names if name]
        if not names or len(names) != len(set(names)):
            raise RuntimeError("The device inventory must contain unique non-empty hostname values.")
        addresses: list[tuple[str, str]] = []
        for name in names:
            payload = self.get_netbox("/api/dcim/devices/?limit=2&name=" + quote(name, safe=""))
            matches = payload.get("results") or []
            if len(matches) != 1:
                raise RuntimeError("NetBox returned %d devices for %s." % (len(matches), name))
            primary = matches[0].get("primary_ip4") or matches[0].get("primary_ip") or {}
            address = (primary.get("address") or "").split("/", 1)[0]
            if not address:
                raise RuntimeError("NetBox has no primary management IP for %s." % name)
            addresses.append((name, address))
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["hostname", "management_address"])
            writer.writerows(addresses)
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
            "netbox": self.netbox_sync,
            "refresh_running": any(item.get("state") == "running" for item in self.refreshes.values()),
            "sync_running": any(item.get("state") == "running" for item in self.syncs.values()),
        }

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

    def device(self, hostname: str) -> dict:
        payload = self.get_netbox("/api/dcim/devices/?limit=2&name=" + quote(hostname, safe=""))
        devices = payload.get("results") or []
        if len(devices) != 1:
            raise RuntimeError("NetBox returned %d devices for %s." % (len(devices), hostname))
        device = devices[0]
        device_type = device.get("device_type") or {}
        manufacturer = device_type.get("manufacturer") or {}
        primary = device.get("primary_ip4") or device.get("primary_ip") or {}
        return {
            "hostname": device.get("name"),
            "primary_ip": primary.get("address") or "not assigned",
            "vendor": manufacturer.get("name") or manufacturer.get("display") or "not recorded",
            "model": device_type.get("model") or device_type.get("display") or "not recorded",
            "status": (device.get("status") or {}).get("value") or (device.get("status") or {}).get("label") or "unknown",
        }

    # ---------- NetBox bulk sync ----------
    def fetch_cables(self) -> dict[str, dict]:
        """Pull every backend cable (IDs in the baseline CSV ranges) from NetBox, paginated."""
        ids = sorted(int(k) for k in self.baseline)
        ranges, lo, prev = [], ids[0], ids[0]
        for cid in ids[1:]:
            if cid - prev > 50:
                ranges.append((lo, prev))
                lo = cid
            prev = cid
        ranges.append((lo, prev))
        cables: dict[str, dict] = {}
        for lo, hi in ranges:
            path = "/api/dcim/cables/?id__gte=%d&id__lte=%d&limit=1000&offset=0" % (lo, hi)
            while path:
                page = self.get_netbox(path)
                for cable in page.get("results", []):
                    cables[str(cable["id"])] = cable
                nxt = page.get("next")
                path = nxt[nxt.find("/api/"):] if nxt else None
        return cables

    def sync_netbox(self) -> dict:
        cables = self.fetch_cables()
        mismatches, missing = [], []
        for cable_id, row in self.baseline.items():
            cable = cables.get(cable_id)
            if not cable:
                missing.append(int(cable_id))
                continue
            terms_a, terms_b = cable.get("a_terminations") or [], cable.get("b_terminations") or []
            if not terms_a or not terms_b:
                mismatches.append([int(cable_id), "", "", "", "", "missing termination"])
                continue
            ea, eb = endpoint_from_termination(terms_a[0]), endpoint_from_termination(terms_b[0])
            expected = sorted([(row["endpoint_a_device"], row["endpoint_a_port"]), (row["endpoint_b_device"], row["endpoint_b_port"])])
            if sorted([(ea["device"], ea["port"]), (eb["device"], eb["port"])]) != expected:
                mismatches.append([int(cable_id), ea["device"], ea["port"], eb["device"], eb["port"], "endpoint changed"])
        result = {"synced_at": now_iso(), "checked": len(cables), "missing": missing, "mismatches": mismatches,
                  "extra_in_range": len([c for c in cables if c not in self.baseline])}
        with self.lock:
            self.netbox_sync = result
        return result

    def start_sync(self) -> dict:
        """One-click sync: NetBox cable records first, then the read-only device collection."""
        with self.lock:
            if any(item.get("state") == "running" for item in self.syncs.values()):
                raise RuntimeError("A sync is already running.")
            run_id = secrets.token_hex(6)
            self.syncs[run_id] = {"state": "running", "started_at": now_iso(),
                                  "netbox": {"state": "running"}, "devices": {"state": "pending"}}
        threading.Thread(target=self.run_sync, args=(run_id,), daemon=True).start()
        return {"run_id": run_id, "state": "running"}

    def run_sync(self, run_id: str) -> None:
        record = self.syncs[run_id]
        try:
            nb = self.sync_netbox()
            record["netbox"] = {"state": "complete", "checked": nb["checked"], "mismatches": len(nb["mismatches"]), "missing": len(nb["missing"])}
        except RuntimeError as error:
            record["netbox"] = {"state": "failed", "error": str(error)}
        try:
            record["devices"] = {"state": "running"}
            refresh = self.start_refresh()
            while self.refreshes[refresh["run_id"]]["state"] == "running":
                time.sleep(2)
            record["devices"] = self.refresh_status(refresh["run_id"])
        except RuntimeError as error:
            record["devices"] = {"state": "failed", "error": str(error)}
        ok = [record["netbox"]["state"], record["devices"]["state"]]
        record["state"] = "complete" if all(s == "complete" for s in ok) else "partial" if "complete" in ok else "failed"
        record["finished_at"] = now_iso()

    def sync_status(self, run_id: str) -> dict:
        item = self.syncs.get(run_id)
        if not item:
            raise RuntimeError("Unknown sync run.")
        return item

    # ---------- collection ----------
    def start_refresh(self) -> dict:
        required = {"bundled collector": COLLECTOR, "device profile": self.device_profile, "device inventory": self.devices, "approved host-key file": self.known_hosts, "read-only command file": self.commands}
        missing = [label for label, path in required.items() if not path or not path.is_file()]
        if missing:
            raise RuntimeError("Local device-access inputs are missing: %s. See the README setup section." % ", ".join(missing))
        with self.lock:
            if any(item.get("state") == "running" for item in self.refreshes.values()):
                raise RuntimeError("A live device collection is already running.")
            run_id = secrets.token_hex(6)
            output_dir = STATE_DIR / run_id
            STATE_DIR.mkdir(exist_ok=True)
            generated_addresses = output_dir / "management_addresses.csv"
            output_dir.mkdir()
            address_count = self.fetch_management_addresses(generated_addresses)
            command = [
                sys.executable, str(COLLECTOR), "--profile", str(self.device_profile),
                "--devices", str(self.devices), "--addresses", str(generated_addresses),
                "--known-hosts", str(self.known_hosts), "--commands-file", str(self.commands),
                "--report-format", "none", "--parallel", "10", "--output-dir", str(output_dir),
            ]
            log_file = STATE_DIR / (run_id + ".log")
            log_handle = log_file.open("w", encoding="utf-8")
            process = subprocess.Popen(command, stdout=log_handle, stderr=subprocess.STDOUT, text=True)
            self.refreshes[run_id] = {"state": "running", "process": process, "output_dir": output_dir, "log": str(log_file), "management_addresses_from_netbox": address_count, "started_at": now_iso()}
        threading.Thread(target=self.finish_refresh, args=(run_id, log_handle), daemon=True).start()
        return {"run_id": run_id, "state": "running"}

    def finish_refresh(self, run_id: str, log_handle) -> None:
        record = self.refreshes[run_id]
        record["process"].wait()
        log_handle.close()
        try:
            if record["process"].returncode != 0:
                raise RuntimeError("collector exited with %s" % record["process"].returncode)
            parsed, switches = self.parse_live_directory(record["output_dir"])
            if not parsed:
                raise RuntimeError("collector produced no parsable interface output")
            stamp = now_iso()
            with self.lock:
                self.live.update(parsed)
                self.collected_at = stamp
                self.switches = switches
                self.source = "device collection %s (%d switches)" % (stamp, len(switches))
                record.update(state="complete", updated_interfaces=len(parsed), switches=len(switches), finished_at=stamp)
            LATEST.write_text(json.dumps({
                "collected_at": stamp, "switches": switches,
                "ports": {"%s|%s" % key: value for key, value in parsed.items()},
            }), encoding="utf-8")
        except (RuntimeError, OSError) as error:
            record.update(state="failed", error=str(error), exit_code=record["process"].returncode)

    def parse_live_directory(self, output_dir: Path) -> tuple[dict[tuple[str, str], str], list[str]]:
        data: dict[tuple[str, str], str] = {}
        switches: list[str] = []
        for raw_file in sorted((output_dir / "raw").glob("*.txt")):
            text = raw_file.read_text(encoding="utf-8", errors="replace")
            start = text.find("__ICE2_COMMAND_001_START__")
            end = text.find("__ICE2_COMMAND_001_END__")
            if start < 0 or end < 0:
                continue
            try:
                interfaces = json.loads(text[start + len("__ICE2_COMMAND_001_START__"):end].strip())
            except json.JSONDecodeError:
                continue
            hostname = raw_file.stem
            switches.append(hostname)
            for port, info in interfaces.items():
                if not isinstance(info, dict) or (info.get("type") != "ib" and not port.startswith("fnm")):
                    continue
                link = info.get("link") or {}
                data[(hostname, port)] = "%s/%s/%s" % (link.get("logical-state", ""), link.get("physical-state", ""), link.get("speed", ""))
        return data, switches

    def refresh_status(self, run_id: str) -> dict:
        item = self.refreshes.get(run_id)
        if not item:
            raise RuntimeError("Unknown refresh run.")
        return {key: value for key, value in item.items() if key not in {"process", "output_dir", "log"}}

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

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in {"/", "/index.html"}:
                self.respond(HTTPStatus.OK, self.diagram.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            elif path == "/api/live":
                self.respond(HTTPStatus.OK, self.state.live_view())
            elif path == "/api/health":
                self.respond(HTTPStatus.OK, self.state.health())
            elif path.startswith("/api/verify/"):
                self.respond(HTTPStatus.OK, self.state.verify(unquote(path.rsplit("/", 1)[-1])))
            elif path.startswith("/api/device/"):
                self.respond(HTTPStatus.OK, self.state.device(unquote(path.rsplit("/", 1)[-1])))
            elif path.startswith("/api/refresh/"):
                self.respond(HTTPStatus.OK, self.state.refresh_status(unquote(path.rsplit("/", 1)[-1])))
            elif path.startswith("/api/sync/"):
                self.respond(HTTPStatus.OK, self.state.sync_status(unquote(path.rsplit("/", 1)[-1])))
            else:
                self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except RuntimeError as error:
            self.respond(HTTPStatus.BAD_GATEWAY, {"error": str(error)})

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
    args = parser.parse_args()
    if not args.diagram.is_file() or not args.connections.is_file():
        raise SystemExit("Diagram or backend connection CSV is missing.")
    Handler.state = SyncState(args.netbox_url, args.netbox_host_header, args.connections, args.device_profile, args.devices, args.known_hosts, args.commands)
    Handler.diagram = args.diagram
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Live evidence: %s" % Handler.state.source)
    print("Open http://127.0.0.1:%d/" % args.port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
