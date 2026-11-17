"""The ripper container runs Python 3.10, older than the dev machine: before 3.12 an f-string may not contain a backslash inside
{...} (and may not reuse its own quote). A deploy once crash-looped on exactly that, and tests on a newer Python passed. This is a
conservative source check that catches it anywhere in the package."""
import glob
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
FSTRING = re.compile(r"""\bf(["'])(?:\\.|(?!\1).)*\1""")      # single-line f-string literals
BRACE = re.compile(r"\{[^{}]*\}")


class Py310FStrings(unittest.TestCase):
    def test_no_backslash_inside_fstring_expressions(self):
        bad = []
        for path in glob.glob(os.path.join(ROOT, "arm_pipeline", "**", "*.py"), recursive=True) + \
                glob.glob(os.path.join(ROOT, "arm_policy", "**", "*.py"), recursive=True):
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    for m in FSTRING.finditer(line):
                        body = m.group(0)
                        for e in BRACE.findall(body):
                            if "\\" in e:
                                bad.append(f"{os.path.relpath(path, ROOT)}:{n}: {e}")
        self.assertEqual(bad, [])

    def test_the_detector_catches_the_bug(self):
        line = """x = f"a `{cli('identify', fp, '\\"T\\"')}` b" """
        hits = [e for m in FSTRING.finditer(line) for e in BRACE.findall(m.group(0)) if "\\" in e]
        self.assertTrue(hits)
