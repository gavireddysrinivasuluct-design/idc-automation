#!/usr/bin/env python3
"""Validate every source before the cabling comparison runs.

Bad input must not turn into a confident-looking finding. Problems are split into:

  errors    the comparison would be misleading; the dashboard shows the validation report
            instead of findings until the source is fixed
  warnings  data-quality problems that are reported but handled safely (e.g. a NetBox cable
            with an unusable termination is left out and named)

Checks: required columns, duplicate cable IDs, one physical port used by two cables,
port names that are not NVOS ports (swNpM / fnmN), leaf-spine rows that do not join a leaf
to a spine, design entries that contradict each other, and an empty or implausible UFM scan.
"""

from __future__ import annotations

import collections
import re

LEAF = re.compile(r"-bel(\d+)$")
SPINE = re.compile(r"-bes(\d+)$")
SW_PORT = re.compile(r"^(sw\d+p\d+|fnm\d+)$", re.I)
NB_COLUMNS = ("netbox_cable_id", "connection_type", "endpoint_a_device", "endpoint_a_port", "endpoint_b_device", "endpoint_b_port")
DESIGN_COLUMNS = ("link_type", "a_device", "a_port", "b_device", "b_port")
EXPECTED_LEAF_SPINE = 64 * 36 * 2


def _issue(bucket: list, code: str, text: str, examples: list | None = None, count: int | None = None) -> None:
    examples = examples or []
    bucket.append({"code": code, "text": text, "count": count if count is not None else len(examples), "examples": examples[:12]})


def check_netbox(rows: list[dict], info: dict | None = None) -> tuple[list, list]:
    errors, warnings = [], []
    if not rows:
        _issue(errors, "netbox-empty", "No NetBox cables to compare against.", count=0)
        return errors, warnings
    missing = [c for c in NB_COLUMNS if c not in rows[0]]
    if missing:
        _issue(errors, "netbox-columns", "NetBox cable data is missing columns: %s." % ", ".join(missing), missing)
        return errors, warnings
    ids = collections.Counter(r["netbox_cable_id"] for r in rows)
    dup_ids = sorted((i for i, n in ids.items() if n > 1), key=str)
    if dup_ids:
        _issue(errors, "netbox-duplicate-id", "Cable IDs that appear more than once.", ["#%s" % i for i in dup_ids])
    ends = collections.defaultdict(list)
    bad_ports, bad_pairs = [], []
    for r in rows:
        a, b = (r["endpoint_a_device"], r["endpoint_a_port"]), (r["endpoint_b_device"], r["endpoint_b_port"])
        for dev, port in (a, b):
            if "-swi-" in dev:
                ends[(dev, port)].append((r["netbox_cable_id"], r["connection_type"]))
                if not SW_PORT.match(port or ""):
                    bad_ports.append("#%s %s %s" % (r["netbox_cable_id"], dev, port))
        if r["connection_type"] == "leaf-spine":
            devs = (a[0], b[0])
            if not (any(LEAF.search(d) for d in devs) and any(SPINE.search(d) for d in devs)):
                bad_pairs.append("#%s %s ⟷ %s" % (r["netbox_cable_id"], a[0], b[0]))
    shared_ls = ["%s %s: #%s" % (dev, port, ", #".join(str(c) for c, _ in cables))
                 for (dev, port), cables in sorted(ends.items()) if len(cables) > 1 and any(t == "leaf-spine" for _, t in cables)]
    shared_gpu = ["%s %s: #%s" % (dev, port, ", #".join(str(c) for c, _ in cables))
                  for (dev, port), cables in sorted(ends.items()) if len(cables) > 1 and not any(t == "leaf-spine" for _, t in cables)]
    if shared_ls:
        _issue(errors, "netbox-shared-port", "Switch ports that NetBox gives two leaf-spine cables (the expected far end is ambiguous).", shared_ls)
    if shared_gpu:
        _issue(warnings, "netbox-shared-gpu-port", "Leaf ports with two GPU cables in NetBox.", shared_gpu)
    if bad_ports:
        _issue(errors, "netbox-port-name", "Switch ends whose port is not an NVOS port name (swNpM / fnmN).", bad_ports)
    if bad_pairs:
        _issue(errors, "netbox-leaf-spine-pair", "Leaf-spine cables that do not join a leaf (bel) to a spine (bes).", bad_pairs)
    info = info or {}
    if info.get("unusable"):
        _issue(warnings, "netbox-unusable", "NetBox cables with unusable terminations, left out of the comparison.",
               ["#%s: %s" % (c, why) for c, why in info.get("unusable_list", [])], count=info["unusable"])
    n_ls = sum(r["connection_type"] == "leaf-spine" for r in rows)
    if n_ls and n_ls != EXPECTED_LEAF_SPINE:
        _issue(warnings, "netbox-leaf-spine-count", "NetBox has %d leaf-spine cables; the fabric has %d." % (n_ls, EXPECTED_LEAF_SPINE), count=n_ls)
    return errors, warnings


