"""NetBox cable-record checks: status, ports two cables claim, and their incident."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import incidents  # noqa: E402
import netbox_live_sync as nls  # noqa: E402


class CableQuality(unittest.TestCase):
    baseline = {"1": {}, "2": {}, "3": {}, "4": {}}
    cables = {"1": ["bel1", "sw1p1", "bes1", "sw1p1"], "2": ["bel1", "sw1p1", "gpu5", "mlx5_0"],
              "3": ["bel2", "sw1p1", "bes1", "sw2p1"], "4": ["", "", "", "", "1 B-side terminations"]}

    def test_status_value(self):
        self.assertEqual(nls.cable_status({"status": {"value": "planned", "label": "Planned"}}), "planned")
        self.assertEqual(nls.cable_status({}), "")

    def test_not_connected_and_shared_ports(self):
        q = nls.cable_quality(self.baseline, self.cables, {"1": "connected", "2": "connected", "3": "planned", "4": ""})
        self.assertEqual(q["not_connected"], [[3, "planned", "bel2", "sw1p1", "bes1", "sw2p1"]])
        self.assertEqual(q["shared_ports"], [["bel1", "sw1p1", [1, 2]]])

    def test_unknown_status_is_not_flagged(self):
        self.assertEqual(nls.cable_quality(self.baseline, self.cables, {})["not_connected"], [])

    def test_incident_lists_each_kind(self):
        out = incidents.Incidents()
        incidents.netbox_records(out, {"mismatches": [[4, "", "", "", "", "1 B-side terminations"], [5, "bel1", "sw2p1", "bes2", "sw1p1", "endpoint changed"]], "missing": [9],
                                       "quality": nls.cable_quality(self.baseline, self.cables, {"3": "planned"})})
        found = out.result()["incidents"]
        self.assertEqual(len(found), 1)
        self.assertIn("4 NetBox cable records with problems", found[0]["title"])
        self.assertTrue(any("#3 is planned" in e for e in found[0]["evidence"]))
        out2 = incidents.Incidents()
        incidents.netbox_records(out2, {"mismatches": [], "missing": [], "quality": {}})
        self.assertEqual(out2.result()["incidents"], [])


if __name__ == "__main__":
    unittest.main()
