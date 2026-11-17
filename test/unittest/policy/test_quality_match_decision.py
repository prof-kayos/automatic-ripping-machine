import unittest

from helpers import scan
from arm_policy import decision, library_match as lm, quality as q


def probe(w, h, codec="hevc", br=None, dur=7200, audio="aac", profile="", trc="", dovi=False, fmt_br=None):
    v = {"codec_type": "video", "width": w, "height": h, "codec_name": codec}
    if br is not None:
        v["bit_rate"] = str(br)
    if trc:
        v["color_transfer"] = trc
    if dovi:
        v["side_data_list"] = [{"side_data_type": "DOVI configuration record"}]
    fmt = {"duration": str(dur)}
    if fmt_br:
        fmt["bit_rate"] = str(fmt_br)
    return {"streams": [v, {"codec_type": "audio", "codec_name": audio, "profile": profile}], "format": fmt}


def disc(res="1920x1080", audio="TrueHD", minutes=120):
    d = scan(dict(idx=0, minutes=minutes, segments="1-20", size=res, audio=(("eng", audio),)))
    return d


class TestQuality(unittest.TestCase):
    def test_res_tier_scope_crop(self):
        self.assertEqual(q.res_tier(1920, 800), 2)      # 2.40:1 1080p
        self.assertEqual(q.res_tier(3840, 1600), 3)
        self.assertEqual(q.res_tier(720, 480), 0)
        self.assertEqual(q.res_tier(1280, 720), 1)
        self.assertEqual(q.res_tier(0, 0), -1)

    def test_audio_rank(self):
        self.assertEqual(q.audio_rank("truehd"), 5)
        self.assertEqual(q.audio_rank("dts", "DTS-HD MA"), 5)
        self.assertEqual(q.audio_rank("dts"), 3)
        self.assertEqual(q.audio_rank("ac3"), 2)
        self.assertEqual(q.audio_rank("aac"), 1)
        self.assertEqual(q.audio_rank("???"), 0)

    def test_ffprobe_hdr_and_dv(self):
        self.assertEqual(q.from_ffprobe(probe(3840, 2160, trc="smpte2084")).hdr, 1)
        self.assertEqual(q.from_ffprobe(probe(3840, 2160, trc="smpte2084", dovi=True)).hdr, 2)
        self.assertEqual(q.from_ffprobe(probe(1920, 1080)).hdr, 0)

    def test_raw_vs_encode_heuristic(self):
        self.assertTrue(q.from_ffprobe(probe(1920, 1080, br=25_000_000)).raw)
        self.assertFalse(q.from_ffprobe(probe(1920, 1080, br=6_000_000)).raw)
        self.assertFalse(q.from_ffprobe(probe(1920, 1080)).raw)       # unknown bitrate: not assumed raw

    def test_no_video(self):
        self.assertEqual(q.from_ffprobe({"streams": []}).res_tier, -1)

    def test_disc_uhd_marks_hdr_assumed(self):
        dq = q.from_disc_title(disc("3840x2160").titles[0])
        self.assertEqual((dq.res_tier, dq.hdr, dq.hdr_known, dq.raw), (3, 1, False, True))


