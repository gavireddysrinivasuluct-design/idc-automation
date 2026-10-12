#!/usr/bin/env python3
"""A stand-in for `nsr` with canned, made-up answers: for tests and the offline demo.

    python3 multi_region_dashboard.py --nsr tests/fake_nsr.py

It answers the same three read verbs the dashboard uses, in nsr's `--json` shapes.
FAKE_NSR_FAIL=<substring> makes any query containing that substring fail like Grafana
would (exit 1, message on stderr). Every value here is invented for testing.
"""

import json
import os
import re
import sys
import time

NOW = time.time()
SITES = {
    "is-sys1-ice2": {
        "ping": (100, 100), "snmp": (100, 100), "ufm_srv": {"10.1.67.190": 1, "10.1.67.191": 1},
        "ufm_ports": 9216, "ufm_switches": 100,
        "nonactive": {"sys1-ice2-p-swi-bel31": 2, "sys1-ice2-p-swi-bes7": 2},
        "linkdown": [("sys1-ice2-p-swi-bel31", "2801cd0300784c80_85", 3)],
        "errors": [("sys1-ice2-p-swi-bel19", "2801cd0300783860_15", 1210)],
        "gnmi": (100, 100), "netbox_site": "ice2", "optics_hot": {"sys1-ice2-p-swi-bel44": 1},
    },
    "gb-sys1-lon14": {
        "ping": (99, 100), "ping_down": ["sys1-lon14-p-swi-bel12"], "snmp": (100, 100),
        "ufm_srv": {"10.2.10.11": 1, "10.2.10.12": 1}, "ufm_ports": 7168, "ufm_switches": 100,
        "nonactive": {}, "linkdown": [], "errors": [], "gnmi": (100, 100), "netbox_site": "lon14",
        "psu": [("sys1-lon14-p-swi-bes3", "PSU 2", "INACTIVE")],
    },
    "no-sys2-osl1": {
        "ping": (48, 48), "snmp": (48, 48), "ufm_srv": {"sys2-osl1-p-phy-ufm0": 1, "sys2-osl1-p-phy-ufm1": 0},
        "ufm_ports": 3144, "ufm_switches": 48, "nonactive": {"sys2-osl1-p-ifs-bel3": 1},
        "linkdown": [("sys2-osl1-p-ifs-bel5", "b8cef60300000005_3", 2)], "errors": [("sys2-osl1-p-ifs-bes9", "b8cef6030012ab40_33", 42)],
    },
    "no-sys2-sta1": {
        "ping": (95, 96), "ping_down": ["ifs378"], "ufm_srv": {"sta1-p-phy-ufm2": 0, "sta1-p-phy-ufm4": 0},
    },
    "no-sta1": {"snmp": (16, 16)},
}
INCIDENTS = [
    {"incident_number": 61, "title": "[FIRING:1] beib-device-unreachable no-sys2-sta1 ifs378", "status": "triggered",
     "urgency": "high", "created_at": "2026-10-12T14:02:11Z", "html_url": "https://nscale.eu.pagerduty.com/incidents/Q1",
     "service": {"summary": "BackEnd Network"}},
    {"incident_number": 59, "title": "[FIRING:2] ufm-ev-link-flap sys1-ice2 bel31 sw43", "status": "acknowledged",
     "urgency": "high", "created_at": "2026-10-12T11:40:00Z", "html_url": "https://nscale.eu.pagerduty.com/incidents/Q2",
     "service": {"summary": "BackEnd Network"}},
]


# -- a small invented OSL1-like fabric for the NetBox + UFM fabric view --------------
P = "sys2-osl1-p-ifs-"
OSL1_SPINES = [P + "bes%d" % i for i in range(4)]
OSL1_LEAVES = [P + "bel%d" % i for i in range(16)]


