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
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import checks  # noqa: E402
import engines  # noqa: E402
import fabric  # noqa: E402
import netbox_source  # noqa: E402
import ufm_direct  # noqa: E402
import multi_region_dashboard as dash  # noqa: E402
from nsr_client import Nsr, NsrError  # noqa: E402

FAKE = str(HERE / "fake_nsr.py")
sys.path.insert(0, str(HERE))
os.environ["NETBOX_TOKEN"] = "test-token"  # the fake NetBox only checks that a token is sent
import fake_netbox  # noqa: E402
import fake_nsr  # noqa: E402
NB_SERVER = fake_netbox.serve(0)
threading.Thread(target=NB_SERVER.serve_forever, daemon=True).start()
NB_URL = "http://127.0.0.1:%d" % NB_SERVER.server_address[1]
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
            Nsr(FAKE, direct=False).run(("netbox", "raw"), "/api/dcim/devices/")

    def test_missing_binary_explains_install(self):
        with self.assertRaises(NsrError) as ctx:
            Nsr("/nonexistent/nsr", direct=False).query("up")
        self.assertIn("uv tool install", ctx.exception.hint)

    def test_failed_query_carries_hint(self):
        os.environ["FAKE_NSR_FAIL"] = "boom_metric"
        try:
            with self.assertRaises(NsrError) as ctx:
                Nsr(FAKE, direct=False).query("boom_metric")
            self.assertIn("400", str(ctx.exception))
        finally:
            del os.environ["FAKE_NSR_FAIL"]


class Overview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        nsr = Nsr(FAKE, direct=False)
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

    def test_incidents_carry_a_pagerduty_list_link(self):
        incidents = checks.fetch_incidents(Nsr(FAKE, direct=False), CONFIG)
        self.assertIn("/incidents", incidents["list_url"])
        if CONFIG.get("pagerduty_service"):
            self.assertIn(CONFIG["pagerduty_service"], incidents["list_url"])
        region = checks.incidents_for(incidents, CONFIG["regions"][0])
        self.assertEqual(region["list_url"], incidents["list_url"])  # the region view keeps it

    def test_a_failing_family_is_reported_not_hidden(self):
        os.environ["FAKE_NSR_FAIL"] = "probe_success"
        try:
            data = checks.run_overview(Nsr(FAKE, direct=False), CONFIG, {"state": "ok", "incidents": []})
        finally:
            del os.environ["FAKE_NSR_FAIL"]
        self.assertTrue(any(e["key"].startswith("ping") for e in data["errors"]))
        ice2 = next(r for r in data["regions"] if r["site"] == "ice2")
        self.assertIsNone(ice2["metrics"]["ping_up"])
        self.assertEqual(ice2["metrics"]["ufm_ports"], 9216)


