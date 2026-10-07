#!/usr/bin/env python3
"""Generate the data for the LON14 sys2 tab (Ethernet backend, sys2-lon14, SN5610) from NetBox.

Input (read-only discovery, kept outside Git):
  local-inputs/netbox/lon14-discovery.json   written by scripts/lon14_discover.py

Outputs:
  assets/sys2/devices.csv            the 328 backend switches (256 BEL + 72 BES)
  assets/sys2/connections.csv        NetBox backend cables: leaf-spine and leaf-gpu-rdma
  assets/sys2/expected_topology.csv  the same links derived from the wiring rules below
  assets/sys2.html                   the embedded data (line starting with "const D2 = ")

Wiring rules (all 18,432 NetBox cables follow them; checked again on every run):
  P1  Two planes with no cable between them: plane A = BEL1-128 + BES1-36,
      plane B = BEL129-256 + BES37-72.
  L1  Leaf uplinks are swp47s0 .. swp64s1 (index u = 0..35). Leaf j, uplink u goes to spine
      36p + 36 - u (p = 0 for plane A, 1 for plane B); on the spine it lands on port index
      j' = j - 1 - 128p, i.e. swp(j'//2 + 1)s(j' % 2).
  G1  Host gpuN has 8 backend ports swp{r}s{p}: rail r = 1..4, plane p = 0 (A) or 1 (B).
      Pod b = (N-1)//144 (8 pods of 144 hosts), SU k = ((N-1) % 144)//36 (4 SUs of 36 hosts
      per pod), index i = (N-1) % 36.
      Port swp{r}s{p} -> leaf 128p + 16b + k + 1 + 4(r-1), leaf port swp(i//2 + 1)s(i % 2).
      So a pod owns 16 leaves per plane in four rail blocks of four, and SU k takes the k-th
      leaf of each rail block (the ICE2 pattern), in both planes.

    python3 scripts/build_sys2_data.py [discovery.json]
"""
import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PFX = "sys2-lon14-p-"
OUT = ROOT / "assets" / "sys2"
num = lambda name: int(re.search(r"(\d+)$", name).group(1))


def pidx(port: str) -> int:
    m = re.match(r"swp(\d+)s(\d)$", port)
    return 2 * (int(m.group(1)) - 1) + int(m.group(2))


