import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from arm_pipeline.config import Config
from arm_pipeline.errors import MetadataError
from arm_pipeline.metadata import Tmdb, parse_disc_name, joined_title_key

KEY = "SECRETKEY123"


class ParseTests(unittest.TestCase):
    CASES = [
        ("HARRY_POTTER_SORCERERS_STONE", "Harry Potter and the Sorcerer's Stone",
         ("Harry Potter and the Sorcerer's Stone", None)),
        ("THE_MATRIX_4K", "", ("The Matrix", None)),
        ("LORD_OF_THE_RINGS_FELLOWSHIP_D1", "", ("Lord of the Rings Fellowship", None)),
        ("BLADE_RUNNER_1982", "", ("Blade Runner", 1982)),
        ("X", "Blade Runner (1982)", ("Blade Runner", 1982)),
        ("BLADE_RUNNER_1982", "Blade Runner", ("Blade Runner", 1982)),
        ("BLADE_RUNNER_2049", "", ("Blade Runner 2049", None)),
        ("2001_A_SPACE_ODYSSEY", "", ("2001 a Space Odyssey", None)),
        ("1917", "", ("1917", None)),
        ("TOY_STORY_BD_DISC1", "", ("Toy Story", None)),
        ("JAWS_DVD_WS", "", ("Jaws", None)),
        ("JAWS_UHD_BLURAY", "", ("Jaws", None)),
        ("ALIEN_REGION1", "", ("Alien", None)),
        ("EXAMPLE_DELUXE_EDITION", "", ("Example Deluxe Edition", None)),
        ("EXAMPLE_MOVIE_1999_FS", "", ("Example Movie", 1999)),
        ("", "", ("", None)),
        ("SOME_MOVIE", "SOME_MOVIE", ("Some Movie", None)),
        ("MOVIE_X", "THE MATRIX 4K", ("The Matrix", None)),
    ]

    def test_table(self):
        for label, disp, want in self.CASES:
            with self.subTest(label=label, disp=disp):
                self.assertEqual(parse_disc_name(label, disp), want)


class FakeTmdb(BaseHTTPRequestHandler):
    searches = {}      # (query) -> list of search results
    details = {}       # id -> detail dict
    mode = "ok"
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        FakeTmdb.seen.append((u.path, q))
        if FakeTmdb.mode == "500":
            self.send_response(500); self.end_headers(); return
        if u.path == "/search/movie":
            res = FakeTmdb.searches.get(q["query"], [])
            if "year" in q:
                res = [r for r in res if r["release_date"].startswith(q["year"])]
            body = {"results": res}
        else:
            d = FakeTmdb.details.get(int(u.path.rsplit("/", 1)[1]))
            if d is None:
                self.send_response(404); self.end_headers(); return
            body = d
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


def movie(i, title, date, rt, lang="en"):
    return {"id": i, "title": title, "original_title": title, "release_date": date, "runtime": rt,
            "original_language": lang}


class TmdbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeTmdb)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        FakeTmdb.mode, FakeTmdb.seen = "ok", []
        FakeTmdb.searches, FakeTmdb.details = {}, {}
        self.tmdb = Tmdb(Config(tmdb_api_key=KEY), self.base)

    def load(self, query, *movies):
        FakeTmdb.searches[query] = list(movies)
        for m in movies:
            FakeTmdb.details[m["id"]] = m

    def test_clear_winner(self):
        self.load("Example Film", movie(1, "Example Film", "2010-05-01", 120, "fr"))
        i = self.tmdb.identify("Example Film", 2010, 118)
        self.assertEqual((i.title, i.year, i.runtime_min, i.original_language, i.tmdb_id, i.confidence),
                         ("Example Film", 2010, 120, "fr", 1, "high"))

    def test_year_retry_without_year(self):
        self.load("Example Film", movie(1, "Example Film", "2010-05-01", 120))
        i = self.tmdb.identify("Example Film", 2011, 120)      # off-by-one year search is empty -> retry
        self.assertEqual(i.tmdb_id, 1)
        self.assertEqual(len([s for s in FakeTmdb.seen if s[0] == "/search/movie"]), 2)

    def test_ambiguous_is_low(self):
        self.load("Sample Story", movie(1, "Sample Story", "1960-01-01", 100),
                  movie(2, "Sample Story", "1990-01-01", 100))
        self.assertEqual(self.tmdb.identify("Sample Story", None, None).confidence, "low")

    def test_runtime_disambiguates_remake(self):
        self.load("Sample Story", movie(1, "Sample Story", "1960-01-01", 95),
                  movie(2, "Sample Story", "1990-01-01", 130))
        i = self.tmdb.identify("Sample Story", None, 131)
        self.assertEqual((i.tmdb_id, i.confidence), (2, "high"))

    def test_extended_edition_runtime_preference(self):
        self.load("Sample Story", movie(1, "Sample Story", "2001-01-01", 178),
                  movie(2, "Sample Story", "2001-01-01", 150))
        i = self.tmdb.identify("Sample Story", 2001, 148)
        self.assertEqual(i.tmdb_id, 2)

    def test_no_year_on_candidate_is_low(self):
        self.load("Sample Story", movie(1, "Sample Story", "", 100))
        self.assertEqual(self.tmdb.identify("Sample Story", None, 100).confidence, "low")

    def test_no_results(self):
        self.assertIsNone(self.tmdb.identify("Nothing Here", None, None))

    def test_implausible_titles_ignored(self):
        self.load("Example Film", movie(1, "Completely Different Thing", "2010-01-01", 100))
        self.assertIsNone(self.tmdb.identify("Example Film", None, None))

    def test_not_configured(self):
        t = Tmdb(Config(), self.base)
        self.assertFalse(t.configured)
        self.assertIsNone(t.identify("Example Film", 2010, 100))
        self.assertEqual(FakeTmdb.seen, [])

    def test_http_error_hides_key(self):
        FakeTmdb.mode = "500"
        with self.assertRaises(MetadataError) as cm:
            self.tmdb.identify("Example Film", None, None)
        self.assertNotIn(KEY, str(cm.exception))

    def test_connection_error_hides_key(self):
        t = Tmdb(Config(tmdb_api_key=KEY), "http://127.0.0.1:1")
        with self.assertRaises(MetadataError) as cm:
            t.identify("Example Film", None, None)
        self.assertNotIn(KEY, str(cm.exception))

    def test_joined_label_prefix_search_high(self):
        self.load("say", movie(7, "Say Anything...", "1989-04-14", 100))
        i = self.tmdb.identify("Sayanything", None, 100)
        self.assertEqual((i.title, i.year, i.confidence), ("Say Anything...", 1989, "high"))
        self.assertEqual(i.original_language, "en")
        searches = [s for s in FakeTmdb.seen if s[0] == "/search/movie"]
        details = [s for s in FakeTmdb.seen if s[0] != "/search/movie"]
        self.assertLessEqual(len(searches), 8 + 4)      # normal variants + prefix budget
        self.assertLessEqual(len([s for s in searches if len(s[1]["query"]) <= 8
                                  and s[1]["query"] != "Sayanything"]), 8)
        self.assertLessEqual(len(details), 12)

    def test_joined_label_runtime_mismatch_not_high(self):
        self.load("say", movie(7, "Say Anything...", "1989-04-14", 100))
        i = self.tmdb.identify("Sayanything", None, 160)
        self.assertTrue(i is None or i.confidence != "high")

    def test_joined_label_no_match(self):
        self.load("say", movie(7, "Say Anything...", "1989-04-14", 100))
        self.assertIsNone(self.tmdb.identify("Qwertyuiop", None, 100))

    def test_joined_label_leading_article(self):
        self.load("mat", movie(9, "The Matrixish", "1999-03-31", 120))
        i = self.tmdb.identify("Matrixish", None, 120)
        self.assertEqual((i.tmdb_id, i.confidence), (9, "high"))

    def test_joined_budget_and_short_token_skipped(self):
        self.tmdb.identify("Abcdefghijklmnopqrstuvwxyz", None, 100)
        self.assertLessEqual(len([s for s in FakeTmdb.seen if s[0] == "/search/movie"]), 4 + 8)
        FakeTmdb.seen = []
        self.assertIsNone(self.tmdb.identify("Abcdef", None, 100))
        self.assertEqual(len([s for s in FakeTmdb.seen if len(s[1]["query"]) < 6]), 0)


class JoinedKeyTests(unittest.TestCase):
    def test_key(self):
        self.assertEqual(joined_title_key("Say Anything..."), "sayanything")
        self.assertEqual(joined_title_key("  Tron: Legacy! "), "tronlegacy")
        self.assertEqual(joined_title_key(None), "")
        self.assertEqual(joined_title_key("..."), "")


class QueryVariantTests(unittest.TestCase):
    def test_trademark_and_publisher_prefix_variants(self):
        from arm_pipeline.metadata import query_variants
        v = query_variants("DC Universe Movie Batman™: The Long Halloween Deluxe Edition")
        self.assertEqual(v[0], "DC Universe Movie Batman™: The Long Halloween Deluxe Edition")   # as given first
        self.assertEqual(v[1], "DC Universe Movie Batman: The Long Halloween Deluxe Edition")          # symbols stripped
        self.assertIn("Batman: The Long Halloween Deluxe Edition", v)                                   # prefix dropped
        self.assertEqual(len(v), len(set(v)))

    def test_short_titles_are_not_reduced_to_nothing(self):
        from arm_pipeline.metadata import query_variants
        self.assertEqual(query_variants("Up"), ["Up"])
        self.assertTrue(all(len(q.split()) >= 2 for q in query_variants("The Big Lebowski")[1:]))

if __name__ == "__main__":
    unittest.main()
