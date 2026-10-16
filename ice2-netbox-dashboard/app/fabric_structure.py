"""Planes and SU slices worked out from the links alone, as a cross-check on the rules.

The method is the one nscaledev/gemelo uses for backend fabrics
(src/gemelo/acquire/planes.py), written again here for UFM and NetBox links:

* plane: spines that share leaves. Two spines are in one plane when at least two leaves
  are linked to both (one, when either has a single leaf), so one stray cable never
  merges two planes. A leaf linked to spines of more than one plane is reported, and
  counted with the plane most of its spines are in; a leaf with no spine is reported.
* SU slice: the leaves of a plane that serve the same GPU trays. Two leaves are in one
  slice when at least two trays are linked to both, so a tray cabled into two slices
  merges neither and is reported ("stray"). A leaf linked to no tray is a slice of its own.

Names only order things; the structure comes from the links. This file is identical in
the ICE2, LON14 and multi-region dashboards.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping

SOURCE = "nscaledev/gemelo src/gemelo/acquire/planes.py (method)"


def natural(text: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(text))]


def shared(through: Mapping[str, Iterable[str]]) -> dict[str, int]:
    """Members of the sets `through` maps to, keyed by class: two members are joined when
    at least two sets hold both (one, when either is in a single set)."""
    sets = [sorted(set(found), key=natural) for found in through.values()]
    members = sorted({m for found in sets for m in found}, key=natural)
    reached = Counter(m for found in sets for m in found)
    pairs = Counter()
    for found in sets:
        pairs.update(zip(found, found[1:]))
    parent = {m: m for m in members}

    def root(m):
        while parent[m] != m:
            parent[m] = parent[parent[m]]
            m = parent[m]
        return m

    for (a, b), count in pairs.items():
        if count >= min(2, reached[a], reached[b]):
            parent[root(b)] = root(a)
    keys: dict[str, int] = {}
    return {m: keys.setdefault(root(m), len(keys)) for m in members}


def infer(leaves: Iterable[str], uplinks: Mapping[str, Iterable[str]], hosts: Mapping[str, Iterable[str]]) -> dict:
    """{planes: [{spines, leaves, slices}], straddling, spineless, stray, hostless} from
    leaf -> spines and leaf -> trays (tray ids of any form)."""
    leaves = sorted(set(leaves), key=natural)
    up = {l: set(uplinks.get(l, ())) for l in leaves}
    spine_class = shared(up)
    straddling, spineless, members = [], [], defaultdict(list)
    for leaf in leaves:
        if not up[leaf]:
            spineless.append(leaf)
            continue
        counted = Counter(spine_class[s] for s in up[leaf])
        if len(counted) > 1:
            straddling.append(leaf)
        members[min(counted, key=lambda k: (-counted[k], k))].append(leaf)
    spines_of = defaultdict(list)
    for spine, key in spine_class.items():
        spines_of[key].append(spine)
    stray: set[str] = set()
    planes = []
    for key, found in members.items():
        planes.append({"spines": sorted(spines_of[key], key=natural), "leaves": found,
                       "slices": _slices(found, hosts, stray)})
    planes.sort(key=lambda p: natural(p["leaves"][0]))
    hostless = [l for l in leaves if not set(hosts.get(l, ()))]
    return {"planes": planes, "straddling": straddling, "spineless": spineless,
            "stray": sorted(stray, key=natural), "hostless": hostless, "method": SOURCE}


def _slices(leaves: list[str], hosts: Mapping[str, Iterable[str]], stray: set[str]) -> list[list[str]]:
    served = defaultdict(set)
    for leaf in leaves:
        for host in hosts.get(leaf, ()):
            served[host].add(leaf)
    classes = shared(served)
    groups = defaultdict(list)
    for leaf in leaves:
        groups[classes.get(leaf, leaf)].append(leaf)
    stray.update(h for h, found in served.items() if len({classes[l] for l in found}) > 1)
    return sorted((sorted(g, key=natural) for g in groups.values()), key=lambda g: natural(g[0]))


def compare(structure: dict, expected: Mapping[str, Iterable[str]]) -> dict:
    """The inferred slices against expected ones ({label: leaves}): each expected slice
    `agrees` (the cabling forms exactly that slice), is `untested` (no tray on any of its
    leaves) or `differs` (with the inferred slices that cover its leaves)."""
    inferred = [tuple(s) for p in structure["planes"] for s in p["slices"]]
    of_leaf = {l: s for s in inferred for l in s}
    hostless = set(structure["hostless"])
    agree, untested, differ = [], [], []
    for label, leaves in expected.items():
        leaves = sorted(leaves, key=natural)
        if all(l in hostless for l in leaves):
            untested.append(label)
            continue
        found = sorted({of_leaf[l] for l in leaves if l in of_leaf and not set(of_leaf[l]) <= hostless}, key=lambda s: natural(s[0]))
        if len(found) == 1 and list(found[0]) == leaves:
            agree.append(label)
        else:
            differ.append({"label": label, "expected": leaves, "inferred": [list(s) for s in found]})
    return {"agree": agree, "untested": untested, "differ": differ}


def summary(structure: dict, check: dict | None = None) -> str:
    """One line for a page or an incident."""
    planes = structure["planes"]
    parts = ["%d plane%s (%s)" % (len(planes), "" if len(planes) == 1 else "s",
                                  ", ".join("%d leaves × %d spines" % (len(p["leaves"]), len(p["spines"])) for p in planes))]
    parts.append("%d SU slices" % sum(len(p["slices"]) for p in planes))
    if check is not None:
        total = len(check["agree"]) + len(check["untested"]) + len(check["differ"])
        parts.append("%d of %d agree with the SU slice rule" % (len(check["agree"]), total)
                     + (" (%d with no trays to check)" % len(check["untested"]) if check["untested"] else ""))
    return "; ".join(parts)
