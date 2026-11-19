import json
import os
import threading
import time
import unittest
from unittest import mock

from arm_pipeline.dashboard import data, issues


# --- shared fixture (fake tokens only; no real hosts)
import tempfile
from arm_pipeline.config import Config
from arm_pipeline.ledger import Ledger, Record

FAKE_SECRETS = ("tok-plex-SECRET", "tok-jelly-SECRET", "tmdb-key-SECRET", "xoxb-bot-SECRET",
                "https://hooks.example.invalid/SECRET")


class DashCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        r = self.td.name
        self.movies = os.path.join(r, "movies")
        self.scratch = os.path.join(r, "scratch")
        os.makedirs(self.movies)
        os.makedirs(os.path.join(self.scratch, "rips"))
        self.cfg = Config(movies_dir=self.movies, scratch_dir=self.scratch, state_dir=os.path.join(r, "state"),
                          plex_token="tok-plex-SECRET", jellyfin_token="tok-jelly-SECRET",
                          tmdb_api_key="tmdb-key-SECRET", slack_bot_token="xoxb-bot-SECRET",
                          slack_url="https://hooks.example.invalid/SECRET", slack_channel_id="C0",
                          plex_url="http://127.0.0.1:9", jellyfin_url="http://127.0.0.1:9")
        self.led = Ledger(self.cfg.state_path)

    def put(self, fp, state, age_s=0.0, now=None, **kw):
        """Write a real record, then back-date updated/history by age_s."""
        now = time.time() if now is None else now
        rec = Record(fp, state, **kw)
        self.led.put(rec)
        path = self.led._path(fp)
        with open(path) as f:
            d = json.load(f)
        ts = now - age_s
        d["updated"] = ts
        d["created"] = kw.get("created", ts)
        d["history"] = [[ts, state, kw.get("message", "")]]
        with open(path, "w") as f:
            json.dump(d, f)
        return self.led.get(fp)


def fake_status(now=None, **over):
    s = {"now": now or time.time(), "service": {"active": True, "state": "active"},
         "drive": {"tray": "NO_DISC", "disc_present": False}, "share": {"ok": True, "checked": now or time.time()},
         "scratch": {"free_gb": 500, "total_gb": 1000, "used_pct": 40, "rips_gb": 0},
         "servers": {"plex": {"configured": True, "reachable": True},
                     "jellyfin": {"configured": True, "reachable": True},
                     "tmdb": {"configured": True}, "slack": {"webhook": True, "bot_approvals": True}},
         "current": None, "counts": {}, "version": "dev"}
    s.update(over)
    return s


def ids(issues):
    return [i["id"] for i in issues]


H = 3600


def by_id(iss, prefix):
    m = [i for i in iss if i["id"].startswith(prefix)]
    return m[0] if m else None


