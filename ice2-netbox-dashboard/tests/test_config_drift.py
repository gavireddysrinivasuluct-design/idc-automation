"""Running config vs the network-deployment standard: parsing, the diff, masking, the store."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import config_drift as cd  # noqa: E402

STANDARD = [{"set": {"system": {"hostname": "bel1", "aaa": {"user": {"admin": {"hashed-password": "$6$abc"}}}},
                     "interface": {"sw1p1": {"type": "swp"}, "sw2p1": {}}}}]
RUNNING = [{"header": {"rev-id": "applied", "model": "Q3400"}},
           {"set": {"system": {"hostname": "bel1", "aaa": {"user": {"admin": {"hashed-password": "$6$xyz"}}}},
                    "interface": {"sw1p1": {"type": "swp", "description": "by hand"}, "sw2p1": {"type": "swp"}},
                    "qos": {"roce": {}}}}]


class Diff(unittest.TestCase):
    def test_normalise_merges_set_and_drops_header(self):
        self.assertNotIn("header", cd.normalise(RUNNING))
        self.assertEqual(cd.normalise([{"set": {"a": {"b": 1}}}, {"set": {"a": {"c": 2}}}]), {"a": {"b": 1, "c": 2}})

    def test_added_removed_changed(self):
        d = cd.diff(cd.normalise(STANDARD), cd.normalise(RUNNING))
        self.assertEqual([p for p, _ in d["added"]], ["interface/sw1p1/description", "interface/sw2p1/type", "qos/roce"])
        self.assertEqual(d["removed"], [])  # sw2p1: {} in the standard, set further on the switch: only the addition shows
        self.assertEqual([p for p, *_ in d["changed"]], ["system/aaa/user/admin/hashed-password"])

    def test_secrets_are_masked(self):
        d = cd.diff(cd.normalise(STANDARD), cd.normalise(RUNNING))
        self.assertEqual(d["changed"][0][1:], [cd.MASK, cd.MASK])
        self.assertNotIn("$6$", json.dumps(d))

    def test_removed_setting(self):
        d = cd.diff({"system": {"ntp": {"server": {"10.0.0.1": {}}}}}, {"system": {}})
        self.assertEqual(d["removed"], [["system/ntp/server/10.0.0.1", "{}"]])

    def test_parse_running_from_collector_output(self):
        raw = "__ICE2_COMMAND_001_START__\n%s\n__ICE2_COMMAND_001_END__\n" % json.dumps(RUNNING)
        self.assertEqual(cd.parse_running(raw)["system"]["hostname"], "bel1")
        self.assertIsNone(cd.parse_running("Permission denied"))

    def test_compare_states(self):
        out = cd.compare({"a": cd.normalise(STANDARD), "b": cd.normalise(STANDARD), "c": {}},
                         {"a": cd.normalise(STANDARD), "b": cd.normalise(RUNNING), "c": None}, ["a", "b", "c", "d"])
        self.assertEqual({h: v["state"] for h, v in out.items()}, {"a": "match", "b": "drift", "c": "unreadable", "d": "no-standard"})
        self.assertEqual(out["b"]["counts"], {"added": 3, "removed": 0, "changed": 1})
        self.assertEqual(out["b"]["settings"], 4)


class HiddenSecrets(unittest.TestCase):
    def test_a_secret_the_switch_prints_as_a_star_is_present_not_changed(self):
        want = {"system": {"aaa": {"user": {"admin": {"hashed-password": "$y$abc"}}}}}
        have = {"system": {"aaa": {"user": {"admin": {"hashed-password": "*"}}}}}
        out = cd.compare({"s": want}, {"s": have}, ["s"])["s"]
        self.assertEqual(out["state"], "match")
        self.assertEqual(out["secrets_unverified"], ["system/aaa/user/admin/hashed-password"])
        gone = cd.compare({"s": want}, {"s": {"system": {"aaa": {"user": {"admin": {}}}}}}, ["s"])["s"]
        self.assertEqual(gone["counts"]["removed"], 1)  # a secret missing altogether is still reported


class Store(unittest.TestCase):
    def test_a_rendered_commit_is_kept_as_it_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            folder = store / "sys1-x" / "abc123"
            folder.mkdir(parents=True)
            (folder / "bel1.json").write_text(json.dumps(STANDARD))
            (folder / "manifest.json").write_text(json.dumps({"commit": "abc123", "hosts": {"bel1": "h"}}))
            with mock.patch.object(cd, "checkout", return_value=("abc123", "2026-10-16")), mock.patch.object(cd, "_run") as run:
                manifest = cd.render("sys1-x", store, "pw")
            run.assert_not_called()  # nothing re-rendered
            self.assertEqual(manifest["commit"], "abc123")
            m, configs = cd.standard(store, "sys1-x")
            self.assertEqual(configs["bel1"]["system"]["hostname"], "bel1")

    def test_vault_reference_is_required_and_plain(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "p.ini"
            profile.write_text("[ufm]\nop_ref = op://a/b/c\n")
            with self.assertRaisesRegex(RuntimeError, "vault_op_ref"):
                cd.vault_password(profile)
            profile.write_text("[config_standard]\nvault_op_ref = op://a/b/c; rm x\n")
            with self.assertRaisesRegex(RuntimeError, "plain op://"):
                cd.vault_password(profile)


class Copies(unittest.TestCase):
    def test_ice2_and_lon14_copies_are_identical(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual((root / "ice2-netbox-dashboard/app/config_drift.py").read_text(),
                         (root / "lon14-netbox-dashboard/app/config_drift.py").read_text())

if __name__ == "__main__":
    unittest.main()