def osl1_cables():
    rows = []
    for li, leaf in enumerate(OSL1_LEAVES):
        for si, spine in enumerate(OSL1_SPINES):
            for k in (1, 2):
                rows.append({"device": leaf, "interface": "sw%dp%d" % (17 + si, k), "remote_device": spine,
                             "remote_interface": "sw%dp%d" % (li + 1, k), "state": "connected", "cable": 1000 + len(rows)})
    for su in range(2):
        for slot in range(1, 7):
            node = "sys2-osl1-p-phy-gpu%03d" % (su * 6 + slot)
            for rail in range(1, 9):
                if node.endswith("gpu004") and rail == 6:
                    continue                                   # one rail never cabled
                leaf = OSL1_LEAVES[su * 8 + rail - 1]
                if node.endswith("gpu009") and rail == 2:
                    leaf = OSL1_LEAVES[su * 8 + 2]             # NIC 2 on a rail-3 leaf
                rows.append({"device": leaf, "interface": "sw%dp1" % slot, "remote_device": node,
                             "remote_interface": "ib%d" % (rail - 1), "state": "connected", "cable": 2000 + len(rows)})
    rows.append({"device": OSL1_LEAVES[15], "interface": "sw16p1", "remote_device": "sys2-osl1-p-phy-ufm0",
                 "remote_interface": "mlx5_0", "state": "connected", "cable": 3999})
    return rows


