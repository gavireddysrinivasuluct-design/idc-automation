#!/usr/bin/env python3
"""Read-only discovery: which live data sources exist for a backend site, and do the
dashboard's selectors in regions.json actually match them?

    python3 discover.py sta1            # one site
    python3 discover.py --all           # every site in regions.json
    python3 discover.py osl1 --nsr ~/.local/bin/nsr

Every question is a small aggregate (count by job / role / metric name) sent through
`nsr grafana query` or `nsr grafana loki`; nothing is changed anywhere. The report is
printed and saved to discovery/<site>-<UTC time>.txt (git-ignored) so it can be shared.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import checks  # noqa: E402
from multi_region_dashboard import load_config  # noqa: E402
from nsr_client import Nsr, NsrError  # noqa: E402

# Every Prometheus-type store worth asking. promxy unions Mimir and the west2 VM;
# LON14 UFM lives only in its own VictoriaMetrics (compendium TOOL-006, NEW-MIMIR-014).
STORES = {"infra-mimir": "infra-mimir", "promxy": "promxy",
          "europe-west-victoriametrics": "dfx8ukladt7ggc", "lon14-victoriametrics": "bfssob4wsbchsb"}
UFM_FAMILIES = ("logical_state|LinkDownedCounterExtended|SymbolErrorCounterExtended|PortRcvErrorsExtended|"
                "PortXmitDiscardsExtended|PortXmitWaitExtended|eeber|infiniband_CBW|device")


def table(rows: list[dict], labels: list[str]) -> list[str]:
    if not rows:
        return ["    (no series)"]
    out = []
    for row in sorted(rows, key=lambda r: [str(r.get("metric", {}).get(l, "")) for l in labels]):
        metric = row.get("metric", {})
        out.append("    %-70s %s" % (" ".join("%s=%s" % (l, metric.get(l, "")) for l in labels),
                                     checks.value_of(row)))
    return out


def discover(nsr: Nsr, config: dict, region: dict) -> list[str]:
    cr = checks.cloud_regex(region)
    lines = ["# %s (%s) — cloud_region =~ %s — %s UTC" % (
        region["name"], region.get("location", ""), cr, datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M"))]

    def ask(title, promql, labels, uid="infra-mimir"):
        lines.append("\n## %s   [%s]\n    %s" % (title, uid, promql))
        try:
            lines.extend(table(nsr.query(promql, uid), labels))
        except NsrError as error:
            lines.append("    ERROR %s  (%s)" % (error, error.hint))

    for name, uid in STORES.items():
        ask("Scrape jobs with series here (%s)" % name,
            'count by (job) (up{cloud_region=~"%s"})' % cr, ["job"], uid)
    ask("Backend switch roles per job", 'count by (job, role) (up{cloud_region=~"%s",role=~"be-.*"})' % cr,
        ["job", "role"])
    ask("Blackbox ping by role", 'count by (role) (probe_success{cloud_region=~"%s"})' % cr, ["role"])
    ask("gNMI targets by connection state (3 = READY)",
        'count_values("state", gnmic_target_connection_state{cloud_region=~"%s"})' % cr, ["state"])
    for name in ("infra-mimir", "lon14-victoriametrics"):
        ask("UFM telemetry families per job (%s)" % name,
            'count by (job, __name__) ({cloud_region=~"%s",__name__=~"%s"})' % (cr, UFM_FAMILIES),
            ["job", "__name__"], STORES[name])
        ask("Label set of one UFM port series (%s)" % name,
            'topk(1, logical_state{cloud_region=~"%s"})' % cr,
            ["job", "device_name", "device_type", "port_id", "source", "level", "node_description"], STORES[name])
    ask("UFM server monitoring", 'up{job="ufm-system-monitoring",cloud_region=~"%s"}' % cr, ["instance", "region"])
    ask("SNMP metric families", 'count by (job, __name__) ({cloud_region=~"%s",job=~"snmp.*|netboxSnmp.*",'
        '__name__=~"ifOperStatus|ifHCInOctets|sysUpTime|.*[Tt]emp.*|.*[Pp]su.*|.*[Ff]an.*"})' % cr,
        ["job", "__name__"])

    lines.append("\n## Loki streams in the last 15 min")
    for title, logql in (("Any stream with this cloud_region", '{cloud_region=~"%s"}' % cr),
                         ("UFM event log", region["sources"].get("ufm_events", {}).get("logql", "")),
                         ("Switch syslog", region["sources"].get("syslog", {}).get("logql", ""))):
        if not logql:
            lines.append("    %s: not mapped in regions.json" % title)
            continue
        try:
            got = nsr.loki(logql, since="15m", limit=5, datasource=checks.ds_uid(config, "loki"))
            lines.append("    %s  %s → %d line(s)" % (title, logql, len(got)))
            for item in got[:3]:
                lines.append("      labels=%s" % json.dumps(item["labels"], sort_keys=True)[:300])
        except NsrError as error:
            lines.append("    %s  %s → ERROR %s" % (title, logql, error))

    lines.append("\n## The dashboard's own checks with the selectors in regions.json")
    for section in ("fabric", "switches", "reach"):
        result = checks.run_section(nsr, region, config, section)
        if not result["configured"]:
            lines.append("    %-9s not configured for this site" % section)
            continue
        for item in result["checks"]:
            shown = item.get("value") if item["kind"] == "value" else "%d row(s)" % len(item.get("rows", []))
            lines.append("    %-9s %-55s %-8s %s" % (section, item["title"][:55], item["state"],
                                                     item.get("error") or shown))
    lines.append("\nIf a source shows series above but the dashboard check says no-data, adjust that "
                 "site's selector in regions.json (label names differ between UFM csets and jobs).")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("site", nargs="?")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--nsr", default=None)
    parser.add_argument("--regions", default=str(ROOT / "regions.json"))
    args = parser.parse_args(argv)
    config = load_config(Path(args.regions))
    by_site = {r["site"]: r for r in config["regions"]}
    sites = list(by_site) if args.all else [args.site]
    if not args.all and args.site not in by_site:
        parser.error("site must be one of: %s" % ", ".join(by_site))
    nsr = Nsr(args.nsr, max_parallel=4)
    out_dir = ROOT / "discovery"
    out_dir.mkdir(exist_ok=True)
    for site in sites:
        lines = discover(nsr, config, by_site[site])
        text = "\n".join(lines) + "\n"
        print(text)
        path = out_dir / ("%s-%s.txt" % (site, datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")))
        path.write_text(text, encoding="utf-8")
        print("Saved %s" % path, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
