"""Offline tests for the LON14 dashboard data workflow (standard library only).

Fixtures describe a 4-switch fabric (leaves bel1, bel2; spines bes1, bes2; designed as
leaf j sw(36+i)p1 <-> spine i sw(j)p1). In the UFM links fixture bel1's two uplinks are
crossed, so the comparison must report one swapped pair with no fabric impact.

Run:  python3 -m unittest discover -s tests -v      (from lon14-netbox-dashboard/)
"""

from __future__ import annotations

import base64
import gzip
import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
APP = HERE.parent / "app"
sys.path.insert(0, str(APP))

import design_gate  # noqa: E402
import ufm_cabling  # noqa: E402
import ufm_fetch  # noqa: E402
import validate_inputs  # noqa: E402

L, S = "sys1-lon14-p-swi-bel", "sys1-lon14-p-swi-bes"


def netbox():
    return ufm_cabling.netbox_rows(json.loads((FIX / "netbox_cables.json").read_text())["cables"])


def design():
    return ufm_cabling.load_design_topo(FIX / "design.topo", None)


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lon14-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self) -> Path:
        """The UFM links fixture converted to the scan format, as a live fetch does."""
        text, stats = ufm_fetch.links_to_lst(json.loads((FIX / "ufm_links.json").read_text()), "10.0.0.1")
        self.assertEqual(stats["cables"], 4)
        path = self.tmp / "ibdiagnet2.lst.gz"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(text)
        return path


class UfmLinks(Workspace):
    def test_links_become_four_plane_lanes(self):
        lanes, meta = ufm_cabling.read_lanes(self.scan())
        self.assertEqual(len(lanes), 16)  # 4 cables x 4 planes
        self.assertEqual(meta["unparsed"], 0)

    def test_unreadable_links_are_refused(self):
        with self.assertRaises(RuntimeError):
            ufm_fetch.links_to_lst([{"unexpected": "format"}], "10.0.0.1")

    def test_rest_worker_is_valid_python(self):
        compile(ufm_fetch.REST_WORKER, "rest_worker", "exec")
        compile(ufm_fetch.WORKER, "files_worker", "exec")


class UfmPorts(unittest.TestCase):
    def reduced_answer(self):
        """What the jump-host worker sends back: switch ports only, PORT_FIELDS each."""
        raw = json.loads((FIX / "ufm_ports.json").read_text())
        rows = [[p.get(k) for k in ufm_fetch.PORT_FIELDS] for p in raw if "-swi-" in str(p.get("system_name") or "")]
        return {"data": base64.b64encode(gzip.compress(json.dumps(rows).encode())).decode()}

    def test_port_states_in_switch_wording(self):
        ports = ufm_fetch.parse_ports(self.reduced_answer())
        self.assertEqual(sorted(ports), [L + "1", L + "2", S + "1", S + "2"])
        self.assertEqual(ports[L + "1"][(L + "1", "sw37p1")], "Active/LinkUp/800G")
        self.assertEqual(ports[L + "2"][(L + "2", "sw38p1")], "Down/Polling/")
        self.assertEqual(ports[S + "1"][(S + "1", "sw2p1")], "Initialize/LinkUp/")
        self.assertEqual(ports[S + "2"][(S + "2", "sw1p1")], "Active/LinkUp/400G")  # NDR x4
        self.assertNotIn((L + "1", "a1/73"), ports[L + "1"])  # per-plane objects are ignored

    def test_port_state_mapping(self):
        self.assertEqual(ufm_fetch.port_state("Active", "Link Up", "XDR", "4x"), "Active/LinkUp/800G")
        self.assertEqual(ufm_fetch.port_state("Armed", "Link Up", "XDR", "4x"), "Armed/LinkUp/")


