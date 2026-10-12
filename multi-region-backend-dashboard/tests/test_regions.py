"""Offline tests for the backend regions dashboard (standard library only).

`tests/fake_nsr.py` stands in for nsr with invented data: ICE2 and OSL1 have port issues,
LON14 has one switch not answering ping, STA1 has ping and UFM-server problems but no UFM
port telemetry, GLO1 has no telemetry at all.

Run:  python3 -m unittest discover -s tests -v      (from multi-region-backend-dashboard/)
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import checks  # noqa: E402
import multi_region_dashboard as dash  # noqa: E402
from nsr_client import Nsr, NsrError  # noqa: E402

FAKE = str(HERE / "fake_nsr.py")
CONFIG = dash.load_config(ROOT / "regions.json")


def region(site):
    return next(r for r in CONFIG["regions"] if r["site"] == site)


class Config(unittest.TestCase):
    def test_every_region_is_complete(self):
        for r in CONFIG["regions"]:
            self.assertTrue(r["cloud_regions"], r["site"])
            for name, src in r["sources"].items():
                if "selector" in src:
                    sel = src["selector"]
                    self.assertEqual(sel.count('"') % 2, 0, (r["site"], name))
                    self.assertNotIn("{", sel, "selectors are label lists, not full selectors")

    def test_generated_promql_is_balanced(self):
        queries = [q["promql"] for q in checks.overview_queries(CONFIG)]
        for r in CONFIG["regions"]:
            for section in checks.SECTIONS.values():
                queries += [c["promql"] for c in section(r, CONFIG)]
        self.assertGreater(len(queries), 40)
        for q in queries:
            self.assertEqual(q.count("("), q.count(")"), q)
            self.assertEqual(q.count("{"), q.count("}"), q)
            self.assertEqual(q.count('"') % 2, 0, q)

    def test_lon14_ufm_uses_its_victoriametrics(self):
        fabric = checks.fabric_checks(region("lon14"), CONFIG)
        self.assertTrue(all(c["datasource"] == "bfssob4wsbchsb" for c in fabric))
        grouped = [q for q in checks.overview_queries(CONFIG) if q["key"] == "ufm_ports"]
        self.assertEqual({q["datasource"] for q in grouped}, {"infra-mimir", "bfssob4wsbchsb"})

    def test_overview_is_grouped_not_per_site(self):
        self.assertLessEqual(len(checks.overview_queries(CONFIG)), 20)


class NsrClient(unittest.TestCase):
    def test_only_read_verbs_are_allowed(self):
        with self.assertRaises(ValueError):
            Nsr(FAKE).run(("netbox", "raw"), "/api/dcim/devices/")

    def test_missing_binary_explains_install(self):
        with self.assertRaises(NsrError) as ctx:
            Nsr("/nonexistent/nsr").query("up")
        self.assertIn("uv tool install", ctx.exception.hint)

    def test_failed_query_carries_hint(self):
        os.environ["FAKE_NSR_FAIL"] = "boom_metric"
        try:
            with self.assertRaises(NsrError) as ctx:
                Nsr(FAKE).query("boom_metric")
            self.assertIn("400", str(ctx.exception))
        finally:
            del os.environ["FAKE_NSR_FAIL"]


class Overview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        nsr = Nsr(FAKE)
        cls.data = checks.run_overview(nsr, CONFIG, checks.fetch_incidents(nsr, CONFIG))
        cls.by = {r["site"]: r for r in cls.data["regions"]}

    def test_statuses(self):
        self.assertEqual(self.by["glo1"]["status"], "no-telemetry")
        self.assertEqual(self.by["osl1"]["status"], "issues")
        self.assertEqual(self.by["lon14"]["status"], "critical")   # a switch not answering ping
        self.assertEqual(self.by["sta1"]["status"], "critical")    # ping + open incident

    def test_absent_is_not_zero(self):
        sta1 = self.by["sta1"]["metrics"]
        self.assertIsNone(sta1["ufm_ports"])
        self.assertIsNone(sta1["ufm_nonactive"], "no UFM data must not read as 0 ports down")
        self.assertEqual(self.by["sta1"]["coverage"]["ufm"], "no-data")
        self.assertEqual(self.by["lon14"]["metrics"]["ufm_nonactive"], 0)

    def test_aliases_are_summed(self):
        self.assertEqual(self.by["sta1"]["metrics"]["snmp_total"], 16)  # from cloud_region no-sta1
        self.assertEqual(self.by["sta1"]["metrics"]["ufm_server_up"], 0)
        self.assertEqual(self.by["sta1"]["coverage"]["ufm_server"], "down")

    def test_incidents_are_matched_by_site(self):
        self.assertEqual(len(self.by["sta1"]["incidents"]), 1)
        self.assertEqual(len(self.by["ice2"]["incidents"]), 1)
        self.assertEqual(len(self.by["osl1"]["incidents"]), 0)

    def test_a_failing_family_is_reported_not_hidden(self):
        os.environ["FAKE_NSR_FAIL"] = "probe_success"
        try:
            data = checks.run_overview(Nsr(FAKE), CONFIG, {"state": "ok", "incidents": []})
        finally:
            del os.environ["FAKE_NSR_FAIL"]
        self.assertTrue(any(e["key"].startswith("ping") for e in data["errors"]))
        ice2 = next(r for r in data["regions"] if r["site"] == "ice2")
        self.assertIsNone(ice2["metrics"]["ping_up"])
        self.assertEqual(ice2["metrics"]["ufm_ports"], 9216)


class Sections(unittest.TestCase):
    def test_fabric_offenders(self):
        out = checks.run_section(Nsr(FAKE), region("ice2"), CONFIG, "fabric")
        byid = {c["id"]: c for c in out["checks"]}
        self.assertEqual(byid["ufm_ports"]["value"], 9216)
        self.assertEqual(byid["ufm_nonactive"]["issues"], 2)
        self.assertEqual(byid["ufm_linkdown"]["rows"][0]["labels"]["port_id"], "2801cd0300784c80_85")
        self.assertEqual(byid["ufm_xmit_wait"]["issues"], 0, "info checks never count as issues")

    def test_no_ufm_data_marks_offender_lists_no_data(self):
        out = checks.run_section(Nsr(FAKE), region("sta1"), CONFIG, "fabric")
        self.assertTrue(all(c["state"] == "no-data" for c in out["checks"]))

    def test_gnmi_only_where_collected(self):
        self.assertFalse(checks.run_section(Nsr(FAKE), region("osl1"), CONFIG, "switches")["configured"])
        out = checks.run_section(Nsr(FAKE), region("lon14"), CONFIG, "switches")
        psu = next(c for c in out["checks"] if c["id"] == "gnmi_psu_fan")
        self.assertEqual(psu["rows"][0]["labels"]["component_name"], "PSU 2")

    def test_ufm_servers_flag_only_down_ones(self):
        out = checks.run_section(Nsr(FAKE), region("osl1"), CONFIG, "reach")
        srv = next(c for c in out["checks"] if c["id"] == "ufm_servers")
        self.assertEqual((len(srv["rows"]), srv["issues"]), (2, 1))

    def test_events(self):
        out = checks.run_events(Nsr(FAKE), region("sta1"), CONFIG)
        ufm = next(s for s in out["streams"] if s["id"] == "ufm_events")
        self.assertEqual(len(ufm["lines"]), 2)
        self.assertIn("Switch is down", ufm["lines"][0]["line"])  # newest first
        self.assertFalse(checks.run_events(Nsr(FAKE), region("glo1"), CONFIG)["configured"])


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dash.Handler.app = dash.App(CONFIG, Nsr(FAKE), ttl=60)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
        cls.port = cls.server.server_address[1]
        dash.Handler.port = cls.port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def get(self, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", path, headers={"Host": host or "127.0.0.1:%d" % self.port})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_pages_and_api(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Backend regions", body)
        self.assertEqual(self.get("/r/sta1")[0], 200)
        status, body = self.get("/api/region/sta1/reach")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["configured"])
        self.assertEqual(json.loads(self.get("/api/config")[1])["regions"][0]["site"], "ice2")

    def test_unknown_region_and_wrong_host(self):
        self.assertEqual(self.get("/api/region/nope/fabric")[0], 404)
        self.assertEqual(self.get("/api/region/ice2/nope")[0], 404)
        self.assertEqual(self.get("/", host="evil.example")[0], 403)

    def test_cache_reuses_answers(self):
        app = dash.Handler.app
        self.get("/api/region/osl1/fabric")
        before = app.nsr.calls
        self.get("/api/region/osl1/fabric")
        self.assertEqual(app.nsr.calls, before)
        self.get("/api/region/osl1/fabric?refresh=1")
        self.assertGreater(app.nsr.calls, before)


if __name__ == "__main__":
    unittest.main()
