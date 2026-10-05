#!/usr/bin/env python3
"""Compare the cabling UFM actually sees with the NetBox cable list.

Input is UFM's own fabric scan, the ibdiagnet link list (`ibdiagnet2.lst`,
optionally gzip-compressed), which UFM refreshes regularly at
/opt/ufm/tmp/fabric_analysis/ibdiagnet.out/ibdiagnet2.lst inside its container.
Reading it sends nothing to the fabric.

Facts about the ICE2 fabric this relies on (checked against the real scan):

* Every Q3400 switch is four chips, one per plane (`.../U1` .. `/U4`), and every
  800G cable appears as four 200G lanes, one per plane, on the same port number.
* Port numbers (`PN:`) are hexadecimal; NVOS port `swNpM` is number 2*(N-1)+M.
* GPU adapters are named `<rack>-T<tray> mlx5_<n>` (for example
  `nvl72d031-T14 mlx5_2`), not by NetBox hostname. The hostname of a tray is
  learned from the NetBox cables that end on its adapters.
* 4 pods of 16 leaves; a scalable unit (SU) takes the k-th leaf of each 4-leaf
  rail block of its pod, and a tray uses the same port on all four of its leaves.
"""

from __future__ import annotations

import collections
import csv
import gzip
import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path

END = re.compile(r"\{ (SW|CA) Ports:[0-9a-fA-F]+ .*?\{([^}]*)\} LID:[0-9a-fA-F]+ PN:([0-9a-fA-F]+) \}")
SWITCH = re.compile(r"^(?:MF\d+;)?([A-Za-z0-9._-]+):[^/]*/U(\d+)$")
TRAY = re.compile(r"^(\S+-T\d+)\s+(mlx5_\d+)$")
UFM_HOST = re.compile(r"-ufm\d", re.I)
LEAF = re.compile(r"-bel(\d+)$")
SPINE = re.compile(r"-bes(\d+)$")
PLANES = 4


def port_label(number: int) -> str:
    return "sw%dp%d" % ((number - 1) // 2 + 1, (number - 1) % 2 + 1)


def slot_of(label: str) -> int | None:
    """Tray slot 0..71 for a leaf downlink label swNpM with N <= 36."""
    m = re.match(r"sw(\d+)p([12])$", label)
    if not m or int(m.group(1)) > 36:
        return None
    return (int(m.group(1)) - 1) * 2 + int(m.group(2)) - 1


def leaf_no(name: str) -> int | None:
    m = LEAF.search(name)
    return int(m.group(1)) if m else None


def su_of_leaf(leaf: int) -> tuple[int, int, int]:
    """(su 1..16, pod 1..4, rail 1..4) for leaf 1..64."""
    pod, offset = divmod(leaf - 1, 16)
    rail, k = divmod(offset, 4)
    return pod * 4 + k + 1, pod + 1, rail + 1


def su_leaves(su: int) -> list[int]:
    pod, k = divmod(su - 1, 4)
    return [pod * 16 + k + 1 + 4 * r for r in range(4)]


def short(name: str) -> str:
    return re.sub(r"^sys1-ice2-p-(swi|phy)-", "", name)


def open_scan(path: Path):
    raw = path.open("rb")
    head = raw.read(2)
    raw.seek(0)
    if head == b"\x1f\x8b":
        return io.TextIOWrapper(gzip.GzipFile(fileobj=raw), encoding="utf-8", errors="replace")
    return io.TextIOWrapper(raw, encoding="utf-8", errors="replace")


def read_lanes(path: Path) -> tuple[list[tuple], dict]:
    """Every link lane as ((kind, name, plane_or_port, label), (..), state dict)."""
    lanes, meta = [], {"tool": "", "unparsed": 0}
    with open_scan(path) as handle:
        for line in handle:
            if line.startswith("# Running version"):
                meta["tool"] = line.split(":", 1)[1].strip().split(",")[0].strip('" ')
            if not line.startswith("{"):
                continue
            ends = END.findall(line)
            if len(ends) != 2:
                meta["unparsed"] += 1
                continue
            parsed = []
            for kind, desc, pn in ends:
                number = int(pn, 16)
                if kind == "SW":
                    m = SWITCH.match(desc)
                    parsed.append(("SW", m.group(1), int(m.group(2)), port_label(number)) if m else ("SW?", desc, 0, port_label(number)))
                else:
                    parsed.append(("CA", desc.strip(), number, ""))
            state = dict(item.split("=", 1) for item in line.rsplit("}", 1)[1].split() if "=" in item)
            lanes.append((parsed[0], parsed[1], state))
    return lanes, meta


TOPO_HEAD = re.compile(r"^(\S+)\s+(\S+)\s*$")
TOPO_LINK = re.compile(r"^\s+(\S+)\s+-\d+x-[^>]*->\s+(\S+)\s+(\S+)\s+(\S+)")
TOPO_SWPORT = re.compile(r"^U(\d+)/P(\d+)$")


def read_master(path: Path) -> dict:
    """UFM's master (reference) topology, an IBDM .topo file (plain or gzip).

    UFM's own Topology Compare uses it as the truth (it is copied to
    /opt/ufm/data/fabric.topo nightly). Ports here are decimal: U<chip>/P<n>.
    Returns leaf-port -> far end for switch links, leaf-port -> (host, adapter) for GPUs.
    """
    switch, gpu, lanes = {}, {}, 0
    node = None
    with open_scan(path) as handle:
        for line in handle:
            if not line.strip() or line.startswith("#"):
                continue
            if not line[0].isspace():
                m = TOPO_HEAD.match(line)
                node = (m.group(1), m.group(2)) if m else None
                continue
            m = TOPO_LINK.match(line)
            if not m or not node:
                continue
            lanes += 1
            lport, rtype, rname, rport = m.groups()
            here_sw, there_sw = node[0].startswith("Q"), rtype.startswith("Q")
            lm, rm = TOPO_SWPORT.match(lport), TOPO_SWPORT.match(rport)
            if here_sw and there_sw and lm and rm and node[1] != rname:
                a, b = (node[1], port_label(int(lm.group(2)))), (rname, port_label(int(rm.group(2))))
                for x, y in ((a, b), (b, a)):
                    if leaf_no(x[0]) is not None:
                        switch[x] = y
            elif node[0].startswith("HCA") and there_sw and rm and not UFM_HOST.search(node[1]):
                gpu[(rname, port_label(int(rm.group(2))))] = (node[1], lport.split("/")[0])
    hosts = sorted({h for h, _ in gpu.values()})
    return {"switch": switch, "gpu": gpu, "lanes": lanes, "hosts": hosts,
            "saved_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"), "file": str(path)}


