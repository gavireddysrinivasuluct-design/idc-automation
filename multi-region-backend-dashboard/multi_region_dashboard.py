#!/usr/bin/env python3
"""Backend fabric dashboard: one read-only service for every backend region.

    python3 multi_region_dashboard.py            # http://127.0.0.1:8765/

* Pick a region; each region page has the ICE2-style fabric dashboard and the live issues.
* ICE2 and LON14: their full engines (<site>-netbox-dashboard) run as child processes and
  are served under /ice2/ and /lon14/, unchanged.
* Other InfiniBand regions (OSL1, STA1, GLO1): topology from NetBox first (direct, through
  the local NetBox proxy and the read-only Keychain token), actual links from UFM directly
  (REST through the site's jump host), drawn in the same design at /f/<site>.
* nsr is used only for metrics, logs and incidents: UFM telemetry, gNMI, SNMP, ping,
  UFM servers (Grafana), the UFM event log and syslog (Loki), PagerDuty.

Nothing is written to NetBox, UFM or any device.

HTTP API (local only):
    GET /                       region picker          GET /r/<site>   region page
    GET /<engine-site>/...      the ICE2 / LON14 engine (proxied)
    GET /f/<site>               NetBox-first fabric view
    GET /api/fabric/<site>      its data;  POST|GET /api/fabric/<site>/netbox | /ufm  snapshot jobs
    GET /api/config | /api/overview | /api/region/<site>/<fabric|switches|reach|events|incidents>
    GET /api/topology/<site>    engine status
    POST /api/connect/<site>/<switch>   open Terminal on the switch (device_connect.py)
    GET /api/ufm/<site>; POST /api/ufm/<site>/<cli|web>/<ufm>   UFM CLI / web UI (ufm_access.py)
    add ?refresh=1 to bypass the cache
"""

from __future__ import annotations

import argparse
import os
import json
import sys
import threading
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import checks  # noqa: E402
import device_connect  # noqa: E402
import engines  # noqa: E402
import theme  # noqa: E402
import ufm_access  # noqa: E402
from fabric import FabricStore  # noqa: E402
from nsr_client import Cache, Nsr  # noqa: E402

PAGE = ROOT / "assets" / "regions.html"
FABRIC_PAGE = ROOT / "assets" / "fabric.html"
CACHE_DIR = ROOT / "cache"
SECTIONS = ("fabric", "switches", "reach", "events", "incidents")


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    sites = [region["site"] for region in config["regions"]]
    if len(sites) != len(set(sites)):
        raise SystemExit("regions.json: duplicate site keys")
    for region in config["regions"]:
        for key in ("site", "name", "cloud_regions", "sources"):
            if key not in region:
                raise SystemExit("regions.json: region %s is missing %r" % (region.get("site"), key))
    return config


class App:
    def __init__(self, config: dict, nsr: Nsr, ttl: int = 60, netbox_url: str = "http://127.0.0.1:8444",
                 profile: Path | None = None, with_engines: bool = True) -> None:
        self.config, self.nsr = config, nsr
        self.by_site = {region["site"]: region for region in config["regions"]}
        self.cache = Cache(ttl)
        self.incident_cache = Cache(ttl)
        self.started = time.time()
        self.fabrics = FabricStore(nsr, config, CACHE_DIR, ttl, netbox_url, profile)
        self.engines = {r["site"]: engines.Engine(r["site"], r["engine"], ROOT.parent, netbox_url, CACHE_DIR)
                        for r in config["regions"] if r.get("engine")} if with_engines else {}

    def start_engines(self) -> None:
        for engine in self.engines.values():
            threading.Thread(target=engine.start, daemon=True).start()

    def incidents(self, force=False) -> dict:
        return self.incident_cache.get("pd", lambda: checks.fetch_incidents(self.nsr, self.config), force)

    def overview(self, force=False) -> dict:
        def compute():
            data = checks.run_overview(self.nsr, self.config, self.incidents(force))
            data["nsr_calls"] = self.nsr.calls
            data["grafana_direct_calls"] = self.nsr.direct_calls
            return data
        return self.cache.get("overview", compute, force)

    def section(self, site: str, section: str, force=False) -> dict:
        region = self.by_site[site]
        if section == "incidents":
            return {"section": "incidents", **checks.incidents_for(self.incidents(force), region)}
        if section == "events":
            return self.cache.get((site, section), lambda: checks.run_events(self.nsr, region, self.config), force)
        return self.cache.get((site, section),
                              lambda: checks.run_section(self.nsr, region, self.config, section), force)

    def public_config(self) -> dict:
        regions = []
        for region in self.config["regions"]:
            regions.append({**{k: region.get(k) for k in ("site", "name", "location", "fabric", "clusters",
                                                         "cloud_regions", "switches_expected",
                                                         "grafana_dashboards", "notes")},
                            "sources": sorted(region["sources"]), "fabric_view": bool(region.get("fabric_view")),
                            "engine": region["site"] in self.engines})
        return {"grafana_url": self.config.get("grafana_url"), "regions": regions,
                "nsr": self.nsr.binary, "started": self.started}

    def topology(self, site: str) -> dict:
        engine = self.engines.get(site)
        return engine.status() if engine else {"site": site, "configured": False}


