"""ICE2 SUs: NetBox location row of the leaves (registry rule), SU slices from the cabling."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import ufm_cabling as u  # noqa: E402


def placement(rows=None, trays=None):
    rows = rows or {}
    out = {"sys1-ice2-p-swi-bel%d" % l: {"location": rows.get(l, "Row " + "ABCD"[(l - 1) // 16]), "rack": ""} for l in range(1, 65)}
    out.update(trays or {})
    return out


class SliceTests(unittest.TestCase):
    def test_slices_are_interleaved_leaves(self):
        self.assertEqual(u.slice_leaves(1), [1, 5, 9, 13])
        self.assertEqual(u.slice_leaves(6), [18, 22, 26, 30])
        for n in range(1, 17):
            self.assertEqual([u.slice_of_leaf(l)[0] for l in u.slice_leaves(n)], [n] * 4)
            self.assertEqual([u.slice_of_leaf(l)[2] for l in u.slice_leaves(n)], [1, 2, 3, 4])


class UnitTests(unittest.TestCase):
    def slices(self):
        return {n: {"su": n, "leaves": u.slice_leaves(n)} for n in range(1, 17)}

    def test_row_gives_su(self):
        self.assertEqual(u.su_of_location("Row C"), 3)
        self.assertEqual(u.su_of_location(" row b "), 2)
        self.assertIsNone(u.su_of_location("Spare A"))
        self.assertIsNone(u.su_of_location(""))

    def test_four_sus_of_four_slices(self):
        sl = self.slices()
        u.assign_units(sl, u.placement_units(placement())[0])
        self.assertEqual([sl[n]["label"] for n in (1, 4, 5, 16)], ["SU1 slice 1", "SU1 slice 4", "SU2 slice 1", "SU4 slice 4"])
        self.assertTrue(all(s["unit_problem"] == "" for s in sl.values()))

    def test_fail_closed_without_netbox(self):
        sl = self.slices()
        u.assign_units(sl, {})
        self.assertEqual(sl[3]["label"], "SU slice 3")
        self.assertIsNone(sl[3]["unit"])
        self.assertIn("Refresh NetBox", sl[3]["unit_problem"])

    def test_leaf_without_row_or_in_two_rows(self):
        sl = self.slices()
        u.assign_units(sl, u.placement_units(placement({5: "", 2: "Row B"}))[0])
        self.assertIsNone(sl[1]["unit"])
        self.assertIn("BEL5", sl[1]["unit_problem"])
        self.assertIsNone(sl[2]["unit"])
        self.assertIn("SU1, SU2", sl[2]["unit_problem"])
        self.assertEqual(sl[3]["label"], "SU1 slice 3")  # numbers do not shift

    def test_tray_placement_keyed_by_gpu_number(self):
        _, trays = u.placement_units(placement(trays={"sys1-ice2-p-phy-gpu1733": {"location": "Row A", "rack": "Spare A"}}))
        self.assertEqual(trays["gpu1733"]["rack"], "Spare A")
        self.assertEqual(u.tray_key("gpu1733"), "gpu1733")
        self.assertEqual(u.tray_key("slice5 slot 3"), "")


if __name__ == "__main__":
    unittest.main()
