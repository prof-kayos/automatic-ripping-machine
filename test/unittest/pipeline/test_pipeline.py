"""End-to-end pipeline tests: fake makemkvcon (makes a real 12 s MKV), fake Slack/drive/servers, REAL policy,
verify, transfer, swap and ledger code. No network, no real disc."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from arm_pipeline import pipeline as pl  # noqa: E402
from arm_pipeline.config import Config  # noqa: E402
from arm_policy import decision  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
FAKE_MAKEMKV = r'''#!/bin/bash
args="$*"
if [[ "$args" == *" info "* ]]; then cat "$FAKE_SCAN"; exit 0; fi
if [[ "$args" == *" mkv "* ]]; then
  out="${@: -1}"; mkdir -p "$out"
  if [ "$FAKE_RIP_MODE" = slow ]; then sleep 30; fi
  if [ "$FAKE_RIP_MODE" = slowprgv ]; then echo "PRGV:16384,16384,65536"; sleep 8; fi
  ffmpeg -v error -y -f lavfi -i testsrc=duration=12:size=640x360:rate=24 -f lavfi -i sine=duration=12 \
    -c:v libx264 -b:v 2M -c:a aac -shortest "$out/title_t00.mkv"
  if [ "$FAKE_RIP_MODE" = truncate ]; then truncate -s 300000 "$out/title_t00.mkv"; fi
  exit 0
fi
exit 1
'''


def scan_text(res="1920x1080", label="TEST_DISC_XYZ", name="Test Movie"):
    return "\n".join([
        f'CINFO:1,6209,"Blu-ray disc"', f'CINFO:2,0,"{name}"', f'CINFO:32,0,"{label}"',
        'TINFO:0,2,0,"Title 0"', 'TINFO:0,8,0,"20"', 'TINFO:0,9,0,"0:00:12"', 'TINFO:0,11,0,"1000000"',
        'TINFO:0,16,0,"00000.mpls"', 'TINFO:0,25,0,"1"', 'TINFO:0,26,0,"1"',
        'SINFO:0,0,1,6201,"Video"', 'SINFO:0,0,6,0,"MPEG-4 AVC"', f'SINFO:0,0,19,0,"{res}"',
        'SINFO:0,1,1,6202,"Audio"', 'SINFO:0,1,3,0,"eng"', 'SINFO:0,1,6,0,"TrueHD"',
    ]) + "\n"


class FakeSlack:
    def __init__(self):
        self.msgs = []

    def post(self, text):
        self.msgs.append(text)
        return True

    def ask_approval(self, text):
        self.msgs.append(text)
        return None

    def poll_approval(self, ticket):
        return "pending"

    def joined(self):
        return "\n".join(self.msgs)


class FakeDrive:
    def __init__(self):
        self.ejects = 0

    def eject(self, cfg):
        self.ejects += 1
        return True, "TRAY_OPEN"


class FakePlex:
    configured = True

    def __init__(self, log):
        self.log = log

    def scan_path(self, d):
        self.log.append(("plex.scan", os.path.basename(d)))

    def wait_for_file(self, q, path, present, timeout, poll_s=5):
        self.log.append(("plex.wait", present))
        return True


class FakeJelly:
    configured = True

    def __init__(self, log):
        self.log = log

    def push(self, path, kind):
        self.log.append(("jelly.push", kind))

    def wait_for_path(self, path, present, timeout, poll_s=10):
        self.log.append(("jelly.wait", present))
        return True


class FakeTmdb:
    def __init__(self, identity="default"):
        self.configured = True
        self.identity = decision.Identity("Test Movie", 2020, 0, "en", 1, "high") if identity == "default" else identity

    def identify(self, title, year, runtime):
        return self.identity


class PipelineBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        t = self.tmp
        for d in ("movies", "scratch", "state", "home"):
            os.makedirs(os.path.join(t, d))
        open(os.path.join(t, "movies", ".arm-canary"), "w").close()
        self.script = os.path.join(t, "fake-makemkvcon")
        with open(self.script, "w") as fh:
            fh.write(FAKE_MAKEMKV)
        os.chmod(self.script, os.stat(self.script).st_mode | stat.S_IEXEC)
        self.scanfile = os.path.join(t, "scan.txt")
        self.env = mock.patch.dict(os.environ, {"HOME": os.path.join(t, "home"), "FAKE_SCAN": self.scanfile,
                                                "FAKE_RIP_MODE": "ok"})
        self.env.start()
        real = pl.decision.title_select.select_main_title
        self.sel = mock.patch.object(pl.decision.title_select, "select_main_title",
                                     lambda titles, expected_runtime_min=None, min_len_s=5: real(titles, None, 5))
        self.sel.start()
        self.cfg = Config(movies_dir=os.path.join(t, "movies"), scratch_dir=os.path.join(t, "scratch"),
                          state_dir=os.path.join(t, "state"), makemkvcon=self.script, server_wait_s=5)
        self.slack, self.drive, self.log = FakeSlack(), FakeDrive(), []

    def tearDown(self):
        self.sel.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pipe(self, tmdb=None):
        return pl.Pipeline(self.cfg, notifier=self.slack, plex=FakePlex(self.log), jelly=FakeJelly(self.log),
                           tmdb=tmdb or FakeTmdb(), drive=self.drive)

    def write_scan(self, **kw):
        with open(self.scanfile, "w") as fh:
            fh.write(scan_text(**kw))

    def make_mkv(self, path, size="320x240"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration=12:size={size}:rate=24",
                        "-f", "lavfi", "-i", "sine=duration=12", "-c:v", "libx264", "-c:a", "aac", "-shortest", path],
                       check=True)

    def movie(self, *parts):
        return os.path.join(self.cfg.movies_dir, *parts)

    def only_fp(self, pipe):
        recs = pipe.ledger.all()
        self.assertEqual(len(recs), 1)
        return recs[0]



@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe needed")
class PipelineE2E(PipelineBase):
    # ---------------------------------------------------------------- scenarios
    def test_new_movie_end_to_end(self):
        self.write_scan()
        p = self.pipe()
        self.assertEqual(p.handle_new_disc(), "done")
        final = self.movie("Test Movie (2020)", "Test Movie (2020).mkv")
        self.assertTrue(os.path.exists(final))
        self.assertEqual(os.listdir(self.movie("Test Movie (2020)")), ["Test Movie (2020).mkv"])   # no .part left
        self.assertIn("ADDED", self.slack.joined())
        self.assertEqual(self.drive.ejects, 1)   # the disc is ejected when the job completes
        self.assertIn("Disc ejected", self.slack.joined())
        self.assertEqual(self.only_fp(p).state, "done")
        self.assertIn(("plex.scan", "Test Movie (2020)"), self.log)
        self.assertIn(("jelly.push", "Created"), self.log)
        self.assertFalse(os.path.exists(os.path.join(self.cfg.scratch_dir, "rips", self.only_fp(p).fingerprint)))

    def test_same_disc_is_not_reprocessed(self):
        self.write_scan()
        p = self.pipe()
        p.handle_new_disc()
        self.assertEqual(p.handle_new_disc(), "already-seen")
        self.assertEqual(self.drive.ejects, 2)   # once at completion, once more for a re-inserted finished disc

    def test_replace_waits_for_approval_then_swaps(self):
        self.make_mkv(self.movie("Test Movie (2020)", "Test Movie (2020).mkv"))      # old: 320x240
        old_size = os.path.getsize(self.movie("Test Movie (2020)", "Test Movie (2020).mkv"))
        open(self.movie("Test Movie (2020)", "backdrop.jpg"), "w").write("art")
        self.write_scan()                                                              # disc: 1080p
        p = self.pipe()
        self.assertEqual(p.handle_new_disc(), "awaiting-approval")
        rec = self.only_fp(p)
        folder = self.movie("Test Movie (2020)")
        self.assertEqual(rec.state, "awaiting_approval")
        self.assertEqual(os.path.getsize(os.path.join(folder, "Test Movie (2020).mkv")), old_size)   # untouched
        self.assertTrue(os.path.exists(os.path.join(folder, "Test Movie (2020).mkv.part")))           # staged
        self.assertEqual(self.drive.ejects, 1)                                         # disc freed while waiting
        self.assertEqual(self.log, [])                                                 # servers not touched yet
        open(os.path.join(self.cfg.state_path, "approve", rec.fingerprint), "w").close()
        p.poll_pending()
        self.assertEqual(self.only_fp(p).state, "done")
        self.assertEqual(sorted(os.listdir(folder)), ["Test Movie (2020).mkv", "backdrop.jpg"])      # art kept
        self.assertNotEqual(os.path.getsize(os.path.join(folder, "Test Movie (2020).mkv")), old_size)
        order = [e for e in self.log if e[0] in ("plex.wait", "jelly.push", "jelly.wait")]
        self.assertEqual(order, [("plex.wait", False), ("jelly.push", "Deleted"), ("jelly.wait", False),
                                 ("plex.wait", True), ("jelly.push", "Created"), ("jelly.wait", True)])
        self.assertIn("REPLACED", self.slack.joined())

    def test_replace_rejected_leaves_library_untouched(self):
        self.make_mkv(self.movie("Test Movie (2020)", "Test Movie (2020).mkv"))
        before = os.path.getsize(self.movie("Test Movie (2020)", "Test Movie (2020).mkv"))
        self.write_scan()
        p = self.pipe()
        p.handle_new_disc()
        rec = self.only_fp(p)
        open(os.path.join(self.cfg.state_path, "reject", rec.fingerprint), "w").close()
        p.poll_pending()
        self.assertEqual(self.only_fp(p).state, "rejected")
        self.assertEqual(os.listdir(self.movie("Test Movie (2020)")), ["Test Movie (2020).mkv"])
        self.assertEqual(os.path.getsize(self.movie("Test Movie (2020)", "Test Movie (2020).mkv")), before)

    def test_existing_better_is_skipped_and_ejected(self):
        self.make_mkv(self.movie("Test Movie (2020)", "Test Movie (2020).mkv"), size="1920x1080")
        self.write_scan(res="1280x720")
        p = self.pipe()
        self.assertEqual(p.handle_new_disc(), "skipped")
        self.assertEqual(self.only_fp(p).state, "skipped")
        self.assertEqual(self.drive.ejects, 1)

    def test_verification_failure_removes_staged_file_and_ejects(self):
        self.write_scan()
        os.environ["FAKE_RIP_MODE"] = "truncate"
        p = self.pipe()
        self.assertEqual(p.handle_new_disc(), "failed")
        self.assertEqual(self.only_fp(p).state, "failed")
        self.assertFalse(os.path.exists(self.movie("Test Movie (2020)", "Test Movie (2020).mkv")))
        self.assertEqual([f for f in os.listdir(self.movie("Test Movie (2020)")) if f.endswith(".part")]
                         if os.path.isdir(self.movie("Test Movie (2020)")) else [], [])
        self.assertEqual(self.drive.ejects, 1)
        self.assertIn("failed", self.slack.joined())
        self.assertEqual(self.log, [])

    def test_unknown_identity_keeps_disc_and_resumes_on_answer(self):
        self.write_scan()
        p = self.pipe(tmdb=FakeTmdb(identity=None))
        self.assertEqual(p.handle_new_disc(), "awaiting-identity")
        rec = self.only_fp(p)
        self.assertEqual(rec.state, "awaiting_identity")
        self.assertEqual(self.drive.ejects, 0)                                         # disc stays for the human
        self.assertFalse(os.path.exists(self.movie("Test Movie (2020)")))
        import json
        json.dump({"title": "Test Movie", "year": 2020},
                  open(os.path.join(self.cfg.state_path, "identify", rec.fingerprint + ".json"), "w"))
        self.assertEqual(p.resume_identity(rec, True), "done")
        self.assertTrue(os.path.exists(self.movie("Test Movie (2020)", "Test Movie (2020).mkv")))

    def test_share_canary_missing_blocks_processing(self):
        os.remove(os.path.join(self.cfg.movies_dir, ".arm-canary"))
        self.write_scan()
        p = self.pipe()
        self.assertEqual(p.handle_new_disc(), "share-unavailable")
        self.assertEqual(p.ledger.all(), [])

    def test_leftover_part_from_interrupted_run_blocks(self):
        os.makedirs(self.movie("Test Movie (2020)"))
        open(self.movie("Test Movie (2020)", "Test Movie (2020).mkv.part"), "w").write("x")
        self.write_scan()
        p = self.pipe()
        self.assertEqual(p.handle_new_disc(), "failed")
        self.assertIn("leftovers", self.slack.joined())


if __name__ == "__main__":
    unittest.main()
