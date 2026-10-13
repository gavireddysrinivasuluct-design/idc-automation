"""NetBox, read first and directly: the expected topology for NetBox-first regions.

Same access path as the ICE2 / LON14 dashboards: the local NetBox proxy
(scripts/netbox_proxy.sh, default http://127.0.0.1:8444) and the read-only API token in
macOS Keychain item `netbox-mcp-token` (or $NETBOX_TOKEN). GET requests only.

The snapshot has the same shape as before, so fabric.build_topology() is unchanged:
    {"site", "cluster", "collected_at", "source", "devices": {name: {...}}, "cables": [...]}
Each cable row is one leaf interface and the far end of its whole NetBox path
(connected_endpoints), with the cable ID and a state: connected, planned, no-path ...
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

SPINE, LEAF = "be-spine-switch", "be-leaf-switch"
IFACE_FIELDS = "id,name,device,cable,connected_endpoints,connected_endpoints_reachable,mark_connected"


class NetBoxError(RuntimeError):
    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def token() -> str:
    value = os.environ.get("NETBOX_TOKEN", "").strip()
    if value:
        return value
    try:
        value = subprocess.run(["security", "find-generic-password", "-s", "netbox-mcp-token", "-a", getpass.getuser(), "-w"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        value = ""
    if not value:
        raise NetBoxError("NetBox token not found", "Run ice2-netbox-dashboard/scripts/configure_netbox_token.sh once "
                          "(Keychain item netbox-mcp-token), or set NETBOX_TOKEN.")
    return value


class NetBox:
    def __init__(self, url: str, api_token: str | None = None, timeout: int = 60) -> None:
        self.url, self.timeout = url.rstrip("/"), timeout
        self._token = api_token
        self.calls = 0

    def get(self, path: str, params: dict | None = None) -> dict:
        if params:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode(params, doseq=True)
        request = urllib.request.Request(self.url + path, headers={
            "Authorization": "Token " + (self._token or token()), "Accept": "application/json"})
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            hint = {401: "The NetBox token was rejected: run configure_netbox_token.sh again.",
                    403: "The token cannot read this object type: ask for NetBox read permission."}.get(error.code, "")
            raise NetBoxError("NetBox HTTP %s on %s" % (error.code, path.split("?")[0]), hint) from error
        except (urllib.error.URLError, OSError) as error:
            raise NetBoxError("Cannot reach NetBox at %s (%s)" % (self.url, getattr(error, "reason", error)),
                              "Start the NetBox proxy: ice2-netbox-dashboard/scripts/netbox_proxy.sh start "
                              "(and tsh login).") from error

    def paged(self, path: str, params: dict) -> list:
        out, offset = [], 0
        while True:
            page = self.get(path, {**params, "limit": 1000, "offset": offset})
            results = page.get("results") or []
            out += results
            offset += len(results)
            if not page.get("next") or not results:
                return out


def _label(value) -> str | None:
    return value.get("name") if isinstance(value, dict) else None


def pull_fabric(nb: NetBox, fabric: dict, progress=lambda text: None) -> dict:
    site, cluster = fabric["netbox_site"], fabric.get("cluster")
    devices = {}
    for role in (SPINE, LEAF):
        progress("NetBox: %s at %s" % (role, site))
        for d in nb.paged("/api/dcim/devices/", {"site": site, "role": role}):
            if cluster and _label(d.get("cluster")) not in (None, cluster):
                continue
            devices[d["name"]] = {
                "name": d["name"], "role": role, "id": d["id"],
                "model": (d.get("device_type") or {}).get("model"),
                "ip": ((d.get("primary_ip4") or d.get("primary_ip") or {}).get("address") or "").split("/")[0] or None,
                "status": (d.get("status") or {}).get("value"), "cluster": _label(d.get("cluster")),
                "rack": _label(d.get("rack")),
            }
    leaves = [d for d in devices.values() if d["role"] == LEAF]
    if not leaves:
        raise NetBoxError("NetBox has no %s at site %s%s" % (LEAF, site, (" in cluster " + cluster) if cluster else ""),
                          "Check netbox_site / cluster for this region in regions.json.")
    cables = []
    ids = [d["id"] for d in leaves]
    for start in range(0, len(ids), 20):
        chunk = ids[start:start + 20]
        progress("NetBox: cabling of leaves %d–%d of %d" % (start + 1, start + len(chunk), len(ids)))
        for iface in nb.paged("/api/dcim/interfaces/", {"device_id": chunk, "cabled": "true", "fields": IFACE_FIELDS}):
            ends = iface.get("connected_endpoints") or []
            reachable = bool(iface.get("connected_endpoints_reachable"))
            far = ends[0] if ends else {}
            cables.append({
                "device": (iface.get("device") or {}).get("name"),
                "interface": iface.get("name"),
                "remote_device": (far.get("device") or {}).get("name"),
                "remote_interface": far.get("name"),
                "state": "connected" if reachable else ("path-down" if ends else "no-path"),
                "cable": (iface.get("cable") or {}).get("id"),
            })
    cables = [c for c in cables if c["remote_device"]]
    return {"site": site, "cluster": cluster, "collected_at": time.time(), "source": "netbox " + nb.url,
            "devices": devices, "cables": cables, "api_calls": nb.calls}
