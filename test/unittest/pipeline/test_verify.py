import os
import random
import shutil
import subprocess
import tempfile
import unittest

from arm_pipeline.config import Config
from arm_pipeline.errors import VerifyError
from arm_pipeline import verify
from arm_pipeline.verify import Expect, scan_makemkv_log, sample_windows, verify_rip

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
DUR = 10


class ScanLogTests(unittest.TestCase):
    def test_benign_3025_only(self):
        log = "\n".join(
            f'MSG:3025,0,2,"Title #{i} was therefore skipped (short title)","%1","x"' for i in range(34))
        self.assertEqual(scan_makemkv_log(log), [])

    def test_success_lines_ignored(self):
        log = 'MSG:5036,0,1,"Copy complete. 1 titles saved, 0 failed."\nMSG:5011,0,0,"Operation successfully completed"'
        self.assertEqual(scan_makemkv_log(log), [])

    def test_real_errors_flagged_among_benign(self):
        bad1 = "MSG:2003,0,1,\"Error 'Scsi error - MEDIUM ERROR:UNRECOVERED READ ERROR' occurred\""
        bad2 = 'MSG:5010,0,0,"Failed to open disc"'
        log = "\n".join(['MSG:3025,0,2,"skipped error failed"', bad1, "PRGV:1,2,3", bad2,
                         "MSG:1,0,0,Corrupt data at 123", "bad sector at lba 5", "Uncorrectable block"])
        self.assertEqual(len(scan_makemkv_log(log)), 5)
        self.assertEqual(scan_makemkv_log(log)[0], bad1)

    def test_case_insensitive_and_empty(self):
        self.assertEqual(scan_makemkv_log("READ ERROR here"), ["READ ERROR here"])
        self.assertEqual(scan_makemkv_log(""), [])


