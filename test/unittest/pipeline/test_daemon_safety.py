"""Safety behaviour around the daemon: clean stop mid-rip, crash recovery inside a replace, hung share, lock."""
import os
import threading
import time
import unittest
from unittest import mock

from test_pipeline import HAVE_FFMPEG, FakeTmdb, PipelineBase  # noqa: E402  (same directory, shared fixtures)
from arm_pipeline import daemon, pipeline as pl, swap  # noqa: E402
from arm_pipeline.errors import PipelineError  # noqa: E402
from arm_pipeline.ledger import Record  # noqa: E402


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe needed")
class DaemonSafety(PipelineBase):
    def test_safety_stop_mid_rip_terminates_cleanly_and_marks_failed(self):
        self.write_scan()
        os.environ["FAKE_RIP_MODE"] = "slow"
        p = self.pipe()
        out = {}
        th = threading.Thread(target=lambda: out.update(r=p.handle_new_disc()))
        t0 = time.time()
        th.start()
        time.sleep(1.5)
        p.stop_requested = True
        th.join(20)
        self.assertFalse(th.is_alive())
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(out["r"], "failed")
        self.assertIn("interrupted", self.slack.joined())
        self.assertEqual(self.only_fp(p).state, "failed")
        self.assertEqual(self.log, [])                                  # servers never touched

    def test_safety_crash_inside_replace_restores_the_old_movie(self):
        folder = self.movie("Test Movie (2020)")
        self.make_mkv(os.path.join(folder, "Test Movie (2020).mkv"))
        old = os.path.join(folder, "Test Movie (2020).mkv")
        part = old + ".part"
        with open(part, "wb") as fh:
            fh.write(b"new")
        p = self.pipe()
        state = swap.replace_begin(old, part, old)                      # simulate: old hidden, then the daemon died
        self.assertFalse(os.path.exists(old))
        p.ledger.put(Record(fingerprint="0123456789abcdef", state="placing", title="Test Movie",
                            paths={"replace": state.__dict__}))
        self.assertEqual(daemon.recover_interrupted(p), 1)
        self.assertTrue(os.path.exists(old))                            # the movie is back
        self.assertFalse(os.path.exists(old + ".arm-old"))
        self.assertEqual(p.ledger.get("0123456789abcdef").state, "failed")
        self.assertIn("rollback", self.slack.joined().lower())

    def test_safety_hung_share_never_blocks_the_daemon(self):
        p = self.pipe()
        p.CANARY_TIMEOUT_S = 0.3
        gate = threading.Event()
        real_stat = os.stat

        def hang(path, *a, **k):
            if str(path).endswith(".arm-canary"):
                gate.wait(5)                                           # stands in for a D-state CIFS stat
            return real_stat(path, *a, **k)
        with mock.patch.object(pl.os, "stat", hang):
            t0 = time.time()
            self.assertFalse(p.canary_ok())
            self.assertLess(time.time() - t0, 2)
            self.assertFalse(p.canary_ok())                            # a stuck probe is not stacked
        gate.set()
        time.sleep(0.2)
        self.assertTrue(p.canary_ok())                                  # recovers once the share answers

    def test_safety_invalid_fingerprint_is_rejected(self):
        p = self.pipe()
        for bad in ("../x", "abc", "ZZZZZZZZZZZZZZZZ", "0123456789abcdef/.."):
            with self.assertRaises(PipelineError):
                p._state_file("approve", bad)

    def test_safety_second_daemon_cannot_start(self):
        a = daemon.acquire_lock(self.cfg)
        with self.assertRaises(SystemExit):
            daemon.acquire_lock(self.cfg)
        a.close()

    def test_safety_scratch_cleanup_keeps_failed_and_active(self):
        p = self.pipe()
        root = os.path.join(self.cfg.scratch_dir, "rips")
        for fp, st in (("aaaaaaaaaaaaaaaa", "done"), ("bbbbbbbbbbbbbbbb", "failed"), ("cccccccccccccccc", None)):
            os.makedirs(os.path.join(root, fp))
            os.utime(os.path.join(root, fp), (0, 0))
            if st:
                p.ledger.put(Record(fingerprint=fp, state=st))
        self.assertEqual(daemon.cleanup_scratch(p), 2)                  # done + orphan removed
        self.assertEqual(os.listdir(root), ["bbbbbbbbbbbbbbbb"])        # failed kept for inspection


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe needed")
class ReviewRegressions(PipelineBase):
    """One regression test per confirmed finding of the independent adversarial review."""

    def staged_replace(self):
        old = self.movie("Test Movie (2020)", "Test Movie (2020).mkv")
        self.make_mkv(old)
        self.write_scan()
        p = self.pipe()
        self.assertEqual(p.handle_new_disc(), "awaiting-approval")
        return p, self.only_fp(p), old

    def test_corrupted_staged_part_is_never_swapped_in(self):                      # HIGH 1
        p, rec, old = self.staged_replace()
        before = os.path.getsize(old)
        with open(old + ".part", "wb") as fh:
            fh.write(b"corrupt")                                                    # damaged during the approval wait
        open(os.path.join(self.cfg.state_path, "approve", rec.fingerprint), "w").close()
        p.poll_pending()
        self.assertEqual(self.only_fp(p).state, "failed")
        self.assertEqual(os.path.getsize(old), before)                             # library untouched
        self.assertFalse(os.path.exists(old + ".arm-old"))
        self.assertEqual(self.log, [])                                             # servers never told anything

    def test_failed_replace_resyncs_the_servers_after_rollback(self):              # HIGH 2
        p, rec, old = self.staged_replace()
        calls = {"n": 0}
        orig = p.plex.wait_for_file

        def flaky(q, path, present, timeout, poll_s=5):
            calls["n"] += 1
            self.log.append(("plex.wait", present))
            return present is False                                                # drops the old file, never shows the new
        p.plex.wait_for_file = flaky
        open(os.path.join(self.cfg.state_path, "approve", rec.fingerprint), "w").close()
        p.poll_pending()
        # new file WAS placed before the sync failed => it must stay, old stays hidden, and we must say so
        self.assertEqual(self.only_fp(p).state, "done")
        self.assertIn("WARNING", self.slack.joined())
        self.assertTrue(os.path.exists(old))
        self.assertTrue(os.path.exists(old + ".arm-old"))                          # old kept until servers confirm

    def test_failure_before_placement_rolls_back_and_resyncs(self):                # HIGH 2 (other half)
        p, rec, old = self.staged_replace()
        p.plex.wait_for_file = lambda q, path, present, timeout, poll_s=5: (self.log.append(("plex.wait", present)) or False)
        open(os.path.join(self.cfg.state_path, "approve", rec.fingerprint), "w").close()
        p.poll_pending()
        self.assertEqual(self.only_fp(p).state, "failed")
        self.assertTrue(os.path.exists(old))                                       # old movie restored
        self.assertFalse(os.path.exists(old + ".arm-old"))
        self.assertIn("rollback", self.slack.joined().lower())
        self.assertIn(("jelly.push", "Created"), self.log)                         # servers told the old file is back

    def test_daemon_does_not_loop_on_a_disc_that_crashes_the_handler(self):        # HIGH 3
        p = self.pipe()
        calls = {"n": 0, "cycles": 0}

        def boom():
            calls["n"] += 1
            raise RuntimeError("boom")
        p.handle_new_disc = boom
        with mock.patch.object(daemon.drive_mod, "disc_present", lambda dev: True):
            daemon.run(self.cfg, pipe=p, stop=lambda: calls.__setitem__("cycles", calls["cycles"] + 1) or calls["cycles"] > 4,
                       sleep=lambda s: None)
        self.assertEqual(calls["n"], 1)                                             # one attempt, not one per cycle

    def test_handler_crash_marks_record_failed_with_one_message(self):             # HIGH 3 (inside the pipeline)
        self.write_scan()
        p = self.pipe()
        with mock.patch.object(p, "_plan_and_run", side_effect=RuntimeError("kaboom")):
            self.assertEqual(p.handle_new_disc(), "failed")
        self.assertEqual(self.only_fp(p).state, "failed")
        self.assertEqual(sum("crashed" in m for m in self.slack.msgs), 1)

    def test_verify_thread_crash_names_the_step_and_removes_the_part(self):        # MEDIUM 4
        self.write_scan()
        p = self.pipe()
        with mock.patch.object(pl.verify, "verify_rip", side_effect=OSError("ffprobe vanished")):
            self.assertEqual(p.handle_new_disc(), "failed")
        self.assertIn("verify", self.slack.joined())
        self.assertNotIn("KeyError", self.slack.joined())
        folder = self.movie("Test Movie (2020)")
        self.assertEqual([f for f in os.listdir(folder) if f.endswith(".part")], [])

    def test_stale_approval_does_not_replace_a_file_that_changed(self):            # HIGH 5
        p, rec, old = self.staged_replace()
        time.sleep(0.05)
        self.make_mkv(old, size="1280x720")                                         # owner dropped in a different file
        changed = os.path.getsize(old)
        open(os.path.join(self.cfg.state_path, "approve", rec.fingerprint), "w").close()
        p.poll_pending()
        self.assertEqual(self.only_fp(p).state, "failed")
        self.assertEqual(os.path.getsize(old), changed)
        self.assertIn("changed", self.slack.joined())

    def test_new_movie_server_timeout_is_done_with_a_warning(self):                # MEDIUM 8
        self.write_scan()
        p = self.pipe()
        p.jelly.wait_for_path = lambda *a, **k: False
        self.assertEqual(p.handle_new_disc(), "done")
        self.assertTrue(os.path.exists(self.movie("Test Movie (2020)", "Test Movie (2020).mkv")))
        self.assertIn("WARNING", self.slack.joined())

    def test_approval_given_before_the_request_does_not_count(self):               # MEDIUM 7
        self.make_mkv(self.movie("Test Movie (2020)", "Test Movie (2020).mkv"))
        self.write_scan()
        p = self.pipe()
        os.makedirs(os.path.join(self.cfg.state_path, "approve"), exist_ok=True)
        # fingerprint is known only after the scan, so pre-place the file the way a stale CLI call would
        from arm_policy import makemkv_scan
        from arm_pipeline.ledger import fingerprint
        with open(self.scanfile) as fh:
            disc = makemkv_scan.parse_scan(fh.read())
        fp = fingerprint(disc.attrs.get(32, ""), disc.titles)
        open(os.path.join(self.cfg.state_path, "approve", fp), "w").close()
        self.assertEqual(p.handle_new_disc(), "awaiting-approval")
        self.assertFalse(os.path.exists(os.path.join(self.cfg.state_path, "approve", fp)))   # cleared on entering the wait
        p.poll_pending()
        self.assertEqual(self.only_fp(p).state, "awaiting_approval")               # still waiting

    def test_slack_text_cannot_ping_channels(self):                                # LOW 11
        from arm_pipeline.slack import escape
        self.assertEqual(escape("<!channel> a&b <@U1>"), "&lt;!channel&gt; a&amp;b &lt;@U1&gt;")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe needed")
