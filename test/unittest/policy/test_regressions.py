"""One test per bug reproduced by the independent adversarial review (2026-11-16)."""
import unittest

from helpers import scan
from test_quality_match_decision import probe, disc
from arm_policy import decision, library_match as lm, makemkv_scan, quality as q, selection, title_select as ts


class TestSequelAndYearMatching(unittest.TestCase):
    def none(self, title, year, folders):
        self.assertEqual(lm.find_match(title, year, folders).status, "none", (title, year, folders))

    def test_sequels_never_match_original(self):
        self.none("Harry Potter and the Deathly Hallows: Part 2", 2011,
                  ["Harry Potter and the Deathly Hallows Part 1 (2010)"])
        self.none("Back to the Future Part II", 1989, ["Back to the Future Part III (1990)"])
        self.none("Kill Bill: Vol. 2", 2004, ["Kill Bill Vol. 1 (2003)"])
        self.none("Toy Story 2", None, ["Toy Story (1995)"])
        self.none("Toy Story 2", 1999, ["Toy Story"])
        self.none("Aliens", None, ["Alien (1979)"])
        self.none("Alien", 1979, ["Aliens (1986)"])
        self.none("Rocky", 1976, ["Rocky V (1990)"])

    def test_part_roman_and_digit_forms_are_equivalent(self):
        self.assertEqual(lm.find_match("Back to the Future Part II", 1989,
                                       ["Back to the Future Part 2 (1989)"]).status, "exact")
        self.assertEqual(lm.find_match("Rocky V", 1990, ["Rocky 5 (1990)"]).folder, "Rocky 5 (1990)")

    def test_no_year_never_exact(self):
        m = lm.find_match("The Thing", None, ["The Thing (1982)"])
        self.assertNotEqual(m.status, "exact")

    def test_leading_article_and_typo_tolerance(self):
        self.assertEqual(lm.find_match("The Matrix", 1999, ["Matrix (1999)"]).folder, "Matrix (1999)")
        self.assertIn(lm.find_match("A Midsummer Night's Dream", 1999,
                                    ["A Midsumer Night's Dream (1999)"]).status, ("likely", "exact"))

    def test_parenthesised_title_prefix(self):
        self.assertEqual(lm.find_match("(500) Days of Summer", 2009,
                                       ["(500) Days of Summer (2009)"]).status, "exact")
        self.assertEqual(lm.parse_folder("Movie (1998) (Extended 2003)")[1], 1998)

    def test_non_latin_titles_not_collapsed(self):
        self.assertEqual(lm.find_match("Ип Ман 2", 2010, ["Рокки 2 (2010)"]).status, "none")

    def test_malformed_input_does_not_crash(self):
        self.assertEqual(lm.find_match(None, 1995, ["Heat (1995)"]).status, "none")
        self.assertEqual(lm.find_match("Heat", "1995", ["Heat (1995)"]).status, "exact")
        self.assertEqual(lm.find_match("Heat", "garbage", ["Heat (1995)"]).status, "likely")
        self.assertEqual(lm.find_match("Heat", 1995, None).status, "none")

    def test_sample_filter_and_extensions(self):
        self.assertEqual(lm.video_files(["Sample (2020).mkv", "The Sampler.mkv"]), ["Sample (2020).mkv",
                                                                                   "The Sampler.mkv"])
        self.assertEqual(lm.video_files(["movie.sample.mkv", "a.webm", "b.vob"]), ["a.webm", "b.vob"])

    def test_canonical_folder_degenerate(self):
        self.assertEqual(lm.canonical_folder(":", 1999), "")
        self.assertEqual(lm.canonical_folder("..", 1999), "")
        self.assertEqual(lm.canonical_folder("Line\nBreak", 1999), "LineBreak (1999)")
        self.assertEqual(lm.canonical_folder(None, 1999), "")