def osl1_devices(role):
    names = OSL1_SPINES if role == "be-spine-switch" else OSL1_LEAVES
    return [{"name": n, "device_type": {"model": "MQM9790"}, "status": {"value": "active"},
             "cluster": {"name": "sys2-osl1"}, "primary_ip4": {"address": "10.50.37.%d/24" % i},
             "rack": {"name": "R%d" % (i // 8)}} for i, n in enumerate(names)]


def osl1_ports():
    rows = []
    for c in osl1_cables():
        m = re.match(r"sw(\d+)p(\d+)$", c["interface"])
        port = 2 * (int(m.group(1)) - 1) + int(m.group(2))
        state = 1 if (c["device"].endswith("bel3") and port == 1) else 4   # one GPU link down
        rows.append(({"device_name": c["device"], "port_id": "b8cef603%08x_%d" % (OSL1_LEAVES.index(c["device"]), port)}, state))
    return rows


def vec(rows):
    return {"status": "success", "data": {"resultType": "vector",
            "result": [{"metric": labels, "value": [NOW, str(value)]} for labels, value in rows]}}


def regions_in(text):
    found = [cr for cr in SITES if cr in text]
    for m in re.finditer(r'cloud_region=~"([^"]+)"', text):
        found += [cr for cr in m.group(1).split("|") if cr in SITES]
    return list(dict.fromkeys(found))


def site_value(cr, q):
    d = SITES[cr]
    if "probe_success" in q:
        return d.get("ping", (None, None))[0 if q.startswith("sum") else 1]
    if "ufm-system-monitoring" in q:
        if "ufm_srv" not in d:
            return None
        return sum(d["ufm_srv"].values()) if q.startswith("sum") else len(d["ufm_srv"])
    if "up{" in q and "snmp" in q:
        return d.get("snmp", (None, None))[0 if q.startswith("sum") else 1]
    if "logical_state" in q:
        if "ufm_ports" not in d:
            return None
        return sum(d["nonactive"].values()) if "!= 4" in q else d["ufm_ports"]
    if "LinkDownedCounterExtended" in q:
        return len(d.get("linkdown", [])) if "ufm_ports" in d else None
    if "SymbolErrorCounterExtended" in q:
        return len(d.get("errors", [])) if "ufm_ports" in d else None
    if "gnmic_target_connection_state" in q:
        return d["gnmi"][0] if "gnmi" in d else None
    if "gnmic_target_up" in q:
        return d["gnmi"][1] if "gnmi" in d else None
    return None


def overview(q):
    rows = []
    for cr in regions_in(q):
        value = site_value(cr, q)
        if value:  # grouped counts with nothing to count are absent, like Prometheus
            rows.append(({"cloud_region": cr}, value))
    return vec(rows)


def per_site(q):
    crs = regions_in(q)
    if not crs:
        site = re.search(r'netbox_site="([a-z0-9]+)"', q)
        crs = [cr for cr, d in SITES.items() if site and d.get("netbox_site") == site.group(1)]
    if not crs:
        return vec([])
    cr = crs[0]
    d = SITES[cr]
    if q.startswith("last_over_time(logical_state") and cr == "no-sys2-osl1":
        return vec(osl1_ports())
    if q.startswith("last_over_time(logical_state"):
        return vec([])
    if q.startswith("count(count by (device_name)") and "logical_state" in q:
        return vec([({}, d["ufm_switches"])] if "ufm_switches" in d else [])
    if "logical_state" in q and "!= 4" in q:
        return vec([({"device_name": k}, v) for k, v in d.get("nonactive", {}).items()])
    if q.startswith("count(last_over_time(logical_state"):
        return vec([({}, d["ufm_ports"])] if "ufm_ports" in d else [])
    if "LinkDownedCounterExtended" in q:
        return vec([({"device_name": a, "port_id": b}, c) for a, b, c in d.get("linkdown", [])])
    if "SymbolErrorCounterExtended" in q:
        return vec([({"device_name": a, "port_id": b}, c) for a, b, c in d.get("errors", [])])
    if "PortXmitDiscardsExtended" in q:
        return vec([])
    if "PortXmitWaitExtended" in q:
        return vec([({"device_name": k}, 1234.5) for k in list(d.get("nonactive", {}))[:2]])
    if "gnmic_target_connection_state" in q:
        return vec([({}, d["gnmi"][0])] if "gnmi" in d else [])
    if "gnmic_target_up" in q:
        return vec([({}, d["gnmi"][1])] if "gnmi" in d else [])
    if "oper_status_info" in q:
        return vec([({"host_name": h, "component_name": c, "oper_status": s}, 1) for h, c, s in d.get("psu", [])])
    if "input_voltage" in q or "port_link_downed" in q:
        return vec([])
    if "temp_high_alarm_flag" in q:
        return vec([({"host_name": k}, v) for k, v in d.get("optics_hot", {}).items()])
    if "asic_temp" in q:
        return vec([({"host_name": "%s-bel%d" % (cr, i)}, 70 - i) for i in range(1, 6)] if "gnmi" in d else [])
    if "probe_success" in q:
        if "== 0" in q:
            return vec([({"device_name": n, "instance": n}, 0) for n in d.get("ping_down", [])])
        if "ping" not in d:
            return vec([])
        return vec([({}, d["ping"][0] if q.startswith("sum") else d["ping"][1])])
    if "ufm_ha_role" in q:
        return vec([({"instance": "ufm1", "role": "master"}, 1)] if cr == "is-sys1-ice2" else [])
    if "cpu_usage" in q or "memory_usage" in q:
        return vec([({"instance": k}, 37.5) for k, v in d.get("ufm_srv", {}).items() if v])
    if "ufm-system-monitoring" in q:
        return vec([({"instance": k}, v) for k, v in d.get("ufm_srv", {}).items()])
    if "up{" in q:
        if "== 0" in q:
            return vec([])
        total = sum(SITES[c].get("snmp", (0, 0))[0 if q.startswith("sum") else 1] for c in crs if "snmp" in SITES[c])
        return vec([({}, total)] if total else [])
    return vec([])


def loki(q):
    streams = []
    if "sys2-sta1-p-phy-ufm" in q:
        streams.append({"stream": {"host_name": "sys2-sta1-p-phy-ufm2"}, "values": [
            [str(int((NOW - 600) * 1e9)), "eventlog WARNING [907] Switch is down: ifs378 (10.40.37.130)"],
            [str(int((NOW - 1800) * 1e9)), "eventlog CRITICAL [920] Link went down: ifs390 port 17"]]})
    if "service_name=\"syslog\"" in q or "infra-collector-syslog" in q:
        streams.append({"stream": {"hostname": "10.40.37.141"}, "values": [
            [str(int((NOW - 60) * 1e9)), "health[3412]: TID 3412: [health.NOTICE]: Port 17 changed state to down"]]})
    return {"status": "success", "data": {"resultType": "streams", "result": streams}}


def main(argv):
    args = [a for a in argv if a not in ("--json", "-q", "--quiet")]
    text = " ".join(args)
    fail = os.environ.get("FAKE_NSR_FAIL")
    if fail and fail in text:
        sys.stderr.write("HTTP 400 from https://observability-grafana/api/datasources/proxy: bad_data\n")
        return 1
    if args[:2] == ["grafana", "query"]:
        q = args[2]
        out = overview(q) if re.match(r"(sum|count) by \(cloud_region\)", q) else per_site(q)
    elif args[:2] == ["grafana", "loki"]:
        out = loki(args[2])
    elif args[:2] == ["netbox", "devices"]:
        site, role = args[args.index("--site") + 1], args[args.index("--role") + 1]
        out = osl1_devices(role) if site == "osl1" else []
    elif args[:2] == ["netbox", "connections"]:
        out = osl1_cables() if args[args.index("--site") + 1] == "osl1" else []
    elif args[:2] == ["pd", "incidents"]:
        out = INCIDENTS
    else:
        sys.stderr.write("fake nsr: unsupported %r\n" % args)
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
