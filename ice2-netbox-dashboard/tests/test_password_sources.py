"""Where Fetch from UFM gets UFM passwords: 1Password (op_ref / rest_op_ref) first, else Keychain.

Fake `op` and `security` commands on PATH stand in for the 1Password CLI and macOS Keychain,
so nothing real is read.

Run:  python3 -m unittest discover -s tests -v      (from ice2-netbox-dashboard/)
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "app"))

import ufm_fetch  # noqa: E402

FAKE_OP = """#!/bin/sh
if [ -n "$FAKE_OP_LOCKED" ]; then echo "[ERROR] account is not signed in" >&2; exit 1; fi
# The account must always be named (a fresh terminal otherwise says "not signed in").
if [ "$1 $2" != "--account nscale.1password.com" ]; then echo "no --account: $*" >&2; exit 2; fi
shift 2
if [ "$1 $2" != "read --no-newline" ]; then echo "unexpected: $*" >&2; exit 2; fi
printf '%s' "op:$3"
"""
FAKE_SECURITY = """#!/bin/sh
printf 'kc:%s:%s\\n' "$3" "$5"
"""


class FakeTools(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        for name, body in (("op", FAKE_OP), ("security", FAKE_SECURITY)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.env = {k: os.environ.get(k) for k in ("PATH", "FAKE_OP_LOCKED")}
        os.environ["PATH"] = str(self.bin)
        os.environ.pop("FAKE_OP_LOCKED", None)

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def ini(self, text: str) -> Path:
        path = self.dir / "device-access.ini"
        path.write_text(text)
        return path


class UfmPassword(FakeTools):
    """app/ufm_fetch.py: the UFM host SSH (op_ref) and UFM REST (rest_op_ref) passwords for Fetch from UFM."""

    def test_op_refs_alone_are_complete_logins(self):
        prof = ufm_fetch.read_profile(self.ini("[ufm]\nufm_user = root\nop_ref = op://Infra/ufm-ssh/password\n"
                                               "rest_user = ro\nrest_op_ref = op://Infra/ufm-rest/password\n"))
        self.assertTrue(prof["has_ssh"] and prof["has_rest"])
        self.assertEqual(ufm_fetch.ufm_password(prof, rest=False), "op:op://Infra/ufm-ssh/password")
        self.assertEqual(ufm_fetch.ufm_password(prof, rest=True), "op:op://Infra/ufm-rest/password")

    def test_keychain_fallback_per_login(self):
        prof = ufm_fetch.read_profile(self.ini("[ufm]\nufm_user = root\nkeychain_service = s1\nkeychain_account = a1\n"
                                               "rest_user = ro\nrest_op_ref = op://Infra/ufm-rest/password\n"
                                               "rest_keychain_service = s2\nrest_keychain_account = a2\n"))
        self.assertEqual(ufm_fetch.ufm_password(prof, rest=False), "kc:s1:a1")
        self.assertEqual(ufm_fetch.ufm_password(prof, rest=True), "op:op://Infra/ufm-rest/password")

    def test_keychain_only_profile_unchanged(self):
        prof = ufm_fetch.read_profile(self.ini("[ufm]\nrest_user = ro\nrest_keychain_service = s2\nrest_keychain_account = a2\n"))
        self.assertEqual((prof["has_ssh"], prof["has_rest"]), (False, True))
        self.assertEqual(ufm_fetch.ufm_password(prof, rest=True), "kc:s2:a2")

    def test_no_login_at_all(self):
        with self.assertRaisesRegex(RuntimeError, "no complete login"):
            ufm_fetch.read_profile(self.ini("[ufm]\nufm_user = root\nrest_user = ro\n"))

    def test_bad_reference_and_locked_1password_name_the_key(self):
        prof = ufm_fetch.read_profile(self.ini("[ufm]\nrest_user = ro\nrest_op_ref = op://Infra/'x'/password\n"))
        with self.assertRaisesRegex(RuntimeError, "rest_op_ref under \\[ufm\\].*plain op://"):
            ufm_fetch.ufm_password(prof, rest=True)
        prof = ufm_fetch.read_profile(self.ini("[ufm]\nufm_user = root\nop_ref = op://Infra/ufm-ssh/password\n"))
        os.environ["FAKE_OP_LOCKED"] = "1"
        with self.assertRaisesRegex(RuntimeError, "op_ref under \\[ufm\\].*not signed in"):
            ufm_fetch.ufm_password(prof, rest=False)

    def test_missing_op_names_the_key(self):
        prof = ufm_fetch.read_profile(self.ini("[ufm]\nrest_user = ro\nrest_op_ref = op://Infra/ufm-rest/password\n"))
        (self.bin / "op").unlink()
        with self.assertRaisesRegex(RuntimeError, "not installed.*rest_op_ref"):
            ufm_fetch.ufm_password(prof, rest=True)


if __name__ == "__main__":
    unittest.main()