def read_ufm_compare(path: Path) -> dict:
    """Summary of UFM's own Topology Compare report (JSON): date, verdict, counts by kind."""
    import json
    with open_scan(path) as handle:
        data = json.load(handle)
    items = []

    def walk(obj):
        if isinstance(obj, dict):
            if "Detected Differences" in obj:
                items.append((obj.get("Severity", ""), str(obj["Detected Differences"]).strip()))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
    walk(data)
    kinds = collections.Counter()
    for severity, text in items:
        key = re.sub(r"0x[0-9a-fA-F]+|[0-9a-fA-F]{16}|'[^']*'|\d+", "…", text)[:120]
        kinds[(severity, key)] += 1
    status = ""
    try:
        status = data["sections"][0]["status"]["value"]
    except (KeyError, IndexError, TypeError):
        pass
    plain = collections.Counter()
    for severity, text in items:
        t = text.lower()
        if t.startswith("total:") or "found mismatches" in t:
            continue
        if "unplanned node" in t:
            plain["nodes not in the master (added after it was saved)"] += 1
        elif "unplanned cable" in t:
            plain["cables not in the master (added after it was saved)"] += 1
        elif "wrong node name" in t:
            plain["node names that differ from the master"] += 1
        elif "non-parsible" in t:
            plain["adapters without a usable node description"] += 1
        elif "missing" in t:
            plain["links in the master that are missing now"] += 1
        elif "wrong" in t or "mismatch" in t:
            plain["links connected differently from the master"] += 1
        else:
            plain["other"] += 1
    return {"date": data.get("date", ""), "status": status, "items": len(items), "categories": plain.most_common(),
            "by_severity": dict(collections.Counter(s for s, _ in items)),
            "top": [[sev, text, n] for (sev, text), n in kinds.most_common(8)]}


def load_baseline(connections: Path) -> list[dict]:
    with connections.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_expected(path: Path) -> list[dict]:
    """The designed topology (assets/expected_topology.csv); '#' lines are comments."""
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(line for line in handle if not line.startswith("#")))


TOPO_DESIGN_HEAD = re.compile(r"^(\S+)\s+(\S+)")
TOPO_DESIGN_LINK = re.compile(r"^\s+P(\d+)\s+-\S+->\s+(\S+)\s+(\S+)\s+(\S+)")


