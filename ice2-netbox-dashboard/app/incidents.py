#!/usr/bin/env python3
"""Fabric incidents: turn the cabling report and the live switch view into a short,
ranked list of what needs attention, from "fabric is broken" down to "labels only".

Inputs (all already collected by the service; nothing new is sent anywhere):
  * the Cabling vs UFM report (ufm_cabling.analyse): what UFM sees vs the design
  * the Live link state view: what the switches say about NetBox's cables
  * tray history (.netbox-live-sync/tray-history.json): when each GPU tray was last
    seen, so a tray that disappears from the fabric is reported as offline

Severity (see README 6.3):
  critical  fabric-wide or many-GPU impact now: a spine or leaf cut off, a big share
            of uplink capacity gone, no UFM data
  major     a real fault that hurts jobs or routing: topology-changing miscabling,
            a GPU tray on the wrong rail/SU or offline, a leaf or spine losing
            many links, an unreachable switch, UFM's own link down
  minor     single links down/Init/degraded, port swaps with no fabric impact
  info      documentation and data hygiene: NetBox differs, unnamed adapters, old data
"""

from __future__ import annotations

import collections
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}
LEAF = re.compile(r"-bel(\d+)$")
SPINE = re.compile(r"-bes(\d+)$")
HISTORY_DAYS = 7


def n(count: int, word: str) -> str:
    """'1 GPU tray', '6 GPU trays' (the last word gets the plural)."""
    if count == 1:
        return "%d %s" % (count, word)
    head, _, last = word.rpartition(" ")
    last = last[:-1] + "ies" if last.endswith("y") and last[-2:-1] not in "aeiou" else last + ("es" if last.endswith(("s", "x", "ch")) else "s")
    return "%d %s" % (count, (head + " " if head else "") + last)


def short(name: str) -> str:
    m = LEAF.search(name) or SPINE.search(name)
    return ("BEL" if LEAF.search(name) else "BES") + m.group(1) if m else name.replace("sys1-ice2-p-", "")


def age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


class Incidents:
    def __init__(self):
        self.items = []

    def add(self, severity, category, title, impact, action, evidence=(), scope=(), tab="cab", key=None):
        evidence = list(evidence)
        self.items.append({"id": key or "%s:%s" % (category, title), "severity": severity, "category": category,
                           "title": title, "impact": impact, "action": action, "scope": sorted(set(scope)),
                           "evidence": evidence[:200], "evidence_total": len(evidence), "tab": tab})

    def result(self) -> dict:
        rank = {"fabric": 0, "ufm": 1, "switch": 2, "gpu": 3, "cabling": 4, "links": 5, "data": 6, "documentation": 7}
        self.items.sort(key=lambda i: (ORDER[i["severity"]], rank.get(i["category"], 9), -i["evidence_total"], i["title"]))
        counts = collections.Counter(i["severity"] for i in self.items)
        return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "counts": {k: counts.get(k, 0) for k in ORDER}, "incidents": self.items}


def update_tray_history(path: Path, report: dict) -> dict:
    """Remember when each GPU tray was last seen; returns the history."""
    try:
        history = json.loads(path.read_text())
    except (OSError, ValueError):
        history = {}
    when = report["source"]["scanned_at"]
    seen = collections.defaultdict(list)
    for leaf, port, code in report.get("gpu_ports_seen", []):
        seen[code].append([leaf, port])
    for code, ports in seen.items():
        old = history.get(code)
        if not old or old["last_seen"] <= when:
            history[code] = {"last_seen": when, "ports": sorted(ports)}
    keep = {c: h for c, h in history.items() if (age_hours(h["last_seen"]) or 0) < HISTORY_DAYS * 24}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(keep, indent=0, sort_keys=True))
    except OSError:
        pass
    return keep


def detect(report: dict | None, live: dict | None = None, history: dict | None = None, fetch: dict | None = None) -> dict:
    out = Incidents()
    if not report:
        out.add("critical", "data", "No UFM data loaded", "The cabling and fabric checks cannot run, so incidents in the fabric would go unnoticed.",
                "Press Fetch from UFM (or run ./scripts/fetch_ufm_scan.sh).")
    else:
        fabric(out, report)
        gpu(out, report, history or {})
        data_age(out, report, fetch)
    if live:
        switches(out, live)
    return out.result()


