import json
import os
import time
import unittest
from unittest import mock

from arm_pipeline.dashboard import data
from arm_pipeline.errors import DriveError, MetadataError


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


SCAN = '''CINFO:2,0,"MY DISC"
TINFO:0,9,0,"1:50:00"
TINFO:0,8,0,"20"
TINFO:0,11,0,"1073741824"
SINFO:0,0,1,6201,"Video"
SINFO:0,0,19,0,"1920x1080"
SINFO:0,1,1,6202,"Audio"
SINFO:0,1,3,0,"eng"
SINFO:0,2,1,6203,"Subtitles"
SINFO:0,2,3,0,"spa"
TINFO:1,9,0,"0:10:00"
'''


REAL_SERVICE_STATE = data._service_state
REAL_HTTP_GET = data._http_get


def no_http(url, timeout=3):
    return True


class CommonPatches(DashCase):
    def setUp(self):
        super().setUp()
        data._REACH_CACHE.clear()
        for p in (mock.patch.object(data, "_service_state", return_value=(True, "active")),
                  mock.patch.object(data.drive_mod, "tray_status", return_value="DISC_OK"),
                  mock.patch.object(data, "_http_get", side_effect=no_http)):
            p.start()
            self.addCleanup(p.stop)


class StatusTests(CommonPatches):
    def test_basic_shape(self):
        open(os.path.join(self.movies, self.cfg.canary_name), "w").close()
        self.put("a" * 16, "done")
        s = data.collect_status(self.cfg, now=1000.0)
        self.assertEqual(s["now"], 1000.0)
        self.assertEqual(s["service"], {"active": True, "state": "active"})
        self.assertEqual(s["drive"], {"tray": "DISC_OK", "disc_present": True})
        self.assertTrue(s["share"]["ok"])
        self.assertEqual(s["counts"], {"done": 1})
        self.assertIsNone(s["current"])
        self.assertTrue(s["servers"]["plex"]["reachable"])
        self.assertTrue(s["servers"]["tmdb"]["configured"])
        self.assertEqual(s["servers"]["slack"], {"webhook": True, "bot_approvals": True})
        self.assertIn("used_pct", s["scratch"])

    def test_no_secrets(self):
        s = json.dumps(data.collect_status(self.cfg))
        for sec in FAKE_SECRETS:
            self.assertNotIn(sec, s)

    def test_share_missing_canary(self):
        self.assertFalse(data.collect_status(self.cfg)["share"]["ok"])

    def test_drive_error_unknown(self):
        with mock.patch.object(data.drive_mod, "tray_status", side_effect=DriveError("x")):
            d = data.collect_status(self.cfg)["drive"]
        self.assertEqual(d, {"tray": "UNKNOWN", "disc_present": False})

    def test_systemctl_unavailable_and_states(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(REAL_SERVICE_STATE(), (None, "unavailable"))
        r = mock.Mock(stdout="inactive\n")
        with mock.patch("subprocess.run", return_value=r):
            self.assertEqual(REAL_SERVICE_STATE(), (False, "inactive"))
        r = mock.Mock(stdout="active\n")
        with mock.patch("subprocess.run", return_value=r):
            self.assertEqual(REAL_SERVICE_STATE(), (True, "active"))

    def test_unconfigured_servers(self):
        cfg = self.cfg.__class__(**{**self.cfg.__dict__, "plex_url": "", "jellyfin_url": "", "tmdb_api_key": "",
                                    "slack_url": ""})
        sv = data.collect_status(cfg)["servers"]
        self.assertEqual(sv["plex"], {"configured": False, "reachable": None})
        self.assertFalse(sv["tmdb"]["configured"])
        self.assertFalse(sv["slack"]["webhook"])

    def test_reachability_cached_and_false(self):
        calls = []

        def g(url, timeout=3):
            calls.append(url)
            return False
        with mock.patch.object(data, "_http_get", side_effect=g):
            s1 = data.collect_status(self.cfg)
            data.collect_status(self.cfg)
        self.assertFalse(s1["servers"]["plex"]["reachable"])
        self.assertEqual(len(calls), 2)                       # one per server, second call cached
        self.assertTrue(any(c.endswith("/identity") for c in calls))
        self.assertTrue(any(c.endswith("/System/Info/Public") for c in calls))
        self.assertFalse(any("SECRET" in c or "token" in c.lower() for c in calls))

    def test_real_http_get_never_raises(self):
        self.assertIs(REAL_HTTP_GET("http://127.0.0.1:9/identity", 1), False)

    def test_share_timeout_prompt(self):
        import threading
        gate = threading.Event()
        self.addCleanup(gate.set)
        with mock.patch.object(data, "_raw_stat", side_effect=lambda p: gate.wait(10)), \
                mock.patch.object(data, "FS_TIMEOUT_S", 0.3):
            t0 = time.time()
            s = data.collect_status(self.cfg)
        self.assertLess(time.time() - t0, 2.5)
        self.assertFalse(s["share"]["ok"])

    def test_current_from_progress(self):
        self.put("b" * 16, "ripping", title="Movie")

        class P:
            def __init__(self, d): pass

            def read(self, now=None):
                return {"fp": "b" * 16, "phase": "rip", "done_bytes": 1024 ** 3, "total_bytes": 4 * 1024 ** 3,
                        "percent": 25.0, "rate_bps": 2e6, "eta_s": 90, "stale": False}
        with mock.patch.object(data, "_lazy_progress", return_value=P(None)):
            c = data.collect_status(self.cfg)["current"]
        self.assertEqual(c["fp"], "b" * 16)
        self.assertEqual((c["percent"], c["done_gb"], c["total_gb"], c["rate_mbps"], c["eta_s"]),
                         (25.0, 1.0, 4.0, 2.0, 90))
        self.assertFalse(c["stale"])

    def test_current_fallback_prgv(self):
        fp = "c" * 16
        self.put(fp, "ripping", title="Movie")
        out = os.path.join(self.scratch, "rips", fp)
        os.makedirs(out)
        with open(os.path.join(out, "rip.log"), "w") as f:
            f.write("PRGV:10,20,100\nPRGV:250,999,1000\n")
        with open(os.path.join(out, "t00.mkv"), "wb") as f:
            f.write(b"x" * 2048)
        with mock.patch.object(data, "_lazy_progress", return_value=None):
            c = data.collect_status(self.cfg)["current"]
        self.assertEqual(c["percent"], 25.0)
        self.assertFalse(c["stale"])
        old = time.time() - 1000
        os.utime(os.path.join(out, "rip.log"), (old, old))
        with mock.patch.object(data, "_lazy_progress", return_value=None):
            self.assertTrue(data.collect_status(self.cfg)["current"]["stale"])

    def test_fallback_real_log_tail_regression(self):
        fp = "9" * 16
        self.put(fp, "ripping", title="Movie", plan={"title_index": 3})
        out = os.path.join(self.scratch, "rips", fp)
        os.makedirs(out)
        with open(os.path.join(out, "rip.log"), "w") as f:
            f.write("PRGV:17000,17000,65536\nPRGV:17844,17844,65536\n")
        with open(os.path.join(out, "t03.mkv"), "wb") as f:
            f.truncate(int(9.84 * 1024 ** 3))
        scan = ('TINFO:3,9,0,"2:00:00"\nTINFO:3,11,0,"%d"\n' % int(40.1 * 1024 ** 3))
        os.makedirs(os.path.join(self.cfg.state_path, "scans"), exist_ok=True)
        with open(os.path.join(self.cfg.state_path, "scans", fp + ".txt"), "w") as f:
            f.write(scan)
        with mock.patch.object(data, "_lazy_progress", return_value=None):
            c = data.collect_status(self.cfg)["current"]
        self.assertEqual(c["percent"], 27.2)
        self.assertEqual(c["done_gb"], 9.84)
        self.assertEqual(c["total_gb"], 40.1)
        self.assertIsNone(c["eta_s"])
        self.assertIsNone(c["rate_mbps"])

    def test_fallback_without_scan_or_title_index(self):
        fp = "8" * 16
        self.put(fp, "ripping", plan={})
        out = os.path.join(self.scratch, "rips", fp)
        os.makedirs(out)
        with open(os.path.join(out, "rip.log"), "w") as f:
            f.write("PRGV:17844,17844,65536\n")
        with mock.patch.object(data, "_lazy_progress", return_value=None):
            c = data.collect_status(self.cfg)["current"]
        self.assertEqual(c["percent"], 27.2)
        self.assertIsNone(c["total_gb"])

    def test_collect_issues_delegates(self):
        self.put("a" * 16, "failed", message="boom")
        with mock.patch.object(data, "_lazy_progress", return_value=None):
            iss = data.collect_issues(self.cfg, status=None, jobs=None)
        self.assertIn("failed-" + "a" * 16, [i["id"] for i in iss])
        iss = data.collect_issues(self.cfg, status={"now": time.time()}, jobs=[])
        self.assertIsInstance(iss, list)

    def test_current_none_when_idle(self):
        self.put("d" * 16, "awaiting_identity")
        self.assertIsNone(data.collect_status(self.cfg)["current"])


class JobTests(CommonPatches):
    def test_list_order_and_flatten(self):
        self.put("a" * 16, "done", age_s=100, label="A", message="m" * 500,
                 plan={"action": "rip_new", "identity": {"title": "T", "year": 1999}, "target_folder": "T (1999)",
                       "needs_approval": True, "warnings": ["w"]})
        self.put("b" * 16, "failed", age_s=10, label="B")
        jobs = data.list_jobs(self.cfg)
        self.assertEqual([j["fp"] for j in jobs], ["b" * 16, "a" * 16])
        a = jobs[1]
        self.assertEqual(len(a["message"]), 300)
        self.assertEqual(a["action"], "rip_new")
        self.assertEqual(a["identity"], {"title": "T", "year": 1999})
        self.assertTrue(a["needs_approval"])
        self.assertEqual(a["warnings"], ["w"])
        self.assertGreaterEqual(a["age_s"], 99)
        self.assertIsNone(jobs[0]["existing"])
        self.assertEqual(len(data.list_jobs(self.cfg, limit=1)), 1)

    def test_detail_none_and_bad_fp(self):
        self.assertIsNone(data.job_detail(self.cfg, "f" * 16))
        self.assertIsNone(data.job_detail(self.cfg, "../x"))

    def test_detail_strips_secrets_and_scan(self):
        fp = "e" * 16
        self.put(fp, "awaiting_approval", plan={"title_index": 0, "reasons": ["r1"]},
                 paths={"outdir": "/o", "final": "/f", "part": "/p", "ripped": "/r", "part_sha256": "h",
                        "ticket": {"channel": "C", "ts": "1"}, "replace": {"x": 1}, "info": "i", "part_size": 5})
        os.makedirs(os.path.join(self.cfg.state_path, "scans"), exist_ok=True)
        with open(os.path.join(self.cfg.state_path, "scans", fp + ".txt"), "w") as f:
            f.write(SCAN)
        d = data.job_detail(self.cfg, fp)
        self.assertEqual(set(d["paths"]), {"outdir", "final", "part", "ripped"})
        self.assertNotIn("ticket", json.dumps(d))
        self.assertEqual(d["reasons"], ["r1"])
        self.assertTrue(d["history"])
        t0, t1 = d["scan"]["titles"]
        self.assertTrue(t0["is_selected"])
        self.assertFalse(t1["is_selected"])
        self.assertEqual((t0["duration_s"], t0["size_gb"], t0["chapters"]), (6600, 1.0, 20))
        self.assertEqual((t0["audio_langs"], t0["sub_langs"]), (["eng"], ["spa"]))
        self.assertEqual(d["scan"]["disc_name"], "MY DISC")

    def test_detail_missing_or_corrupt_scan(self):
        fp = "1" * 16
        self.put(fp, "done")
        self.assertIsNone(data.job_detail(self.cfg, fp)["scan"])
        os.makedirs(os.path.join(self.cfg.state_path, "scans"), exist_ok=True)
        with open(os.path.join(self.cfg.state_path, "scans", fp + ".txt"), "wb") as f:
            f.write(b"\xff\xfe garbage \x00")
        d = data.job_detail(self.cfg, fp)
        self.assertIsNotNone(d)
        self.assertEqual(d["scan"]["titles"], [])


class EventTests(CommonPatches):
    def test_synthesized_from_history(self):
        self.put("a" * 16, "failed", title="T", message="boom")
        self.put("b" * 16, "done")
        ev = data.tail_events(self.cfg)
        self.assertEqual({e["fp"] for e in ev}, {"a" * 16, "b" * 16})
        e = [e for e in ev if e["fp"] == "a" * 16][0]
        self.assertEqual((e["kind"], e["level"], e["data"]), ("state", "error", {"state": "failed"}))
        self.assertIn("boom", e["text"])
        self.assertEqual(len(data.tail_events(self.cfg, fp="b" * 16)), 1)
        self.assertEqual(data.tail_events(self.cfg, since=time.time() + 5), [])
        self.assertEqual(len(data.tail_events(self.cfg, limit=1)), 1)

    def test_uses_eventlog_when_file_exists(self):
        open(os.path.join(self.cfg.state_path, "events.jsonl"), "w").close()

        class EL:
            def tail(self, limit, since, fp):
                return [{"ts": 1, "level": "info", "kind": "x", "fp": fp, "text": "t", "data": {}}]
        with mock.patch.object(data, "_lazy_eventlog", return_value=EL()):
            self.assertEqual(data.tail_events(self.cfg, fp="z")[0]["kind"], "x")

    def test_eventlog_error_gives_empty(self):
        open(os.path.join(self.cfg.state_path, "events.jsonl"), "w").close()

        class EL:
            def tail(self, *a):
                raise RuntimeError
        with mock.patch.object(data, "_lazy_eventlog", return_value=EL()):
            self.assertEqual(data.tail_events(self.cfg), [])


class TmdbTests(CommonPatches):
    def test_search(self):
        calls = []
        results = [{"id": i, "title": f"M{i}", "release_date": "1999-01-01", "original_language": "en",
                    "overview": "o" * 400} for i in range(1, 11)]

        def get(self_, path, params):
            calls.append((path, params))
            if path == "/search/movie":
                return {"results": results}
            return {"runtime": 100 + int(path.rsplit("/", 1)[1])}
        with mock.patch.object(data.metadata.Tmdb, "_get", get):
            out = data.tmdb_search(self.cfg, "x" * 300, year=1999)
        self.assertEqual(len(out), 8)
        self.assertEqual(len(calls), 1 + 5)
        self.assertEqual(len(calls[0][1]["query"]), 100)
        self.assertEqual(calls[0][1]["year"], 1999)
        self.assertEqual(out[0], {"tmdb_id": 1, "title": "M1", "year": 1999, "runtime": 101,
                                  "original_language": "en", "overview": "o" * 160})
        self.assertIsNone(out[6]["runtime"])
        for sec in FAKE_SECRETS:
            self.assertNotIn(sec, json.dumps(out))

    def test_unconfigured_and_errors(self):
        cfg = self.cfg.__class__(**{**self.cfg.__dict__, "tmdb_api_key": ""})
        self.assertEqual(data.tmdb_search(cfg, "x"), [])
        with mock.patch.object(data.metadata.Tmdb, "_get", side_effect=MetadataError("x")):
            self.assertEqual(data.tmdb_search(self.cfg, "x"), [])
        self.assertEqual(data.tmdb_search(self.cfg, "  "), [])


class TimeoutTests(CommonPatches):
    def test_fs_exists_many_shared_deadline(self):
        import threading
        gate = threading.Event()
        self.addCleanup(gate.set)
        with mock.patch.object(data, "_raw_stat", side_effect=lambda p: gate.wait(10)):
            t0 = time.time()
            r = data.fs_exists_many(["/a", "/b", "/c"], timeout=0.3)
        self.assertLess(time.time() - t0, 1.5)
        self.assertEqual(set(r.values()), {None})

    def test_fs_exists_values(self):
        p = os.path.join(self.movies, "f")
        self.assertFalse(data.fs_exists(p))
        open(p, "w").close()
        self.assertTrue(data.fs_exists(p))
        self.assertFalse(data.fs_exists(""))


if __name__ == "__main__":
    unittest.main()
