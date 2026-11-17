import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from arm_pipeline.config import Config
from arm_pipeline.errors import ServerError
from arm_pipeline.media_servers import Jellyfin, Plex, to_server_path

CFG = Config(movies_dir="/mnt/movies", plex_url="x", plex_token="PLEXTOK", plex_section_id="7",
             plex_movies_path="/data/movies", jellyfin_url="x", jellyfin_token="JFTOK",
             jellyfin_movies_path="/jf/movies")


class Fake(BaseHTTPRequestHandler):
    log = []          # (method, path, headers, body)
    routes = {}       # path-without-query -> callable(query, body) -> (code, obj|None)

    def log_message(self, *a):
        pass

    def _handle(self, method):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""
        u = urllib.parse.urlparse(self.path)
        Fake.log.append((method, self.path, dict(self.headers), body))
        fn = Fake.routes.get(u.path)
        code, obj = fn(urllib.parse.parse_qs(u.query), body) if fn else (404, {})
        data = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Fake.log, Fake.routes = [], {}


class PathTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(to_server_path("/mnt/movies/A (2000)/a.mkv", "/mnt/movies", "/data/movies"),
                         "/data/movies/A (2000)/a.mkv")

    def test_trailing_slashes(self):
        self.assertEqual(to_server_path("/mnt/movies/A (2000)/", "/mnt/movies/", "/data/movies/"),
                         "/data/movies/A (2000)")

    def test_root_itself(self):
        self.assertEqual(to_server_path("/mnt/movies", "/mnt/movies", "/data/movies"), "/data/movies")

    def test_apostrophe_unicode(self):
        self.assertEqual(to_server_path("/mnt/movies/Amélie's, Tale (2001)/x.mkv", "/mnt/movies", "/d"),
                         "/d/Amélie's, Tale (2001)/x.mkv")

    def test_outside_refused(self):
        for bad in ("/mnt/other/x.mkv", "/mnt/movies2/x.mkv", "/mnt/movies/../etc/passwd", "relative"):
            with self.assertRaises(ServerError):
                to_server_path(bad, "/mnt/movies", "/data/movies")


class PlexTests(Base):
    def plex(self):
        return Plex(CFG, api_base=self.base)

    def test_configured(self):
        self.assertTrue(Plex(CFG).configured)
        self.assertFalse(Plex(Config()).configured)

    def test_scan_path_encoded(self):
        Fake.routes["/library/sections/7/refresh"] = lambda q, b: (200, None)
        self.plex().scan_path("/mnt/movies/Harry's Movie, The (2001)/")
        _, path, headers, _ = Fake.log[0]
        self.assertIn("path=%2Fdata%2Fmovies%2FHarry%27s%20Movie%2C%20The%20%282001%29", path)
        self.assertEqual(headers["X-Plex-Token"], "PLEXTOK")
        self.assertNotIn("PLEXTOK", path)

    def test_scan_error(self):
        Fake.routes["/library/sections/7/refresh"] = lambda q, b: (500, None)
        with self.assertRaises(ServerError) as cm:
            self.plex().scan_path("/mnt/movies/A")
        self.assertNotIn("PLEXTOK", str(cm.exception))

    def meta(self, f):
        return {"MediaContainer": {"Metadata": [{"ratingKey": "5", "title": "T", "year": 2001, "addedAt": 1700000000,
                "Media": [{"videoResolution": "1080", "videoCodec": "h264", "Part": [{"file": f}]}]}]}}

    def test_items_parse(self):
        Fake.routes["/library/sections/7/all"] = lambda q, b: (200, self.meta("/data/movies/T/t.mkv"))
        it = self.plex().items("T")
        self.assertEqual((it[0]["key"], it[0]["title"], it[0]["year"], it[0]["file"], it[0]["res"],
                          it[0]["codec"], it[0]["added"]),
                         ("5", "T", 2001, "/data/movies/T/t.mkv", "1080", "h264", 1700000000))

    def test_wait_for_file_after_polls(self):
        calls = []

        def route(q, b):
            calls.append(1)
            return 200, self.meta("/data/movies/T/t.mkv" if len(calls) >= 3 else "/other")
        Fake.routes["/library/sections/7/all"] = route
        sleeps = []
        self.assertTrue(self.plex().wait_for_file("T", "/data/movies/T/t.mkv", True, 100, poll_s=5,
                                                  sleep=sleeps.append, now=lambda: 0))
        self.assertEqual(sleeps, [5, 5])

    def test_wait_for_file_absent_and_timeout(self):
        Fake.routes["/library/sections/7/all"] = lambda q, b: (200, self.meta("/x"))
        self.assertTrue(self.plex().wait_for_file("T", "/gone", False, 10, sleep=lambda s: None))
        clock = [0]

        def sleep(s):
            clock[0] += s
        self.assertFalse(self.plex().wait_for_file("T", "/gone", True, 12, poll_s=5, sleep=sleep,
                                                   now=lambda: clock[0]))


