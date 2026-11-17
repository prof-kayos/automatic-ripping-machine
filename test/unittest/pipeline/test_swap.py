import os
import tempfile
import unittest
from dataclasses import asdict

from arm_pipeline import swap
from arm_pipeline.errors import SwapError


class SwapTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = os.path.join(self.td.name, "Ocean's Eleven, Café (2001)")
        os.mkdir(self.dir)
        self.name = "Ocean's Eleven, Café (2001).mkv"
        self.final = os.path.join(self.dir, self.name)
        self.part = self.final + '.part'
        for n in ('backdrop.jpg', 'movie.xml'):
            self.w(os.path.join(self.dir, n), 'art-' + n)

    def w(self, p, c):
        with open(p, 'w') as f:
            f.write(c)

    def r(self, p):
        with open(p) as f:
            return f.read()

    def others_ok(self):
        self.assertEqual(self.r(os.path.join(self.dir, 'backdrop.jpg')), 'art-backdrop.jpg')
        self.assertEqual(self.r(os.path.join(self.dir, 'movie.xml')), 'art-movie.xml')

    def test_place_new(self):
        self.w(self.part, 'new')
        swap.place_new(self.part, self.final)
        self.assertEqual(self.r(self.final), 'new')
        self.assertFalse(os.path.exists(self.part))
        self.others_ok()

    def test_place_new_refusals(self):
        with self.assertRaises(SwapError):
            swap.place_new(self.part, self.final)          # part missing
        self.w(self.part, 'new')
        self.w(self.final, 'old')
        with self.assertRaises(SwapError):
            swap.place_new(self.part, self.final)          # final exists
        self.assertEqual(self.r(self.final), 'old')
        with self.assertRaises(SwapError):
            swap.place_new(self.final, self.final + 'x')   # not .part

    def test_hide_restore(self):
        self.w(self.final, 'old')
        h = swap.hide_old(self.final)
        self.assertEqual(h, self.final + '.arm-old')
        self.assertFalse(os.path.exists(self.final))
        with self.assertRaises(SwapError):
            swap.hide_old(self.final)                      # missing now
        self.w(self.final, 'x')
        with self.assertRaises(SwapError):
            swap.restore_old(h)                            # original occupied
        os.unlink(self.final)
        self.assertEqual(swap.restore_old(h), self.final)
        self.assertEqual(self.r(self.final), 'old')

    def test_hide_refuses_existing_target(self):
        self.w(self.final, 'old')
        self.w(self.final + '.arm-old', 'stale')
        with self.assertRaises(SwapError):
            swap.hide_old(self.final)
        self.assertEqual(self.r(self.final + '.arm-old'), 'stale')

    def test_delete_hidden_only_arm_old(self):
        self.w(self.final, 'precious')
        for p in (self.final, os.path.join(self.dir, 'backdrop.jpg'), self.dir):
            with self.assertRaises(SwapError):
                swap.delete_hidden(p)
        self.assertEqual(self.r(self.final), 'precious')
        self.others_ok()
        h = self.final + '.arm-old'
        self.w(h, 'x')
        swap.delete_hidden(h)
        swap.delete_hidden(h)   # idempotent
        self.assertFalse(os.path.exists(h))

    def test_replace_happy_path_same_name(self):
        self.w(self.final, 'old')
        self.w(self.part, 'new')
        st = swap.replace_begin(self.final, self.part, self.final)
        self.assertEqual(swap.ReplaceState(**asdict(st)), st)
        self.assertFalse(os.path.exists(self.final))
        self.assertEqual(self.r(st.hidden_path), 'old')
        swap.replace_place(st)
        swap.replace_place(st)  # idempotent
        self.assertEqual(self.r(self.final), 'new')
        swap.replace_finish(st)
        self.assertEqual(sorted(os.listdir(self.dir)), sorted([self.name, 'backdrop.jpg', 'movie.xml']))
        self.others_ok()

    def test_replace_different_name(self):
        old = os.path.join(self.dir, 'Old.mkv')
        self.w(old, 'old')
        self.w(self.part, 'new')
        st = swap.replace_begin(old, self.part, self.final)
        swap.replace_place(st)
        swap.replace_finish(st)
        self.assertEqual(self.r(self.final), 'new')
        self.assertFalse(os.path.exists(old))
        self.others_ok()

    def test_rollback_after_begin(self):
        self.w(self.final, 'old')
        self.w(self.part, 'new')
        st = swap.replace_begin(self.final, self.part, self.final)
        msg = swap.replace_rollback(st)
        self.assertIsInstance(msg, str)
        self.assertEqual(self.r(self.final), 'old')
        self.assertEqual(self.r(self.part), 'new')
        self.assertFalse(os.path.exists(st.hidden_path))
        self.others_ok()

    def test_rollback_after_place(self):
        self.w(self.final, 'old')
        self.w(self.part, 'new')
        st = swap.replace_begin(self.final, self.part, self.final)
        swap.replace_place(st)
        swap.replace_rollback(st)
        self.assertEqual(self.r(self.final), 'old')
        self.assertEqual(self.r(self.part), 'new')
        self.others_ok()
        self.assertIn('nothing', swap.replace_rollback(st))   # idempotent

    def test_rollback_after_finish_refused(self):
        self.w(self.final, 'old')
        self.w(self.part, 'new')
        st = swap.replace_begin(self.final, self.part, self.final)
        swap.replace_place(st)
        swap.replace_finish(st)
        with self.assertRaises(SwapError):
            swap.replace_rollback(st)
        self.assertEqual(self.r(self.final), 'new')

    def test_begin_refusals(self):
        with self.assertRaises(SwapError):
            swap.replace_begin(self.final, self.part, self.final)   # nothing exists
        self.w(self.final, 'old')
        with self.assertRaises(SwapError):
            swap.replace_begin(self.final, self.part, self.final)   # no part
        self.w(self.part, 'new')
        other = os.path.join(self.dir, 'other.mkv')
        self.w(other, 'o')
        with self.assertRaises(SwapError):
            swap.replace_begin(self.final, self.part, other)        # final occupied by other
        self.assertEqual(self.r(self.final), 'old')

    def test_finish_refuses_without_new(self):
        self.w(self.final, 'old')
        self.w(self.part, 'new')
        st = swap.replace_begin(self.final, self.part, self.final)
        with self.assertRaises(SwapError):
            swap.replace_finish(st)
        self.assertEqual(self.r(st.hidden_path), 'old')

    def test_place_refuses_if_final_taken(self):
        self.w(self.final, 'old')
        self.w(self.part, 'new')
        st = swap.replace_begin(self.final, self.part, self.final)
        self.w(self.final, 'intruder')
        with self.assertRaises(SwapError):
            swap.replace_place(st)
        self.assertEqual(self.r(self.final), 'intruder')


if __name__ == '__main__':
    unittest.main()