class IssueTests(DashCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()

    def run_issues(self, **over):
        return issues.collect_issues(self.cfg, status=fake_status(self.now, **over))

    def put(self, fp, state, age_s=0.0, **kw):
        return super().put(fp, state, age_s, now=self.now, **kw)

    def test_healthy_is_empty(self):
        self.assertEqual(self.run_issues(), [])

    def test_service_down(self):
        i = by_id(self.run_issues(service={"active": False, "state": "failed"}), "service-down")
        self.assertEqual((i["severity"], i["actions"], i["fp"]), ("critical", [], None))
        self.assertEqual(self.run_issues(service={"active": None, "state": "unavailable"}), [])

    def test_identity(self):
        self.put("a" * 16, "awaiting_identity", 100, label="L", plan={"reasons": ["no TMDb match"]})
        i = by_id(self.run_issues(), "identity-")
        self.assertEqual(i["severity"], "warning")
        self.assertEqual(i["id"], "identity-" + "a" * 16)
        self.assertIn("no TMDb match", i["detail"])
        self.assertNotIn("still in the drive", i["detail"])
        self.assertEqual([a["id"] for a in i["actions"]], ["identify", "skip"])
        self.assertEqual(i["actions"][0]["needs_input"], "identify")
        self.assertEqual(i["actions"][0]["path"], "/api/jobs/" + "a" * 16 + "/identify")
        self.assertTrue(i["actions"][1]["confirm"])
        self.assertEqual(i["actions"][1]["method"], "POST")

    def test_identity_disc_in_drive_and_critical_when_old(self):
        self.put("a" * 16, "awaiting_identity", 25 * H, label="L")
        i = by_id(self.run_issues(drive={"tray": "DISC_OK", "disc_present": True}), "identity-")
        self.assertEqual(i["severity"], "critical")
        self.assertIn("the disc is still in the drive", i["detail"].lower())

    def test_identity_dismiss_when_label_shared(self):
        self.put("a" * 16, "awaiting_identity", 200, label="L")
        self.put("b" * 16, "done", 100, label="L")
        i = [x for x in self.run_issues() if x["id"] == "identity-" + "a" * 16][0]
        self.assertEqual([a["id"] for a in i["actions"]], ["identify", "skip", "dismiss"])

    def test_approval(self):
        self.put("a" * 16, "awaiting_approval", 100, label="L",
                 plan={"existing": {"folder": "Old Movie (1990)"}, "reasons": ["better quality"]})
        i = by_id(self.run_issues(), "approval-")
        self.assertEqual(i["severity"], "warning")
        self.assertIn("Old Movie (1990)", i["detail"])
        self.assertIn("better quality", i["detail"])
        acts = {a["id"]: a for a in i["actions"]}
        self.assertFalse(acts["approve"]["confirm"])
        self.assertTrue(acts["reject"]["confirm"])

    def test_approval_critical_near_timeout(self):
        days = self.cfg.approval_timeout_days
        self.put("a" * 16, "awaiting_approval", days * 86400 * 0.79)
        self.assertEqual(by_id(self.run_issues(), "approval-")["severity"], "warning")
        self.put("b" * 16, "awaiting_approval", days * 86400 * 0.81)
        self.assertEqual(by_id(self.run_issues(), "approval-" + "b" * 16)["severity"], "critical")

    def test_failed(self):
        self.put("a" * 16, "failed", 50, message="rip failed (exit 1)")
        i = by_id(self.run_issues(), "failed-")
        self.assertEqual(i["severity"], "critical")
        self.assertEqual(i["detail"], "rip failed (exit 1)")
        acts = {a["id"]: a for a in i["actions"]}
        self.assertFalse(acts["retry"]["confirm"])
        self.assertEqual(acts["retry"]["path"], "/api/jobs/" + "a" * 16 + "/retry")
        self.assertIn("dismiss", acts)

    def test_duplicates(self):
        self.put("a" * 16, "awaiting_identity", 300, label="My Disc")
        self.put("b" * 16, "failed", 200, label="My Disc")
        self.put("c" * 16, "awaiting_approval", 100, label="My Disc")
        self.put("d" * 16, "done", 10, label="My Disc")          # terminal: not counted
        self.put("e" * 16, "awaiting_identity", 10, label="Other")
        iss = self.run_issues()
        d = by_id(iss, "duplicate-")
        self.assertEqual(d["id"], "duplicate-my-disc")
        self.assertEqual(d["title"], "Duplicate records for My Disc")
        self.assertEqual(d["severity"], "warning")
        self.assertEqual(d["fp"], "c" * 16)
        self.assertEqual(sorted(a["path"] for a in d["actions"]),
                         sorted(f"/api/jobs/{x * 16}/dismiss" for x in "ab"))
        self.assertEqual(len([i for i in iss if i["id"].startswith("duplicate-")]), 1)

    def test_no_duplicate_for_single(self):
        self.put("a" * 16, "awaiting_identity", 300, label="X")
        self.assertIsNone(by_id(self.run_issues(), "duplicate-"))

    def test_stuck(self):
        self.put("a" * 16, "ripping", 4 * H)
        i = by_id(self.run_issues(), "stuck-")
        self.assertEqual((i["severity"], i["title"]), ("critical", "Job looks stuck"))
        # actively progressing job is not stuck
        cur = {"fp": "a" * 16, "stale": False}
        self.assertIsNone(by_id(self.run_issues(current=cur), "stuck-"))
        cur["stale"] = True
        self.assertIsNotNone(by_id(self.run_issues(current=cur), "stuck-"))
        # recent update is not stuck
        self.put("b" * 16, "verifying", 2 * H)
        self.assertIsNone(by_id(self.run_issues(), "stuck-" + "b" * 16))

    def test_share_down(self):
        i = by_id(self.run_issues(share={"ok": False, "checked": self.now}), "share-down")
        self.assertEqual(i["severity"], "critical")

    def test_drive_unknown(self):
        i = by_id(self.run_issues(drive={"tray": "UNKNOWN", "disc_present": False}), "drive-unknown")
        self.assertEqual(i["severity"], "warning")

    def test_disc_finished_eject(self):
        self.put("a" * 16, "done", 100, label="L")
        drv = {"tray": "DISC_OK", "disc_present": True}
        i = by_id(self.run_issues(drive=drv), "disc-finished")
        self.assertEqual(i["severity"], "info")
        a = i["actions"][0]
        self.assertEqual((a["method"], a["path"], a["confirm"]), ("POST", "/api/drive/eject", True))
        self.assertIsNone(by_id(self.run_issues(drive=drv, service={"active": False, "state": "x"}), "disc-"))

    def test_disc_waiting_and_not_when_active(self):
        drv = {"tray": "DISC_OK", "disc_present": True}
        i = by_id(self.run_issues(drive=drv), "disc-waiting")
        self.assertEqual(i["title"], "Disc present, waiting for the next poll")
        self.put("a" * 16, "ripping", 10)
        self.assertIsNone(by_id(self.run_issues(drive=drv), "disc-"))

    def test_scratch(self):
        sc = lambda p, free=500: {"free_gb": free, "total_gb": 1000, "used_pct": p, "rips_gb": 0}
        self.assertIsNone(by_id(self.run_issues(scratch=sc(84.9)), "scratch"))
        self.assertEqual(by_id(self.run_issues(scratch=sc(85)), "scratch")["severity"], "warning")
        self.assertEqual(by_id(self.run_issues(scratch=sc(95)), "scratch")["severity"], "critical")
        self.assertIsNone(by_id(self.run_issues(scratch=sc(None)), "scratch"))

    def test_scratch_low_for_a_4k_rip_offers_cleanup(self):
        i = by_id(self.run_issues(scratch={"free_gb": 60, "total_gb": 200, "used_pct": 70, "rips_gb": 55}), "scratch")
        self.assertEqual(i["severity"], "warning")
        self.assertIn("55 GB", i["detail"])
        self.assertEqual([a["path"] for a in i["actions"]], ["/api/scratch/clean"])
        self.assertTrue(i["actions"][0]["confirm"])
        self.assertIsNone(by_id(self.run_issues(scratch={"free_gb": 120, "total_gb": 200, "used_pct": 40, "rips_gb": 0}), "scratch"))

    def test_integrations(self):
        sv = {"plex": {"configured": False, "reachable": None}, "jellyfin": {"configured": False, "reachable": None},
              "tmdb": {"configured": False}, "slack": {"webhook": False, "bot_approvals": False}}
        iss = {i["id"]: i for i in self.run_issues(servers=sv)}
        self.assertEqual(iss["plex-not-configured"]["severity"], "info")
        self.assertEqual(iss["plex-not-configured"]["title"],
                         "Plex is not configured: new movies will not appear until it rescans")
        self.assertEqual(iss["jellyfin-not-configured"]["severity"], "info")
        self.assertEqual(iss["tmdb-not-configured"]["severity"], "warning")
        self.assertEqual(iss["slack-webhook-missing"]["severity"], "warning")
        sv = {"plex": {"configured": True, "reachable": False}, "jellyfin": {"configured": True, "reachable": False},
              "tmdb": {"configured": True}, "slack": {"webhook": True}}
        iss = {i["id"]: i for i in self.run_issues(servers=sv)}
        self.assertEqual(set(iss), {"plex-unreachable", "jellyfin-unreachable"})
        self.assertTrue(all(i["severity"] == "warning" for i in iss.values()))
        sv["plex"]["reachable"] = None                          # unknown is not an issue
        self.assertNotIn("plex-unreachable", ids(self.run_issues(servers=sv)))

    def test_leftover_staged(self):
        part = os.path.join(self.movies, "M.mkv.part")
        open(part, "w").close()
        self.put("a" * 16, "failed", 10, paths={"part": part})
        self.put("b" * 16, "rejected", 10, paths={"part": part})
        self.put("c" * 16, "rejected", 10, paths={"part": part + ".gone"})
        self.put("d" * 16, "done", 10, paths={"part": part})
        iss = self.run_issues()
        left = [i for i in iss if i["id"].startswith("leftover-")]
        self.assertEqual(sorted(i["fp"] for i in left), ["a" * 16, "b" * 16])
        self.assertEqual(left[0]["severity"], "warning")
        self.assertEqual(left[0]["actions"], [])
        self.assertIn(part, left[0]["detail"])

    def test_staged_missing_on_approval(self):
        part = os.path.join(self.movies, "M.mkv.part")
        self.put("a" * 16, "awaiting_approval", 10, paths={"part": part})
        i = by_id(self.run_issues(), "staged-missing-")
        self.assertEqual(i["severity"], "critical")
        open(part, "w").close()
        self.assertIsNone(by_id(self.run_issues(), "staged-missing-"))
        os.unlink(part)
        # share down: absence proves nothing
        self.assertIsNone(by_id(self.run_issues(share={"ok": False, "checked": self.now}), "staged-missing-"))

    def test_sorting(self):
        self.put("a" * 16, "awaiting_identity", 100, label="A")                  # warning
        self.put("b" * 16, "awaiting_identity", 500, label="B")                  # warning, older
        self.put("c" * 16, "failed", 50, label="C")                              # critical
        self.put("d" * 16, "failed", 900, label="D")                             # critical, older
        sv = fake_status(self.now)["servers"]
        sv["plex"] = {"configured": False, "reachable": None}                    # info
        iss = self.run_issues(servers=sv)
        self.assertEqual(ids(iss), ["failed-" + "d" * 16, "failed-" + "c" * 16, "identity-" + "b" * 16,
                                    "identity-" + "a" * 16, "plex-not-configured"])

    def test_rule_error_becomes_info(self):
        def boom(c):
            raise RuntimeError("x")
        rules = [("service", boom)] + issues.RULES[1:]
        with mock.patch.object(issues, "RULES", rules):
            iss = self.run_issues()
        self.assertEqual(ids(iss), ["check-failed-service"])
        self.assertEqual((iss[0]["severity"], iss[0]["title"]), ("info", "Check failed: service"))

    def test_computes_status_and_jobs_when_none(self):
        with mock.patch.object(data, "collect_status", return_value=fake_status(self.now)) as cs:
            self.assertEqual(issues.collect_issues(self.cfg), [])
        cs.assert_called_once()

    def test_hung_share_returns_promptly(self):
        part = os.path.join(self.movies, "M.mkv.part")
        for n in "abc":
            self.put(n * 16, "failed", 10, paths={"part": part + n})
        self.put("d" * 16, "awaiting_approval", 10, paths={"part": part})
        gate = threading.Event()
        self.addCleanup(gate.set)
        with mock.patch.object(data, "_raw_stat", side_effect=lambda p: gate.wait(10)), \
                mock.patch.object(data, "FS_TIMEOUT_S", 0.3):
            t0 = time.time()
            iss = self.run_issues()
        self.assertLess(time.time() - t0, 2.5)
        self.assertIsNone(by_id(iss, "staged-missing-"))        # unknown, not "gone"
        self.assertIsNone(by_id(iss, "leftover-"))

    def test_no_secrets_in_output(self):
        self.put("a" * 16, "failed", 10, message="m", paths={"part": "/x", "ticket": {"channel": "C", "ts": "9"}})
        s = json.dumps(issues.collect_issues(self.cfg, status=fake_status(self.now)))
        for sec in FAKE_SECRETS + ('"ticket"',):
            self.assertNotIn(sec, s)


if __name__ == "__main__":
    unittest.main()