class LinkStatus(unittest.TestCase):
    def test_down_and_unverified(self):
        import netbox_live_sync as service
        self.assertEqual(service.link_status(["Down/Polling/", "Active/LinkUp/800G"], 2), "down")
        self.assertEqual(service.link_status(["Initialize/LinkUp/", "Active/LinkUp/800G"], 2), "init")
        self.assertEqual(service.link_status(["Active/LinkUp/800G", "stale"], 2), "unverified")
        self.assertEqual(service.link_status(["Active/LinkUp/800G", "Active/LinkUp/800G"], 2), "active")
        self.assertEqual(service.link_status(["Active/LinkUp/800G", "not-collected"], 1), "active")  # leaf-GPU: one end


class NetBoxAndDesign(unittest.TestCase):
    def test_netbox_rows(self):
        rows, info = netbox()
        self.assertEqual(info["leaf_spine"], 4)
        self.assertEqual(info["gpu"], 1)
        self.assertEqual(info["unusable"], 1)
        flipped = next(r for r in rows if r["netbox_cable_id"] == "1004")
        self.assertEqual(flipped["endpoint_a_device"], L + "2")  # leaf end first, whatever NetBox's A side is

    def test_design_topology(self):
        rows, info = design()
        ls = {(r["a_device"], r["a_port"]): (r["b_device"], r["b_port"]) for r in rows if r["link_type"] == "leaf-spine"}
        self.assertEqual(ls[(L + "1", "sw37p1")], (S + "1", "sw1p1"))
        self.assertEqual(ls[(L + "2", "sw38p1")], (S + "2", "sw2p1"))
        self.assertEqual(info["leaf_spine"], 4)
        self.assertEqual(info["suspect_count"], 0)


class Comparison(Workspace):
    def test_crossed_uplinks_are_one_swap_without_impact(self):
        nb_rows, nb_info = netbox()
        rows, info = design()
        report = ufm_cabling.analyse(self.scan(), nb_rows, None, None, None, dict(nb_info, kind="NetBox + design"), rows, info, combined=True)
        s = report["summary"]
        self.assertEqual(s["switch_miscabled"], 2)
        self.assertEqual(s["swaps"], 1)
        self.assertEqual(s["switch_ok"], 2)
        mis = [f for f in report["switch_findings"] if f["status"] == "miscabled"]
        self.assertTrue(all(f["impact"] == "port-swap" for f in mis))
        self.assertTrue(all(f["design_state"] == "matches-expected" for f in mis))  # NetBox and design agree: the cable is wrong

    def test_netbox_record_disagreeing_with_ufm_and_design(self):
        cables = json.loads((FIX / "netbox_cables.json").read_text())["cables"]
        cables["1003"] = [L + "2", "sw37p1", S + "2", "sw9p1"]  # NetBox wrong; UFM and design agree
        nb_rows, nb_info = ufm_cabling.netbox_rows(cables)
        rows, info = design()
        report = ufm_cabling.analyse(self.scan(), nb_rows, None, None, None, dict(nb_info, kind="NetBox + design"), rows, info, combined=True)
        statuses = {tuple(f["leaf"]): f["status"] for f in report["switch_findings"]}
        self.assertEqual(statuses[(L + "2", "sw37p1")], "netbox-differs")


class Validation(unittest.TestCase):
    def test_clean_inputs_pass(self):
        nb_rows, nb_info = netbox()
        rows, info = design()
        v = validate_inputs.validate(nb_rows, nb_info, rows, info, lanes=16)
        self.assertTrue(v["ok"], v["errors"])
        self.assertIn("netbox-unusable", [w["code"] for w in v["warnings"]])

    def test_errors_stop_the_comparison(self):
        nb_rows, nb_info = netbox()
        dup = dict(nb_rows[0])
        shared = dict(nb_rows[0], netbox_cable_id="2001", endpoint_b_device=S + "2", endpoint_b_port="sw5p1")
        bad = dict(nb_rows[1], netbox_cable_id="2002", endpoint_a_port="eth0")
        v = validate_inputs.validate(nb_rows + [dup, shared, bad], nb_info, None, None, lanes=16)
        codes = {e["code"] for e in v["errors"]}
        self.assertFalse(v["ok"])
        self.assertTrue({"netbox-duplicate-id", "netbox-shared-port", "netbox-port-name"} <= codes, codes)

    def test_missing_columns_and_empty_scan(self):
        v = validate_inputs.validate([{"netbox_cable_id": "1"}], None, None, None, lanes=0)
        codes = {e["code"] for e in v["errors"]}
        self.assertTrue({"netbox-columns", "scan-empty"} <= codes, codes)

    def test_conflicting_design_rows(self):
        rows, info = design()
        rows = rows + [dict(rows[0], b_port="sw9p1")]
        v = validate_inputs.validate(netbox()[0], None, rows, info, lanes=16)
        self.assertIn("design-conflict", {e["code"] for e in v["errors"]})