def check_design(rows: list[dict] | None, info: dict | None = None) -> tuple[list, list]:
    errors, warnings = [], []
    if rows is None:
        return errors, warnings
    if rows and any(c not in rows[0] for c in DESIGN_COLUMNS):
        _issue(errors, "design-columns", "The design rows are missing columns: %s." % ", ".join(c for c in DESIGN_COLUMNS if c not in rows[0]))
        return errors, warnings
    seen = collections.defaultdict(set)
    bad = []
    for r in rows:
        seen[(r["a_device"], r["a_port"])].add((r["b_device"], r["b_port"]))
        if not SW_PORT.match(r["a_port"] or ""):
            bad.append("%s %s" % (r["a_device"], r["a_port"]))
        if r["link_type"] == "leaf-spine" and not (LEAF.search(r["a_device"]) and SPINE.search(r["b_device"])):
            bad.append("%s ⟷ %s" % (r["a_device"], r["b_device"]))
    conflicts = ["%s %s: %s" % (d, p, " | ".join(" ".join(x) for x in sorted(far))) for (d, p), far in sorted(seen.items()) if len(far) > 1]
    if conflicts:
        _issue(errors, "design-conflict", "Design ports with more than one expected far end.", conflicts)
    if bad:
        _issue(errors, "design-row", "Design rows with an invalid port name or a leaf-spine row that is not leaf to spine.", bad)
    info = info or {}
    if info.get("suspect_count"):
        _issue(warnings, "design-suspect", "Design entries that cannot be physically right; the inferred rule is used for those ports.",
               ["%s %s: %s" % (s["port"][0], s["port"][1], s["why"]) for s in info.get("suspect", [])], count=info["suspect_count"])
    n_ls = sum(r["link_type"] == "leaf-spine" for r in rows)
    if n_ls != EXPECTED_LEAF_SPINE:
        _issue(warnings, "design-leaf-spine-count", "The design has %d leaf-spine links; the fabric has %d." % (n_ls, EXPECTED_LEAF_SPINE), count=n_ls)
    return errors, warnings


def check_scan(lanes: int, unparsed: int = 0) -> tuple[list, list]:
    errors, warnings = [], []
    if lanes == 0:
        _issue(errors, "scan-empty", "The UFM snapshot has no link lanes.", count=0)
    if unparsed:
        _issue(warnings, "scan-unparsed", "UFM scan lines that could not be read.", count=unparsed)
    return errors, warnings


def check_cross(nb_rows: list[dict], design_rows: list[dict] | None) -> tuple[list, list]:
    """NetBox and the design must describe the same leaf-spine cable set when both are references."""
    errors, warnings = [], []
    if not design_rows:
        return errors, warnings
    nb = {(r["endpoint_a_device"], r["endpoint_a_port"]) for r in nb_rows if r["connection_type"] == "leaf-spine" and LEAF.search(r["endpoint_a_device"])}
    nb |= {(r["endpoint_b_device"], r["endpoint_b_port"]) for r in nb_rows if r["connection_type"] == "leaf-spine" and LEAF.search(r["endpoint_b_device"])}
    ds = {(r["a_device"], r["a_port"]) for r in design_rows if r["link_type"] == "leaf-spine"}
    only_nb, only_ds = sorted(nb - ds), sorted(ds - nb)
    if only_nb:
        _issue(warnings, "netbox-only-ports", "Leaf uplink ports NetBox has but the design does not.", ["%s %s" % p for p in only_nb])
    if only_ds:
        _issue(warnings, "design-only-ports", "Leaf uplink ports the design has but NetBox does not.", ["%s %s" % p for p in only_ds])
    return errors, warnings


def validate(nb_rows: list[dict], nb_info: dict | None, design_rows: list[dict] | None, design_info: dict | None,
             lanes: int, unparsed: int = 0) -> dict:
    """All checks; `ok` is False when any error makes the comparison untrustworthy."""
    errors, warnings = [], []
    for e, w in (check_netbox(nb_rows, nb_info), check_design(design_rows, design_info), check_scan(lanes, unparsed),
                 check_cross(nb_rows, design_rows)):
        errors += e
        warnings += w
    return {"ok": not errors, "errors": errors, "warnings": warnings}
