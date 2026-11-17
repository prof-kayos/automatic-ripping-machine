import os
import tempfile
import time
import unittest
from types import SimpleNamespace as NS

from arm_pipeline import operator as ops
from arm_pipeline.config import Config
from arm_pipeline.ledger import Ledger

FP = "0123456789abcdef"
FP2 = "fedcba9876543210"


class FakeDrive:
    def __init__(self, present=True):
        self.present, self.ejects = present, 0

    def disc_present(self, dev):
        return self.present

    def eject(self, cfg):
        self.ejects += 1
        return True, "TRAY_OPEN"


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.cfg = Config(state_dir=os.path.join(self.td.name, "state"))
        self.led = Ledger(self.cfg.state_path)

    def put(self, state, fp=FP):
        self.led.set_state(fp, state, "m")

    def test_valid_fp(self):
        self.assertTrue(ops.valid_fp(FP))
        for bad in ("../x", "short", FP.upper(), FP + "0", "", None, 5, FP + "\n"):
            self.assertFalse(ops.valid_fp(bad), bad)

    def test_approve_reject_state_guard(self):
        r = ops.approve(self.cfg, self.led, FP)
        self.assertFalse(r["ok"])
        self.put("ripping")
        self.assertFalse(ops.reject(self.cfg, self.led, FP)["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.cfg.state_path, "reject", FP)))
        self.put("awaiting_approval")
        self.assertTrue(ops.approve(self.cfg, self.led, FP)["ok"])
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.state_path, "approve", FP)))
        self.assertEqual(os.stat(self.cfg.state_path).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(self.cfg.state_path, "approve")).st_mode & 0o777, 0o700)

    def test_approve_removes_stale_reject_and_vice_versa(self):
        self.put("awaiting_approval")
        self.assertTrue(ops.reject(self.cfg, self.led, FP)["ok"])
        self.assertTrue(ops.approve(self.cfg, self.led, FP)["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.cfg.state_path, "reject", FP)))
        self.assertTrue(ops.reject(self.cfg, self.led, FP)["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.cfg.state_path, "approve", FP)))
        self.assertTrue(ops.reject(self.cfg, self.led, FP)["ok"])     # idempotent

    def test_command_file_symlink_refused(self):
        self.put("awaiting_approval")
        d = os.path.join(self.cfg.state_path, "approve")
        os.makedirs(d, mode=0o700)
        victim = os.path.join(self.td.name, "victim")
        open(victim, "w").close()
        os.symlink(victim, os.path.join(d, FP))
        r = ops.approve(self.cfg, self.led, FP)
        self.assertFalse(r["ok"])
        self.assertIn("symlink", r["message"])

    def test_identify(self):
        self.assertFalse(ops.identify(self.cfg, self.led, FP, "T", 2000)["ok"])    # unknown
        self.put("awaiting_identity")
        r = ops.identify(self.cfg, self.led, FP, "  Heat ", 1995, tmdb_id=949)
        self.assertTrue(r["ok"], r)
        import json
        with open(os.path.join(self.cfg.state_path, "identify", FP + ".json")) as fh:
            self.assertEqual(json.load(fh), {"title": "Heat", "year": 1995, "tmdb_id": 949})
        self.assertEqual([n for n in os.listdir(os.path.join(self.cfg.state_path, "identify"))],
                         [FP + ".json"])

    def test_identify_title_table(self):
        self.put("awaiting_identity")
        nextyear = time.gmtime().tm_year + 1
        bad = ["", "   ", "...", "a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b", "a\x00b",
               "a\x07b", "a\nb", ".hidden", "..", "x" * 201, "é" * 150, "a b", 5, None]
        for t in bad:
            self.assertFalse(ops.identify(self.cfg, self.led, FP, t, 2000)["ok"], repr(t))
        for y in (1887, nextyear + 1, "abc", None, True, 2e10, float("inf"), float("nan"), 10 ** 400, "1e400"):
            self.assertFalse(ops.identify(self.cfg, self.led, FP, "Ok", y)["ok"], repr(y))
        self.assertTrue(ops.identify(self.cfg, self.led, FP, "Ok", nextyear)["ok"])
        self.assertFalse(ops.identify(self.cfg, self.led, FP, "Ok", 2000, tmdb_id=-1)["ok"])
        # NFC normalisation
        r = ops.identify(self.cfg, self.led, FP, "Café", 2000)
        self.assertEqual(r["title"], "Café")

    def test_identify_overflow_and_odd_values_are_clean_refusals(self):
        self.put("awaiting_identity")
        for kw in ({"year": float("inf")}, {"year": float("-inf")}, {"year": float("nan")},
                   {"year": 1e400}, {"year": [2000]}, {"year": {"a": 1}}, {"tmdb_id": 1e400},
                   {"tmdb_id": float("inf")}, {"tmdb_id": "abc"}, {"tmdb_id": [1]}, {"tmdb_id": 10 ** 30}):
            args = dict(title="Ok", year=2000)
            args.update(kw)
            r = ops.identify(self.cfg, self.led, FP, **args)
            self.assertFalse(r["ok"], kw)
            self.assertIn("message", r)

    def test_identify_target_is_directory(self):
        self.put("awaiting_identity")
        d = os.path.join(self.cfg.state_path, "identify")
        os.makedirs(os.path.join(d, FP + ".json"))
        r = ops.identify(self.cfg, self.led, FP, "Ok", 2000)
        self.assertFalse(r["ok"])
        self.assertEqual([n for n in os.listdir(d) if n.startswith(".tmp")], [])

    def test_identify_os_error_is_clean(self):
        self.put("awaiting_identity")
        orig = os.replace

        def boom(*a):
            raise PermissionError(13, "denied")
        os.replace = boom
        try:
            r = ops.identify(self.cfg, self.led, FP, "Ok", 2000)
        finally:
            os.replace = orig
        self.assertFalse(r["ok"])
        d = os.path.join(self.cfg.state_path, "identify")
        self.assertEqual([n for n in os.listdir(d) if n.startswith(".tmp")], [])

    def test_clean_title_function(self):
        self.assertEqual(ops.clean_title("Say Anything..."), "Say Anything...")
        self.assertEqual(ops.clean_title("Say Anything"), "Say Anything")
        self.assertEqual(ops.clean_title("  Heat . "), "Heat")
        self.assertEqual(ops.clean_title("Cafe\u0301"), "Caf\u00e9")
        self.assertEqual(ops.clean_title("Mr. Smith"), "Mr. Smith")
        for bad in ("", "..", "\ud800", "a\u202eb", "a\u00a0b", ".x", "a:b", "x" * 201, 7):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ops.clean_title(bad)
        r = None
        self.put("awaiting_identity")
        r = ops.identify(self.cfg, self.led, FP, "Say Anything...", 1989)
        self.assertTrue(r["ok"])
        self.assertEqual(r["title"], "Say Anything...")
        import json
        with open(os.path.join(self.cfg.state_path, "identify", FP + ".json")) as fh:
            self.assertEqual(json.load(fh)["title"], "Say Anything...")

    def test_identify_symlink_target_refused(self):
        self.put("awaiting_identity")
        d = os.path.join(self.cfg.state_path, "identify")
        os.makedirs(d, mode=0o700)
        os.symlink(os.path.join(self.td.name, "x"), os.path.join(d, FP + ".json"))
        self.assertFalse(ops.identify(self.cfg, self.led, FP, "Ok", 2000)["ok"])

    def test_retry(self):
        self.assertFalse(ops.retry(self.cfg, self.led, FP)["ok"])
        self.put("ripping")
        self.assertFalse(ops.retry(self.cfg, self.led, FP)["ok"])
        self.put("failed")
        self.assertTrue(ops.retry(self.cfg, self.led, FP)["ok"])
        self.assertIsNone(self.led.get(FP))
        self.put("rejected")
        self.assertTrue(ops.retry(self.cfg, self.led, FP)["ok"])

    def test_skip(self):
        self.put("failed")
        self.assertFalse(ops.skip(self.cfg, self.led, FP)["ok"])
        self.put("awaiting_identity")
        d = FakeDrive(True)
        r = ops.skip(self.cfg, self.led, FP, drive_mod=d)
        self.assertTrue(r["ok"])
        self.assertEqual((d.ejects, self.led.get(FP).state), (1, "skipped"))
        self.put("awaiting_identity", FP2)
        d = FakeDrive(False)
        self.assertTrue(ops.skip(self.cfg, self.led, FP2, drive_mod=d)["ok"])
        self.assertEqual(d.ejects, 0)

    def test_dismiss(self):
        for st in ("ripping", "done", "awaiting_approval"):
            self.put(st)
            self.assertFalse(ops.dismiss(self.cfg, self.led, FP)["ok"], st)
        for st in ("awaiting_identity", "failed"):
            self.put(st)
            self.assertTrue(ops.dismiss(self.cfg, self.led, FP)["ok"], st)
            rec = self.led.get(FP)
            self.assertEqual((rec.state, rec.message), ("skipped", "dismissed by owner"))

    def test_eject_now(self):
        for st in ("ripping", "verifying", "transferring", "placing"):
            self.put(st)
            d = FakeDrive()
            self.assertFalse(ops.eject_now(self.cfg, self.led, drive_mod=d)["ok"], st)
            self.assertEqual(d.ejects, 0)
        self.put("done")
        d = FakeDrive()
        self.assertTrue(ops.eject_now(self.cfg, self.led, drive_mod=d)["ok"])
        self.assertEqual(d.ejects, 1)


if __name__ == "__main__":
    unittest.main()
