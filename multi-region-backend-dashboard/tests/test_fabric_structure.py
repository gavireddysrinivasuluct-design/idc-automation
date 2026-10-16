"""Planes and SU slices from links alone (gemelo's method), and the cross-check on the rules."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fabric  # noqa: E402
import fabric_structure as fs  # noqa: E402


def mesh(leaves, spines):
    return {l: set(spines) for l in leaves}


def trays(slices, per=3):
    hosts = {}
    for i, leaves in enumerate(slices):
        for t in range(per):
            for l in leaves:
                hosts.setdefault(l, set()).add("tray%d-%d" % (i, t))
    return hosts


class Structure(unittest.TestCase):
    leaves = ["bel%d" % i for i in range(1, 9)]
    spines = ["bes1", "bes2", "bes3"]
    rule = {"SU1 slice 1": ["bel1", "bel2", "bel3", "bel4"], "SU1 slice 2": ["bel5", "bel6", "bel7", "bel8"]}

    def test_one_plane_and_slices_agree(self):
        st = fs.infer(self.leaves, mesh(self.leaves, self.spines), trays(list(self.rule.values())))
        self.assertEqual(len(st["planes"]), 1)
        self.assertEqual(st["planes"][0]["slices"], list(self.rule.values()))
        check = fs.compare(st, self.rule)
        self.assertEqual(check["agree"], list(self.rule))
        self.assertIn("2 of 2 agree", fs.summary(st, check))

    def test_one_stray_cable_never_merges(self):
        hosts = trays(list(self.rule.values()))
        hosts["bel5"].add("tray0-0")  # one tray of slice 1 also cabled to bel5
        st = fs.infer(self.leaves, mesh(self.leaves, self.spines), hosts)
        self.assertEqual(len(st["planes"][0]["slices"]), 2)
        self.assertEqual(st["stray"], ["tray0-0"])

    def test_wrong_leaf_group_differs(self):
        st = fs.infer(self.leaves, mesh(self.leaves, self.spines), trays([["bel1", "bel2", "bel3", "bel5"], ["bel4", "bel6", "bel7", "bel8"]]))
        check = fs.compare(st, self.rule)
        self.assertEqual([d["label"] for d in check["differ"]], list(self.rule))

    def test_two_planes_and_a_straddling_leaf(self):
        up = {**mesh(self.leaves[:4], ["bes1", "bes2"]), **mesh(self.leaves[4:], ["bes3", "bes4"])}
        up["bel8"] = {"bes3", "bes4", "bes1"}
        st = fs.infer(self.leaves, up, {})
        self.assertEqual(len(st["planes"]), 2)
        self.assertEqual(st["straddling"], ["bel8"])

    def test_leaves_without_trays_are_untested(self):
        st = fs.infer(self.leaves, mesh(self.leaves, self.spines), trays([self.rule["SU1 slice 1"]]))
        self.assertEqual(fs.compare(st, self.rule)["untested"], ["SU1 slice 2"])

    def test_the_three_copies_are_identical(self):
        here = (ROOT / "fabric_structure.py").read_text()
        for site in ("ice2-netbox-dashboard", "lon14-netbox-dashboard"):
            self.assertEqual((ROOT.parent / site / "app" / "fabric_structure.py").read_text(), here, site)


class ManagementPorts(unittest.TestCase):
    def test_out_of_band_switch_on_mgmt_ports_is_not_a_gpu_node(self):
        devices = {n: {"name": n, "role": fabric.LEAF} for n in ("x-bel1", "x-bel2")}
        devices["x-bes1"] = {"name": "x-bes1", "role": fabric.SPINE}
        cables = [{"device": l, "interface": "sw1p1", "remote_device": "x-bes1", "remote_interface": "sw%dp1" % i, "state": "connected", "cable": i}
                  for i, l in enumerate(("x-bel1", "x-bel2"), 1)]
        for i, l in enumerate(("x-bel1", "x-bel2")):
            cables.append({"device": l, "interface": "mgmt0", "remote_device": "x-obl1", "remote_interface": "eth%d" % i, "state": "connected", "cable": 10 + i})
            for g in range(3):
                cables.append({"device": l, "interface": "sw%dp1" % (g + 2), "remote_device": "x-gpu%d" % g, "remote_interface": "mlx5_%d" % i, "state": "connected", "cable": 20 + 10 * i + g})
        t = fabric.build_topology({"devices": devices, "cables": cables})
        self.assertEqual([n["name"] for n in t["nodes"]], ["x-gpu0", "x-gpu1", "x-gpu2"])
        self.assertEqual([m["name"] for m in t["management"]], ["x-obl1"])
        self.assertEqual(t["structure"]["stray"], [])


if __name__ == "__main__":
    unittest.main()