def fabric(out: Incidents, r: dict) -> None:
    s, up = r["summary"], r.get("uplinks") or {"leaves": {}, "spines": {}, "uneven": []}
    findings = r["switch_findings"]
    by_status = collections.defaultdict(list)
    for f in findings:
        by_status[f["status"]].append(f)
    total_exp = sum(e for e, _ in up["leaves"].values()) or s["switch_cables"]
    total_seen = sum(c for _, c in up["leaves"].values())

    # whole spines / leaves cut off, or losing many links
    covered = set()
    for kind, table, label in (("spine", up["spines"], "downlinks"), ("leaf", up["leaves"], "uplinks")):
        for dev, (exp, seen) in table.items():
            lost = exp - seen
            if exp and (seen == 0 or lost >= max(2, exp // 8)):
                covered.add(dev)
            if exp and seen == 0:
                out.add("critical", "fabric", "%s %s is cut off from the fabric" % (kind.capitalize(), short(dev)),
                        "UFM sees none of its %d %s. %s" % (exp, label, "Every leaf loses 1/%d of its uplink bandwidth." % len(up["spines"]) if kind == "spine"
                                                            else "Every GPU on this leaf's rail is off the fabric."),
                        "Check the switch is powered and healthy (UFM events, console), then its cables.", scope=[dev], tab="cab",
                        evidence=["%s: 0 of %d %s seen" % (short(dev), exp, label)])
            elif exp and lost >= max(2, exp // 8):
                out.add("major", "fabric", "%s %s lost %d of %d %s" % (kind.capitalize(), short(dev), lost, exp, label),
                        "%.0f%% less bandwidth through %s; traffic concentrates on the remaining links." % (100.0 * lost / exp, short(dev)),
                        "See section 4 of Cabling vs UFM for the cables; check optics and the far ends.", scope=[dev],
                        evidence=["%s %s %s ↔ %s %s" % (f["status"], short(f["leaf"][0]), f["leaf"][1], short(f["spine"][0]), f["spine"][1])
                                  for f in findings if f["status"] in ("not-seen", "down") and dev in (f["leaf"][0], f["spine"][0])])
    if total_exp and total_seen and (total_exp - total_seen) / total_exp >= 0.25:
        out.add("critical", "fabric", "%.0f%% of leaf–spine capacity is missing" % (100.0 * (total_exp - total_seen) / total_exp),
                "%d of %d leaf–spine cables are not seen by UFM." % (total_exp - total_seen, total_exp), "Check UFM and the spine layer first.")

    # miscabling, by impact
    ref_name = {"NetBox": "NetBox", "NetBox + design": "NetBox and the design"}.get((r.get("reference") or {}).get("kind"), "design")
    # NetBox reference: a difference the approved design sides with UFM on is a NetBox record to fix, not a cable
    nb_wrong = [f for f in by_status["miscabled"] if f.get("design_state") == "matches-current"]
    mis = [f for f in by_status["miscabled"] if f not in nb_wrong]
    groups = collections.defaultdict(list)
    for f in mis:
        groups[f.get("impact", "topology")].append(f)
    def line(f):
        tag = " · in UFM master" if f.get("master_state") == "matches-current" else ""
        tag += " · design agrees" if f.get("design_state") == "matches-expected" else ""
        if f["leaf_actual"]:
            return "%s %s → %s %s (%s: %s %s)%s" % (short(f["leaf"][0]), f["leaf"][1], short(f["leaf_actual"][0][0]), f["leaf_actual"][0][1],
                                                 ref_name, short(f["spine"][0]), f["spine"][1], tag)
        other = ", ".join("%s %s" % (short(a), b) for a, b in f.get("spine_actual") or []) or "another cable"
        return "%s %s not seen; its %s far end %s %s has %s%s" % (short(f["leaf"][0]), f["leaf"][1], ref_name, short(f["spine"][0]), f["spine"][1], other, tag)
    if nb_wrong:
        out.add("minor", "documentation", n(len(nb_wrong), "NetBox leaf–spine record") + " disagree with UFM and the approved design",
                "UFM's cabling matches the approved design here, so the NetBox record is probably wrong rather than the cable.",
                "Confirm on site, then correct these NetBox cables.", evidence=map(line, nb_wrong), scope=[f["leaf"][0] for f in nb_wrong])
    if groups["topology"]:
        out.add("major", "cabling", n(len(groups["topology"]), "leaf–spine cable") + (" changes" if len(groups["topology"]) == 1 else " change") + " the fabric topology",
                "Some leaf–spine pairs have more cables than designed and others fewer: uneven bandwidth, hot spots, and the fat-tree routing may not hold.",
                "Re-patch these cables as %s records them soon (section 1 of Cabling vs UFM)." % ref_name, evidence=map(line, groups["topology"]),
                scope=[f["leaf"][0] for f in groups["topology"]])
    if groups["plane-split"]:
        out.add("major", "cabling", n(len(groups["plane-split"]), "cable") + " with planes landing on different far ends",
                "The four planes of one cable reach different switches or ports: plane imbalance on the Q3400s.",
                "Inspect the breakout/optics on these ports and re-patch.", evidence=map(line, groups["plane-split"]),
                scope=[f["leaf"][0] for f in groups["plane-split"]])
    if groups["port-swap"]:
        leaves = sorted({short(f["leaf"][0]) for f in groups["port-swap"]})
        out.add("minor", "cabling", "%s swapped between ports (%s)" % (n(len(groups["port-swap"]), "leaf–spine cable"), ", ".join(leaves)),
                "No fabric impact: every leaf still has its designed links to every spine. Labels, runbooks and port-based maintenance are wrong for these ports.",
                "Re-patch in a maintenance window, then save a new UFM master topology.", evidence=map(line, groups["port-swap"]),
                scope=[f["leaf"][0] for f in groups["port-swap"]])
    if r.get("switch_undocumented"):
        bad = [u for u in r["switch_undocumented"] if (LEAF.search(u["a"][0]) and LEAF.search(u["b"][0])) or (SPINE.search(u["a"][0]) and SPINE.search(u["b"][0]))]
        sev = "major" if bad else "minor"
        out.add(sev, "cabling", n(len(r["switch_undocumented"]), "switch-to-switch link") + " not in " + ref_name,
                "Leaf–leaf or spine–spine links break the fat tree and can cause routing loops or credit deadlocks." if bad
                else "Extra links the design does not have.", "Find and remove or document these links.",
                evidence=["%s %s ↔ %s %s" % (short(u["a"][0]), u["a"][1], short(u["b"][0]), u["b"][1]) for u in r["switch_undocumented"]],
                scope=[u["a"][0] for u in r["switch_undocumented"]])

    # individual link faults
    small = [f for f in by_status["not-seen"] + by_status["down"] if f["leaf"][0] not in covered and f["spine"][0] not in covered]
    if small:
        out.add("minor", "links", n(len(small), "leaf–spine cable") + " not up", "Each missing cable removes one 800G path; routing works around it.",
                "Check optics, seating and the far end; see section 4 of Cabling vs UFM.",
                evidence=["%s %s ↔ %s %s" % (short(f["leaf"][0]), f["leaf"][1], short(f["spine"][0]), f["spine"][1]) for f in small],
                scope=[f["leaf"][0] for f in small])
    for status, title, impact, action in (
            ("init", "leaf–spine cables stuck in Init", "Physically up but not configured by the subnet manager, so they carry no traffic.",
             "Check UFM events/alarms for these ports (isolation, flapping, SM sweep)."),
            ("degraded", "leaf–spine cables with planes missing", "Fewer than four planes are up: reduced bandwidth on these cables.",
             "Check the optics/cable on these ports.")):
        items = by_status[status]
        if items:
            out.add("minor", "links", n(len(items), "leaf–spine cable") + " " + title.split(" ", 2)[2], impact, action,
                    evidence=["%s %s ↔ %s %s (%d/4 planes)" % (short(f["leaf"][0]), f["leaf"][1], short(f["spine"][0]), f["spine"][1], f["planes"]) for f in items],
                    scope=[f["leaf"][0] for f in items])

    # UFM's own links
    bad_ufm = [u for u in r.get("ufm_links", []) if u["state"] != "ok"]
    if bad_ufm:
        out.add("major", "ufm", "UFM host links not fully active", "UFM (the subnet manager) has reduced connectivity to the fabric.",
                "Check the UFM host HCAs and their leaf ports.", evidence=["%s %s %s (%s)" % (short(u["leaf"]), u["port"], u["adapter"], u["state"]) for u in bad_ufm])

    ref = r.get("reference") or {}
    if str(ref.get("kind", "")).startswith("NetBox") and ref.get("source") == "bundled export":
        out.add("info", "data", "Cabling is checked against the bundled NetBox export, not live NetBox",
                "NetBox has not been read by this dashboard yet, so recent NetBox changes are not reflected.",
                "Press Refresh NetBox.")
    if str(ref.get("kind", "")).startswith("NetBox") and ref.get("unusable"):
        out.add("info", "documentation", "%d NetBox cables have unusable terminations" % ref["unusable"],
                "A cable that is not one interface on each side cannot be compared, so it is left out of the cabling check.",
                "Fix these cables in NetBox.", evidence=["#%s: %s" % (c, why) for c, why in ref.get("unusable_list", [])])
    design = r.get("design") or {}
    if str(ref.get("kind", "")).startswith("NetBox") and not design:
        out.add("info", "data", "No approved design to cross-check NetBox",
                "Differences between NetBox and UFM cannot be told apart from wrong NetBox records.",
                "Fetch from UFM (it copies /root/nscale_Compute.topo) or copy it to local-inputs/ufm/.")
    if ref.get("kind") == "inferred design":
        out.add("info", "data", "Cabling is checked against inferred rules, not an approved design",
                "expected_topology.csv is derived from the pattern the fabric follows; it is not a signed-off cabling plan.",
                "Copy the approved design (UFM host /root/nscale_Compute.topo) to local-inputs/ufm/ (README 6.1).")
    sus_ref = ref if ref.get("suspect_count") else design
    if sus_ref.get("suspect_count"):
        ref = sus_ref
        out.add("info", "documentation", "%d entries in the approved design file look wrong" % ref["suspect_count"],
                "These entries cannot be physically right (for example one adapter port on four leaves), so the inferred rule is used for those ports.",
                "Have the design owner correct %s." % ref.get("file", "the design file"),
                evidence=["%s %s: %s" % (short(x["port"][0]), x["port"][1], x["why"]) for x in ref.get("suspect", [])])
    # documentation
    if s.get("switch_design_differs"):
        out.add("minor", "documentation", "%d leaf–spine cables where NetBox and the approved design disagree" % s["switch_design_differs"],
                "UFM matches NetBox for these, but the design file says otherwise: either the design entry is out of date, "
                "or the cable was miscabled and NetBox was updated to match it.", "Ask the design owner which is intended.",
                evidence=["%s %s: NetBox %s %s, design %s" % (short(f["leaf"][0]), f["leaf"][1], short(f["spine"][0]), f["spine"][1],
                                                            " ".join(f["design"]) if f.get("design") else "none")
                          for f in r["switch_findings"] if f["status"] == "design-differs"])
    if s.get("switch_netbox_differs") and (r.get("reference") or {}).get("kind") == "NetBox + design":
        out.add("minor", "documentation", "%d NetBox leaf–spine records disagree with UFM and the approved design" % s["switch_netbox_differs"],
                "The cable matches the approved design, so the NetBox record is missing or wrong. Runbooks built on NetBox will be wrong.",
                "Correct these cables in NetBox (Findings CSV, status netbox-differs).",
                evidence=["%s %s: NetBox %s, UFM and design %s %s" % (short(f["leaf"][0]), f["leaf"][1], " ".join(f["netbox"]) if f.get("netbox") else "none",
                                                                    short(f["spine"][0]), f["spine"][1])
                          for f in r["switch_findings"] if f["status"] == "netbox-differs"])
    elif s.get("switch_netbox_differs"):
        out.add("info", "documentation", "%d leaf–spine cables differ in NetBox from the design" % s["switch_netbox_differs"],
                "NetBox is not the reference here, but runbooks built on it will be wrong.", "Review the NetBox import CSV.")
    m = r.get("master") or {}
    if m.get("switch_matches_current_not_design"):
        out.add("info", "documentation", "UFM's master topology contains %d miscabled cables" % m["switch_matches_current_not_design"],
                "UFM's own Topology Compare treats them as correct and will never flag them.",
                "After re-patching, save a new master topology in UFM.")


def gpu(out: Incidents, r: dict, history: dict) -> None:
    s = r["summary"]
    errors, incomplete = [], []
    for su in r["sus"]:
        for t in su["trays"]:
            name = t["host"].replace("sys1-ice2-p-phy-", "") or t["code"]
            label = "SU%d slot %s %s" % (su["su"], t["slot"], t["code"]) + (" (%s)" % name if name != t["code"] else "")
            if t["status"] == "error":
                errors.append((label, t))
            elif t["status"] == "incomplete":
                incomplete.append((label, t))
    wiring = [(l, t) for l, t in errors if any(k in i for i in t["issues"] for k in ("rail", "different SUs", "same port", "not a GPU port"))]
    if wiring:
        out.add("major", "gpu", n(len(wiring), "GPU tray") + " cabled to the wrong rail, SU or slot",
                "Rail-optimised collectives (NCCL) on these GPUs cross the spines or hit the wrong leaf: slower jobs and congestion for neighbours.",
                "Re-patch the tray's adapters to the design (Cabling vs UFM, trays needing attention).",
                evidence=["%s: %s" % (l, "; ".join(i for i in t["issues"] if "NetBox" not in i)) for l, t in wiring])
    rails = [(l, t) for l, t in incomplete if any(i.startswith("no link on rail") for i in t["issues"])]
    if rails:
        out.add("major", "gpu", n(len(rails), "GPU tray") + " running without all four rails",
                "Jobs on these trays lose a rail: lower bandwidth, and schedulers may still place multi-node jobs there.",
                "Check the missing adapter link (optics, cable, NIC) or drain the tray.",
                evidence=["%s: %s" % (l, "; ".join(t["issues"])) for l, t in rails])
    planes = [(l, t) for l, t in incomplete if (l, t) not in rails]
    if planes:
        out.add("minor", "gpu", n(len(planes), "GPU tray") + " with links not fully active", "Reduced bandwidth on one or more adapters.",
                "Check the listed adapter ports.", evidence=["%s: %s" % (l, "; ".join(t["issues"])) for l, t in planes])

    # NVL72 racks: 4 per SU, 18 trays each (slots 1-18, 19-36, 37-54, 55-72)
    racks = [(su["su"], rk) for su in r["sus"] for rk in su.get("racks", [])]
    rname = lambda su, rk: "SU%d rack %d%s" % (su, rk["pos"], " (%s)" % rk["name"] if rk["name"] else "")
    split = [(su, rk) for su, rk in racks if rk["state"] == "split"]
    if split:
        out.add("major", "gpu", n(len(split), "NVL72 rack position") + " holding trays of more than one rack",
                "A rack's 18 trays share one NVLink domain and should land on one rack position of one SU. Split racks put "
                "NVLink peers on different leaves or SUs, so rail-local traffic crosses the spines.",
                "Re-patch the trays to their rack's position (Cabling vs UFM, trays needing attention).",
                evidence=["%s: %s" % (rname(su, rk), "; ".join(rk["issues"])) for su, rk in split])
    dark = [(su, rk) for su, rk in racks if rk["off"] >= 9]
    if dark:
        out.add("major", "gpu", n(len(dark), "NVL72 rack") + " mostly off the fabric",
                "Half or more of the rack's designed trays have no adapter in UFM: the rack is probably powered off or being serviced.",
                "Confirm with the DC team whether the rack is in maintenance.",
                evidence=["%s: %d of 18 designed trays not seen (%s)" % (rname(su, rk), rk["off"], "–".join(rk["hosts"])) for su, rk in dark])
    nameless = [(su, rk) for su, rk in racks if rk["state"] == "unnamed"]
    if nameless:
        out.add("info", "documentation", n(len(nameless), "NVL72 rack") + " whose trays UFM cannot name",
                "Every adapter in the rack position is up but has no node description, so the rack and its trays cannot be identified.",
                "Set the node description on these hosts and refetch from UFM.",
                evidence=["%s: %d unnamed trays · design hosts %s" % (rname(su, rk), rk["unnamed"], "–".join(rk["hosts"])) for su, rk in nameless])

    # trays that disappeared since they were last seen
    now = r["source"]["scanned_at"]
    current = {code for _, _, code in r.get("gpu_ports_seen", [])}
    gone = sorted((code, h) for code, h in history.items() if code not in current and h["last_seen"] < now)
    nb_host = {}
    for g in r.get("gpu_not_seen", []):
        for code, h in gone:
            if [g["leaf"], g["port"]] in h["ports"]:
                nb_host[code] = g["host"]
    if gone:
        by_leaf = collections.Counter(leaf for _, h in gone for leaf, _ in h["ports"])
        sev = "critical" if len(gone) >= 18 else "major"
        out.add(sev, "gpu", n(len(gone), "GPU tray") + " went offline",
                "UFM no longer sees these trays that were on the fabric before: their GPUs cannot run fabric jobs." +
                (" Many share leaves %s, which points at a leaf problem." % ", ".join(short(l) for l, n in by_leaf.most_common(3) if n >= 4) if any(n >= 4 for n in by_leaf.values()) else ""),
                "Check the trays (power, OS, NICs), or the leaves if many are on the same leaf.",
                evidence=["%s%s last seen %s on %s" % (code, " (NetBox %s)" % nb_host[code].replace("sys1-ice2-p-phy-", "") if code in nb_host else "",
                                                       h["last_seen"][:16].replace("T", " ") + " UTC", ", ".join("%s %s" % (short(a), b) for a, b in h["ports"][:4])) for code, h in gone],
                scope=[l for _, h in gone for l, _ in h["ports"]])
    nb_gone = collections.defaultdict(list)
    offline_ports = {(l, p): code for code, h in gone for l, p in h["ports"]}
    for g in r.get("gpu_not_seen", []):
        if (g["leaf"], g["port"]) not in offline_ports:  # already reported as an offline tray
            nb_gone[g["host"]].append(g)
    whole = {h: g for h, g in nb_gone.items() if len(g) >= 4}
    if whole:
        out.add("major", "gpu", n(len(whole), "NetBox GPU host") + " not on the fabric at all",
                "NetBox documents these hosts' four RDMA cables, but UFM sees none of them.",
                "Check whether the hosts are down, unplugged, or decommissioned (then update NetBox).",
                evidence=["%s: %s" % (h.replace("sys1-ice2-p-phy-", ""), ", ".join("%s %s" % (short(x["leaf"]), x["port"]) for x in g)) for h, g in sorted(whole.items())])
    part = {h: g for h, g in nb_gone.items() if len(g) < 4}
    if part:
        out.add("minor", "gpu", n(sum(len(g) for g in part.values()), "NetBox GPU cable") + " not seen on hosts that are otherwise up",
                "Some RDMA links documented in NetBox are missing in UFM.", "Check these adapter ports.",
                evidence=["%s %s: %s %s" % (x["host"].replace("sys1-ice2-p-phy-", ""), x["rdma"], short(x["leaf"]), x["port"]) for g in part.values() for x in g])
    if s.get("unnamed_adapters"):
        out.add("info", "documentation", n(s["unnamed_adapters"], "GPU adapter") + " without a host name",
                "UFM sees these adapters but they report no tray name, so faults on them are hard to attribute.",
                "Set the node description on these hosts.")
    if s.get("trays_missing"):
        out.add("info", "documentation", n(s["trays_missing"], "GPU tray") + " missing from NetBox",
                "Working trays that NetBox does not document.", "Use the NetBox import CSV.")


def data_age(out: Incidents, r: dict, fetch: dict | None) -> None:
    hours = age_hours(r["source"]["scanned_at"])
    live = "REST" in (r["source"].get("tool") or "")
    if hours is not None and hours > 6:
        out.add("info", "data", "Cabling data is %.0f hours old" % hours,
                "Anything that changed since %s is not reflected here." % r["source"]["scanned_at"][:16].replace("T", " "),
                "Press Fetch from UFM" + ("" if live else " (with a UFM web user it reads live links)") + ".")
    last = (fetch or {}).get("last") or {}
    if last.get("state") == "failed":
        out.add("info", "data", "Last UFM fetch failed", last.get("error", ""), "See the message next to Fetch from UFM.")


def switches(out: Incidents, live: dict) -> None:
    fresh = live.get("freshness") or {}
    cov = live.get("coverage") or {}
    if fresh.get("state") in ("snapshot", "stale"):
        out.add("info" if fresh.get("state") == "stale" else "major", "data",
                "Switch states are %s" % ("stale" if fresh.get("state") == "stale" else "not verified yet"),
                (fresh.get("note") or "") + ". Live link state shows the last known values, not current ones.",
                "Press Sync fabric (or start the service with --sync-every-minutes).", tab="live")
    if cov.get("missing"):
        out.add("major", "switch", n(len(cov["missing"]), "switch") + " not reached by the last collection",
                "Their links are shown as unverified, not as up: unreachable, login refused, or down.",
                "Check the sync errors (.netbox-live-sync/<run>/errors/) and the switches.", scope=cov["missing"], tab="live",
                evidence=["%s%s" % (short(d), " (login/collection error)" if d in cov.get("failed", []) else " (unparsable output)" if d in cov.get("unparsable", []) else "")
                          for d in cov["missing"]])
    if cov.get("missing_ports"):
        out.add("minor", "switch", n(cov.get("missing_port_count", 0), "designed port") + " not reported by their switch",
                "The switch answered but did not list these ports (renamed, breakout changed, or not an IB port).",
                "Compare the port names on the switch with the design topology.", scope=list(cov["missing_ports"]), tab="live",
                evidence=["%s (%d): %s" % (short(d), (cov.get("missing_port_counts") or {}).get(d, len(ports)), ", ".join(ports)) for d, ports in sorted(cov["missing_ports"].items())])
    ex = live.get("exceptions") or []
    if not ex or not live.get("collected_at"):
        return
    per_switch = collections.defaultdict(lambda: collections.Counter())
    rows = collections.defaultdict(list)
    for cid, ctype, status, a_dev, a_port, a_state, b_dev, b_port, b_state in ex:
        for dev, port, state in ((a_dev, a_port, a_state), (b_dev, b_port, b_state)):
            if "-swi-" not in dev:
                continue
            kind = "down" if state.startswith("Down/") else "init" if state.startswith(("Initialize/", "Armed/")) else None
            if kind:
                per_switch[dev][kind] += 1
                rows[(dev, kind)].append("%s %s%s" % (short(dev), port, " (cable %s)" % cid if cid else ""))
    down = {d: c["down"] for d, c in per_switch.items() if c["down"]}
    if down:
        heavy = [d for d, k in down.items() if k >= 8]
        out.add("major" if heavy else "minor", "switch", "%s down on %s" % (n(sum(down.values()), "switch port"), n(len(down), "switch")),
                "Switches report designed ports as Down." + (" %s has many down ports." % ", ".join(short(d) for d in heavy) if heavy else ""),
                "See the Live link state tab.", scope=list(down), tab="live",
                evidence=[e for d in sorted(down) for e in rows[(d, "down")]])