class TestCompare(unittest.TestCase):
    def cmp(self, disc_q, ex_q):
        return q.compare(disc_q, ex_q).action

    def d(self, res="1920x1080", audio="TrueHD", minutes=120):
        return q.from_disc_title(disc(res, audio, minutes).titles[0])

    def test_dvd_replaced_by_bluray(self):
        self.assertEqual(self.cmp(self.d(), q.from_ffprobe(probe(720, 480, "mpeg2video", dur=7200))), q.REPLACE)

    def test_1080_encode_replaced_by_4k(self):
        self.assertEqual(self.cmp(self.d("3840x2160"), q.from_ffprobe(probe(1920, 1080, br=8_000_000))), q.REPLACE)

    def test_better_existing_skipped(self):
        self.assertEqual(self.cmp(self.d(), q.from_ffprobe(probe(3840, 2160, trc="smpte2084", br=60_000_000))), q.SKIP)

    def test_same_res_raw_disc_vs_encode_asks(self):
        self.assertEqual(self.cmp(self.d(), q.from_ffprobe(probe(1920, 1080, br=6_000_000))), q.ASK)

    def test_same_res_remux_equal_audio_skips(self):
        ex = q.from_ffprobe(probe(1920, 1080, br=30_000_000, audio="truehd"))
        self.assertEqual(self.cmp(self.d(), ex), q.SKIP)

    def test_audio_upgrade_replaces(self):
        ex = q.from_ffprobe(probe(1920, 1080, br=30_000_000, audio="ac3"))
        self.assertEqual(self.cmp(self.d(), ex), q.REPLACE)

    def test_existing_dolby_vision_vs_uhd_disc_asks(self):
        ex = q.from_ffprobe(probe(3840, 2160, trc="smpte2084", dovi=True, br=60_000_000))
        self.assertEqual(self.cmp(self.d("3840x2160"), ex), q.ASK)

    def test_longer_existing_file_asks_even_if_disc_better(self):
        ex = q.from_ffprobe(probe(720, 480, "mpeg2video", dur=9000))     # 150 min vs 120 min disc
        self.assertEqual(self.cmp(self.d(), ex), q.ASK)

    def test_unknown_existing_asks(self):
        self.assertEqual(self.cmp(self.d(), q.Quality()), q.ASK)

    def test_not_in_library(self):
        self.assertEqual(q.compare(self.d(), None).action, q.REPLACE)


class TestLibraryMatch(unittest.TestCase):
    FOLDERS = ["28 Days Later. (2002)", "A Charlie Brown Christmas (Short 1965) (1965)",
               "A Midsummer NIght's Dream (1999)", "300 (2006)", "3-10 to Yuma (2007)",
               "21 (2008)", "21 Jump Street (2012)", "Total Recall (1990)"]

    def test_messy_folders(self):
        self.assertEqual(lm.find_match("28 Days Later", 2002, self.FOLDERS).folder, "28 Days Later. (2002)")
        m = lm.find_match("A Charlie Brown Christmas", 1965, self.FOLDERS)
        self.assertEqual(m.folder, "A Charlie Brown Christmas (Short 1965) (1965)")
        self.assertEqual(lm.find_match("3:10 to Yuma", 2007, self.FOLDERS).folder, "3-10 to Yuma (2007)")

    def test_short_numeric_titles_do_not_cross_match(self):
        self.assertEqual(lm.find_match("21", 2008, self.FOLDERS).folder, "21 (2008)")
        self.assertEqual(lm.find_match("21 Jump Street", 2012, self.FOLDERS).folder, "21 Jump Street (2012)")

    def test_remake_year_is_a_different_movie(self):
        self.assertEqual(lm.find_match("Total Recall", 2012, self.FOLDERS).status, "none")

    def test_one_year_off_still_matches(self):
        self.assertIn(lm.find_match("300", 2007, self.FOLDERS).status, ("likely", "exact"))

    def test_ambiguous_when_two_equal_folders(self):
        f = ["Heat (1995)", "Heat (1995) "]
        self.assertEqual(lm.find_match("Heat", 1995, f).status, "ambiguous")

    def test_video_files_filter(self):
        self.assertEqual(lm.video_files(["a.mkv", "poster.jpg", ".hidden.mkv", "a-trailer.mkv", "x.nfo"]), ["a.mkv"])

    def test_canonical_folder_strips_bad_chars_and_trailing_dot(self):
        self.assertEqual(lm.canonical_folder('Who Framed: Roger? Rabbit.', 1988), "Who Framed Roger Rabbit (1988)")

    def test_canonical_folder_keeps_closing_ellipsis_only_with_a_year(self):
        self.assertEqual(lm.canonical_folder("Say Anything...", 1989), "Say Anything... (1989)")
        self.assertEqual(lm.canonical_folder("Say Anything...", None), "Say Anything")     # would end in dots
        self.assertEqual(lm.canonical_folder("Say Anything....", 1989), "Say Anything (1989)")
        self.assertEqual(lm.canonical_folder("...", 1989), "")
        self.assertEqual(lm.trim_title_tail(" ... "), "")
        # the library matcher treats the dotted and plain spellings as the same film
        self.assertEqual(lm.find_match("Say Anything...", 1989, ["Say Anything (1989)"]).status, "exact")
        self.assertEqual(lm.find_match("Say Anything", 1989, ["Say Anything... (1989)"]).status, "exact")


