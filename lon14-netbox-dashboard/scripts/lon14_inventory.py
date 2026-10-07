#!/usr/bin/env python3
"""Read-only: LON14 device inventory (rack, U, serial, model, role, IP) for the dashboard build.
Writes lon14-inventory.json to local-inputs/netbox/ (not tracked by git). NetBox GET requests only; no token is written."""
import getpass, json, subprocess, sys, urllib.parse, urllib.request
from pathlib import Path
NB = "http://127.0.0.1:8444"
OUT = Path(__file__).resolve().parent.parent / "local-inputs" / "netbox" / "lon14-inventory.json"
OUT.parent.mkdir(parents=True, exist_ok=True)
token = subprocess.run(["security", "find-generic-password", "-s", "netbox-mcp-token", "-a", getpass.getuser(), "-w"], capture_output=True, text=True).stdout.strip()
if not token:
    sys.exit("NetBox token not found in Keychain (netbox-mcp-token).")
def get(path):
    req = urllib.request.Request(NB + path, headers={"Authorization": "Token " + token, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())
lab = lambda v: (v.get("slug") or v.get("model") or v.get("name") or v.get("display") or "") if isinstance(v, dict) else str(v or "")
out, url, n = [], "/api/dcim/devices/?site=lon14&limit=1000", 0
while url:
    page = get(url)
    for d in page.get("results", []):
        name = d.get("name") or ""
        if not name.startswith("sys1-lon14"):
            continue
        out.append({"name": name, "role": lab(d.get("role")), "type": lab(d.get("device_type")), "rack": lab(d.get("rack")),
                    "position": d.get("position"), "serial": d.get("serial") or "", "status": lab(d.get("status")),
                    "ip": ((d.get("primary_ip4") or d.get("primary_ip") or {}) or {}).get("address")})
    n += len(page.get("results", []))
    print("  %d / %s devices read" % (n, page.get("count")), flush=True)
    nxt = page.get("next")
    url = urllib.parse.urlsplit(nxt)._replace(scheme="", netloc="").geturl() if nxt else None
OUT.write_text(json.dumps(out))
print("Wrote %s (%d sys1-lon14 devices)" % (OUT, len(out)))