def load_design_topo(path: Path, fallback: list[dict] | None = None) -> tuple[list[dict], dict]:
    """An approved design in IBDM .topo form (e.g. UFM host /root/nscale_Compute.topo), as
    expected_topology rows. Switch ports are physical numbers P1..P144 (swNpM = 2(N-1)+M).

    Entries that cannot be right are not trusted: a switch link whose two ends disagree, or
    one host adapter listed on several leaves. Those ports, and ports the file does not
    cover, take the rule-based row from `fallback` (expected_topology.csv), marked as such."""
    import hashlib
    sw, gpu, node = {}, {}, None
    with open_scan(path) as handle:
        for line in handle:
            if not line.strip() or line.startswith("#"):
                continue
            if not line[0].isspace():
                m = TOPO_DESIGN_HEAD.match(line)
                node = m.group(2) if m else None
                continue
            m = TOPO_DESIGN_LINK.match(line)
            if not m or not node:
                continue
            port, kind, far, far_port = m.groups()
            here = (node, port_label(int(port)))
            if kind.startswith("Q"):
                fp = re.match(r"P?(\d+)$", far_port.split("/")[-1])
                if fp:
                    sw[here] = (far, port_label(int(fp.group(1))))
            else:
                gpu[here] = (far, far_port.split("/")[0])
    suspect = []
    pairs = {}
    for a, b in sw.items():
        if sw.get(b) != a:
            suspect.append({"port": list(a), "why": "the far end lists %s %s instead" % (b[0], sw.get(b, ("?", "?"))[1]) if b in sw else "the far end does not list this link"})
            continue
        if leaf_no(a[0]) is not None and SPINE.search(b[0]):
            pairs[a] = b
    by_host = collections.defaultdict(list)
    for port, (host, adapter) in gpu.items():
        by_host[(host, adapter)].append(port)
    bad_gpu = set()
    for (host, adapter), ports in by_host.items():
        if len(ports) > 1:
            for port in ports:
                bad_gpu.add(port)
            suspect.append({"port": [ports[0][0], ports[0][1]], "why": "%s %s is listed on %d leaves (%s); one adapter port can only reach one leaf"
                            % (host, adapter, len(ports), ", ".join(short(l) for l, _ in sorted(ports)))})
    rows, from_design, filled = [], 0, 0
    fb = {(r["a_device"], r["a_port"]): r for r in fallback or []}
    covered = set()
    for a, b in sorted(pairs.items()):
        rows.append({"link_type": "leaf-spine", "a_device": a[0], "a_port": a[1], "b_device": b[0], "b_port": b[1], "rule": "approved design"})
        covered.add(a)
        from_design += 1
    for port, (host, adapter) in sorted(gpu.items()):
        if port in bad_gpu:
            continue
        rows.append({"link_type": "leaf-gpu", "a_device": port[0], "a_port": port[1], "b_device": host, "b_port": adapter, "rule": "approved design"})
        covered.add(port)
        from_design += 1
    for key, row in fb.items():
        if key not in covered:
            rows.append(dict(row, rule="inferred rule (%s)" % ("design entry suspect" if key in bad_gpu or any(s_["port"] == list(key) for s_ in suspect) else "not in design file")))
            filled += 1
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    info = {"kind": "approved design", "file": path.name, "path": str(path), "sha256": digest,
            "saved_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
            "links_from_design": from_design, "filled_by_rules": filled, "suspect": suspect[:50], "suspect_count": len(suspect),
            "leaf_spine": len(pairs), "gpu_ports": len(gpu) - len(bad_gpu), "gpu_hosts": len({h for h, _ in gpu.values()})}
    return rows, info


