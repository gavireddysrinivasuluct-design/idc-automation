#!/usr/bin/env python3
"""Backend regions dashboard: pick a region, see its live fabric issues.

Read-only and lightweight. Everything live comes through the `nsr` CLI
(nscaledev/netops-misc), which owns Teleport mTLS and the PagerDuty token:

  * Grafana / Mimir / VictoriaMetrics: UFM telemetry, gNMI, SNMP, blackbox ping,
    UFM server health (small aggregated PromQL only)
  * Loki: UFM event log and backend switch syslog
  * PagerDuty: open BackEnd Network incidents

Nothing logs in to a switch, UFM or NetBox, and nothing is written anywhere. The deep
per-site dashboards (ICE2 on 8765, LON14 on 8766) are linked from their region page.

    python3 multi_region_dashboard.py            # http://127.0.0.1:8780/
    python3 multi_region_dashboard.py --nsr ~/.local/bin/nsr --port 8780

HTTP API (local only):
    GET /                       region picker          GET /r/<site>   region page
    GET /api/config             regions, sources, notes
    GET /api/overview           all regions, grouped queries (cache 60 s)
    GET /api/region/<site>/<section>   section = fabric | switches | reach | events | incidents
    GET /api/topology/<site>    is that site's NetBox/UFM dashboard running?
    GET /f/<site>               ICE2-style spine · leaf · rail view built from NetBox + UFM (nsr)
    GET /api/fabric/<site>      that view's data;  POST|GET /api/fabric/<site>/netbox  snapshot job
    add ?refresh=1 to bypass the cache
"""

from __future__ import annotations

import argparse
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
    def __init__(self, config: dict, nsr: Nsr, ttl: int = 60) -> None:
        self.config, self.nsr = config, nsr
        self.by_site = {region["site"]: region for region in config["regions"]}
        self.cache = Cache(ttl)
        self.incident_cache = Cache(ttl)
        self.started = time.time()
        self.fabrics = FabricStore(nsr, config, CACHE_DIR, ttl)

    def incidents(self, force=False) -> dict:
        return self.incident_cache.get("pd", lambda: checks.fetch_incidents(self.nsr, self.config), force)

    def overview(self, force=False) -> dict:
        def compute():
            data = checks.run_overview(self.nsr, self.config, self.incidents(force))
            data["nsr_calls"] = self.nsr.calls
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
                                                         "cloud_regions", "switches_expected", "topology_dashboard",
                                                         "grafana_dashboards", "notes")},
                            "sources": sorted(region["sources"]), "fabric_view": bool(region.get("fabric_view"))})
        return {"grafana_url": self.config.get("grafana_url"), "regions": regions,
                "nsr": self.nsr.binary, "started": self.started}

    def topology(self, site: str) -> dict:
        url = self.by_site[site].get("topology_dashboard")
        if not url:
            return {"site": site, "configured": False}
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=2) as response:
                return {"site": site, "configured": True, "running": response.status == 200, "url": url}
        except Exception:  # noqa: BLE001 - any failure just means "not running"
            return {"site": site, "configured": True, "running": False, "url": url}


class Handler(BaseHTTPRequestHandler):
    app: App
    port: int = 8780
    server_version = "backend-regions/1"

    def log_message(self, fmt, *args):  # quiet: the page polls
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("[regions] " + fmt % args + "\n")

    def respond(self, code, body, content_type="application/json; charset=utf-8"):
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
            if parts[:2] == ["api", "topology"] and len(parts) == 3 and parts[2] in self.app.by_site:
                return self.respond(HTTPStatus.OK, self.app.topology(parts[2]))
            if parts[0] == "f" and len(parts) == 2 and parts[1] in self.app.by_site:
                return self.respond(HTTPStatus.OK, FABRIC_PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            if parts[:2] == ["api", "fabric"] and len(parts) >= 3 and parts[2] in self.app.by_site:
                if not self.app.by_site[parts[2]].get("fabric_view"):
                    return self.respond(HTTPStatus.NOT_FOUND, {"error": "no fabric view configured for this region"})
                if len(parts) == 4 and parts[3] == "netbox":
                    return self.respond(HTTPStatus.OK, self.app.fabrics.jobs.get(parts[2]) or {"state": "idle"})
                return self.respond(HTTPStatus.OK, self.app.fabrics.view(parts[2], force))
            if path == "/api/health":
                return self.respond(HTTPStatus.OK, {"ok": True, "nsr_calls": self.app.nsr.calls})
            return self.respond(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception as error:  # noqa: BLE001 - never leave the browser hanging
            return self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})


    def do_POST(self):  # noqa: N802 - only starts a read-only NetBox snapshot
        if not self.host_ok():
            return self.respond(HTTPStatus.FORBIDDEN, {"error": "wrong host"})
        origin = self.headers.get("Origin")
        if origin and origin.split("//")[-1].split(":")[0] not in ("127.0.0.1", "localhost"):
            return self.respond(HTTPStatus.FORBIDDEN, {"error": "cross-site request refused"})
        parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/")]
        if parts[:2] == ["api", "fabric"] and len(parts) == 4 and parts[3] == "netbox" \
                and parts[2] in self.app.by_site and self.app.by_site[parts[2]].get("fabric_view"):
            return self.respond(HTTPStatus.ACCEPTED, self.app.fabrics.refresh(parts[2]))
        return self.respond(HTTPStatus.NOT_FOUND, {"error": "not found"})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only backend regions dashboard (live data via nsr).")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--nsr", default=None, help="path to nsr (default: PATH or ~/.local/bin/nsr)")
    parser.add_argument("--regions", default=str(ROOT / "regions.json"))
    parser.add_argument("--cache-seconds", type=int, default=60, help="how long live answers are reused (default 60)")
    parser.add_argument("--max-parallel", type=int, default=6, help="max simultaneous nsr processes (default 6)")
    parser.add_argument("--warm", action="store_true", help="load the overview once at start")
    args = parser.parse_args(argv)
    config = load_config(Path(args.regions))
    nsr = Nsr(args.nsr, max_parallel=max(1, min(args.max_parallel, 12)))
    Handler.app, Handler.port = App(config, nsr, args.cache_seconds), args.port
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print("Backend regions dashboard: http://127.0.0.1:%d/   (nsr: %s)" % (args.port, nsr.binary), flush=True)
    if args.warm:
        threading.Thread(target=Handler.app.overview, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
