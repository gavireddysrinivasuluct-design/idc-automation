"""Generic backend fabric model: the ICE2-style spine · leaf · rail view for any site.

Built only from what nsr can read, so a new InfiniBand site needs a regions.json entry
and nothing else (no jump host, switch login or UFM password):

* Topology (expected): NetBox devices and cabling through `nsr netbox devices` and
  `nsr netbox connections`, saved as a snapshot under cache/ and refreshed on request.
* Live state (actual): UFM telemetry per switch port, blackbox ping per switch, through
  `nsr grafana query`, cached like every other live answer.

Structure is inferred, not hard-coded:
* rails: a GPU node's leaf-facing NICs, in natural name order, are rails 1..R;
  a leaf's rail is the rail most of its GPU links arrive on;
* scalable units (SU): GPU nodes that use exactly the same set of leaves;
* UFM servers and anything with fewer than half the rails are "other endpoints".

UFM numbers ports by GUID_port; NetBox names them (`swNpM`, `IB1/17/2`, `17`). The map
between the two is a convention, so how many cables it matched is reported, never hidden.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from nsr_client import Nsr, NsrError

SPINE, LEAF = "be-spine-switch", "be-leaf-switch"
UFM_ACTIVE = 4


def natural(text: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text or "")]


def short_name(name: str) -> str:
    """sys2-osl1-p-ifs-bel3 -> bel3, sys1-ice2-p-swi-bes12 -> bes12, ifs398 -> ifs398."""
    match = re.search(r"(be[lsc]\d+)$", name or "")
    if match:
        return match.group(1)
    return (name or "").split("-p-")[-1] if "-p-" in (name or "") else (name or "")


def port_number(interface: str, scheme: str = "auto") -> int | None:
    """NetBox interface name -> UFM port number (the part after '_' in port_id)."""
    name = (interface or "").strip()
    if scheme in ("auto", "swNpM"):
        m = re.search(r"sw(\d+)p(\d+)$", name)
        if m:
            return 2 * (int(m.group(1)) - 1) + int(m.group(2))
        if scheme == "swNpM":
            return None
    if scheme in ("auto", "cage/split"):
        m = re.search(r"(\d+)/(\d+)/(\d+)$", name)
        if m:
            return 2 * (int(m.group(2)) - 1) + int(m.group(3))
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else None


def _device_fields(device: dict) -> dict:
    return {
        "name": device.get("name"),
        "short": short_name(device.get("name") or ""),
        "model": (device.get("device_type") or {}).get("model"),
        "ip": ((device.get("primary_ip4") or device.get("primary_ip") or {}).get("address") or "").split("/")[0] or None,
        "status": (device.get("status") or {}).get("value"),
        "cluster": (device.get("cluster") or {}).get("name"),
        "rack": (device.get("rack") or {}).get("name"),
    }


# -- NetBox snapshot ----------------------------------------------------------

def pull_netbox(nsr: Nsr, fabric: dict, progress=lambda text: None) -> dict:
    """Read the fabric's switches and every leaf cable from NetBox (read-only)."""
    site, cluster = fabric["netbox_site"], fabric.get("cluster")
    devices = {}
    for role in (SPINE, LEAF):
        progress("NetBox: %s at %s" % (role, site))
        rows = nsr.run(("netbox", "devices"), "--site", site, "--role", role, "--limit", "2000", timeout=180) or []
        for device in rows:
            info = _device_fields(device)
            if cluster and info["cluster"] and info["cluster"] != cluster:
                continue
            info["role"] = role
            devices[info["name"]] = info
    leaves = [name for name, d in devices.items() if d["role"] == LEAF]
    if not leaves:
        raise NsrError("NetBox has no %s at site %s%s" % (LEAF, site, " cluster " + cluster if cluster else ""),
                       "Check netbox_site / cluster for this fabric in regions.json.")
    progress("NetBox: cabling of %d leaves" % len(leaves))
    args = ["--site", site, "--role", LEAF, "--max-devices", str(max(25, len(leaves) * 2 + 10)), "--limit", "800"]
    if not fabric.get("fiber_graph"):
        args.append("--no-fiber-graph")
    rows = nsr.run(("netbox", "connections"), *args, timeout=600) or []
    keep = set(leaves)
    cables = [{k: r.get(k) for k in ("device", "interface", "remote_device", "remote_interface", "state", "cable")}
              for r in rows if r.get("device") in keep and r.get("remote_device")]
    return {"site": site, "cluster": cluster, "collected_at": time.time(),
            "devices": devices, "cables": cables}