def pname(index: int) -> str:
    return "swp%ds%d" % (index // 2 + 1, index % 2)


def expected_spine(leaf: int, leaf_port: str) -> tuple[int, str] | None:
    plane = 0 if leaf <= 128 else 1
    u = pidx(leaf_port) - 92  # swp47s0 is index 92
    if not 0 <= u < 36:
        return None
    return 36 * plane + 36 - u, pname(leaf - 1 - 128 * plane)


def expected_leaf(host: int, host_port: str) -> tuple[int, str]:
    m = re.match(r"swp(\d)s(\d)$", host_port)
    rail, plane = int(m.group(1)), int(m.group(2))
    pod, rest = divmod(host - 1, 144)
    su, i = divmod(rest, 36)
    return 128 * plane + 16 * pod + su + 1 + 4 * (rail - 1), pname(i)


def rule_rows() -> list[list]:
    rows = []
    for leaf in range(1, 257):
        plane = 0 if leaf <= 128 else 1
        for u in range(36):
            port = pname(92 + u)
            spine, sport = expected_spine(leaf, port)
            rows.append(["leaf-spine", PFX + "swi-bel%d" % leaf, port, PFX + "swi-bes%d" % spine, sport, "P1+L1"])
    for host in range(1, 1153):
        for rail in range(1, 5):
            for plane in (0, 1):
                hp = "swp%ds%d" % (rail, plane)
                leaf, lport = expected_leaf(host, hp)
                rows.append(["leaf-gpu", PFX + "swi-bel%d" % leaf, lport, PFX + "phy-gpu%d" % host, hp, "G1"])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("discovery", type=Path, nargs="?", default=ROOT / "local-inputs" / "netbox" / "lon14-discovery.json")
    args = ap.parse_args()
    disc = json.loads(args.discovery.read_text())
    OUT.mkdir(parents=True, exist_ok=True)

    switches = [s for s in disc["switches"] if re.match(PFX + r"swi-be[ls]\d+$", s["hostname"])]
    switches.sort(key=lambda s: ("-bes" in s["hostname"], num(s["hostname"])))
    with (OUT / "devices.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["hostname", "fabric_role", "netbox_role", "site"])
        for s in switches:
            spine = "-bes" in s["hostname"]
            w.writerow([s["hostname"], "Spine" if spine else "Leaf", s["netbox_role"] or ("be-spine-switch" if spine else "be-leaf-switch"), disc.get("site", "lon14")])

    mesh, gpu, att, rows, off_rule = [], [], {}, [], []
    for c in disc["switch_cables"]:
        a, b = c["a"], c["b"]
        names = a[0] + " " + b[0]
        if PFX not in names:
            continue
        if PFX + "swi-bel" in names and PFX + "swi-bes" in names:
            l, s = (a, b) if "-bel" in a[0] else (b, a)
            mesh.append([num(l[0]), l[1], num(s[0]), s[1], c["id"]])
            rows.append([c["id"], "leaf-spine", l[0], l[1], "", s[0], s[1], ""])
            if expected_spine(num(l[0]), l[1]) != (num(s[0]), s[1]):
                off_rule.append(["leaf-spine", c["id"], l[0], l[1], s[0], s[1]])
        elif PFX + "phy-gpu" in names:
            h, s = (a, b) if "-phy-gpu" in a[0] else (b, a)
            host = num(h[0])
            if PFX + "swi-bel" in s[0] and re.match(r"swp\ds\d$", h[1]):
                gpu.append([num(s[0]), s[1], host, h[1], c["id"]])
                rows.append([c["id"], "leaf-gpu-rdma", s[0], s[1], "", h[0], h[1], ""])
                if expected_leaf(host, h[1]) != (num(s[0]), s[1]):
                    off_rule.append(["leaf-gpu", c["id"], s[0], s[1], h[0], h[1]])
            elif "-swi-fel" in s[0] or "-swi-obl" in s[0]:
                att.setdefault(str(host), []).append([h[1], c["id"], s[0].split("-")[-1], s[1]])
    for v in att.values():
        v.sort(key=lambda x: x[0])
    mesh.sort()
    gpu.sort()
    rows.sort(key=lambda r: (r[1] != "leaf-spine", int(r[0])))
    with (OUT / "connections.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["netbox_cable_id", "connection_type", "endpoint_a_device", "endpoint_a_port", "endpoint_a_live_state",
                    "endpoint_b_device", "endpoint_b_port", "endpoint_b_live_state"])
        w.writerows(rows)
    with (OUT / "expected_topology.csv").open("w", newline="", encoding="utf-8") as f:
        f.write("# LON14 sys2 Ethernet backend (SN5610) as designed: generated by scripts/build_sys2_data.py (rules in that file).\n")
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["link_type", "a_device", "a_port", "b_device", "b_port", "rule"])
        w.writerows(rule_rows())

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = {"mesh": mesh, "gpu": gpu, "att": att, "off_rule": off_rule, "export": stamp,
            "ips": {s["hostname"].split("-")[-1]: (s.get("primary_ip") or "") for s in switches}}
    page = ROOT / "assets" / "sys2.html"
    html = page.read_text(encoding="utf-8").split("\n")
    for i, line in enumerate(html):
        if line.startswith("const D2 = "):
            html[i] = "const D2 = " + json.dumps(data, separators=(",", ":")) + ";"
            break
    else:
        raise SystemExit("assets/sys2.html has no 'const D2 = ' line")
    page.write_text("\n".join(html), encoding="utf-8")
    print("sys2: %d switches · %d leaf-spine · %d host links · %d hosts · %d cables off the rules"
          % (len(switches), len(mesh), len(gpu), len({g[2] for g in gpu}), len(off_rule)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