class TestDecisionGuards(unittest.TestCase):
    ID = decision.Identity(title="Heat", year=1995, runtime_min=170, original_language="en")

    def test_year_none_asks_identity_even_with_auto_replace(self):
        p = decision.decide(disc(minutes=170), decision.Identity("The Thing", None),
                            {"The Thing (1982)": [("The Thing (1982).mkv", q.from_ffprobe(probe(720, 480, dur=6000)))]},
                            auto_replace=True)
        self.assertEqual(p.action, decision.ASK_IDENTITY)
        self.assertEqual(p.existing_file, "")

    def test_missing_quality_asks_never_replaces(self):
        lib = {"Heat (1995)": [("Heat (1995).mkv", None)]}
        p = decision.decide(disc(minutes=170), self.ID, lib, auto_replace=True)
        self.assertEqual(p.action, decision.ASK)

    def test_sidecar_files_do_not_block_or_confuse(self):
        lib = {"Heat (1995)": [("Heat (1995).nfo", q.Quality()), ("poster.jpg", q.Quality()),
                               ("Heat (1995).srt", q.Quality())]}
        p = decision.decide(disc(minutes=170), self.ID, lib)
        self.assertEqual((p.action, p.target_folder), (decision.RIP_NEW, "Heat (1995)"))

    def test_title_containing_sample_not_hidden(self):
        ident = decision.Identity("Sample", 2020, runtime_min=120)
        lib = {"Sample (2020)": [("Sample (2020).mkv", q.from_ffprobe(probe(720, 480, dur=7200)))]}
        p = decision.decide(disc(minutes=120), ident, lib)
        self.assertEqual((p.action, p.existing_file), (decision.RIP_REPLACE, "Sample (2020).mkv"))

    def test_wrong_sequel_folder_is_rip_new_not_replace(self):
        ident = decision.Identity("Toy Story 2", 1999, runtime_min=92)
        lib = {"Toy Story (1995)": [("Toy Story (1995).mkv", q.from_ffprobe(probe(720, 480, dur=4900)))]}
        p = decision.decide(disc(minutes=92), ident, lib)
        self.assertEqual((p.action, p.target_folder), (decision.RIP_NEW, "Toy Story 2 (1999)"))

    def test_unmapped_language_warns_and_keeps_all_audio(self):
        p = decision.decide(disc(minutes=120), decision.Identity("Some Film", 2001, 120, "bn"), {})
        self.assertIn("ben", p.selection_string)           # Bengali is mapped
        p2 = decision.decide(disc(minutes=120), decision.Identity("Some Film", 2001, 120, "xx"), {})
        self.assertIn("+sel:audio,", p2.selection_string)
        self.assertTrue(any("keeping ALL audio" in w for w in p2.warnings))


class TestTitlePickingRegressions(unittest.TestCase):
    def test_main_with_short_clips_not_compound_and_bonus_not_picked(self):
        d = scan(dict(idx=0, minutes=100, segments="1-10"),
                 dict(idx=1, minutes=3, segments="3"), dict(idx=2, minutes=3, segments="7"),
                 dict(idx=3, minutes=65, segments="50-60"))
        s = ts.select_main_title(d.titles)
        self.assertEqual(s.status, "ambiguous")                  # 65 min second cluster, no TMDb runtime
        s2 = ts.select_main_title(d.titles, expected_runtime_min=100)
        self.assertEqual((s2.status, s2.title.index), ("ok", 0))

    def test_main_with_repeated_segment_not_dropped_for_bonus(self):
        d = scan(dict(idx=0, minutes=120, segments="1-5,3"), dict(idx=1, minutes=65, segments="50-60"))
        s = ts.select_main_title(d.titles, expected_runtime_min=120)
        self.assertEqual((s.status, s.title.index), ("ok", 0))

    def test_runtime_gates_pick(self):
        d = scan(dict(idx=0, minutes=100, segments="1-10"), dict(idx=1, minutes=140, segments="20-30"))
        s = ts.select_main_title(d.titles, expected_runtime_min=100)
        self.assertEqual((s.status, s.title.index), ("ok", 0))
        d2 = scan(dict(idx=0, minutes=65, segments="1-10"))
        self.assertEqual(ts.select_main_title(d2.titles, expected_runtime_min=140).status, "ambiguous")

    def test_no_runtime_second_sizeable_feature_asks(self):
        d = scan(dict(idx=0, minutes=150, segments="1-10"), dict(idx=1, minutes=120, segments="20-30"))
        self.assertEqual(ts.select_main_title(d.titles).status, "ambiguous")

    def test_malformed_inputs(self):
        self.assertEqual(ts.select_main_title(None).status, "no_candidate")
        d = scan(dict(idx=0, minutes=100, segments="1-10"))
        self.assertEqual(ts.select_main_title(d.titles, expected_runtime_min="100").status, "ok")
        self.assertEqual(ts.select_main_title(d.titles, expected_runtime_min="abc").status, "ok")
        self.assertEqual(makemkv_scan.parse_scan(None).titles, [])


