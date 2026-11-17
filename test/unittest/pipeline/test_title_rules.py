"""Drift guard: the dashboard (operator.clean_title) and the daemon (pipeline._clean_identity) must accept and reject
exactly the same titles and normalise them identically. The daemon is the real trust boundary (it builds the folder
name), the dashboard is defence in depth; if the two ever disagree, one of them is not protecting what we think."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from arm_pipeline import operator as ops  # noqa: E402
from arm_pipeline import pipeline as pl  # noqa: E402
from arm_pipeline.errors import PipelineError  # noqa: E402

YEAR = 1989
ACCEPT = {
    "Say Anything": "Say Anything",
    "Say Anything...": "Say Anything",          # trailing dots stripped, explicitly
    "  Say Anything . ": "Say Anything",
    "Amélie": "Amélie",
    "Se7en": "Se7en",
    "Tron: Legacy".replace(":", ""): "Tron Legacy",
    "The Lord of the Rings (Extended)": "The Lord of the Rings (Extended)",
    "Café": "Café",                  # NFD input is normalised to NFC
}
REJECT = [
    "", "   ", "...", " . ", ".hidden", "..hidden", "../../etc", "A/B", "A\\B", "A:B", "A*B", "A?B", 'A"B', "A<B", "A>B", "A|B",
    "tab\there", "new\nline", "nul\x00byte", "del\x7fchar", "c1\x85char",
    "Foo‮gpj.exe", "Foo⁦bar", "zero​width", "bom﻿x", "nb sp", "ideo　space", "line sep",
    "\ud800", "\udfff", "privuse", "x" * 201, "é" * 130,
]


def verdicts(title):
    try:
        ops_out = ops.clean_title(title)
    except ValueError:
        ops_out = None
    try:
        pl_out = pl._clean_identity({"title": title, "year": YEAR})[0]
    except PipelineError:
        pl_out = None
    return ops_out, pl_out


class TitleRuleDrift(unittest.TestCase):
    def test_accepted_titles_normalise_identically(self):
        for raw, want in ACCEPT.items():
            ops_out, pl_out = verdicts(raw)
            self.assertEqual(ops_out, want, f"dashboard rule on {raw!r}")
            self.assertEqual(pl_out, want, f"daemon rule on {raw!r}")

    def test_rejected_titles_are_rejected_by_both(self):
        for raw in REJECT:
            ops_out, pl_out = verdicts(raw)
            self.assertIsNone(ops_out, f"dashboard accepted {raw!r}")
            self.assertIsNone(pl_out, f"daemon accepted {raw!r}")

    def test_folder_names_never_escape_or_vanish(self):
        from arm_policy import library_match
        for raw in list(ACCEPT):
            title = verdicts(raw)[1]
            folder = library_match.canonical_folder(title, YEAR)
            self.assertTrue(folder)
            self.assertNotIn("/", folder)
            self.assertNotIn("\\", folder)
            self.assertFalse(folder.startswith("."))
            self.assertNotIn("..", folder)


if __name__ == "__main__":
    unittest.main()