class JellyfinTests(Base):
    def jf(self):
        return Jellyfin(CFG, api_base=self.base)

    def test_push_exact(self):
        Fake.routes["/Library/Media/Updated"] = lambda q, b: (204, None)
        self.jf().push("/mnt/movies/A (2000)/a.mkv", "Created")
        method, path, headers, body = Fake.log[0]
        self.assertEqual(method, "POST")
        self.assertEqual(headers["Authorization"], 'MediaBrowser Token="JFTOK"')
        self.assertNotIn("X-Emby-Token", headers)
        self.assertNotIn("JFTOK", path)
        self.assertEqual(json.loads(body), {"Updates": [{"Path": "/jf/movies/A (2000)/a.mkv",
                                                         "UpdateType": "Created"}]})

    def test_push_bad_type(self):
        with self.assertRaises(ServerError):
            self.jf().push("/mnt/movies/a", "Bogus")

    def test_401(self):
        Fake.routes["/Library/Media/Updated"] = lambda q, b: (401, None)
        with self.assertRaises(ServerError) as cm:
            self.jf().push("/mnt/movies/a", "Modified")
        self.assertNotIn("JFTOK", str(cm.exception))

    def test_200_is_not_204(self):
        Fake.routes["/Library/Media/Updated"] = lambda q, b: (200, {})
        with self.assertRaises(ServerError):
            self.jf().push("/mnt/movies/a", "Deleted")

    def item(self, i, path):
        return {"Id": f"id{i}", "Name": "Philosopher's Stone", "ProductionYear": 2001, "Path": path,
                "ProviderIds": {"Tmdb": "671"},
                "MediaStreams": [{"Type": "Audio"}, {"Type": "Video", "Height": 1080, "Codec": "hevc",
                                                     "VideoRangeType": "HDR10"}]}

    def paged(self, target_page_item=None):
        def route(q, b):
            start = int(q["StartIndex"][0])
            self.assertEqual(q["Limit"], ["500"])
            self.assertIn("Path", q["Fields"][0])
            items = [self.item(start + i, f"/jf/movies/f{start + i}/x.mkv") for i in range(500)]
            if target_page_item and start == 500:
                items[3] = self.item("T", target_page_item)
            return 200, {"Items": items, "TotalRecordCount": 1000}
        Fake.routes["/Items"] = route

    def test_find_by_path_paginates(self):
        self.paged("/jf/movies/Sorcerer's Stone (2001)/s.mkv")
        r = self.jf().find_by_path("/mnt/movies/Sorcerer's Stone (2001)/s.mkv")
        self.assertEqual(r, {"id": "idT", "name": "Philosopher's Stone", "year": 2001,
                             "path": "/jf/movies/Sorcerer's Stone (2001)/s.mkv", "res": 1080,
                             "codec": "hevc", "range": "HDR10", "tmdb": "671"})
        self.assertEqual(len(Fake.log), 2)
        self.assertNotIn("JFTOK", Fake.log[0][1])

    def test_find_by_path_exhausted(self):
        self.paged()
        self.assertIsNone(self.jf().find_by_path("/mnt/movies/nope/x.mkv"))
        self.assertEqual(len(Fake.log), 2)       # stops at TotalRecordCount

    def test_page_cap(self):
        Fake.routes["/Items"] = lambda q, b: (200, {"Items": [self.item(0, "/other")] * 500,
                                                    "TotalRecordCount": 10 ** 6})
        self.assertIsNone(self.jf().find_by_path("/mnt/movies/nope/x.mkv"))
        self.assertEqual(len(Fake.log), 20)

    def test_wait_for_path_timeout_and_success(self):
        Fake.routes["/Items"] = lambda q, b: (200, {"Items": [], "TotalRecordCount": 0})
        clock = [0]

        def sleep(s):
            clock[0] += s
        self.assertFalse(self.jf().wait_for_path("/mnt/movies/a/a.mkv", True, 25, poll_s=10, sleep=sleep,
                                                 now=lambda: clock[0]))
        self.assertGreaterEqual(clock[0], 25)
        self.assertTrue(self.jf().wait_for_path("/mnt/movies/a/a.mkv", False, 25, sleep=sleep,
                                                now=lambda: clock[0]))

    def test_wait_for_path_appears(self):
        n = []

        def route(q, b):
            n.append(1)
            items = [self.item(1, "/jf/movies/a/a.mkv")] if len(n) >= 3 else []
            return 200, {"Items": items, "TotalRecordCount": len(items)}
        Fake.routes["/Items"] = route
        self.assertTrue(self.jf().wait_for_path("/mnt/movies/a/a.mkv", True, 100, sleep=lambda s: None,
                                                now=lambda: 0))


if __name__ == "__main__":
    unittest.main()
