import unittest

from helpers import scan, hms  # noqa: F401
from arm_policy import makemkv_scan, title_select, selection


class TestParse(unittest.TestCase):
    def test_basic_fields(self):
        d = scan(dict(idx=0, minutes=110, segments="1-3,5", chapters=28, size_gb=35.5,
                      audio=(("eng", "TrueHD"), ("fre", "DD")), subs=("eng", "spa")))
        self.assertEqual(d.name, "TEST_DISC")
        t = d.titles[0]
        self.assertEqual(t.duration_s, 110 * 60)
        self.assertEqual(t.chapters, 28)
        self.assertEqual(t.segment_map, [1, 2, 3, 5])
        self.assertEqual(t.segment_count, 4)
        self.assertEqual(t.video.width, 1920)
        self.assertEqual([s.lang for s in t.streams_of("audio")], ["eng", "fre"])
        self.assertEqual(len(t.streams_of("subtitle")), 2)

    def test_quotes_commas_and_garbage_lines(self):
        txt = 'MSG:1005,0,1,"x"\nTINFO:0,2,0,"Title, with \\"quotes\\""\nTINFO:0,9,0,"0:45:30"\nnonsense\n'
        d = makemkv_scan.parse_scan(txt)
        self.assertEqual(d.titles[0].name, 'Title, with "quotes"')
        self.assertEqual(d.titles[0].duration_s, 45 * 60 + 30)

    def test_segment_map_and_duration_helpers(self):
        self.assertEqual(makemkv_scan.parse_segment_map("1-3,5,7-8"), [1, 2, 3, 5, 7, 8])
        self.assertEqual(makemkv_scan.parse_segment_map("1,1,2"), [1, 1, 2])
        self.assertEqual(makemkv_scan.parse_duration("1:50:07"), 6607)
        self.assertEqual(makemkv_scan.parse_duration("bad"), 0)

    def test_empty_scan(self):
        self.assertEqual(makemkv_scan.parse_scan("").titles, [])


class TestTitleSelect(unittest.TestCase):
    def test_single_feature_ignores_extras(self):
        d = scan(dict(idx=0, minutes=118, segments="1-20"),
                 dict(idx=1, minutes=12, segments="30"), dict(idx=2, minutes=5, segments="31"))
        s = title_select.select_main_title(d.titles)
        self.assertEqual((s.status, s.title.index, s.is_extended), ("ok", 0, False))

    def test_extended_cut_preferred(self):
        theatrical = dict(idx=0, minutes=130, segments="1-20")
        extended = dict(idx=1, minutes=148, segments="1-20,40-44")
        d = scan(theatrical, extended)
        s = title_select.select_main_title(d.titles)
        self.assertEqual(s.status, "ok")
        self.assertEqual(s.title.index, 1)
        self.assertTrue(s.is_extended)
        self.assertEqual(len(s.variants), 2)

    def test_duplicate_playlists_pick_fewest_segments_not_extended(self):
        # same duration (within 2 min): not a distinct cut
        d = scan(dict(idx=0, minutes=120, segments="1-60"),
                 dict(idx=1, minutes=120.5, segments="1-5"))
        s = title_select.select_main_title(d.titles)
        self.assertEqual(s.title.index, 1)
        self.assertFalse(s.is_extended)

    def test_repeated_segments_obfuscation_ignored(self):
        d = scan(dict(idx=0, minutes=200, segments="1-10,1-10"),   # looped 'play all' style
                 dict(idx=1, minutes=100, segments="1-10"))
        s = title_select.select_main_title(d.titles)
        self.assertEqual(s.title.index, 1)
        self.assertTrue(any("repeated" in w for w in s.warnings))

    def test_play_all_compound_title_dropped(self):
        d = scan(dict(idx=0, minutes=200, segments="1-10,11-20"),
                 dict(idx=1, minutes=100, segments="1-10"),
                 dict(idx=2, minutes=100, segments="11-20"))
        s = title_select.select_main_title(d.titles)
        self.assertEqual(s.status, "ambiguous")   # double feature: two equal features -> ask
        self.assertTrue(any("play all" in w for w in s.warnings))

    def test_double_feature_ambiguous_but_runtime_resolves(self):
        d = scan(dict(idx=0, minutes=105, segments="1-10"), dict(idx=1, minutes=98, segments="20-30"))
        self.assertEqual(title_select.select_main_title(d.titles).status, "ambiguous")
        s = title_select.select_main_title(d.titles, expected_runtime_min=98)
        self.assertEqual((s.status, s.title.index), ("ok", 1))

    def test_tv_like_disc(self):
        d = scan(*[dict(idx=i, minutes=42, segments=f"{i * 10}-{i * 10 + 5}") for i in range(5)])
        self.assertEqual(title_select.select_main_title(d.titles).status, "tv_like")

    def test_no_candidate(self):
        d = scan(dict(idx=0, minutes=30, segments="1-3"))
        self.assertEqual(title_select.select_main_title(d.titles).status, "no_candidate")
        self.assertEqual(title_select.select_main_title([]).status, "no_candidate")

    def test_runtime_is_a_hard_gate(self):
        d = scan(dict(idx=0, minutes=65, segments="1-10"))
        s = title_select.select_main_title(d.titles, expected_runtime_min=140)
        self.assertEqual((s.status, s.title), ("ambiguous", None))
        # extended cut up to +45 min over the TMDb runtime is still accepted
        d2 = scan(dict(idx=0, minutes=148, segments="1-20"))
        self.assertEqual(title_select.select_main_title(d2.titles, expected_runtime_min=120).status, "ok")


class TestSelection(unittest.TestCase):
    def test_english_movie(self):
        self.assertEqual(selection.build_selection_string("en"),
                         "-sel:all,+sel:video,+sel:(audio&eng),+sel:(subtitle&eng),=100:all")

    def test_foreign_keeps_original_and_english(self):
        s = selection.build_selection_string("fr")
        self.assertIn("+sel:(audio&(eng|fre|fra))", s)
        self.assertTrue(selection.is_foreign("ja"))
        self.assertFalse(selection.is_foreign("EN"))
        self.assertFalse(selection.is_foreign(None))

    def test_unmapped_language_keeps_all_audio(self):
        for code in ("xx", "zz", "ta"):   # unknown/unmapped must never silently drop the original audio
            if code == "ta":
                self.assertIn("tam", selection.build_selection_string(code))
            else:
                self.assertIn("+sel:audio,", selection.build_selection_string(code))
        d = scan(dict(idx=0, minutes=100, segments="1", audio=(("eng", "DD"), ("zzz", "DD"))))
        self.assertTrue(selection.needs_audio_fallback(d.titles[0], "xx"))

    def test_audio_fallback_string(self):
        self.assertIn("+sel:audio,", selection.build_selection_string("en", audio_fallback=True))

    def test_needs_audio_fallback(self):
        d = scan(dict(idx=0, minutes=100, segments="1", audio=(("fre", "DD"),)))
        t = d.titles[0]
        self.assertTrue(selection.needs_audio_fallback(t, "en"))
        self.assertFalse(selection.needs_audio_fallback(t, "fr"))
        d2 = scan(dict(idx=0, minutes=100, segments="1", audio=(("eng", "DD"),)))
        self.assertFalse(selection.needs_audio_fallback(d2.titles[0], "en"))


if __name__ == "__main__":
    unittest.main()