class RegionalTitles(PipelineBase):
    def test_tmdb_regional_title_does_not_create_a_duplicate_folder(self):
        """TMDb says 'Philosopher's Stone'; the library (and the disc) say 'Sorcerer's Stone': find the existing movie."""
        self.make_mkv(self.movie("Harry Potter and the Sorcerer's Stone (2001)", "Harry Potter and the Sorcerer's Stone (2001).mkv"))
        self.write_scan(label="HARRY_POTTER_SORCERERS_STONE", name="Harry Potter and the Sorcerer's Stone")
        from arm_policy import decision
        tmdb = type("T", (), {"configured": True, "identify": lambda *a: decision.Identity(
            "Harry Potter and the Philosopher's Stone", 2001, 0, "en", 671, "high")})()
        p = self.pipe(tmdb=tmdb)
        self.assertEqual(p.handle_new_disc(), "awaiting-approval")          # matched the existing folder: a replace
        folders = sorted(os.listdir(self.cfg.movies_dir))
        self.assertEqual([f for f in folders if not f.startswith(".")], ["Harry Potter and the Sorcerer's Stone (2001)"])

    def test_tmdb_requests_ask_for_us_titles(self):
        from arm_pipeline import metadata
        seen = []

        def fake_urlopen(url, timeout=15):
            seen.append(url)
            raise OSError("stop")
        t = metadata.Tmdb(self.cfg.__class__(tmdb_api_key="K" * 32), "http://127.0.0.1:1")
        with mock.patch.object(metadata.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(metadata.MetadataError):
                t._get("/search/movie", {"query": "x"})
        self.assertTrue(seen and "language=en-US" in seen[0])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe needed")
class IntegrationHooks(PipelineBase):
    """Events + live progress for the dashboard, and the daemon-side trust boundary for answers from outside."""

    def test_every_slack_message_is_also_an_event_and_progress_is_cleared(self):
        self.write_scan()
        p = self.pipe()
        self.assertEqual(p.handle_new_disc(), "done")
        evs = p.events.tail(50)
        self.assertTrue(any(e["kind"] == "slack" and "ADDED" in e["text"] for e in evs))
        self.assertTrue(any(e["kind"] == "slack" and "disc detected" in e["text"] for e in evs))
        self.assertTrue(all(e["fp"] == self.only_fp(p).fingerprint for e in evs if e["kind"] == "slack"))
        self.assertIsNone(p.progress.read())                                           # nothing left over after a job

    def test_rip_progress_is_published_while_ripping(self):
        self.write_scan()
        os.environ["FAKE_RIP_MODE"] = "slowprgv"
        p = self.pipe()
        out = {}
        th = threading.Thread(target=lambda: out.update(r=p.handle_new_disc()))
        th.start()
        seen = None
        for _ in range(60):                                                             # up to ~12 s
            time.sleep(0.2)
            seen = p.progress.read()
            if seen and seen["phase"] == "ripping" and seen.get("percent"):
                break
        self.assertIsNotNone(seen)
        self.assertEqual(seen["phase"], "ripping")
        self.assertAlmostEqual(seen["percent"], 25.0, places=1)                         # 16384 / 65536 from the PRGV line
        th.join(40)
        self.assertEqual(out["r"], "done")
        self.assertIsNone(p.progress.read())

    def test_legacy_fingerprint_records_are_still_recognised(self):
        from arm_policy import makemkv_scan
        from arm_pipeline.ledger import legacy_fingerprint
        self.write_scan()
        with open(self.scanfile) as fh:
            disc = makemkv_scan.parse_scan(fh.read())
        old_id = legacy_fingerprint(disc.attrs.get(32, ""), disc.titles)
        p = self.pipe()
        p.ledger.put(Record(fingerprint=old_id, state="done", title="Test Movie"))
        self.assertEqual(p.handle_new_disc(), "already-seen")                           # not re-ripped under the new id
        self.assertEqual(self.drive.ejects, 1)

    def test_reject_wins_when_both_answers_exist(self):
        self.make_mkv(self.movie("Test Movie (2020)", "Test Movie (2020).mkv"))
        old = self.movie("Test Movie (2020)", "Test Movie (2020).mkv")
        before = os.path.getsize(old)
        self.write_scan()
        p = self.pipe()
        p.handle_new_disc()
        fp = self.only_fp(p).fingerprint
        for kind in ("approve", "reject"):
            open(os.path.join(self.cfg.state_path, kind, fp), "w").close()
        p.poll_pending()
        self.assertEqual(self.only_fp(p).state, "rejected")
        self.assertEqual(os.path.getsize(old), before)

    def _awaiting(self):
        self.write_scan()
        p = self.pipe(tmdb=FakeTmdb(identity=None))
        self.assertEqual(p.handle_new_disc(), "awaiting-identity")
        return p, self.only_fp(p)

    def test_invalid_identity_answers_are_refused_and_the_disc_keeps_waiting(self):
        import json
        p, rec = self._awaiting()
        path = os.path.join(self.cfg.state_path, "identify", rec.fingerprint + ".json")
        bad = [{"title": "../../etc", "year": 2020}, {"title": "A/B", "year": 2020}, {"title": ".hidden", "year": 2020},
               {"title": "ok", "year": 1500}, {"title": "ok", "year": "2020"}, {"title": "x" * 300, "year": 2020},
               {"title": "tab\there", "year": 2020}, {"title": ":", "year": 2020}, {"year": 2020}, ["not", "a", "dict"]]
        for ans in bad:
            with open(path, "w") as fh:
                json.dump(ans, fh)
            self.assertIsNone(p.resume_identity(rec, True), ans)
            self.assertFalse(os.path.exists(path))
        self.assertEqual(self.only_fp(p).state, "awaiting_identity")
        self.assertFalse(os.path.exists(self.movie("Test Movie (2020)")))
        self.assertIn("invalid identity answer", self.slack.joined())

    def test_symlinked_or_huge_answer_files_are_not_followed(self):
        p, rec = self._awaiting()
        path = os.path.join(self.cfg.state_path, "identify", rec.fingerprint + ".json")
        target = os.path.join(self.tmp, "elsewhere.json")
        with open(target, "w") as fh:
            fh.write('{"title": "Test Movie", "year": 2020}')
        os.symlink(target, path)
        self.assertIsNone(p.resume_identity(rec, True))
        self.assertEqual(self.only_fp(p).state, "awaiting_identity")
        self.assertTrue(os.path.exists(target))                                        # the link target was not touched
        with open(path, "w") as fh:
            fh.write('{"title": "' + "a" * 5000 + '", "year": 2020}')
        self.assertIsNone(p.resume_identity(rec, True))

    def test_valid_identity_answer_still_resumes_and_records_an_event(self):
        import json
        p, rec = self._awaiting()
        with open(os.path.join(self.cfg.state_path, "identify", rec.fingerprint + ".json"), "w") as fh:
            json.dump({"title": "Test Movie", "year": 2020}, fh)
        self.assertEqual(p.resume_identity(rec, True), "done")
        self.assertTrue(any(e["kind"] == "operator" and "Test Movie (2020)" in e["text"] for e in p.events.tail(50)))