class TestDecision(unittest.TestCase):
    ID = decision.Identity(title="Heat", year=1995, runtime_min=170, original_language="en")

    def lib(self, **files):
        return {"Heat (1995)": [(n, q.from_ffprobe(p)) for n, p in files.items()]}

    def test_new_movie_ripped_new(self):
        p = decision.decide(disc(minutes=170), self.ID, {"300 (2006)": []})
        self.assertEqual((p.action, p.target_folder), (decision.RIP_NEW, "Heat (1995)"))
        self.assertEqual(p.title_index, 0)

    def test_better_disc_replaces_and_needs_approval(self):
        lib = self.lib(**{"Heat (1995).mkv": probe(720, 480, "mpeg2video", dur=10200)})
        p = decision.decide(disc(minutes=170), self.ID, lib)
        self.assertEqual((p.action, p.needs_approval, p.existing_file), (decision.RIP_REPLACE, True, "Heat (1995).mkv"))
        self.assertEqual(p.target_filename_stem, "Heat (1995)")

    def test_auto_replace_only_for_exact_folder(self):
        lib = self.lib(**{"Heat (1995).mkv": probe(720, 480, "mpeg2video", dur=10200)})
        p = decision.decide(disc(minutes=170), self.ID, lib, auto_replace=True)
        self.assertFalse(p.needs_approval)
        lib2 = {"Heat. (1996)": [("Heat.mkv", q.from_ffprobe(probe(720, 480, "mpeg2video", dur=10200)))]}
        p2 = decision.decide(disc(minutes=170), self.ID, lib2, auto_replace=True)
        self.assertEqual(p2.action, decision.RIP_REPLACE)
        self.assertTrue(p2.needs_approval)

    def test_equal_or_better_existing_skipped(self):
        lib = self.lib(**{"Heat (1995).mkv": probe(1920, 1080, br=30_000_000, audio="truehd", dur=10200)})
        self.assertEqual(decision.decide(disc(minutes=170), self.ID, lib).action, decision.SKIP)

    def test_multiple_files_asks(self):
        lib = self.lib(**{"a.mkv": probe(1920, 1080), "b.mkv": probe(3840, 2160)})
        p = decision.decide(disc(minutes=170), self.ID, lib)
        self.assertEqual(p.action, decision.ASK)

    def test_empty_folder_rips_into_it(self):
        p = decision.decide(disc(minutes=170), self.ID, {"Heat (1995)": [("poster.jpg", q.Quality())]})
        self.assertEqual((p.action, p.target_folder), (decision.RIP_NEW, "Heat (1995)"))

    def test_identity_missing_or_low_confidence(self):
        self.assertEqual(decision.decide(disc(), None, {}).action, decision.ASK_IDENTITY)
        low = decision.Identity("Heat", 1995, confidence="low")
        self.assertEqual(decision.decide(disc(), low, {}).action, decision.ASK_IDENTITY)

    def test_tv_disc_skipped_and_ambiguous_asks(self):
        tv = scan(*[dict(idx=i, minutes=42, segments=f"{i * 10}-{i * 10 + 5}") for i in range(5)])
        self.assertEqual(decision.decide(tv, self.ID, {}).action, decision.SKIP)
        amb = scan(dict(idx=0, minutes=105, segments="1-10"), dict(idx=1, minutes=100, segments="20-30"))
        self.assertEqual(decision.decide(amb, decision.Identity("Heat", 1995), {}).action, decision.ASK)

    def test_foreign_film_selection_and_audio_fallback(self):
        ident = decision.Identity("Amelie", 2001, original_language="fr")
        p = decision.decide(disc(minutes=122), ident, {})
        self.assertTrue(p.foreign)
        self.assertIn("fre", p.selection_string)
        d = scan(dict(idx=0, minutes=120, segments="1-9", audio=(("ger", "DD"),)))
        p2 = decision.decide(d, decision.Identity("Heat", 1995, original_language="en"), {})
        self.assertIn("+sel:audio,", p2.selection_string)
        self.assertTrue(any("keeping ALL audio" in w for w in p2.warnings))


if __name__ == "__main__":
    unittest.main()
