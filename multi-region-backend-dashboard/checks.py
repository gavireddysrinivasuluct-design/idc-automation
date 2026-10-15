"""What the dashboard asks Grafana, Loki and PagerDuty for, and how it shapes the answers.

Rules that keep this cheap and honest on fabrics with hundreds of thousands of series:

* Every query aggregates before data leaves Grafana (count/sum by device or region), or
  returns only offending series (`> 0`, `!= 4`, `== 0`). No raw per-port dumps.
* Absent is not zero. A count that returns no series means "this source has no data
  here", and is reported as such, never as a healthy 0.
* The all-regions overview uses one query per metric family and datasource, grouped by
  cloud_region, instead of one query per site.
"""

from __future__ import annotations

import concurrent.futures
import time

from nsr_client import Nsr, NsrError

MAX_ROWS = 200
UFM_ACTIVE = 4  # UFM logical_state: 4 = Active
ERROR_COUNTERS = ("SymbolErrorCounterExtended|PortRcvErrorsExtended|LocalLinkIntegrityErrorsExtended|"
                  "PortRcvRemotePhysicalErrorsExtended")
DISCARD_COUNTERS = "PortXmitDiscardsExtended|VL15DroppedExtended|ExcessiveBufferOverrunErrorsExtended"
UFM_EVENT_FILTER = ' |~ "(?i)critical|fatal|error|is down|node is down|link went down"'
NAME_LABELS = ("device_name", "host_name", "hostname", "name", "ufm_node", "node", "target", "instance")


# -- small helpers ----------------------------------------------------------

def ds_uid(config: dict, key: str | None) -> str:
    return config["datasources"].get(key or "mimir", key or "infra-mimir")


