#!/usr/bin/env python3
"""Generate the LON14 dashboard's NetBox-derived data from a discovery export.

Inputs (written by the read-only discovery scripts, kept outside Git):
  lon14-discovery.json   switches + every cable touching a switch at site lon14
  lon14-inventory.json   optional: rack, U, serial, model for every sys1-lon14 device

Outputs:
  assets/devices.csv       the 100 InfiniBand switches (64 BEL + 36 BES, Q3400)
  assets/connections.csv   NetBox backend cables: leaf-spine and leaf-GPU (RDMA1-4)
  assets/dashboard.html    the embedded NetBox data (const D / UFM / UFMC / LIVE_SNAPSHOT)

Only the InfiniBand fabric (sys1-lon14) is used; the Ethernet fabric (sys2-lon14,
SN5610) at the same site is left out. Re-run whenever NetBox changes:

    python3 scripts/lon14_discover.py && python3 scripts/lon14_inventory.py   # read-only NetBox GETs
    python3 scripts/build_site_data.py
"""
import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PFX = "sys1-lon14-p-"
num = lambda name: int(re.search(r"(\d+)$", name).group(1))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    nb = ROOT / "local-inputs" / "netbox"
    ap.add_argument("discovery", type=Path, nargs="?", default=nb / "lon14-discovery.json", help="default local-inputs/netbox/lon14-discovery.json")
    ap.add_argument("inventory", type=Path, nargs="?", default=nb / "lon14-inventory.json", help="optional; default local-inputs/netbox/lon14-inventory.json")
    args = ap.parse_args()
    disc = json.loads(args.discovery.read_text())
    inv_rows = json.loads(args.inventory.read_text()) if args.inventory and args.inventory.is_file() else []
    inv_by = {d["name"]: d for d in inv_rows}

    switches = [s for s in disc["switches"] if re.match(PFX + r"swi-be[ls]\d+$", s["hostname"])]
    switches.sort(key=lambda s: ("bes" in s["hostname"], num(s["hostname"])))
    with (ROOT / "assets" / "devices.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["hostname", "fabric_role", "netbox_role", "site"])
        for s in switches:
            w.writerow([s["hostname"], "Spine" if "-bes" in s["hostname"] else "Leaf", s["netbox_role"] or ("be-spine-switch" if "-bes" in s["hostname"] else "be-leaf-switch"), disc.get("site", "lon14")])

    mesh, gpu, att, ufmc, rows = [], [], {}, {1: [], 2: []}, []
    for c in disc["switch_cables"]:
        a, b = c["a"], c["b"]
        names = a[0] + " " + b[0]
        if PFX not in names:
            continue
        if PFX + "swi-bel" in names and PFX + "swi-bes" in names:
            l, s = (a, b) if "-bel" in a[0] else (b, a)
            mesh.append([num(l[0]), l[1], num(s[0]), s[1], c["id"]])
            rows.append([c["id"], "leaf-spine", l[0], l[1], "", s[0], s[1], ""])
        elif PFX + "phy-gpu" in names:
            h, s = (a, b) if "-phy-gpu" in a[0] else (b, a)
            host = num(h[0])
            if PFX + "swi-bel" in s[0] and h[1].upper().startswith("RDMA"):
                gpu.append([num(s[0]), s[1], host, h[1].upper(), c["id"]])
                rows.append([c["id"], "leaf-gpu-rdma", s[0], s[1], "", h[0], h[1].upper(), ""])
                att.setdefault(str(host), []).append([h[1].upper(), c["id"]])
            elif "-swi-fel" in s[0] or "-swi-obl" in s[0]:
                att.setdefault(str(host), []).append([h[1], c["id"], s[0].split("-")[-1], s[1]])
        elif PFX + "phy-ufm" in names:
            h, s = (a, b) if "-phy-ufm" in a[0] else (b, a)
            kind = "fabric" if "-swi-bel" in s[0] else "frontend" if "-swi-fel" in s[0] else "oob" if "-swi-obl" in s[0] else "console"
            ufmc[num(h[0])].append([h[1], s[0].split("-")[-1], s[1], kind])
    for v in att.values():
        v.sort(key=lambda x: x[0])
    mesh.sort(); gpu.sort()
    rows.sort(key=lambda r: (r[1] != "leaf-spine", int(r[0])))
    with (ROOT / "assets" / "connections.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["netbox_cable_id", "connection_type", "endpoint_a_device", "endpoint_a_port", "endpoint_a_live_state",
                    "endpoint_b_device", "endpoint_b_port", "endpoint_b_live_state"])
        w.writerows(rows)

    inv = {}
    for name, d in inv_by.items():
        if "-phy-gpu" in name:
            inv[str(num(name))] = [d.get("rack") or "", str(d.get("position") or "").rstrip("0").rstrip(".") or "", d.get("serial") or "", d.get("type") or ""]
    ufm = []
    for n in (1, 2):
        name = PFX + "phy-ufm%d" % n
        d = inv_by.get(name) or next((u for u in disc.get("ufm_like_devices", []) if u["name"] == name), {})
        ip = d.get("ip") or d.get("primary_ip") or ""
        where = " · ".join(x for x in (d.get("rack") or "", ("U%s" % str(d.get("position")).split(".")[0]) if d.get("position") else "") if x)
        ufm.append(["ufm%d" % n, name, ip, where or "rack not in export"])
        ufmc[n].sort(key=lambda x: ({"fabric": 0, "frontend": 1, "oob": 2, "console": 3}[x[3]], x[0].lower()))

    total = len(rows)
    live = {"mode": "snapshot", "source": "NetBox cabling only (no switch collection yet)", "collected_at": None, "switches_collected": 0,
            "endpoints": {"compared": 0, "active": 0, "init": 0, "down": 0, "other": 0, "not_collected": 2 * total},
            "cables": {"total": total, "active": 0, "init": 0, "down": 0, "unknown": total}, "exceptions": [], "ufm": [],
            "netbox": None, "refresh_running": False, "sync_running": False}
    data = {"mesh": mesh, "gpu": gpu, "inv": inv, "att": att}
    page = ROOT / "assets" / "dashboard.html"
    html = page.read_text(encoding="utf-8").split("\n")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for i, line in enumerate(html):
        if line.startswith("const D = "):
            html[i] = "const D = " + json.dumps(data, separators=(",", ":")) + ";"
        elif line.startswith("const UFM = "):
            html[i] = "const UFM = " + json.dumps(ufm) + ";"
        elif line.startswith("const UFMC = "):
            html[i] = "const UFMC = " + json.dumps([ufmc[1], ufmc[2]]) + ";"
        elif line.startswith("const LIVE_SNAPSHOT = "):
            html[i] = "const LIVE_SNAPSHOT = " + json.dumps(live, separators=(",", ":")) + ";"
        elif line.startswith("const NB_EXPORT = "):
            html[i] = "const NB_EXPORT = " + json.dumps({"date": stamp, "hosts": len({g[2] for g in gpu}), "inventory": len(inv)}) + ";"
    page.write_text("\n".join(html), encoding="utf-8")
    print("devices.csv: %d switches · connections.csv: %d cables (%d leaf-spine, %d leaf-GPU) · inventory: %d hosts · UFM links: %s"
          % (len(switches), total, len(mesh), len(gpu), len(inv), {k: len(v) for k, v in ufmc.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