class Sections(unittest.TestCase):
    def test_fabric_offenders(self):
        out = checks.run_section(Nsr(FAKE, direct=False), region("ice2"), CONFIG, "fabric")
        byid = {c["id"]: c for c in out["checks"]}
        self.assertEqual(byid["ufm_ports"]["value"], 9216)
        self.assertEqual(byid["ufm_nonactive"]["issues"], 2)
        self.assertEqual(byid["ufm_linkdown"]["rows"][0]["labels"]["port_id"], "2801cd0300784c80_85")
        self.assertEqual(byid["ufm_xmit_wait"]["issues"], 0, "info checks never count as issues")

    def test_no_ufm_data_marks_offender_lists_no_data(self):
        out = checks.run_section(Nsr(FAKE, direct=False), region("sta1"), CONFIG, "fabric")
        self.assertTrue(all(c["state"] == "no-data" for c in out["checks"]))

    def test_gnmi_only_where_collected(self):
        self.assertFalse(checks.run_section(Nsr(FAKE, direct=False), region("osl1"), CONFIG, "switches")["configured"])
        out = checks.run_section(Nsr(FAKE, direct=False), region("lon14"), CONFIG, "switches")
        psu = next(c for c in out["checks"] if c["id"] == "gnmi_psu_fan")
        self.assertEqual(psu["rows"][0]["labels"]["component_name"], "PSU 2")

    def test_ufm_servers_flag_only_down_ones(self):
        out = checks.run_section(Nsr(FAKE, direct=False), region("osl1"), CONFIG, "reach")
        srv = next(c for c in out["checks"] if c["id"] == "ufm_servers")
        self.assertEqual((len(srv["rows"]), srv["issues"]), (2, 1))

    def test_ufm_servers_use_ha_roles_where_the_command_exporter_reports(self):
        out = checks.run_section(Nsr(FAKE, direct=False), region("ice2"), CONFIG, "reach")
        srv = next(c for c in out["checks"] if c["id"] == "ufm_servers")
        self.assertIn("HA cluster status", srv["title"])
        self.assertEqual(sorted(r["labels"]["role"] for r in srv["rows"]), ["master (primary)", "standby (secondary)"])
        self.assertEqual(srv["issues"], 0)                          # a healthy standby is not "down"
        pair = next(c for c in out["checks"] if c["id"] == "ufm_ha_pair")
        self.assertEqual(pair["issues"], 0)
        osl1 = checks.run_section(Nsr(FAKE, direct=False), region("osl1"), CONFIG, "reach")
        self.assertEqual(next(c for c in osl1["checks"] if c["id"] == "ufm_ha_pair")["issues"], 1)
        sta1 = checks.run_section(Nsr(FAKE, direct=False), region("sta1"), CONFIG, "reach")
        self.assertIn("monitoring scrape", next(c for c in sta1["checks"] if c["id"] == "ufm_servers")["title"])

    def test_overview_counts_ufm_servers_by_ha_role(self):
        data = checks.run_overview(Nsr(FAKE, direct=False), CONFIG, {"state": "ok", "incidents": []})
        by = {r["site"]: r["metrics"] for r in data["regions"]}
        self.assertEqual((by["ice2"]["ufm_server_up"], by["ice2"]["ufm_server_total"]), (2, 2))
        self.assertEqual((by["osl1"]["ufm_server_up"], by["osl1"]["ufm_server_total"]), (1, 2))

    def test_events(self):
        out = checks.run_events(Nsr(FAKE, direct=False), region("sta1"), CONFIG)
        ufm = next(s for s in out["streams"] if s["id"] == "ufm_events")
        self.assertEqual(len(ufm["lines"]), 2)
        self.assertIn("Switch is down", ufm["lines"][0]["line"])  # newest first
        self.assertFalse(checks.run_events(Nsr(FAKE, direct=False), region("glo1"), CONFIG)["configured"])


class FabricModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = netbox_source.pull_fabric(netbox_source.NetBox(NB_URL), region("osl1")["fabric_view"])
        cls.topo = fabric.build_topology(cls.snap)

    def test_port_numbers(self):
        self.assertEqual(fabric.port_number("sw33p1"), 65)
        self.assertEqual(fabric.port_number("sw1p2"), 2)
        self.assertEqual(fabric.port_number("IB1/17/2"), 34)
        self.assertEqual(fabric.port_number("IB1/17"), 17)
        self.assertEqual(fabric.port_number("swp17s0"), 33)
        self.assertEqual(fabric.port_number("swp17s1"), 34)
        self.assertIsNone(fabric.port_number("mgmt0", "swNpM"))
        self.assertEqual(fabric.short_name("sys2-osl1-p-ifs-bel12"), "bel12")
        self.assertEqual(fabric.short_name("ifs398"), "ifs398")

    def test_structure_is_inferred(self):
        t = self.topo
        self.assertEqual((len(t["spines"]), len(t["leaves"]), t["rails"]), (4, 16, 8))
        self.assertEqual([len(su["nodes"]) for su in t["sus"]], [6, 6], "nodes with a missing/misplaced NIC stay in their SU")
        self.assertEqual({l["short"]: l["rail"] for l in t["leaves"]}["bel13"], 6)
        gpu004 = next(n for n in t["nodes"] if n["name"].endswith("gpu004"))
        self.assertEqual(sorted(l["rail"] for l in gpu004["links"]), [1, 2, 3, 4, 5, 7, 8], "ib6 stays rail 7")
        self.assertEqual([o["name"] for o in t["others"]], ["sys2-osl1-p-phy-ufm0"])

    def test_design_checks(self):
        text = " ".join(c["text"] for c in self.topo["checks"])
        self.assertIn("Full mesh", text)
        self.assertIn("fewer than 8 rails: sys2-osl1-p-phy-gpu004", text)
        self.assertIn("phy-gpu009 ib1→bel10", text)

    def test_live_overlay_matches_ports(self):
        r = region("osl1")
        live = fabric.live_overlay(Nsr(FAKE, direct=False), self.topo, r["sources"]["ufm"]["selector"], "infra-mimir", None, "infra-mimir")
        self.assertEqual(live["coverage"]["matched"], live["coverage"]["cables"])
        self.assertEqual(live["switches"]["sys2-osl1-p-ifs-bel3"]["nonactive"], 1)

    def test_missing_fabric_is_explained(self):
        with self.assertRaises(netbox_source.NetBoxError) as ctx:
            netbox_source.pull_fabric(netbox_source.NetBox(NB_URL), region("sta1")["fabric_view"])
        self.assertIn("sys2-sta1", str(ctx.exception))

    def test_netbox_is_read_directly_not_through_nsr(self):
        self.assertTrue(self.snap["source"].startswith("netbox http://127.0.0.1"))
        self.assertNotIn(("netbox", "connections"), __import__("nsr_client").ALLOWED)

    def test_ufm_links_vs_netbox(self):
        leaves = fake_nsr.OSL1_LEAVES
        records = []
        for c in fake_nsr.osl1_cables():
            m = __import__("re").match(r"sw(\d+)p(\d+)$", c["interface"])
            port = 2 * (int(m.group(1)) - 1) + int(m.group(2))
            peer = c["remote_device"].split("-")[-1]          # UFM names hosts without the site prefix
            if c["device"] == leaves[0] and port == 3:
                peer = "gpu011"                              # the cable at bel0 port 3 goes elsewhere
            records.append({"source_port_node_description": "%s:%s" % (c["device"], c["interface"]), "source_port": port,
                            "destination_port_node_description": "%s %s" % (peer, c["remote_interface"]), "destination_port": 1})
        records.append({"source_port_node_description": leaves[1] + ":sw30p1", "source_port": 59,
                        "destination_port_node_description": "gpu099 mlx5_0", "destination_port": 1})
        switches = {s["name"] for s in self.topo["spines"]} | {l["name"] for l in self.topo["leaves"]}
        result = fabric.compare_with_ufm(self.topo, ufm_direct.links_by_switch_port(records, switches))
        k = result["counts"]
        self.assertEqual((k["miscabled"], k["not_in_netbox"]), (1, 1))
        self.assertEqual(k["match"], 223, "leaf-spine, GPU and the UFM server cable")
        bad = result["rows"][0]
        self.assertEqual((bad["status"], bad["leaf"], bad["actual"]), ("miscabled", leaves[0], "gpu011"))

    def test_direct_ufm_node_profile(self):
        import tempfile
        ini = Path(tempfile.mkdtemp()) / "p.ini"
        ini.write_text("[ufm.sta1]\nufm_node = sta1-p-phy-ufm2\njump_host =\njump_user =\n"
                       "hosts = 127.0.0.1 10.40.40.54\nrest_user = ro\nrest_keychain_service = idc-automation-sta1-ufm-rest\n")
        cfg = ufm_direct.read_profile(ini, "sta1")
        self.assertTrue(cfg["direct"])
        cmd = ufm_direct.tsh_command(cfg)
        self.assertEqual(cmd[:3], ["tsh", "ssh", "sta1-p-phy-ufm2"], "no --login when none is set; straight to the UFM node")
        self.assertIn("platform-python", cmd[3])
        ini.write_text("[ufm.osl1]\njump_host = jmp-osl1\njump_user = me\nhosts = 10.50.40.9\nrest_user = ro\n"
                       "rest_keychain_service = s\n")
        cfg = ufm_direct.read_profile(ini, "osl1")
        self.assertEqual(ufm_direct.tsh_command(cfg)[:5], ["tsh", "ssh", "--login", "me", "jmp-osl1"])
        ini.write_text("[ufm.glo1]\nhosts = 1.2.3.4\nrest_user = ro\nrest_keychain_service = s\n")
        with self.assertRaises(ufm_direct.UfmError):
            ufm_direct.read_profile(ini, "glo1")

    def test_same_device_spellings(self):
        self.assertTrue(ufm_direct.same_device("sys2-osl1-p-phy-gpu012", "gpu012"))
        self.assertTrue(ufm_direct.same_device("ifs398", "ifs398"))
        self.assertFalse(ufm_direct.same_device("sys2-osl1-p-phy-gpu012", "gpu013"))


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        dash.CACHE_DIR = Path(tempfile.mkdtemp(prefix="fabric-cache-"))
        dash.Handler.app = dash.App(CONFIG, Nsr(FAKE, direct=False), ttl=60, netbox_url=NB_URL, with_engines=False)
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

    def post(self, path, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": "127.0.0.1:%d" % self.port}
        if origin:
            headers["Origin"] = origin
        conn.request("POST", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_fabric_view_flow(self):
        import time as _t
        self.assertEqual(self.get("/f/osl1")[0], 200)
        self.assertEqual(self.get("/api/fabric/ice2")[0], 404, "ICE2 keeps its own engine")
        self.assertEqual(self.post("/api/fabric/osl1/netbox", origin="https://evil.example")[0], 403)
        self.assertEqual(self.post("/api/fabric/osl1/netbox")[0], 202)
        for _ in range(50):
            job = json.loads(self.get("/api/fabric/osl1/netbox")[1])
            if job["state"] != "running":
                break
            _t.sleep(0.1)
        self.assertEqual(job["state"], "done")
        view = json.loads(self.get("/api/fabric/osl1")[1])
        self.assertEqual(view["topology"]["rails"], 8)
        self.assertEqual(view["live"]["coverage"]["matched"], 223)
        self.assertIsNone(view["cabling"], "no UFM links fetched yet")

    def test_cache_reuses_answers(self):
        app = dash.Handler.app
        self.get("/api/region/osl1/fabric")
        before = app.nsr.calls
        self.get("/api/region/osl1/fabric")
        self.assertEqual(app.nsr.calls, before)
        self.get("/api/region/osl1/fabric?refresh=1")  # Refresh within min_force: still shared
        self.assertEqual(app.nsr.calls, before)
        app.cache.min_force = 0
        self.get("/api/region/osl1/fabric?refresh=1")
        self.assertGreater(app.nsr.calls, before)

    def test_stale_answer_served_while_one_background_refresh_runs(self):
        from nsr_client import Cache
        cache, computes, gate = Cache(ttl=0), [], threading.Event()

        def compute():
            computes.append(1)
            if len(computes) > 1:
                gate.wait(5)
            return len(computes)

        self.assertEqual(cache.get("k", compute), 1)                  # first request waits once
        answers = [cache.get("k", compute) for _ in range(5)]          # five tabs, answer is stale
        self.assertEqual(answers, [1] * 5)                             # all served at once
        gate.set()
        for _ in range(100):
            if not cache.running:
                break
            time.sleep(0.02)
        self.assertEqual(len(computes), 2)                             # one refresh for all of them
        self.assertEqual(cache.items["k"][1], 2)                       # and it replaced the answer


if __name__ == "__main__":
    unittest.main()


class EngineProxy(unittest.TestCase):
    """The single service starts an engine and serves it under /<site>/ with its guards intact."""

    FAKE_ENGINE = """
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def ok(self):
        return self.headers.get("Host") == "127.0.0.1:%d" % port
    def send(self, code, body, ctype="application/json"):
        data = body.encode(); self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if not self.ok(): return self.send(403, "{}")
        if self.path == "/api/health": return self.send(200, json.dumps({"ok": True}))
        if self.path == "/": return self.send(200, '<html><head></head><body><a href="/traffic-paths.html">t</a><script>fetch("/api/live")</script></body></html>', "text/html")
        return self.send(200, json.dumps({"path": self.path}))
    def do_POST(self):
        origin = self.headers.get("Origin")
        if not self.ok() or (origin and origin != "http://127.0.0.1:%d" % port): return self.send(403, "{}")
        n = int(self.headers.get("Content-Length") or 0)
        self.send(200, json.dumps({"posted": self.rfile.read(n).decode(), "csrf": self.headers.get("X-LON14-CSRF")}))
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
"""

    @classmethod
    def setUpClass(cls):
        import socket
        import tempfile
        root = Path(tempfile.mkdtemp(prefix="engine-"))
        (root / "eng" / "app").mkdir(parents=True)
        (root / "eng" / "app" / "netbox_live_sync.py").write_text(cls.FAKE_ENGINE)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            free = sock.getsockname()[1]
        cls.engine = engines.Engine("ice2", {"dir": "eng", "port": free, "profile": "/nonexistent"}, root, NB_URL, root)
        cls.engine.start()
        app = dash.App(CONFIG, Nsr(FAKE, direct=False), ttl=60, netbox_url=NB_URL, with_engines=False)
        app.engines = {"ice2": cls.engine}
        dash.Handler.app = app
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
        cls.port = cls.server.server_address[1]
        dash.Handler.port = cls.port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.engine.stop()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, body=body, headers={"Host": "127.0.0.1:%d" % self.port, **(headers or {})})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_engine_started_and_page_rewritten(self):
        self.assertTrue(self.engine.status()["running"])
        status, page = self.req("GET", "/ice2/")
        self.assertEqual(status, 200)
        self.assertIn(b'href="/ice2/traffic-paths.html"', page)
        self.assertIn(b'var B="/ice2"', page, "fetch shim prefixes /api/ calls")
        self.assertNotIn(b"nsr-live", page, "the nsr panel goes only on the main dashboard page (id=rail)")
        page = engines.rewrite_html('<html><head></head><body><div class="rail" id="rail"></div></body></html>', "ice2")
        self.assertIn('data-site="ice2" data-after="#rail"', page)
        self.assertIn('<script src="/assets/nsr_panel.js">', page, "added after the prefix rewrite, so served by this service")
        status, js = self.req("GET", "/assets/nsr_panel.js")
        self.assertEqual(status, 200)
        import theme
        self.assertIn(theme.light("#2dd4bf").encode(), js)  # served in the white theme by default
        self.assertEqual(self.req("GET", "/ice2")[0], 302)

    def test_api_and_post_pass_the_engine_guards(self):
        self.assertEqual(json.loads(self.req("GET", "/ice2/api/live?x=1")[1])["path"], "/api/live?x=1")
        status, body = self.req("POST", "/ice2/api/sync", body=b"{}", headers={
            "Origin": "http://127.0.0.1:%d" % self.port, "X-LON14-CSRF": "tok", "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"posted": "{}", "csrf": "tok"})
        self.assertEqual(self.req("POST", "/ice2/api/sync", headers={"Origin": "https://evil.example"})[0], 403)

    def test_region_config_marks_engines(self):
        cfg = json.loads(self.req("GET", "/api/config")[1])
        self.assertTrue(next(r for r in cfg["regions"] if r["site"] == "ice2")["engine"])