def value_of(row: dict) -> float | None:
    try:
        return float(row["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def scalar(rows: list[dict]) -> float | None:
    """First value of an aggregate; None when the query returned no series (no data)."""
    return value_of(rows[0]) if rows else None


def display_name(labels: dict) -> str:
    for key in NAME_LABELS:
        if labels.get(key):
            return str(labels[key])
    return ",".join("%s=%s" % item for item in sorted(labels.items()) if item[0] != "__name__") or "?"


def gnmi_selector(site: str) -> str:
    return 'environment="prod",netbox_site="%s",netbox_role=~"be-(leaf|spine)-switch"' % site


def cloud_regex(region: dict) -> str:
    return "|".join(region["cloud_regions"])


# -- check catalogue --------------------------------------------------------
# kind "value": one number.  kind "rows": a table of offenders (columns from labels).
# severity applies when a "rows" check returns rows, or a "value" check is non-zero.

def fabric_checks(region: dict, config: dict) -> list[dict]:
    ufm = region["sources"].get("ufm")
    if not ufm:
        return []
    s, ds = ufm["selector"], ds_uid(config, ufm.get("datasource"))
    state = "last_over_time(logical_state{%s}[6m])" % s
    return [
        {"id": "ufm_switches", "kind": "value", "title": "Switches reporting to UFM", "datasource": ds,
         "promql": "count(count by (device_name) (%s))" % state},
        {"id": "ufm_ports", "kind": "value", "title": "Switch ports seen by UFM", "datasource": ds,
         "promql": "count(%s)" % state},
        {"id": "ufm_nonactive", "kind": "rows", "severity": "major", "datasource": ds,
         "title": "Ports not Active, per switch (UFM)", "columns": ["device_name", "value:ports"],
         "action": "Open the switch in the region's topology dashboard or UFM; check cable, optic and the far end.",
         "promql": "sort_desc(count by (device_name) (%s != %d))" % (state, UFM_ACTIVE)},
        {"id": "ufm_linkdown", "kind": "rows", "severity": "major", "datasource": ds,
         "title": "Link-down events, last 15 min (UFM)", "columns": ["device_name", "port_id", "value:events"],
         "action": "A flapping link: check the cable and optic; repeated flaps make the SM mark the port unhealthy.",
         "promql": "sort_desc(sum by (device_name, port_id) (increase(LinkDownedCounterExtended{%s}[15m])) > 0)" % s},
        {"id": "ufm_errors", "kind": "rows", "severity": "minor", "datasource": ds,
         "title": "Ports with symbol / receive errors, last 15 min (UFM)", "columns": ["device_name", "port_id", "value:errors"],
         "action": "Rising errors on one port usually mean a dirty or failing optic or cable.",
         "promql": 'sort_desc(sum by (device_name, port_id) (increase({__name__=~"%s",%s}[15m])) > 0)'
                   % (ERROR_COUNTERS, s)},
        {"id": "ufm_discards", "kind": "rows", "severity": "minor", "datasource": ds,
         "title": "Ports with discards / drops, last 15 min (UFM)", "columns": ["device_name", "port_id", "value:drops"],
         "action": "Discards point at congestion or a stuck far end; compare with transmit wait.",
         "promql": 'sort_desc(sum by (device_name, port_id) (increase({__name__=~"%s",%s}[15m])) > 0)'
                   % (DISCARD_COUNTERS, s)},
        {"id": "ufm_xmit_wait", "kind": "rows", "severity": "info", "datasource": ds,
         "title": "Most congested switches (transmit wait rate, top 10)", "columns": ["device_name", "value:wait/s"],
         "promql": "topk(10, sum by (device_name) (rate(PortXmitWaitExtended{%s}[5m])) > 0)" % s},
    ]


def switch_checks(region: dict, config: dict) -> list[dict]:
    gnmi = region["sources"].get("gnmi")
    if not gnmi:
        return []
    g, ds = gnmi_selector(gnmi["netbox_site"]), ds_uid(config, "mimir")
    cr = cloud_regex(region)
    return [
        {"id": "gnmi_ready", "kind": "value", "title": "gNMI sessions READY", "datasource": ds,
         "promql": 'count(gnmic_target_connection_state{cloud_region=~"%s"} == 3)' % cr},
        {"id": "gnmi_targets", "kind": "value", "title": "gNMI targets", "datasource": ds,
         "promql": 'count(gnmic_target_up{cloud_region=~"%s"})' % cr},
        {"id": "gnmi_psu_fan", "kind": "rows", "severity": "major", "datasource": ds,
         "title": "PSUs and fans not ACTIVE (gNMI)", "columns": ["host_name", "component_name", "oper_status"],
         "action": "Raise a hardware ticket for the PSU or fan; check the PDU feed for a PSU.",
         "promql": 'max by (host_name, component_name, oper_status) (gnmi_components_component_state_oper_status_info'
                   '{%s,component_name=~"PSU.*|FAN.*",oper_status!="ACTIVE"} == 1)' % g},
        {"id": "gnmi_psu_input", "kind": "rows", "severity": "major", "datasource": ds,
         "title": "PSUs with no input voltage (gNMI)", "columns": ["host_name", "component_name"],
         "action": "One feed is lost: check the PDU and power cord before the second feed fails.",
         "promql": 'max by (host_name, component_name) (gnmi_components_component_power_supply_state_input_voltage'
                   '{%s,component_name=~"PSU [0-9]+"} == 0)' % g},
        {"id": "gnmi_optics_hot", "kind": "rows", "severity": "minor", "datasource": ds,
         "title": "Optics with a high-temperature alarm (gNMI)", "columns": ["host_name", "value:optics"],
         "promql": "sort_desc(count by (host_name) (gnmi_components_component_transceiver_transceiver_diag_state_"
                   "temp_high_alarm_flag{%s} == 1))" % g},
        {"id": "gnmi_linkdown", "kind": "rows", "severity": "major", "datasource": ds,
         "title": "Link-down events, last 15 min (switch gNMI)", "columns": ["host_name", "interface_name", "value:events"],
         "promql": "sort_desc(sum by (host_name, interface_name) (increase(gnmi_interfaces_interface_infiniband_state_"
                   "counters_port_link_downed{%s}[15m])) > 0)" % g},
        {"id": "gnmi_asic_temp", "kind": "rows", "severity": "info", "datasource": ds,
         "title": "Hottest ASICs (°C, top 5)", "columns": ["host_name", "value:°C"],
         "promql": "topk(5, max by (host_name) (gnmi_components_component_asic_state_asic_temp{%s}))" % g},
    ]


def reach_checks(region: dict, config: dict) -> list[dict]:
    src, mimir, out = region["sources"], ds_uid(config, "mimir"), []
    if src.get("ping"):
        p = src["ping"]["selector"]
        out += [
            {"id": "ping_up", "kind": "value", "title": "Switches answering ping", "datasource": mimir,
             "promql": "sum(probe_success{%s})" % p},
            {"id": "ping_total", "kind": "value", "title": "Switches probed", "datasource": mimir,
             "promql": "count(probe_success{%s})" % p},
            {"id": "ping_down", "kind": "rows", "severity": "critical", "datasource": mimir,
             "title": "Switches not answering management ping", "columns": ["name"],
             "action": "Check the switch from the OOB network / console; a whole row down points at the OOB switch.",
             "promql": "probe_success{%s} == 0" % p},
        ]
    if src.get("snmp"):
        sn = src["snmp"]["selector"]
        out += [
            {"id": "snmp_up", "kind": "value", "title": "SNMP targets up", "datasource": mimir,
             "promql": "sum(up{%s})" % sn},
            {"id": "snmp_total", "kind": "value", "title": "SNMP targets", "datasource": mimir,
             "promql": "count(up{%s})" % sn},
            {"id": "snmp_down", "kind": "rows", "severity": "minor", "datasource": mimir,
             "title": "SNMP targets not answering", "columns": ["name", "job"],
             "promql": "up{%s} == 0" % sn},
        ]
    if src.get("ufm_server"):
        u, ds = src["ufm_server"]["selector"], ds_uid(config, src["ufm_server"].get("datasource"))
        out += [
            {"id": "ufm_servers", "kind": "rows", "severity": "major", "datasource": ds, "all_rows": True,
             "title": "UFM servers (monitoring scrape)", "columns": ["name", "value:up"], "bad_when": "value==0",
             "action": "A UFM server that stops answering loses its view of the fabric; check UFM HA and the monitoring account.",
             "promql": "up{%s}" % u},
            {"id": "ufm_cpu", "kind": "rows", "severity": "info", "datasource": ds, "all_rows": True,
             "title": "UFM CPU %, last 5 min", "columns": ["name", "value:%"],
             "promql": 'max by (instance) (ufm_model_main_cpu_usage_percent_avg{%s,duration="Last 5 minutes"})' % u},
            {"id": "ufm_mem", "kind": "rows", "severity": "info", "datasource": ds, "all_rows": True,
             "title": "UFM memory %, last 5 min", "columns": ["name", "value:%"],
             "promql": 'max by (instance) (ufm_model_main_memory_usage_percent_avg{%s,duration="Last 5 minutes"})' % u},
            {"id": "ufm_ha_role", "kind": "rows", "severity": "info", "datasource": mimir, "all_rows": True,
             "title": "UFM HA role (command-exporter)", "columns": ["name", "role", "value:"],
             "promql": 'max by (instance, ufm_node, role) (ufm_ha_role{cloud_region=~"%s"})' % cloud_regex(region)},
        ]
    return out


SECTIONS = {"fabric": fabric_checks, "switches": switch_checks, "reach": reach_checks}


# -- running checks ---------------------------------------------------------

def shape_rows(check: dict, rows: list[dict]) -> dict:
    shaped = []
    for row in rows[:MAX_ROWS]:
        labels = dict(row.get("metric") or {})
        labels["name"] = display_name(labels)
        value = value_of(row)
        shaped.append({"labels": labels, "value": value})
    bad = shaped
    if check.get("bad_when") == "value==0":
        bad = [row for row in shaped if row["value"] == 0]
    elif check.get("all_rows") or check.get("severity") == "info":
        bad = []
    return {"rows": shaped, "issues": len(bad), "truncated": max(0, len(rows) - MAX_ROWS)}


def run_check(nsr: Nsr, check: dict) -> dict:
    started = time.monotonic()
    result = {key: check[key] for key in ("id", "kind", "title", "severity", "columns", "action", "promql", "datasource", "bad_when")
              if key in check}
    try:
        rows = nsr.query(check["promql"], check.get("datasource"))
        if check["kind"] == "value":
            result["value"] = scalar(rows)
            result["state"] = "no-data" if result["value"] is None else "ok"
        else:
            result.update(shape_rows(check, rows))
            result["state"] = "ok"
    except NsrError as error:
        result.update(state="error", error=str(error), hint=error.hint)
    result["ms"] = int((time.monotonic() - started) * 1000)
    return result


def run_section(nsr: Nsr, region: dict, config: dict, section: str) -> dict:
    checks = SECTIONS[section](region, config)
    if not checks:
        return {"section": section, "configured": False, "checks": [], "collected_at": time.time()}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(checks)) as pool:
        results = list(pool.map(lambda check: run_check(nsr, check), checks))
    # Offender lists are empty both when nothing is wrong and when the source has no data.
    # If the section's base count found nothing, say "no data" instead of a reassuring "none".
    base = {"fabric": "ufm_ports", "switches": "gnmi_targets"}.get(section)
    if base and any(r["id"] == base and r.get("state") == "no-data" for r in results):
        for r in results:
            if r["kind"] == "rows" and r.get("state") == "ok" and not r["rows"]:
                r["state"] = "no-data"
    return {"section": section, "configured": True, "checks": results, "collected_at": time.time()}


def run_events(nsr: Nsr, region: dict, config: dict) -> dict:
    src, out = region["sources"], {"section": "events", "collected_at": time.time(), "streams": []}
    loki = ds_uid(config, "loki")
    jobs = []
    if src.get("ufm_events"):
        jobs.append(("ufm_events", "UFM event log: critical, errors and down events, last 1 h",
                     src["ufm_events"]["logql"] + UFM_EVENT_FILTER, "1h", 40))
    if src.get("syslog"):
        jobs.append(("syslog", "Backend switch syslog, latest lines (15 min)", src["syslog"]["logql"], "15m", 15))
    out["configured"] = bool(jobs)

    def fetch(job):
        key, title, logql, since, limit = job
        item = {"id": key, "title": title, "logql": logql, "since": since}
        try:
            item["lines"] = nsr.loki(logql, since=since, limit=limit, datasource=loki)
            item["state"] = "ok" if item["lines"] else "no-data"
        except NsrError as error:
            item.update(state="error", error=str(error), hint=error.hint, lines=[])
        return item

    if jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            out["streams"] = list(pool.map(fetch, jobs))
    return out


def incidents_for(all_incidents: dict, region: dict) -> dict:
    """PagerDuty incidents whose title names this site. Titles name only the first site of a
    grouped incident, so a multi-site incident shows under its first site only."""
    if all_incidents.get("state") != "ok":
        return all_incidents
    tokens = [token.lower() for token in region.get("match", [region["site"]])]
    mine = [incident for incident in all_incidents["incidents"]
            if any(token in (incident.get("title") or "").lower() for token in tokens)]
    return {**all_incidents, "incidents": mine, "other_sites": len(all_incidents["incidents"]) - len(mine)}


def fetch_incidents(nsr: Nsr, config: dict) -> dict:
    try:
        raw = nsr.pd_incidents(config.get("pagerduty_service"))
    except NsrError as error:
        return {"state": "error", "error": str(error), "hint": error.hint, "incidents": []}
    slim = [{
        "number": incident.get("incident_number"),
        "title": incident.get("title"),
        "status": incident.get("status"),
        "urgency": incident.get("urgency"),
        "created_at": incident.get("created_at"),
        "url": incident.get("html_url"),
        "service": (incident.get("service") or {}).get("summary"),
    } for incident in raw]
    # PagerDuty's own list of the open incidents on this service, for the dashboard's links.
    base = next((i["url"].split("/incidents/")[0] for i in slim if (i.get("url") or "").count("/incidents/")),
                "https://nscale.eu.pagerduty.com")
    service = config.get("pagerduty_service")
    list_url = base + "/incidents" + ("?service_ids%5B%5D=" + service if service else "")
    return {"state": "ok", "incidents": slim, "list_url": list_url, "collected_at": time.time()}


# -- all-regions overview ---------------------------------------------------

def _union(template: str, selectors: list[str]) -> str:
    return " or ".join("(%s)" % (template % selector) for selector in selectors)


def overview_queries(config: dict) -> list[dict]:
    """One grouped query per metric family and datasource, `by (cloud_region)`."""
    regions, mimir = config["regions"], ds_uid(config, "mimir")
    queries = []

    def add(key, template, source, datasource_of=lambda src: mimir, wrap="count"):
        groups: dict[str, list[str]] = {}
        for region in regions:
            src = region["sources"].get(source)
            if src:
                groups.setdefault(datasource_of(src), []).append(src["selector"])
        for ds, selectors in groups.items():
            queries.append({"key": key, "datasource": ds,
                            "promql": "%s by (cloud_region) (%s)" % (wrap, _union(template, selectors))})

    ufm_ds = lambda src: ds_uid(config, src.get("datasource"))  # noqa: E731
    add("ping_up", "probe_success{%s}", "ping", wrap="sum")
    add("ping_total", "probe_success{%s}", "ping")
    add("snmp_up", "up{%s}", "snmp", wrap="sum")
    add("snmp_total", "up{%s}", "snmp")
    add("ufm_server_up", "up{%s}", "ufm_server", ufm_ds, wrap="sum")
    add("ufm_server_total", "up{%s}", "ufm_server", ufm_ds)
    add("ufm_ports", "last_over_time(logical_state{%s}[6m])", "ufm", ufm_ds)
    add("ufm_nonactive", "last_over_time(logical_state{%%s}[6m]) != %d" % UFM_ACTIVE, "ufm", ufm_ds)
    add("ufm_linkdown_ports", "sum by (cloud_region, device_name, port_id) "
        "(increase(LinkDownedCounterExtended{%s}[15m])) > 0", "ufm", ufm_ds)
    add("ufm_error_ports", "sum by (cloud_region, device_name, port_id) "
        '(increase({__name__=~"' + ERROR_COUNTERS + '",%s}[15m])) > 0', "ufm", ufm_ds)
    gnmi = [r for r in regions if r["sources"].get("gnmi")]
    if gnmi:
        cr = "|".join(cloud_regex(r) for r in gnmi)
        queries.append({"key": "gnmi_ready", "datasource": mimir,
                        "promql": 'count by (cloud_region) (gnmic_target_connection_state{cloud_region=~"%s"} == 3)' % cr})
        queries.append({"key": "gnmi_total", "datasource": mimir,
                        "promql": 'count by (cloud_region) (gnmic_target_up{cloud_region=~"%s"})' % cr})
    return queries


def run_overview(nsr: Nsr, config: dict, incidents: dict) -> dict:
    queries = overview_queries(config)
    values: dict[str, dict[str, float]] = {}
    errors = []

    def one(q):
        try:
            return q, nsr.query(q["promql"], q["datasource"]), None
        except NsrError as error:
            return q, [], error

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(queries) or 1)) as pool:
        for q, rows, error in pool.map(one, queries):
            if error:
                errors.append({"key": q["key"], "datasource": q["datasource"], "error": str(error), "hint": error.hint})
                continue
            bucket = values.setdefault(q["key"], {})
            for row in rows:
                cr = (row.get("metric") or {}).get("cloud_region", "?")
                bucket[cr] = bucket.get(cr, 0.0) + (value_of(row) or 0.0)

    failed_keys = {e["key"] for e in errors}
    out = []
    for region in config["regions"]:
        crs = region["cloud_regions"]

        def total(key):
            bucket = values.get(key, {})
            hits = [bucket[c] for c in crs if c in bucket]
            return sum(hits) if hits else None

        m = {key: total(key) for key in ("ping_up", "ping_total", "snmp_up", "snmp_total", "ufm_server_up",
                                          "ufm_server_total", "ufm_ports", "ufm_nonactive", "ufm_linkdown_ports",
                                          "ufm_error_ports", "gnmi_ready", "gnmi_total")}
        # A grouped count with no offenders is absent; when the site has data at all, that means 0.
        for key, base in (("ping_up", "ping_total"), ("snmp_up", "snmp_total"),
                          ("ufm_server_up", "ufm_server_total"), ("ufm_nonactive", "ufm_ports"), ("ufm_linkdown_ports", "ufm_ports"),
                          ("ufm_error_ports", "ufm_ports"), ("gnmi_ready", "gnmi_total")):
            if m[key] is None and m[base] is not None and key not in failed_keys:
                m[key] = 0.0
        site_inc = incidents_for(incidents, region)
        coverage = {
            "gnmi": "live" if m["gnmi_total"] else ("off" if not region["sources"].get("gnmi") else "no-data"),
            "ufm": "live" if m["ufm_ports"] else ("off" if not region["sources"].get("ufm") else "no-data"),
            "snmp": "live" if m["snmp_total"] else ("off" if not region["sources"].get("snmp") else "no-data"),
            "ping": "live" if m["ping_total"] else ("off" if not region["sources"].get("ping") else "no-data"),
            "ufm_server": ("live" if m["ufm_server_up"] else "down") if m["ufm_server_total"] else
                          ("off" if not region["sources"].get("ufm_server") else "no-data"),
        }
        issues = {
            "critical": int(max(0, (m["ping_total"] or 0) - (m["ping_up"] or 0))),
            "major": int((m["ufm_nonactive"] or 0) > 0) + int((m["ufm_linkdown_ports"] or 0) > 0) +
                     int((m["ufm_server_total"] or 0) > (m["ufm_server_up"] or 0)),
            "minor": int((m["ufm_error_ports"] or 0) > 0) + int((m["snmp_total"] or 0) > (m["snmp_up"] or 0)),
            "incidents": len(site_inc.get("incidents", [])),
        }
        live = any(state in ("live", "down") for state in coverage.values())
        if not live:
            status = "no-telemetry"
        elif issues["critical"] or issues["incidents"]:
            status = "critical"
        elif issues["major"] or issues["minor"]:
            status = "issues"
        else:
            status = "healthy"
        out.append({**{k: region.get(k) for k in ("site", "name", "location", "fabric", "clusters",
                                                     "switches_expected")},
                    "metrics": m, "coverage": coverage, "issues": issues, "status": status,
                    "incidents": site_inc.get("incidents", [])[:5]})
    return {"regions": out, "errors": errors, "queries": len(queries), "collected_at": time.time(),
            "incidents_state": {k: incidents.get(k) for k in ("state", "error", "hint")}}