class DesignGate(Workspace):
    def test_first_fetch_is_unapproved_then_approved(self):
        data = (FIX / "design.topo").read_bytes()
        self.assertEqual(design_gate.ingest_fetched(data, self.tmp)["state"], "unapproved")
        sha = design_gate.status(self.tmp)["active_sha256"]
        self.assertEqual(design_gate.approve(self.tmp, sha, by="tester")["state"], "approved")
        self.assertEqual(design_gate.ingest_fetched(data, self.tmp)["state"], "approved")

    def test_changed_design_waits_for_review(self):
        data = (FIX / "design.topo").read_bytes()
        design_gate.ingest_fetched(data, self.tmp)
        design_gate.approve(self.tmp, design_gate.status(self.tmp)["active_sha256"], by="tester")
        edited = data.replace(b"P75 -4x-100G-> Q3400 sys1-lon14-p-swi-bes2 P1", b"P75 -4x-100G-> Q3400 sys1-lon14-p-swi-bes2 P5")
        state = design_gate.ingest_fetched(edited, self.tmp)
        self.assertEqual(state["state"], "change-pending")
        self.assertGreaterEqual(state["diff"]["changed_ports"], 1)
        self.assertEqual((self.tmp / design_gate.ACTIVE).read_bytes(), data)  # the approved copy is still the one in use
        new = design_gate.status(self.tmp)["candidate_sha256"]
        design_gate.approve(self.tmp, new, by="tester", note="CHG-1")
        self.assertEqual((self.tmp / design_gate.ACTIVE).read_bytes(), edited)
        manifest = json.loads((self.tmp / design_gate.MANIFEST).read_text())
        self.assertEqual(manifest["note"], "CHG-1")
        self.assertEqual(len(manifest["history"]), 1)

    def test_local_edit_of_the_approved_copy_is_detected(self):
        data = (FIX / "design.topo").read_bytes()
        design_gate.ingest_fetched(data, self.tmp)
        design_gate.approve(self.tmp, design_gate.status(self.tmp)["active_sha256"], by="tester")
        (self.tmp / design_gate.ACTIVE).write_bytes(data + b"\n")
        self.assertEqual(design_gate.status(self.tmp)["state"], "tampered")

    def test_unknown_hash_cannot_be_approved(self):
        design_gate.ingest_fetched((FIX / "design.topo").read_bytes(), self.tmp)
        with self.assertRaises(RuntimeError):
            design_gate.approve(self.tmp, "0" * 64, by="tester")


class TlsPin(Workspace):
    def test_first_fingerprint_is_saved_and_a_change_refused(self):
        pins = ufm_fetch.load_pins(self.tmp)
        ufm_fetch.save_pin(self.tmp, pins, "10.0.0.1", "aa" * 32)
        self.assertEqual(ufm_fetch.load_pins(self.tmp), {"10.0.0.1": "aa" * 32})
        with self.assertRaises(RuntimeError):
            ufm_fetch.save_pin(self.tmp, ufm_fetch.load_pins(self.tmp), "10.0.0.1", "bb" * 32)


