import os
import shutil
import subprocess
import tempfile
import unittest

from arm_pipeline import library
from arm_pipeline.config import Config
from arm_policy import quality


def make_mkv(path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                    "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "mpeg4", "-c:a", "aac", path], check=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        library._CACHE.clear()

    def mk(self, folder, *files, size=48):
        d = os.path.join(self.root, folder)
        os.makedirs(d, exist_ok=True)
        for f in files:
            with open(os.path.join(d, f), "wb") as fh:
                fh.write(b"\0" * size)
        return d


class ListFoldersTests(Base):
    def test_skips_dotdirs_and_files(self):
        self.mk("Zed (2000)"); self.mk("Alpha (1999)"); self.mk(".hidden"); self.mk("@eaDir")
        open(os.path.join(self.root, "stray.txt"), "w").close()
        self.assertEqual(library.list_folders(self.root), ["@eaDir", "Alpha (1999)", "Zed (2000)"])


class ProbeTests(Base):
    def test_stub_and_zero_files_give_empty(self):
        self.mk("a", "stub.mkv"); self.mk("a", "zero.mkv", size=0); self.mk("a", "zeros.mkv", size=1 << 20)
        for n in ("stub.mkv", "zero.mkv", "zeros.mkv"):
            q = library.probe_quality(os.path.join(self.root, "a", n))
            self.assertEqual(q, quality.Quality(), n)

    def test_missing_binary_and_file(self):
        self.assertEqual(library.probe_quality("/nonexistent/x.mkv"), quality.Quality())
        p = os.path.join(self.mk("a", "x.mkv"), "x.mkv")
        self.assertEqual(library.probe_quality(p, ffprobe="/nonexistent/ffprobe"), quality.Quality())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    def test_real_mkv(self):
        d = self.mk("a")
        p = os.path.join(d, "real.mkv")
        make_mkv(p)
        q = library.probe_quality(p)
        self.assertEqual((q.width, q.height, q.res_tier), (320, 240, 0))
        self.assertGreaterEqual(q.duration_s, 1)


class BuildLibraryTests(Base):
    def cfg(self):
        return Config(movies_dir=self.root)

    def test_exact_match_probes_only_videos(self):
        self.mk("Example Film (2010)", "Example Film (2010).mkv", "poster.jpg"); self.mk("Other (2001)", "o.mkv")
        calls = []
        orig = library.probe_quality
        library.probe_quality = lambda p, ff="ffprobe", t=120: calls.append(os.path.basename(p)) or quality.Quality()
        self.addCleanup(setattr, library, "probe_quality", orig)
        lib = library.build_library(self.cfg(), "Example Film", 2010)
        self.assertEqual(calls, ["Example Film (2010).mkv"])
        self.assertEqual([n for n, _ in lib["Example Film (2010)"]], ["Example Film (2010).mkv", "poster.jpg"])
        self.assertEqual(lib["Other (2001)"], [])

    def test_no_match(self):
        self.mk("Other (2001)", "o.mkv")
        self.assertEqual(library.build_library(self.cfg(), "Example Film", 2010), {"Other (2001)": []})

    def test_ambiguous_lists_all_alternatives(self):
        self.mk("Example Film (2010)", "a.mkv"); self.mk("Example Film. (2010)", "b.mkv")
        lib = library.build_library(self.cfg(), "Example Film", 2010)
        self.assertEqual({k: [n for n, _ in v] for k, v in lib.items()},
                         {"Example Film (2010)": ["a.mkv"], "Example Film. (2010)": ["b.mkv"]})

    def test_zero_filled_file_is_empty_quality(self):
        self.mk("Example Film (2010)", "Example Film (2010).mkv", size=4096)
        lib = library.build_library(self.cfg(), "Example Film", 2010)
        (name, q), = lib["Example Film (2010)"]
        self.assertEqual(q, quality.Quality())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    def test_cache_hit_and_invalidation(self):
        d = self.mk("Example Film (2010)")
        p = os.path.join(d, "Example Film (2010).mkv")
        make_mkv(p)
        calls = []
        orig = library.probe_quality
        def counting(*a, **k):
            calls.append(1)
            return orig(*a, **k)
        library.probe_quality = counting
        self.addCleanup(setattr, library, "probe_quality", orig)
        q1 = library.build_library(self.cfg(), "Example Film", 2010)["Example Film (2010)"][0][1]
        q2 = library.build_library(self.cfg(), "Example Film", 2010)["Example Film (2010)"][0][1]
        self.assertEqual(len(calls), 1)
        self.assertEqual(q1, q2)
        self.assertIsNot(q1, q2)
        with open(p, "ab") as fh:           # file changed -> re-probe
            fh.write(b"\0" * 10)
        library.build_library(self.cfg(), "Example Film", 2010)
        self.assertEqual(len(calls), 2)


class FolderStateTests(Base):
    def test_classification(self):
        self.mk("F (2000)", "F (2000).mkv", "F (2000).mkv.arm-old", "F (2000).mkv.part", "poster.jpg", "x-trailer.mkv")
        s = library.folder_file_state(self.root, "F (2000)")
        self.assertEqual(s["videos"], ["F (2000).mkv"])
        self.assertEqual(s["hidden_old"], ["F (2000).mkv.arm-old"])
        self.assertEqual(s["parts"], ["F (2000).mkv.part"])
        self.assertEqual(s["other"], ["poster.jpg", "x-trailer.mkv"])


if __name__ == "__main__":
    unittest.main()
