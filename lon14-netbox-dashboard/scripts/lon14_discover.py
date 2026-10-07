#!/usr/bin/env python3
"""Read-only LON14 discovery for the dashboard build (NetBox GET requests + `tsh ls`).

Writes lon14-discovery.json to local-inputs/netbox/ (not tracked by git): site, device roles/types/name patterns,
backend switch inventory and backend cable list (the same shape as the ICE2 dashboard's
devices.csv / connections.csv), and the Teleport node names that look like LON14 jump/UFM
hosts. No token or password is written; nothing is changed anywhere.

Run:  python3 lon14_discover.py          (needs the NetBox proxy on 127.0.0.1:8444 and tsh login)
"""
import collections, getpass, json, re, subprocess, sys, urllib.parse, urllib.request
from pathlib import Path

NB = "http://127.0.0.1:8444"
OUT = Path(__file__).resolve().parent.parent / "local-inputs" / "netbox" / "lon14-discovery.json"
OUT.parent.mkdir(parents=True, exist_ok=True)
token = subprocess.run(["security", "find-generic-password", "-s", "netbox-mcp-token", "-a", getpass.getuser(), "-w"],
                       capture_output=True, text=True).stdout.strip()
if not token:
    sys.exit("NetBox token not found in Keychain (netbox-mcp-token).")

def get(path):
    req = urllib.request.Request(NB + path, headers={"Authorization": "Token " + token, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

def get_all(path):
    out, url = [], path
    while url:
        page = get(url)
        out += page.get("results", [])
        nxt = page.get("next")
        url = urllib.parse.urlsplit(nxt)._replace(scheme="", netloc="").geturl() if nxt else None
    return out

report = {}
sites = get_all("/api/dcim/sites/?q=lon14&limit=100") or get_all("/api/dcim/sites/?q=lon&limit=100")
report["sites"] = [{"id": s["id"], "slug": s["slug"], "name": s["name"]} for s in sites]
print("Sites:", [s["slug"] for s in sites])
if not sites:
    OUT.write_text(json.dumps(report, indent=1)); sys.exit("No LON14 site found in NetBox.")
site = next((s for s in sites if "lon14" in s["slug"].lower()), sites[0])
slug = site["slug"]

devices = get_all("/api/dcim/devices/?site=%s&limit=1000" % urllib.parse.quote(slug))
report["device_record_shape"] = {k: (type(v).__name__, (list(v.keys())[:8] if isinstance(v, dict) else str(v)[:40])) for k, v in (devices[0] if devices else {}).items()}
def _label(v):
    if isinstance(v, dict):
        return v.get("slug") or v.get("model") or v.get("name") or v.get("display") or ""
    return str(v or "")
def role(d): return _label(d.get("role") or d.get("device_role"))
def dtype(d): return _label(d.get("device_type"))
def pattern(name): return re.sub(r"\d+", "#", name or "")
roles = collections.Counter(role(d) for d in devices)
report["site"] = slug
report["device_roles"] = roles.most_common()
report["roles_detail"] = {}
for r in roles:
    ds = [d for d in devices if role(d) == r]
    report["roles_detail"][r] = {"count": len(ds), "types": collections.Counter(dtype(d) for d in ds).most_common(5),
                                 "name_patterns": collections.Counter(pattern(d["name"]) for d in ds).most_common(40),
                                 "examples": sorted(d["name"] or "" for d in ds)[:6]}
report["switch_name_patterns"] = collections.Counter(pattern(d["name"]) for d in devices if "-swi-" in (d["name"] or "")).most_common()
report["all_name_patterns"] = collections.Counter(pattern(d["name"]) for d in devices).most_common(60)
switches = [d for d in devices if "-swi-" in (d["name"] or "")]
report["switches"] = [{"hostname": d["name"], "netbox_role": role(d), "type": dtype(d),
                       "primary_ip": ((d.get("primary_ip4") or d.get("primary_ip") or {}).get("address")), "status": _label(d.get("status"))}
                      for d in sorted(switches, key=lambda x: x["name"] or "")]
print("Switches by name pattern:", report["switch_name_patterns"])
OUT.write_text(json.dumps(report, indent=1))  # devices first: useful even if the cable pull is stopped
print("Saved the device summary; now reading the LON14 cables in bulk pages…", flush=True)
by_name = {d["name"]: d for d in devices}
cables, total, url = [], 0, "/api/dcim/cables/?site=%s&limit=1000" % urllib.parse.quote(slug)
while url:
    page = get(url)
    total += len(page.get("results", []))
    print("  %d / %s cables read" % (total, page.get("count")), flush=True)
    for c in page.get("results", []):
        ends = []
        for side in ("a_terminations", "b_terminations"):
            t = (c.get(side) or [{}])
            o = (t[0] or {}).get("object") or {}
            ends.append(((o.get("device") or {}).get("name") or "", o.get("name") or "", (t[0] or {}).get("object_type") or "", len(c.get(side) or [])))
        if any("-swi-" in e[0] for e in ends):  # keep only cables that touch a switch
            cables.append({"id": c["id"], "a": ends[0], "b": ends[1], "status": _label(c.get("status"))})
    nxt = page.get("next")
    url = urllib.parse.urlsplit(nxt)._replace(scheme="", netloc="").geturl() if nxt else None
report["cables_total"] = total
report["switch_cables"] = cables
kinds = collections.Counter(tuple(sorted((pattern(c["a"][0]), pattern(c["b"][0])))) for c in cables)
report["cable_kinds"] = [[list(k), n] for k, n in kinds.most_common(40)]
print("Cables touching a switch:", len(cables)); [print("  ", n, " <-> ".join(k)) for k, n in kinds.most_common(12)]
report["ufm_like_devices"] = [{"name": d["name"], "role": role(d), "type": dtype(d), "primary_ip": ((d.get("primary_ip4") or {}).get("address"))}
                              for d in devices if re.search(r"ufm", (d["name"] or "") + role(d), re.I)]
try:
    nodes = json.loads(subprocess.run(["tsh", "ls", "--format=json"], capture_output=True, text=True, timeout=60).stdout or "[]")
    report["teleport_nodes"] = sorted({n.get("spec", {}).get("hostname", "") for n in nodes if re.search(r"lon14|lon-14", json.dumps(n), re.I)})[:40]
except Exception as e:
    report["teleport_nodes_error"] = str(e)
OUT.write_text(json.dumps(report, indent=1))
print("Wrote", OUT)