def analyse(scan: Path, baseline: list[dict], expected: list[dict] | None = None, master: dict | None = None,
            ufm_compare: dict | None = None, reference_info: dict | None = None) -> dict:
    """Compare UFM's current cabling with the designed topology (or NetBox if no design is given).

    NetBox is reported next to each link (agrees with the design / records the current
    cabling / differs / missing) but is not used as the reference when a design exists.
    """
    lanes, meta = read_lanes(scan)
    stat = scan.stat()

    # ---- per switch port: who is on the other end, on how many planes ------------
    ports: dict[tuple[str, str], dict] = collections.defaultdict(lambda: {"peers": set(), "planes": set(), "logs": set(), "phys": set()})
    for a, b, state in lanes:
        for here, there in ((a, b), (b, a)):
            if here[0] != "SW":
                continue
            if there[0] == "SW" and there[1] == here[1]:
                continue  # a switch's own chip-to-chip links
            peer = ("SW", there[1], there[3]) if there[0] == "SW" else ("CA", there[1], "")
            item = ports[(here[1], here[3])]
            item["peers"].add(peer)
            item["planes"].add(here[2])
            item["logs"].add(state.get("LOG", "?"))
            item["phys"].add(state.get("PHY", "?") + "@" + state.get("SPD", "?"))

    def health(item: dict) -> str:
        if item["logs"] - {"ACT"}:
            return "init" if "INI" in item["logs"] or "ARM" in item["logs"] else "down"
        return "degraded" if len(item["planes"]) < PLANES else "ok"

    # ---- NetBox, indexed both ways -------------------------------------------------
    nb_switch: dict[tuple[str, str], tuple] = {}
    nb_gpu: dict[tuple[str, str], tuple] = {}
    switch_cables = []
    for row in baseline:
        a = (row["endpoint_a_device"], row["endpoint_a_port"])
        b = (row["endpoint_b_device"], row["endpoint_b_port"])
        if row["connection_type"] == "leaf-spine":
            leaf, spine = (a, b) if leaf_no(a[0]) else (b, a)
            switch_cables.append((row["netbox_cable_id"], leaf, spine))
            nb_switch[leaf] = (spine, row["netbox_cable_id"])
            nb_switch[spine] = (leaf, row["netbox_cable_id"])
        elif row["connection_type"] == "leaf-gpu-rdma":
            sw, gpu = (a, b) if "-swi-" in a[0] else (b, a)
            nb_gpu[sw] = (gpu[0], gpu[1], row["netbox_cable_id"])

    # ---- the reference: designed topology ---------------------------------------------
    exp_switch: list[tuple] = []
    exp_gpu: dict[tuple[str, str], tuple[str, str]] = {}
    for row in expected or []:
        ea, eb = (row["a_device"], row["a_port"]), (row["b_device"], row["b_port"])
        if row["link_type"] == "leaf-spine":
            exp_switch.append((ea, eb))
        elif row["link_type"] == "leaf-gpu":
            exp_gpu[ea] = eb
    reference = ((reference_info or {}).get("kind") or "inferred design") if exp_switch else "NetBox"
    if not exp_switch:
        exp_switch = [(leaf, spine) for cid, leaf, spine in switch_cables]
    exp_ends = {end for pair in exp_switch for end in pair}

    # ---- leaf <-> spine: expected (design) vs current (UFM), NetBox alongside ----------
    switch_findings, counts = [], collections.Counter()
    for leaf, spine in exp_switch:
        pl, ps = ports.get(leaf), ports.get(spine)
        if not pl and not ps:
            status, actual = "not-seen", []
        else:
            item = pl or ps
            peer_expected = ("SW",) + (spine if pl else leaf)
            if peer_expected in item["peers"]:
                status, actual = health(item), []
            else:
                status = "miscabled"
                actual = sorted({(p[1], p[2]) for p in (pl or {"peers": set()})["peers"] if p[0] == "SW"})
        nb = nb_switch.get(leaf)
        current = tuple(actual[0]) if actual else (spine if status != "not-seen" else None)
        if nb is None:
            nb_state = "missing"
        elif tuple(nb[0]) == tuple(spine):
            nb_state = "matches-expected"
        elif current and tuple(nb[0]) == current:
            nb_state = "matches-current"
        else:
            nb_state = "differs"
        m_far = master["switch"].get(tuple(leaf)) if master else None
        if master is None:
            m_state = None
        elif m_far is None:
            m_state = "missing"
        elif tuple(m_far) == tuple(spine):
            m_state = "matches-expected"
        elif current and tuple(m_far) == current:
            m_state = "matches-current"
        else:
            m_state = "differs"
        if m_state:
            counts["master-" + m_state] += 1
            if current and m_far and tuple(m_far) != current:
                counts["changed-since-master"] += 1
        counts["switch-" + status] += 1
        if nb_state != "matches-expected":
            counts["netbox-differs"] += 1
        changed_ok = status == "ok" and m_state not in (None, "matches-expected")
        if status != "ok" or nb_state != "matches-expected" or changed_ok:
            item = pl or ps or {"planes": set(), "logs": set()}
            switch_findings.append({"status": status if status != "ok" else ("changed-since-master" if changed_ok and nb_state == "matches-expected" else "netbox-differs"),
                                    "cable": nb[1] if nb else "", "master": list(m_far) if m_far else None, "master_state": m_state,
                                    "leaf": list(leaf), "spine": list(spine), "leaf_actual": actual,
                                    "spine_actual": sorted({(p[1], p[2]) for p in (ps or {"peers": set()})["peers"] if p[0] == "SW"}),
                                    "netbox": list(nb[0]) if nb else None, "netbox_state": nb_state,
                                    "planes": len(item["planes"]), "state": "/".join(sorted(item["logs"])) or "-"})
    # pair crossed cables on the same leaf into swaps
    by_leaf = collections.defaultdict(list)
    for f in switch_findings:
        if f["status"] == "miscabled":
            by_leaf[f["leaf"][0]].append(f)
    swap_no = 0
    for leaf, items in sorted(by_leaf.items()):
        for f in items:
            if f.get("swap"):
                continue
            for g in items:
                if g is f or g.get("swap") or not f["leaf_actual"] or not g["leaf_actual"]:
                    continue
                if f["leaf_actual"][0][0] == g["spine"][0] and g["leaf_actual"][0][0] == f["spine"][0] and f["leaf"][1][-2:] == g["leaf"][1][-2:]:
                    swap_no += 1
                    f["swap"] = g["swap"] = swap_no
                    break
    # ---- connectivity graph: how many cables join each leaf and spine -----------------
    # A miscabling that keeps this graph (only port positions differ) does not change
    # routing or bandwidth; one that changes it does.
    exp_adj = collections.Counter((leaf[0], spine[0]) for leaf, spine in exp_switch)
    cur_adj = collections.Counter()
    leaf_seen, spine_seen = collections.Counter(), collections.Counter()
    for (dev, label), item in ports.items():
        if leaf_no(dev) is None:
            continue
        for p in item["peers"]:
            if p[0] == "SW" and SPINE.search(p[1]):
                cur_adj[(dev, p[1])] += 1
                leaf_seen[dev] += 1
                spine_seen[p[1]] += 1
    leaf_exp = collections.Counter(l for l, _ in exp_adj.elements())
    spine_exp = collections.Counter(sp for _, sp in exp_adj.elements())
    deviations = {k: [exp_adj.get(k, 0), cur_adj.get(k, 0)] for k in set(exp_adj) | set(cur_adj)
                  if exp_adj.get(k, 0) != cur_adj.get(k, 0) and cur_adj.get(k, 0) > exp_adj.get(k, 0)}
    deviations.update({k: [exp_adj[k], cur_adj.get(k, 0)] for k in exp_adj
                       if cur_adj.get(k, 0) < exp_adj[k] and any((k[0], s2) in deviations for s2 in spine_exp)})
    for f in switch_findings:
        if f["status"] != "miscabled":
            continue
        if len(f["leaf_actual"]) > 1:
            f["impact"] = "plane-split"
        elif any(exp_adj.get((f["leaf"][0], sp), 0) != cur_adj.get((f["leaf"][0], sp), 0) for sp in [f["spine"][0]] + [a[0] for a in f["leaf_actual"]]):
            f["impact"] = "topology"
        elif f["leaf_actual"] and not SPINE.search(f["leaf_actual"][0][0]):
            f["impact"] = "topology"
        else:
            f["impact"] = "port-swap"
    uplinks = {
        "leaves": {l: [leaf_exp[l], leaf_seen.get(l, 0)] for l in sorted(leaf_exp, key=lambda x: leaf_no(x) or 0)},
        "spines": {sp: [spine_exp[sp], spine_seen.get(sp, 0)] for sp in sorted(spine_exp, key=lambda x: int(SPINE.search(x).group(1)))},
        "uneven": sorted([[l, sp, e, c] for (l, sp), (e, c) in deviations.items()]),
    }
    undocumented_switch = []  # links UFM sees between switches that the design does not have
    for (dev, label), item in ports.items():
        for p in item["peers"]:
            if p[0] == "SW" and (dev, label) not in exp_ends and (p[1], p[2]) not in exp_ends and dev < p[1]:
                undocumented_switch.append({"a": [dev, label], "b": [p[1], p[2]], "planes": len(item["planes"]), "state": health(item)})

    # ---- GPU trays as UFM sees them ---------------------------------------------------
    trays: dict[str, list] = collections.defaultdict(list)
    unnamed, ufm_links = [], []
    for (dev, label), item in ports.items():
        leaf = leaf_no(dev)
        cas = [p[1] for p in item["peers"] if p[0] == "CA"]
        if not cas or leaf is None:
            continue
        for desc in cas:
            m = TRAY.match(desc)
            if UFM_HOST.search(desc):
                ufm_links.append({"leaf": dev, "port": label, "adapter": desc, "state": health(item), "planes": len(item["planes"])})
            elif "Aggregation Node" in desc:
                continue
            elif m:
                trays[m.group(1)].append({"leaf": leaf, "port": label, "hca": m.group(2), "planes": len(item["planes"]), "state": health(item),
                                           "nb": nb_gpu.get((dev, label))})
            else:
                su, pod, rail = su_of_leaf(leaf)
                known = master["gpu"].get((dev, label)) if master else None
                unnamed.append({"su": su, "rail": rail, "leaf": leaf, "port": label, "slot": slot_of(label), "adapter": desc,
                                "state": health(item), "nb": nb_gpu.get((dev, label)),
                                "master_host": known[0] if known and "-phy-" in known[0] else ""})
    # learn tray -> NetBox host, and rail -> adapter, by majority
    hca_votes = collections.defaultdict(collections.Counter)
    for code, ads in trays.items():
        for ad in ads:
            hca_votes[su_of_leaf(ad["leaf"])[2]][ad["hca"]] += 1
    rail_hca = {rail: votes.most_common(1)[0][0] for rail, votes in hca_votes.items()}
    rdma_votes = collections.defaultdict(collections.Counter)
    for code, ads in trays.items():
        for ad in ads:
            if ad["nb"]:
                rdma_votes[ad["nb"][1]][ad["hca"]] += 1
    rdma_hca = {rdma: votes.most_common(1)[0][0] for rdma, votes in rdma_votes.items()}
    hca_rdma = {hca: rdma for rdma, hca in rdma_hca.items()}

    sus = {su: {"su": su, "pod": (su - 1) // 4 + 1, "leaves": su_leaves(su), "trays": [], "unnamed": []} for su in range(1, 17)}
    gpu_counts = collections.Counter()
    host_of_tray = {}
    for code, ads in trays.items():
        hosts = collections.Counter(ad["nb"][0] for ad in ads if ad["nb"])
        host = hosts.most_common(1)[0][0] if hosts else ""
        host_of_tray[code] = host
        m_hosts = collections.Counter(master["gpu"][("sys1-ice2-p-swi-bel%d" % ad["leaf"], ad["port"])][0] for ad in ads
                                      if master and ("sys1-ice2-p-swi-bel%d" % ad["leaf"], ad["port"]) in master["gpu"])
        master_host = m_hosts.most_common(1)[0][0] if m_hosts else ""
        issues = []
        leaves = sorted({ad["leaf"] for ad in ads})
        su_set = {su_of_leaf(l)[0] for l in leaves}
        slots = {slot_of(ad["port"]) for ad in ads}
        su = collections.Counter(su_of_leaf(ad["leaf"])[0] for ad in ads).most_common(1)[0][0]
        slot = collections.Counter(slot_of(ad["port"]) for ad in ads).most_common(1)[0][0]
        if len(su_set) > 1:
            issues.append("adapters on leaves of different SUs")
        if len(slots) > 1:
            issues.append("not on the same port of all four leaves")
        for ad in ads:
            rail = su_of_leaf(ad["leaf"])[2]
            design = exp_gpu.get(("sys1-ice2-p-swi-bel%d" % ad["leaf"], ad["port"]))
            want = design[1] if design else rail_hca.get(rail)
            if design is None and exp_gpu:
                issues.append("BEL%d %s is not a GPU port in the design" % (ad["leaf"], ad["port"]))
            elif want and ad["hca"] != want:
                issues.append("%s on rail %d leaf BEL%d (design expects %s)" % (ad["hca"], rail, ad["leaf"], want))
        rails_seen = {su_of_leaf(ad["leaf"])[2] for ad in ads}
        missing_rails = [r for r in range(1, 5) if r not in rails_seen]
        if missing_rails:
            issues.append("no link on rail %s" % ", ".join(map(str, missing_rails)))
        documented = [ad for ad in ads if ad["nb"]]
        for ad in documented:
            if ad["nb"][0] != host:
                issues.append("NetBox says BEL%d %s goes to %s" % (ad["leaf"], ad["port"], short(ad["nb"][0])))
            if rdma_hca.get(ad["nb"][1]) and rdma_hca[ad["nb"][1]] != ad["hca"]:
                issues.append("NetBox %s on BEL%d is %s in UFM" % (ad["nb"][1], ad["leaf"], ad["hca"]))
        for ad in ads:
            if ad["state"] != "ok":
                issues.append("BEL%d %s %s (%d/4 planes)" % (ad["leaf"], ad["port"], ad["state"], ad["planes"]))
        if not documented:
            doc = "missing"
        elif len(documented) < len(ads):
            doc = "partial"
        else:
            doc = "documented"
        if issues and any(not i.startswith("no link") and "planes" not in i for i in issues):
            status = "error"
        elif issues:
            status = "incomplete"
        else:
            status = "ok"
        gpu_counts["trays-" + doc] += 1
        gpu_counts["trays-" + status] += 1
        gpu_counts["adapters"] += len(ads)
        gpu_counts["adapters-documented"] += len(documented)
        sus[su]["trays"].append({
            "slot": slot, "code": code, "host": host, "doc": doc, "status": status, "issues": issues,
            "master_host": master_host if "-phy-" in master_host else "", "in_master": bool(m_hosts),
            "design_host": next((exp_gpu[k][0] for k in (("sys1-ice2-p-swi-bel%d" % ad["leaf"], ad["port"]) for ad in ads)
                                 if k in exp_gpu and not exp_gpu[k][0].startswith("SU")), ""),
            "adapters": sorted(([su_of_leaf(ad["leaf"])[2], ad["leaf"], ad["port"], ad["hca"], ad["planes"], ad["state"],
                                 ad["nb"][2] if ad["nb"] else "", hca_rdma.get(ad["hca"], "")] for ad in ads)),
        })
    for item in unnamed:
        sus[item["su"]]["unnamed"].append([item["slot"], item["rail"], item["leaf"], item["port"], item["adapter"], item["state"], item["master_host"]])
    for su in sus.values():
        su["trays"].sort(key=lambda t: (t["slot"] if t["slot"] is not None else 99, t["code"]))
        su["unnamed"].sort(key=lambda u: (u[0] if u[0] is not None else 99, u[1]))
    gpu_not_seen = []
    seen_ports = {(f"sys1-ice2-p-swi-bel{ad['leaf']}", ad["port"]) for ads in trays.values() for ad in ads}
    seen_ports |= {(f"sys1-ice2-p-swi-bel{u['leaf']}", u["port"]) for u in unnamed}
    for (dev, label), (host, rdma, cid) in nb_gpu.items():
        if (dev, label) not in seen_ports:
            gpu_not_seen.append({"cable": cid, "leaf": dev, "port": label, "host": host, "rdma": rdma})
    gpu_not_seen.sort(key=lambda x: (x["host"], x["rdma"]))

    summary = {
        "reference": reference,
        "switch_cables": len(exp_switch), "netbox_switch_cables": len(switch_cables),
        "switch_netbox_differs": counts["netbox-differs"],
        "switch_ok": counts["switch-ok"], "switch_miscabled": counts["switch-miscabled"],
        "switch_not_seen": counts["switch-not-seen"], "switch_init": counts["switch-init"],
        "switch_degraded": counts["switch-degraded"] + counts["switch-down"], "swaps": swap_no,
        "switch_undocumented": len(undocumented_switch),
        "trays": len(trays), "tray_slots": 16 * 72,
        "trays_documented": gpu_counts["trays-documented"], "trays_partial": gpu_counts["trays-partial"],
        "trays_missing": gpu_counts["trays-missing"],
        "trays_ok": gpu_counts["trays-ok"], "trays_error": gpu_counts["trays-error"], "trays_incomplete": gpu_counts["trays-incomplete"],
        "gpu_links": gpu_counts["adapters"], "gpu_links_documented": gpu_counts["adapters-documented"],
        "gpu_links_undocumented": gpu_counts["adapters"] - gpu_counts["adapters-documented"],
        "gpu_not_seen": len(gpu_not_seen), "unnamed_adapters": len(unnamed),
        "netbox_gpu_cables": len(nb_gpu),
    }
    master_summary = None
    if master:
        seen_now = {(f"sys1-ice2-p-swi-bel{ad['leaf']}", ad["port"]) for ads in trays.values() for ad in ads} | {(f"sys1-ice2-p-swi-bel{u['leaf']}", u["port"]) for u in unnamed}
        master_summary = {
            "file": master["file"], "saved_at": master["saved_at"], "lanes": master["lanes"],
            "switch_links": len(master["switch"]), "gpu_ports": len(master["gpu"]), "gpu_hosts": len(master["hosts"]),
            "agree_all_three": counts["master-matches-expected"] - sum(1 for f in switch_findings if f["status"] in ("miscabled", "not-seen") and f.get("master_state") == "matches-expected"),
            "switch_matches_design": counts["master-matches-expected"], "switch_matches_current_not_design": counts["master-matches-current"],
            "switch_differs": counts["master-differs"], "switch_missing": counts["master-missing"],
            "changed_since_master": counts["changed-since-master"],
            "gpu_ports_gone": sum(1 for k in master["gpu"] if k not in seen_now),
            "named_unnamed": sum(1 for u in unnamed if u["master_host"]),
        }
    return {
        "master": master_summary, "ufm_compare": ufm_compare,
        "reference": reference_info or ({"kind": "inferred design", "file": "expected_topology.csv"} if expected else {"kind": "NetBox"}),
        "source": {"file": str(scan), "scanned_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                   "bytes": stat.st_size, "lanes": len(lanes), "tool": meta["tool"], "unparsed": meta["unparsed"]},
        "summary": summary,
        "switch_findings": sorted(switch_findings, key=lambda f: ({"miscabled": 0, "not-seen": 1, "down": 2, "init": 3, "degraded": 4, "netbox-differs": 5, "changed-since-master": 6}.get(f["status"], 9), f["leaf"])),
        "switch_undocumented": sorted(undocumented_switch, key=lambda x: x["a"]),
        "sus": [sus[k] for k in sorted(sus)],
        "gpu_not_seen": gpu_not_seen,
        "ufm_links": sorted(ufm_links, key=lambda x: (x["leaf"], x["port"])),
        "rail_adapter": {str(k): v for k, v in sorted(rail_hca.items())},
        "uplinks": uplinks,
        "gpu_ports_seen": sorted([f"sys1-ice2-p-swi-bel{ad['leaf']}", ad["port"], code] for code, ads in trays.items() for ad in ads),
        "rdma_adapter": dict(sorted(rdma_hca.items())),
    }


def findings_csv(report: dict) -> str:
    """Every non-OK observation, one row each; suitable for a ticket or a spreadsheet."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["finding", "netbox_cable_id", "switch", "port", "expected_far_end", "current_far_end_ufm", "planes_up_of_4", "state",
                "expected_connection", "current_connection_ufm", "netbox_connection", "netbox_vs_expected", "fix_or_note",
                "master_connection_ufm", "master_vs_expected"])
    end = lambda e: " ".join(e) if e else ""
    for f in report["switch_findings"]:
        current = f["leaf_actual"][0] if f["leaf_actual"] else None
        if f["status"] == "miscabled":
            note = "Re-patch the cable in %s from %s to %s (expected by the design)" % (end(f["leaf"]), end(current), end(f["spine"]))
        elif f["status"] == "netbox-differs":
            note = "Cabling matches the design; correct NetBox%s" % ((" #" + f["cable"]) if f["cable"] else "")
        else:
            note = ""
        nb = f.get("netbox")
        w.writerow([f["status"], f["cable"], f["leaf"][0], f["leaf"][1], end(f["spine"]),
                    "; ".join(" ".join(x) for x in f["leaf_actual"]), f["planes"], f["state"],
                    "%s <-> %s" % (end(f["leaf"]), end(f["spine"])), ("%s <-> %s" % (end(f["leaf"]), end(current))) if current else "",
                    ("%s <-> %s" % (end(f["leaf"]), end(nb))) if nb else "", f.get("netbox_state", ""), note,
                    ("%s <-> %s" % (end(f["leaf"]), end(f["master"]))) if f.get("master") else "", f.get("master_state") or ""])
    for x in report["switch_undocumented"]:
        w.writerow(["switch-link-not-in-netbox", "", x["a"][0], x["a"][1], "", " ".join(x["b"]), x["planes"], x["state"], "", "%s <-> %s" % (end(x["a"]), end(x["b"])), "", "", "not in the design topology", "", ""])
    for su in report["sus"]:
        for t in su["trays"]:
            for rail, leaf, port, hca, planes, state, cid, rdma in t["adapters"]:
                if t["status"] == "ok" and cid:
                    continue
                finding = "gpu-link-not-in-netbox" if not cid else "gpu-" + t["status"]
                expected = "SU%d slot %d %s" % (su["su"], t["slot"] + 1, "mlx5_%d" % (rail - 1)) if t["slot"] is not None else ""
                w.writerow([finding, cid, "sys1-ice2-p-swi-bel%d" % leaf, port, expected, "%s %s" % (t["code"], hca), planes, state,
                            ("sys1-ice2-p-swi-bel%d %s <-> %s" % (leaf, port, expected)) if expected else "",
                            "sys1-ice2-p-swi-bel%d %s <-> %s %s" % (leaf, port, t["code"], hca),
                            ("sys1-ice2-p-swi-bel%d %s <-> %s %s" % (leaf, port, t["host"], rdma)) if cid else "",
                            "documented" if cid else "missing", "; ".join(t["issues"]),
                            ("master host " + t["master_host"]) if t.get("master_host") else "", "in master" if t.get("in_master") else "not in master"])
        for slot, rail, leaf, port, adapter, state, master_host in su["unnamed"]:
            w.writerow(["gpu-adapter-unnamed", "", "sys1-ice2-p-swi-bel%d" % leaf, port, "", adapter, "", state, "",
                        "sys1-ice2-p-swi-bel%d %s <-> %s" % (leaf, port, adapter), "", "", "adapter has no node description; tray cannot be identified",
                        ("master host " + master_host) if master_host else "", ""])
    for x in report["gpu_not_seen"]:
        w.writerow(["gpu-not-seen-by-ufm", x["cable"], x["leaf"], x["port"], x["host"] + " " + x["rdma"], "", 0, "",
                    "%s %s <-> %s %s" % (x["leaf"], x["port"], x["host"], x["rdma"]), "", "%s %s <-> %s %s" % (x["leaf"], x["port"], x["host"], x["rdma"]), "", "no link on this port in UFM", "", ""])
    return out.getvalue()


def netbox_import_csv(report: dict) -> str:
    """Cables UFM sees but NetBox lacks, in NetBox's cable bulk-import columns.

    Trays not yet linked to a NetBox device keep `side_b_device` empty and carry the
    UFM tray code in `label`, so the hostname can be filled in before importing.
    """
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["side_a_device", "side_a_type", "side_a_name", "side_b_device", "side_b_type", "side_b_name", "status", "label"])
    for su in report["sus"]:
        for t in su["trays"]:
            for rail, leaf, port, hca, planes, state, cid, rdma in t["adapters"]:
                if cid:
                    continue
                w.writerow(["sys1-ice2-p-swi-bel%d" % leaf, "dcim.interface", port, t["host"], "dcim.interface",
                            rdma or ("RDMA%d" % rail), "connected", t["code"]])
    return out.getvalue()


if __name__ == "__main__":
    import argparse
    import json

    here = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Read-only comparison of UFM cabling against the approved design topology."
    )
    parser.add_argument("scan", type=Path, help="UFM ibdiagnet2.lst file (plain or .gz)")
    parser.add_argument("connections", nargs="?", type=Path,
                        default=here / "assets" / "connections.csv",
                        help="NetBox cable export (default: assets/connections.csv)")
    parser.add_argument("--design-topo", type=Path,
                        default=here / "local-inputs" / "ufm" / "nscale_Compute.topo",
                        help="approved IBDM design topology; used when it exists")
    parser.add_argument("--expected-topology", type=Path,
                        default=here / "assets" / "expected_topology.csv",
                        help="rule-based fallback topology")
    parser.add_argument("--ufm-master", type=Path,
                        default=here / "local-inputs" / "ufm" / "master.topo.gz",
                        help="optional UFM master topology")
    parser.add_argument("--ufm-report", type=Path,
                        default=here / "local-inputs" / "ufm" / "topology-compare.json.gz",
                        help="optional UFM Topology Compare report")
    args = parser.parse_args()

    rules = load_expected(args.expected_topology)
    if args.design_topo.is_file():
        expected, reference = load_design_topo(args.design_topo, rules)
    else:
        expected = rules
        reference = {"kind": "inferred design", "file": args.expected_topology.name}
    master = read_master(args.ufm_master) if args.ufm_master.is_file() else None
    ufm_report = read_ufm_compare(args.ufm_report) if args.ufm_report.is_file() else None
    report = analyse(args.scan, load_baseline(args.connections), expected, master, ufm_report, reference)
    print(json.dumps({
        "source": report["source"],
        "reference": report["reference"],
        "summary": report["summary"],
        "miscabled": [f for f in report["switch_findings"] if f["status"] == "miscabled"],
    }, indent=1))