class RequestProtection(unittest.TestCase):
    """POST endpoints start collection jobs: only this service's own page may call them."""

    @classmethod
    def setUpClass(cls):
        import netbox_live_sync as service

        class Probe(service.Handler):
            pass
        Probe.state = None  # the protection answers before any state is touched
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Probe)
        Probe.port = cls.server.server_address[1]
        cls.port, cls.token = Probe.port, Probe.csrf
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, method, path, headers=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_post_without_token_is_refused(self):
        self.assertEqual(self.call("POST", "/api/sync"), 403)

    def test_cross_site_post_is_refused(self):
        self.assertEqual(self.call("POST", "/api/sync", {"X-LON14-CSRF": self.token, "Origin": "https://example.com"}), 403)

    def test_foreign_host_header_is_refused(self):
        self.assertEqual(self.call("GET", "/api/live", {"Host": "attacker.example:%d" % self.port}), 403)


class Lon14Rules(unittest.TestCase):
    """The LON14 numbering: SU k = BEL4k-3..4k, rail r = BEL4(k-1)+r, pod = 4 SUs."""

    def test_su_and_rail_of_leaf(self):
        self.assertEqual(ufm_cabling.su_of_leaf(1), (1, 1, 1))
        self.assertEqual(ufm_cabling.su_of_leaf(6), (2, 1, 2))
        self.assertEqual(ufm_cabling.su_of_leaf(16), (4, 1, 4))
        self.assertEqual(ufm_cabling.su_of_leaf(17), (5, 2, 1))
        self.assertEqual(ufm_cabling.su_of_leaf(64), (16, 4, 4))
        for su in range(1, 17):
            self.assertEqual([ufm_cabling.su_of_leaf(l)[0] for l in ufm_cabling.su_leaves(su)], [su] * 4)
            self.assertEqual([ufm_cabling.su_of_leaf(l)[2] for l in ufm_cabling.su_leaves(su)], [1, 2, 3, 4])

    def test_tray_names(self):
        self.assertEqual(ufm_cabling.TRAY.match("nvl72d031-T14 mlx5_2").groups(), ("nvl72d031-T14", "mlx5_2"))
        self.assertEqual(ufm_cabling.TRAY.match("sys1-lon14-p-phy-gpu7 mlx5_0").group(1), "sys1-lon14-p-phy-gpu7")
        self.assertIsNone(ufm_cabling.TRAY.match("localhost mlx5_0"))
        self.assertIsNone(ufm_cabling.TRAY.match("MT4131 ConnectX8 Mellanox Technologies"))

    def test_netbox_leaf_spine_cables_follow_the_rule(self):
        import csv
        with (HERE.parent / "assets" / "connections.csv").open(encoding="utf-8") as handle:
            rows = [r for r in csv.DictReader(handle) if r["connection_type"] == "leaf-spine"]
        self.assertEqual(len(rows), 4608)
        for r in rows:
            j = int(r["endpoint_a_device"].rsplit("bel", 1)[1]); i = int(r["endpoint_b_device"].rsplit("bes", 1)[1])
            n, m = map(int, r["endpoint_b_port"][2:].split("p"))
            self.assertEqual(n, j, r)
            self.assertEqual(r["endpoint_a_port"], "sw%dp%d" % (73 - i, 3 - m), r)

    def test_design_file_is_optional(self):
        tmp = Path(tempfile.mkdtemp(prefix="lon14-test-"))
        try:
            prof = tmp / "p.ini"
            prof.write_text("[lon14]\nssh_user = u\n\n[ufm]\nrest_user = r\nrest_keychain_service = s\nrest_keychain_account = r\ndesign_path = none\n")
            got = ufm_fetch.read_profile(prof)
            self.assertIsNone(got["design"])
            self.assertEqual(got["jump_host"], "lon14deploy1")
            self.assertEqual(got["ufm_hosts"], ["10.2.64.75", "10.2.64.76"])
            prof.write_text("[ufm]\nrest_user = r\nrest_keychain_service = s\nrest_keychain_account = r\n")
            self.assertEqual(ufm_fetch.read_profile(prof)["design"], "/root/nscale_Compute.topo")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