class SampleWindowTests(unittest.TestCase):
    def test_long_file(self):
        w = sample_windows(9000, 12, 20)
        self.assertEqual(len(w), 12)
        self.assertEqual(w[0], (0.0, 20.0))
        self.assertLessEqual(w[-1][0] + w[-1][1], 9000 - 2 + 1e-6)
        self.assertGreater(w[-1][0] + w[-1][1], 9000 - 3)
        self.assertTrue(all(s >= 0 for s, _ in w))
        self.assertEqual(w, sorted(w))

    def test_short_file_collapses(self):
        w = sample_windows(60, 12, 20)
        self.assertLess(len(w), 12)
        self.assertEqual(w[0][0], 0)
        self.assertLessEqual(w[-1][0] + w[-1][1], 58 + 1e-6)
        self.assertEqual(len(sample_windows(15, 12, 20)), 1)
        self.assertEqual(len(sample_windows(3, 12, 20)), 1)
        for s, n in sample_windows(3, 12, 20):
            self.assertGreaterEqual(s, 0)
            self.assertGreater(n, 0)

    def test_degenerate(self):
        self.assertEqual(sample_windows(0, 12, 20), [])
        self.assertEqual(len(sample_windows(1000, 1, 20)), 1)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class VerifyRipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.good = os.path.join(cls.tmp, "good.mkv")
        # ~5 MB, 10 s, 1 video + 1 audio; noise keeps the bitrate honest
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={DUR}",
                        "-f", "lavfi", "-i", f"sine=frequency=440:duration={DUR}",
                        "-vf", "noise=alls=40:allf=t", "-c:v", "libx264", "-b:v", "4M", "-minrate", "4M",
                        "-maxrate", "4M", "-bufsize", "1M", "-c:a", "aac", cls.good],
                       check=True, timeout=120)
        cls.size = os.path.getsize(cls.good)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.cfg = Config(sample_windows=4, sample_seconds=3)
        self.expect = Expect(duration_s=DUR, min_size_bytes=int(self.size * 0.9))

    def _copy(self, name):
        p = os.path.join(self.tmp, name)
        shutil.copy(self.good, p)
        return p

    def test_good_file_passes_quick(self):
        r = verify_rip(self.good, self.expect, self.cfg)
        self.assertTrue(r.ok, r.summary())
        self.assertFalse(r.escalated)
        self.assertEqual([c.name.split()[0] for c in r.checks], ["T0", "T1", "T2", "T3", "T4"])
        self.assertTrue(r.checks[-1].skipped)
        self.assertIn("quick verify OK", r.summary())

    def test_full_decode_configured(self):
        cfg = Config(sample_windows=4, sample_seconds=3, verify_full_decode=True)
        r = verify_rip(self.good, self.expect, cfg)
        self.assertTrue(r.ok, r.summary())
        self.assertTrue(r.escalated)

    def test_suspicious_log_escalates(self):
        msgs = []
        r = verify_rip(self.good, self.expect, self.cfg, "MSG:2003,0,1,READ ERROR at 5", msgs.append)
        self.assertTrue(r.ok, r.summary())
        self.assertTrue(r.escalated)
        self.assertEqual(len(r.warnings), 1)
        self.assertTrue(msgs)

    def test_truncated_fails(self):
        p = self._copy("trunc.mkv")
        with open(p, "r+b") as f:
            f.truncate(int(self.size * 0.5))
        r = verify_rip(p, self.expect, self.cfg)
        self.assertFalse(r.ok)
        self.assertTrue(r.summary().startswith("FAILED"))

    def test_slightly_small_warns_and_escalates(self):
        e = Expect(DUR, int(self.size / 0.85))
        r = verify_rip(self.good, e, self.cfg)
        self.assertTrue(r.ok, r.summary())
        self.assertTrue(r.escalated)
        self.assertTrue(r.warnings)

    def test_corrupted_middle_fails(self):
        p = self._copy("corrupt.mkv")
        rnd = random.Random(1234)
        with open(p, "r+b") as f:
            f.seek(self.size // 2)
            f.write(bytes(rnd.getrandbits(8) for _ in range(4096)))
        cfg = Config(sample_windows=4, sample_seconds=3, verify_full_decode=True)  # deterministic
        r = verify_rip(p, self.expect, cfg)
        self.assertFalse(r.ok, r.summary())

    def test_duration_mismatch_fails(self):
        r = verify_rip(self.good, Expect(DUR * 2, 0), self.cfg)
        self.assertFalse(r.ok)
        self.assertIn("T1", r.summary())
        self.assertEqual(len(r.checks), 2)       # stopped after T1

    def test_stream_count_fails(self):
        r = verify_rip(self.good, Expect(DUR, 0, video_streams=2), self.cfg)
        self.assertFalse(r.ok)
        r = verify_rip(self.good, Expect(DUR, 0, min_audio=2), self.cfg)
        self.assertFalse(r.ok)

    def test_garbage_file_fails_not_raises(self):
        p = os.path.join(self.tmp, "junk.mkv")
        with open(p, "wb") as f:
            f.write(b"not a video" * 100)
        r = verify_rip(p, self.expect, self.cfg)
        self.assertFalse(r.ok)

    def test_missing_file_raises(self):
        with self.assertRaises(VerifyError):
            verify_rip(os.path.join(self.tmp, "nope.mkv"), self.expect, self.cfg)

    def test_probe_failure_raises(self):
        with self.assertRaises(VerifyError):
            verify.probe(os.path.join(self.tmp, "nope.mkv"))


if __name__ == "__main__":
    unittest.main()


class NullMuxerNoiseTests(unittest.TestCase):
    def test_null_muxer_dts_warning_is_not_an_error_but_decoder_errors_are(self):
        from arm_pipeline import verify
        noise = b"[null @ 0x5c] Application provided invalid, non monotonically increasing dts to muxer in stream 0: 4 >= 4\n"
        self.assertEqual(verify._significant_stderr(noise).strip(), b"")
        real = b"[hevc @ 0x1] Invalid NAL unit size\n"
        self.assertEqual(verify._significant_stderr(noise + real).strip(), real.strip())
        self.assertIn(b"matroska", verify._significant_stderr(b"[matroska,webm @ 0x2] Read error at pos. 5\n"))