class TestCompareRegressions(unittest.TestCase):
    def test_worse_on_lower_tier_asks(self):
        better_res_worse_audio = q.compare(q.Quality(res_tier=3, hdr=1, audio=0, raw=True, duration_s=7200),
                                           q.Quality(res_tier=2, hdr=1, audio=5, raw=True, duration_s=7200))
        self.assertEqual(better_res_worse_audio.action, q.ASK)
        hdr_up_audio_down = q.compare(q.Quality(res_tier=2, hdr=1, audio=0, raw=True, duration_s=7200),
                                      q.Quality(res_tier=2, hdr=0, audio=5, raw=True, duration_s=7200))
        self.assertEqual(hdr_up_audio_down.action, q.ASK)

    def test_marginal_audio_only_difference_asks(self):
        v = q.compare(q.Quality(res_tier=2, audio=5, raw=True, duration_s=7200),
                      q.Quality(res_tier=2, audio=4, raw=True, duration_s=7200))
        self.assertEqual(v.action, q.ASK)

    def test_missing_or_implausible_duration_asks(self):
        base = dict(res_tier=2, audio=5, raw=True)
        self.assertEqual(q.compare(q.Quality(duration_s=7200, **base), q.Quality(res_tier=0, duration_s=0, raw=True)).action,
                         q.ASK)
        self.assertEqual(q.compare(q.Quality(duration_s=7200, **base),
                                   q.Quality(res_tier=0, duration_s=1800, raw=True)).action, q.ASK)

    def test_4k_without_hdr_tags_asks(self):
        v = q.compare(q.from_disc_title(disc("3840x2160", minutes=120).titles[0]),
                      q.from_ffprobe(probe(3840, 2160, br=60_000_000, dur=7200, audio="truehd")))
        self.assertEqual(v.action, q.ASK)

    def test_encode_vs_raw_asks_even_with_audio_difference(self):
        v = q.compare(q.Quality(res_tier=2, audio=5, raw=True, duration_s=7200),
                      q.Quality(res_tier=2, audio=1, raw=False, duration_s=7200))
        self.assertEqual(v.action, q.ASK)

    def test_ffprobe_malformed(self):
        for bad in (None, {}, {"streams": None}, {"streams": [None, 3]},
                    {"streams": [{"codec_type": "video", "width": "N/A", "height": None}]}):
            self.assertEqual(q.from_ffprobe(bad).res_tier, -1)


class TestSmallStuff(unittest.TestCase):
    def test_duration_validation(self):
        self.assertEqual(makemkv_scan.parse_duration("-1:00:00"), 0)
        self.assertEqual(makemkv_scan.parse_duration("1:90:99"), 0)
        self.assertEqual(makemkv_scan.parse_duration("1:2:3:4"), 0)
        self.assertEqual(makemkv_scan.parse_duration(None), 0)

    def test_hostile_segment_range_is_rejected(self):
        self.assertEqual(makemkv_scan.parse_segment_map("1-999999999,5"), [5])


class ExactBeatsFuzzyTests(unittest.TestCase):
    """Found by dry-running the real library: a typo'd leftover folder made an exact match 'ambiguous'."""

    def test_single_exact_match_wins_over_a_fuzzy_near_duplicate(self):
        from arm_policy import library_match
        folders = ["Harry Potter and the Sorcerer's Stone (2001)", "Harry Potter and the Sorceror's Stone (2001)",
                   "Harry Potter and the Chamber of Secrets (2002)"]
        m = library_match.find_match("Harry Potter and the Sorcerer's Stone", 2001, folders)
        self.assertEqual(m.status, "exact")
        self.assertEqual(m.folder, "Harry Potter and the Sorcerer's Stone (2001)")

    def test_two_exact_folders_are_still_ambiguous(self):
        from arm_policy import library_match
        m = library_match.find_match("Some Film", 2001, ["Some Film (2001)", "Some Film (2001) "])
        self.assertEqual(m.status, "ambiguous")              # never silently picks between two identical names
        m2 = library_match.find_match("Some Film", 2001, ["Some Film (2001)", "Some  Film (2001)"])
        self.assertEqual(m2.status, "ambiguous")

    def test_two_fuzzy_candidates_without_an_exact_one_stay_ambiguous(self):
        from arm_policy import library_match
        m = library_match.find_match("Harry Potter and the Sorcerer's Stone", 2001,
                                     ["Harry Potter and the Sorceror's Stone (2001)",
                                      "Harry Potter and the Sorcerers Stone (2001)"])
        self.assertEqual(m.status, "ambiguous")              # no exact folder: a human decides


class ColonTitleFolder(unittest.TestCase):
    def test_tmdb_colon_titles_become_dash_folders(self):
        from arm_policy import library_match as lm
        self.assertEqual(lm.canonical_folder("Mission: Impossible", 1996), "Mission - Impossible (1996)")
        self.assertEqual(lm.canonical_folder("Dogma: What? Ever*", 1999), "Dogma - What Ever (1999)")
        self.assertEqual(lm.canonical_folder("AC/DC: Live", 1991), "AC - DC - Live (1991)")


if __name__ == "__main__":
    unittest.main()
