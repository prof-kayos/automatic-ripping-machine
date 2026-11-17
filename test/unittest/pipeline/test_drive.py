import subprocess
import unittest
from unittest import mock

from arm_pipeline import drive
from arm_pipeline.config import Config
from arm_pipeline.errors import DriveError


class Runner:
    def __init__(self, missing=()):
        self.calls, self.missing = [], missing

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] in self.missing:
            raise FileNotFoundError(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, "", "")


def seq(*vals):
    it = iter(vals)
    return lambda dev: next(it)


class DriveTests(unittest.TestCase):
    cfg = Config(sr_dev="/dev/sr9", sg_dev="/dev/sg9")

    def run_eject(self, statuses, runner=None, tries=10):
        runner = runner or Runner()
        sleeps = []
        with mock.patch.object(drive, "tray_status", seq(*statuses)):
            res = drive.eject(self.cfg, runner=runner, sleep=sleeps.append, tries=tries)
        return res, runner, sleeps

    def test_success_after_three_polls(self):
        res, r, sleeps = self.run_eject(["DISC_OK", "NOT_READY", "TRAY_OPEN"])
        self.assertEqual(res, (True, "TRAY_OPEN"))
        self.assertEqual(r.calls, [["sg_prevent", "--allow", "/dev/sg9"], ["sg_start", "--eject", "/dev/sg9"]])
        self.assertEqual(sleeps, [1, 1])

    def test_no_disc_counts_as_success(self):
        self.assertEqual(self.run_eject(["NO_DISC"])[0], (True, "NO_DISC"))

    def test_failure_after_tries(self):
        res, _, sleeps = self.run_eject(["DISC_OK"] * 4, tries=4)
        self.assertEqual(res, (False, "DISC_OK"))
        self.assertEqual(len(sleeps), 4)

    def test_sg_start_missing_falls_back(self):
        res, r, _ = self.run_eject(["DISC_OK", "TRAY_OPEN"], Runner(missing=("sg_start",)))
        self.assertEqual(res, (True, "TRAY_OPEN"))
        self.assertIn(["eject", "-m", "/dev/sr9"], r.calls)

    def test_drive_error_while_polling_is_unknown(self):
        def boom(dev):
            raise DriveError("gone")
        with mock.patch.object(drive, "tray_status", boom):
            res = drive.eject(self.cfg, runner=Runner(), sleep=lambda s: None, tries=2)
        self.assertEqual(res, (False, "UNKNOWN"))

    def test_disc_present(self):
        with mock.patch.object(drive, "tray_status", lambda d: "DISC_OK"):
            self.assertTrue(drive.disc_present("x"))
        with mock.patch.object(drive, "tray_status", lambda d: "NOT_READY"):
            self.assertFalse(drive.disc_present("x"))

    def test_tray_status_unopenable(self):
        with self.assertRaises(DriveError):
            drive.tray_status("/dev/does-not-exist-arm")

    def test_tray_status_maps_ioctl(self):
        with mock.patch("os.open", return_value=99), mock.patch("os.close"), \
                mock.patch("fcntl.ioctl", return_value=2) as io:
            self.assertEqual(drive.tray_status("/dev/sr0"), "TRAY_OPEN")
            self.assertEqual(io.call_args[0][1], 0x5326)


if __name__ == "__main__":
    unittest.main()