class Handler(BaseHTTPRequestHandler):
    app: App
    port: int = 8765
    server_version = "backend-regions/1"
    light = True  # serve pages in the white theme (theme.py); --dark serves them as written

    def log_message(self, fmt, *args):  # quiet: the page polls
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("[regions] " + fmt % args + "\n")

    def respond(self, code, body, content_type="application/json; charset=utf-8"):
        if self.light and isinstance(body, str) and content_type.startswith(("text/html", "text/javascript")):
            body = theme.apply(body)
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):  # noqa: N802
        if not self.host_ok():
            return self.respond(HTTPStatus.FORBIDDEN, {"error": "Open http://127.0.0.1:%d/" % self.port})
        url = urlparse(self.path)
        path, force = url.path.rstrip("/") or "/", "refresh" in parse_qs(url.query)
        parts = [unquote(p) for p in path.strip("/").split("/")]
        try:
            if path in ("/", "/index.html") or (parts[0] == "r" and len(parts) == 2):
                return self.respond(HTTPStatus.OK, PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            if path == "/api/config":
                return self.respond(HTTPStatus.OK, self.app.public_config())
            if path == "/api/overview":
                return self.respond(HTTPStatus.OK, self.app.overview(force))
            if parts[:2] == ["api", "region"] and len(parts) == 4:
                site, section = parts[2].lower(), parts[3]
                if site not in self.app.by_site or section not in SECTIONS:
                    return self.respond(HTTPStatus.NOT_FOUND, {"error": "unknown region or section"})
                return self.respond(HTTPStatus.OK, self.app.section(site, section, force))
            if parts[:2] == ["api", "ufm"] and len(parts) == 3 and parts[2] in self.app.by_site:
                return self.respond(HTTPStatus.OK, {"site": parts[2], "ufms": ufm_access.ufms(self.app, parts[2])})
            if parts[:2] == ["api", "topology"] and len(parts) == 3 and parts[2] in self.app.by_site:
                return self.respond(HTTPStatus.OK, self.app.topology(parts[2]))
            if path == "/assets/nsr_panel.js":
                return self.respond(HTTPStatus.OK, (ROOT / "assets" / "nsr_panel.js").read_text(encoding="utf-8"),
                                    "text/javascript; charset=utf-8")
            if parts[0] in self.app.engines:
                if len(parts) == 1 and not url.path.endswith("/"):
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", "/%s/" % parts[0])
                    self.end_headers()
                    return None
                rest = "/" + url.path.split("/", 2)[2] if url.path.count("/") >= 2 else "/"
                return engines.forward(self.app.engines[parts[0]], self, "GET", rest + ("?" + url.query if url.query else ""))
            if parts[0] == "f" and len(parts) == 2 and parts[1] in self.app.by_site:
                return self.respond(HTTPStatus.OK, FABRIC_PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            if parts[:2] == ["api", "fabric"] and len(parts) >= 3 and parts[2] in self.app.by_site:
                if not self.app.by_site[parts[2]].get("fabric_view"):
                    return self.respond(HTTPStatus.NOT_FOUND, {"error": "no fabric view configured for this region"})
                if len(parts) == 4 and parts[3] in ("netbox", "ufm"):
                    return self.respond(HTTPStatus.OK, self.app.fabrics.jobs.get(parts[2] + ":" + parts[3]) or {"state": "idle"})
                return self.respond(HTTPStatus.OK, self.app.fabrics.view(parts[2], force))
            if path == "/api/health":
                return self.respond(HTTPStatus.OK, {"ok": True, "nsr_calls": self.app.nsr.calls,
                                                     "grafana_direct_calls": self.app.nsr.direct_calls,
                                                     "grafana_fallback": self.app.nsr.direct_error or None})
            return self.respond(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception as error:  # noqa: BLE001 - never leave the browser hanging
            return self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})


    def do_POST(self):  # noqa: N802 - read-only jobs (NetBox / UFM snapshot) or an engine's own POSTs
        if not self.host_ok():
            return self.respond(HTTPStatus.FORBIDDEN, {"error": "wrong host"})
        origin = self.headers.get("Origin")
        if origin and origin.split("//")[-1].split(":")[0] not in ("127.0.0.1", "localhost"):
            return self.respond(HTTPStatus.FORBIDDEN, {"error": "cross-site request refused"})
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.strip("/").split("/")]
        # One-click switch login, from a fabric page (/api/connect/<site>/<host>) or an engine
        # page, whose fetch shim sends it as /<site>/api/connect/<host>.
        target = (parts[2], parts[3]) if parts[:2] == ["api", "connect"] and len(parts) == 4 else \
            (parts[0], parts[3]) if parts[1:3] == ["api", "connect"] and len(parts) == 4 else None
        # UFM CLI / web UI, from a fabric page (/api/ufm/<site>/<cli|web>/<ufm>) or an engine page
        # (/<site>/api/ufm/<cli|web>/<ufm> through its fetch shim).
        ufm = (parts[2], parts[3], parts[4]) if parts[:2] == ["api", "ufm"] and len(parts) == 5 else \
            (parts[0], parts[3], parts[4]) if parts[1:3] == ["api", "ufm"] and len(parts) == 5 else None
        if ufm and ufm[1] in ("cli", "web"):
            if not origin or self.headers.get("X-Requested-With") != "connect":
                return self.respond(HTTPStatus.FORBIDDEN, {"error": "Open the UFM from the dashboard page."})
            try:
                site = ufm[0].lower()
                if site not in self.app.by_site:
                    return self.respond(HTTPStatus.NOT_FOUND, {"error": "Unknown region."})
                opener = ufm_access.open_cli if ufm[1] == "cli" else ufm_access.open_web
                return self.respond(HTTPStatus.OK, opener(self.app, site, ufm[2]))
            except device_connect.ConnectError as error:
                return self.respond(error.status, {"error": str(error)})
            except Exception as error:  # noqa: BLE001 - the page shows it next to the button
                return self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
        if target:
            if not origin or self.headers.get("X-Requested-With") != "connect":
                return self.respond(HTTPStatus.FORBIDDEN, {"error": "Open the switch from the dashboard page."})
            try:
                return self.respond(HTTPStatus.OK, device_connect.connect(self.app, target[0].lower(), target[1]))
            except device_connect.ConnectError as error:
                return self.respond(error.status, {"error": str(error)})
            except Exception as error:  # noqa: BLE001 - the page shows it next to the IP
                return self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
        if parts and parts[0] in self.app.engines and len(parts) > 1:
            rest = "/" + url.path.split("/", 2)[2]
            return engines.forward(self.app.engines[parts[0]], self, "POST", rest + ("?" + url.query if url.query else ""))
        if parts[:2] == ["api", "fabric"] and len(parts) == 4 and parts[2] in self.app.by_site \
                and self.app.by_site[parts[2]].get("fabric_view"):
            if parts[3] == "netbox":
                return self.respond(HTTPStatus.ACCEPTED, self.app.fabrics.refresh(parts[2]))
            if parts[3] == "ufm":
                try:
                    return self.respond(HTTPStatus.ACCEPTED, self.app.fabrics.fetch_ufm(parts[2]))
                except KeyError:
                    return self.respond(HTTPStatus.BAD_REQUEST, {"error": "no device profile"})
        return self.respond(HTTPStatus.NOT_FOUND, {"error": "not found"})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Backend fabric dashboard: one read-only service for every region.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--netbox-url", default="http://127.0.0.1:8444", help="local NetBox proxy (default 127.0.0.1:8444)")
    parser.add_argument("--device-profile", default=os.path.expanduser("~/.config/idc-automation/device-access.ini"),
                        help="private profile with [ufm.<site>] sections for direct UFM access")
    parser.add_argument("--no-engines", action="store_true", help="do not start the ICE2/LON14 engines")
    parser.add_argument("--nsr", default=None, help="path to nsr (default: PATH or ~/.local/bin/nsr)")
    parser.add_argument("--regions", default=str(ROOT / "regions.json"))
    parser.add_argument("--cache-seconds", type=int, default=120,
                        help="how old a live answer may get before one background refresh (default 120)")
    parser.add_argument("--dark", action="store_true", help="serve the pages in their original dark theme")
    parser.add_argument("--no-direct", action="store_true",
                        help="run every Grafana query through nsr (one process each) instead of in-process")
    parser.add_argument("--max-parallel", type=int, default=6, help="max simultaneous nsr processes (default 6)")
    parser.add_argument("--warm", action="store_true", help="load the overview once at start")
    args = parser.parse_args(argv)
    config = load_config(Path(args.regions))
    nsr = Nsr(args.nsr, max_parallel=max(1, min(args.max_parallel, 12)), direct=not args.no_direct)
    app = App(config, nsr, args.cache_seconds, args.netbox_url, Path(args.device_profile), not args.no_engines)
    Handler.app, Handler.port = app, args.port
    Handler.light = engines.LIGHT = not args.dark
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print("Backend fabric dashboard: http://127.0.0.1:%d/   (nsr: %s; engines: %s)" % (
        args.port, nsr.binary, ", ".join("%s on %d" % (s, e.port) for s, e in app.engines.items()) or "none"), flush=True)
    app.start_engines()
    if args.warm:
        threading.Thread(target=Handler.app.overview, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for engine in app.engines.values():
            engine.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
