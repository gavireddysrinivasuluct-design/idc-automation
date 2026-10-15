"""Where ufm_direct gets the UFM REST password: 1Password (rest_op_ref) first, else Keychain.

Fake `op` and `security` commands on PATH stand in for the 1Password CLI and macOS Keychain,
so nothing real is read.

Run:  python3 -m unittest discover -s tests -v      (from multi-region-backend-dashboard/)
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import ufm_direct  # noqa: E402

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
BASE = "[ufm.osl1]\njump_host = jmp-osl1\njump_user = me\nhosts = 10.50.40.9\nrest_user = ro\n"


class RestPassword(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        for name, body in (("op", FAKE_OP), ("security", FAKE_SECURITY)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        saved = {k: os.environ.get(k) for k in ("PATH", "FAKE_OP_LOCKED")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        os.environ["PATH"] = str(self.bin)
        os.environ.pop("FAKE_OP_LOCKED", None)
        self.ini = self.dir / "device-access.ini"

    def password(self, extra: str) -> str:
        self.ini.write_text(BASE + extra)
        return ufm_direct.rest_password(ufm_direct.read_profile(self.ini, "osl1"), self.ini)

    def test_rest_op_ref_alone_is_enough(self):
        self.assertEqual(self.password("rest_op_ref = op://Infra/osl1-ufm/password\n"), "op:op://Infra/osl1-ufm/password")

    def test_rest_op_ref_wins_over_keychain(self):
        self.assertEqual(self.password("rest_op_ref = op://Infra/osl1-ufm/password\nrest_keychain_service = s\n"),
                         "op:op://Infra/osl1-ufm/password")

    def test_keychain_fallback(self):
        self.assertEqual(self.password("rest_keychain_service = s\n"), "kc:s:ro", "account defaults to rest_user")

    def test_needs_a_password_source(self):
        with self.assertRaisesRegex(ufm_direct.UfmError, "rest_op_ref \\(or rest_keychain_service\\)"):
            self.password("")

    def test_reference_must_be_plain(self):
        with self.assertRaisesRegex(ufm_direct.UfmError, "rest_op_ref in .* plain op://"):
            self.password("rest_op_ref = op://Infra/`id`/password\n")

    def test_locked_1password_names_the_key(self):
        os.environ["FAKE_OP_LOCKED"] = "1"
        with self.assertRaisesRegex(ufm_direct.UfmError, "rest_op_ref in .*not signed in") as caught:
            self.password("rest_op_ref = op://Infra/osl1-ufm/password\n")
        self.assertIn("Unlock the 1Password app", caught.exception.hint)

    def test_missing_op_names_the_key(self):
        (self.bin / "op").unlink()
        with self.assertRaisesRegex(ufm_direct.UfmError, "not installed; rest_op_ref") as caught:
            self.password("rest_op_ref = op://Infra/osl1-ufm/password\n")
        self.assertIn("remove rest_op_ref to use Keychain", caught.exception.hint)


if __name__ == "__main__":
    unittest.main()