# -- structure ----------------------------------------------------------------

def build_topology(snapshot: dict, scheme: str = "auto") -> dict:
    devices, cables = snapshot["devices"], snapshot["cables"]
    roles = {name: d["role"] for name, d in devices.items()}
    spines = sorted((n for n, r in roles.items() if r == SPINE), key=natural)
    leaves = sorted((n for n, r in roles.items() if r == LEAF), key=natural)

    uplinks, endpoint_links = [], defaultdict(list)
    for c in cables:
        leaf, far = c["device"], c["remote_device"]
        if roles.get(far) == SPINE:
            uplinks.append({"leaf": leaf, "leaf_port": c["interface"], "spine": far,
                            "spine_port": c["remote_interface"], "cable": c["cable"], "state": c["state"]})
        elif roles.get(far) == LEAF:
            uplinks.append({"leaf": leaf, "leaf_port": c["interface"], "spine": far, "spine_port": c["remote_interface"],
                            "cable": c["cable"], "state": c["state"], "leaf_to_leaf": True})
        else:
            endpoint_links[far].append({"leaf": leaf, "leaf_port": c["interface"], "iface": c["remote_interface"] or "?",
                                        "cable": c["cable"], "state": c["state"]})

    # Rails: the fleet-wide NIC names (ib0..ib7, mlx5_0..3) in natural order. The rail
    # count is the most common number of NICs per node, and the rail names are the most
    # used NIC names, so a node with a NIC missing keeps the right rail for the others.
    counts = Counter(len({l["iface"] for l in links}) for links in endpoint_links.values())
    rails = max((n for n, k in counts.items() if k == max(counts.values())), default=0) if counts else 0
    nic_use = Counter(l["iface"] for links in endpoint_links.values() if len(links) >= max(2, rails // 2) for l in links)
    rail_of = {name: i + 1 for i, name in enumerate(sorted((n for n, _ in nic_use.most_common(rails)), key=natural))}
    gpu_nodes, others = {}, {}
    for node, links in endpoint_links.items():
        ifaces = sorted({l["iface"] for l in links}, key=natural)
        is_gpu = rails and len(ifaces) >= max(2, rails // 2) and "ufm" not in node.lower()
        rank = {iface: rail_of.get(iface, i + 1) for i, iface in enumerate(ifaces)}
        for l in links:
            l["rail"] = rank[l["iface"]] if is_gpu else None
        (gpu_nodes if is_gpu else others)[node] = sorted(links, key=lambda l: (l["rail"] or 0, natural(l["leaf"])))

    # A leaf's rail is where most of its GPU links come from.
    by_leaf = defaultdict(Counter)
    for links in gpu_nodes.values():
        for l in links:
            by_leaf[l["leaf"]][l["rail"]] += 1
    leaf_rail = {leaf: c.most_common(1)[0][0] for leaf, c in by_leaf.items()}

    # SUs: nodes sharing the same leaf set. A small group that shares most of its leaves
    # with a bigger one (a node missing a rail, or one NIC on the wrong leaf) joins it;
    # the design checks report those nodes. Ordered by first leaf.
    groups = defaultdict(list)
    for node, links in gpu_nodes.items():
        groups[tuple(sorted({l["leaf"] for l in links}, key=natural))].append(node)
    merged: list[tuple[tuple, list]] = []
    for leafset, nodes in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        home = max(merged, key=lambda m: len(set(m[0]) & set(leafset)), default=None)
        if home and len(set(home[0]) & set(leafset)) * 2 >= len(leafset) and len(nodes) < len(home[1]):
            home[1].extend(nodes)
        else:
            merged.append((leafset, list(nodes)))
    su_list = sorted(merged, key=lambda kv: natural(kv[0][0]))
    sus, node_su = [], {}
    for index, (leafset, nodes) in enumerate(su_list, 1):
        nodes.sort(key=natural)
        rail_leaves = {}
        for leaf in leafset:
            rail_leaves.setdefault(leaf_rail.get(leaf), []).append(leaf)
        sus.append({"id": index, "name": "SU%d" % index, "leaves": list(leafset),
                    "rail_leaves": {str(k): v for k, v in rail_leaves.items() if k},
                    "nodes": nodes})
        for node in nodes:
            node_su[node] = index

    up_by_leaf = Counter(u["leaf"] for u in uplinks)
    spines_by_leaf = defaultdict(set)
    for u in uplinks:
        spines_by_leaf[u["leaf"]].add(u["spine"])
    down_by_leaf = Counter(l["leaf"] for links in gpu_nodes.values() for l in links)
    leaf_su = {}
    for su in sus:
        for leaf in su["leaves"]:
            leaf_su.setdefault(leaf, su["id"])

    def switch(name):
        return {**devices[name], "short": short_name(name)}

    topo = {
        "rails": rails,
        "spines": [{**switch(s), "leaves": sum(1 for u in uplinks if u["spine"] == s)} for s in spines],
        "leaves": [{**switch(l), "rail": leaf_rail.get(l), "su": leaf_su.get(l), "uplinks": up_by_leaf[l],
                    "spines": len(spines_by_leaf[l]), "downlinks": down_by_leaf[l]} for l in leaves],
        "uplinks": uplinks,
        "sus": sus,
        "nodes": [{"name": n, "short": short_name(n), "su": node_su.get(n), "links": gpu_nodes[n]}
                  for n in sorted(gpu_nodes, key=natural)],
        "others": [{"name": n, "links": others[n]} for n in sorted(others, key=natural)],
    }
    for link in topo["uplinks"]:
        link["leaf_portnum"] = port_number(link["leaf_port"], scheme)
        link["spine_portnum"] = port_number(link["spine_port"], scheme)
    for node in topo["nodes"]:
        for link in node["links"]:
            link["leaf_portnum"] = port_number(link["leaf_port"], scheme)
    topo["checks"] = design_checks(topo)
    return topo


def design_checks(t: dict) -> list[dict]:
    out = []
    spines, leaves, rails = t["spines"], t["leaves"], t["rails"]
    pairs = Counter((u["leaf"], u["spine"]) for u in t["uplinks"] if not u.get("leaf_to_leaf"))
    per_pair = Counter(pairs.values())
    missing = len(leaves) * len(spines) - len(pairs) if spines else 0
    if spines and leaves:
        if not missing and len(per_pair) == 1:
            n = next(iter(per_pair))
            out.append({"level": "ok", "text": "Full mesh: every one of the %d leaves has %d cable(s) to each of the %d spines "
                        "(%s leaf–spine cables)." % (len(leaves), n, len(spines), format(len(t["uplinks"]), ","))})
        else:
            out.append({"level": "bad" if missing else "warn",
                        "text": "Leaf–spine mesh is uneven: %d leaf–spine pairs have no cable; cables per pair: %s." % (
                            missing, ", ".join("%d×%d" % (k, v) for k, v in sorted(per_pair.items())))})
    l2l = [u for u in t["uplinks"] if u.get("leaf_to_leaf")]
    if l2l:
        out.append({"level": "bad", "text": "%d leaf-to-leaf cable(s) recorded: not part of a spine/leaf design." % len(l2l)})
    if rails:
        short = [n["name"] for n in t["nodes"] if len({l["rail"] for l in n["links"]}) < rails]
        out.append({"level": "warn" if short else "ok",
                    "text": ("%d GPU node(s) cabled on fewer than %d rails: %s" % (len(short), rails, ", ".join(short[:8]) + (" …" if len(short) > 8 else "")))
                    if short else "Every GPU node is cabled on all %d rails." % rails})
        wrong = []
        leaf_rail = {l["name"]: l["rail"] for l in t["leaves"]}
        for n in t["nodes"]:
            for link in n["links"]:
                if leaf_rail.get(link["leaf"]) not in (None, link["rail"]):
                    wrong.append("%s %s→%s" % (n["short"], link["iface"], short_name(link["leaf"])))
        out.append({"level": "bad" if wrong else "ok",
                    "text": ("%d GPU link(s) land on a leaf of another rail: %s" % (len(wrong), ", ".join(wrong[:6]) + (" …" if len(wrong) > 6 else "")))
                    if wrong else "Rail-aligned: every NIC lands on a leaf of its own rail."})
        mixed = [n["short"] for n in t["nodes"] if len({l["leaf_port"] for l in n["links"]}) > 1]
        if t["nodes"]:
            out.append({"level": "info" if mixed else "ok",
                        "text": ("%d node(s) use different leaf ports on different rails (slot numbering differs per rail)." % len(mixed))
                        if mixed else "Each node uses the same leaf port on every rail."})
    states = Counter(u["state"] for u in t["uplinks"]) + Counter(l["state"] for n in t["nodes"] for l in n["links"])
    odd = {k: v for k, v in states.items() if k != "connected"}
    if odd:
        out.append({"level": "info", "text": "NetBox cable states other than connected: %s." % ", ".join("%s %d" % kv for kv in sorted(odd.items()))})
    uplinks_only = [l["short"] for l in t["leaves"] if not l["downlinks"]]
    if uplinks_only:
        out.append({"level": "info", "text": "%d leaf/leaves have spine uplinks but no GPU cables in NetBox: %s." % (
            len(uplinks_only), ", ".join(uplinks_only[:10]) + (" …" if len(uplinks_only) > 10 else ""))})
    if t["others"]:
        out.append({"level": "info", "text": "Other endpoints on the leaves: %s." % ", ".join(
            "%s (%d)" % (o["name"], len(o["links"])) for o in t["others"][:6])})
    return out


# -- live overlay (UFM telemetry + ping) ---------------------------------------

def live_overlay(nsr: Nsr, topo: dict, ufm_selector: str | None, ufm_ds: str | None,
                 ping_selector: str | None, mimir: str) -> dict:
    out = {"collected_at": time.time(), "switches": {}, "ports": {}, "errors": []}
    names = {s["name"] for s in topo["spines"]} | {l["name"] for l in topo["leaves"]}

    def q(promql, ds):
        try:
            return nsr.query(promql, ds)
        except NsrError as error:
            out["errors"].append({"query": promql[:160], "error": str(error), "hint": error.hint})
            return None

    if ufm_selector:
        state = q("last_over_time(logical_state{%s}[6m])" % ufm_selector, ufm_ds)
        flaps = q("sum by (device_name, port_id) (increase(LinkDownedCounterExtended{%s}[15m])) > 0" % ufm_selector, ufm_ds)
        errs = q('sum by (device_name, port_id) (increase({__name__=~"SymbolErrorCounterExtended|PortRcvErrorsExtended",%s}[15m])) > 0'
                 % ufm_selector, ufm_ds)
        out["ufm"] = state is not None and bool(state)
        for rows, key in ((state, "state"), (flaps, "linkdown"), (errs, "errors")):
            for row in rows or []:
                m = row.get("metric") or {}
                dev, pid = m.get("device_name"), m.get("port_id") or ""
                try:
                    value = float(row["value"][1])
                    port = int(pid.rsplit("_", 1)[-1])
                except (KeyError, ValueError, IndexError, TypeError):
                    continue
                out["ports"].setdefault("%s|%d" % (dev, port), {})[key] = value
                sw = out["switches"].setdefault(dev, {"ports": 0, "nonactive": 0, "linkdown": 0, "errors": 0})
                if key == "state":
                    sw["ports"] += 1
                    sw["nonactive"] += int(value != UFM_ACTIVE)
                else:
                    sw[key] += 1
    if ping_selector:
        rows = q("probe_success{%s}" % ping_selector, mimir) or []
        for row in rows:
            m = row.get("metric") or {}
            for key in ("device_name", "host_name", "name", "instance", "target"):
                if m.get(key) in names:
                    out["switches"].setdefault(m[key], {"ports": 0, "nonactive": 0, "linkdown": 0, "errors": 0})["ping"] = float(row["value"][1])
                    break

    # How many designed cables could be matched to a UFM port (both conventions agree)?
    leaf_ports = [(u["leaf"], u["leaf_portnum"]) for u in topo["uplinks"]] + \
                 [(l["leaf"], l["leaf_portnum"]) for n in topo["nodes"] for l in n["links"]]
    matched = sum(1 for leaf, p in leaf_ports if p is not None and "%s|%d" % (leaf, p) in out["ports"])
    out["coverage"] = {"cables": len(leaf_ports), "matched": matched}
    return out


# -- service glue ------------------------------------------------------------

class FabricStore:
    """NetBox snapshots on disk (refreshed on request) plus a cached live overlay."""

    def __init__(self, nsr: Nsr, config: dict, cache_dir: Path, ttl: int = 60) -> None:
        self.nsr, self.config, self.dir, self.ttl = nsr, config, cache_dir, ttl
        self.dir.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.jobs: dict = {}
        self.topos: dict = {}
        self.live: dict = {}

    def fabric(self, site: str) -> tuple[dict, dict]:
        region = next(r for r in self.config["regions"] if r["site"] == site)
        fabric = region.get("fabric_view")
        if not fabric:
            raise KeyError(site)
        return region, fabric

    def path(self, site: str) -> Path:
        return self.dir / ("%s-netbox.json" % site)

    def refresh(self, site: str) -> dict:
        region, fabric = self.fabric(site)
        with self.lock:
            job = self.jobs.get(site)
            if job and job["state"] == "running":
                return job
            job = self.jobs[site] = {"state": "running", "started": time.time(), "step": "starting"}

        def work():
            try:
                snap = pull_netbox(self.nsr, fabric, progress=lambda text: job.update(step=text))
                tmp = self.path(site).with_suffix(".tmp")
                tmp.write_text(json.dumps(snap), encoding="utf-8")
                tmp.replace(self.path(site))
                self.topos.pop(site, None)
                job.update(state="done", step="NetBox snapshot saved", finished=time.time())
            except (NsrError, ValueError) as error:
                job.update(state="error", error=str(error), hint=getattr(error, "hint", ""), finished=time.time())

        threading.Thread(target=work, daemon=True).start()
        return job

    def topology(self, site: str) -> dict | None:
        path = self.path(site)
        if not path.is_file():
            return None
        mtime = path.stat().st_mtime
        cached = self.topos.get(site)
        if cached and cached[0] == mtime:
            return cached[1]
        snap = json.loads(path.read_text(encoding="utf-8"))
        topo = build_topology(snap, self.fabric(site)[1].get("port_names", "auto"))
        topo["netbox_collected_at"] = snap.get("collected_at")
        self.topos[site] = (mtime, topo)
        return topo

    def view(self, site: str, force: bool = False) -> dict:
        region, fabric = self.fabric(site)
        topo = self.topology(site)
        base = {"site": site, "name": region["name"], "location": region.get("location"),
                "fabric_label": region.get("fabric"), "cluster": fabric.get("cluster"),
                "job": self.jobs.get(site)}
        if topo is None:
            return {**base, "topology": None}
        live = self.live.get(site)
        if force or not live or time.time() - live["collected_at"] > self.ttl:
            src = region["sources"]
            ufm = src.get("ufm") or {}
            from checks import ds_uid  # local import keeps fabric.py usable on its own
            live = live_overlay(self.nsr, topo, ufm.get("selector"), ds_uid(self.config, ufm.get("datasource")),
                                (src.get("ping") or {}).get("selector"), ds_uid(self.config, "mimir"))
            self.live[site] = live
        return {**base, "topology": topo, "live": live}
