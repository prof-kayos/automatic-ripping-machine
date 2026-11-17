import os
import tempfile
import unittest
from types import SimpleNamespace as T

from arm_pipeline.ledger import Ledger, Record, fingerprint, legacy_fingerprint, STATES


def titles(*pairs):
    return [T(duration_s=d, size_bytes=s) for d, s in pairs]


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.l = Ledger(os.path.join(self.td.name, 'state'))

    def test_dir_mode(self):
        self.assertEqual(os.stat(self.l.root).st_mode & 0o777, 0o700)

    def test_put_get_history(self):
        self.l.put(Record('abc', 'scanning', label='L'))
        self.l.set_state('abc', 'ripping', 'go', paths={'mkv': '/x'})
        self.l.set_state('abc', 'ripping', 'still')
        r = self.l.get('abc')
        self.assertEqual([h[1] for h in r.history], ['scanning', 'ripping'])
        self.assertEqual(r.paths, {'mkv': '/x'})
        self.assertEqual(r.message, 'still')
        self.assertIsNone(self.l.get('nope'))

    def test_corrupt_neighbour(self):
        self.l.put(Record('good', 'done'))
        with open(os.path.join(self.l.root, 'bad.json'), 'w') as f:
            f.write('{trunc')
        self.assertEqual([r.fingerprint for r in self.l.all()], ['good'])
        self.assertIsNone(self.l.get('bad'))
        self.assertFalse(self.l.should_process('bad'))

    def test_no_temp_leftovers(self):
        self.l.put(Record('a', 'done'))
        self.assertEqual(os.listdir(self.l.root), ['a.json'])

    def test_active(self):
        for i, s in enumerate(STATES):
            self.l.put(Record(f'f{i}', s))
        act = {r.state for r in self.l.active()}
        self.assertEqual(act, {'scanning', 'awaiting_identity', 'awaiting_approval', 'ripping',
                               'verifying', 'transferring', 'placing'})

    def test_should_process_matrix(self):
        self.assertTrue(self.l.should_process('new'))
        for s in STATES:
            self.l.put(Record('x', s))
            self.assertFalse(self.l.should_process('x'), s)

    def test_retry(self):
        self.l.put(Record('x', 'failed'))
        self.l.retry('x')
        self.assertTrue(self.l.should_process('x'))
        self.l.put(Record('y', 'rejected'))
        self.l.retry('y')
        self.assertTrue(self.l.should_process('y'))
        self.l.put(Record('z', 'done'))
        with self.assertRaises(ValueError):
            self.l.retry('z')

    def test_bad_state_and_fp(self):
        with self.assertRaises(ValueError):
            self.l.put(Record('x', 'bogus'))
        with self.assertRaises(ValueError):
            self.l.get('../evil')

    def test_survives_reopen(self):
        self.l.put(Record('x', 'awaiting_approval', plan={'a': 1}))
        r = Ledger(os.path.dirname(self.l.root)).get('x')
        self.assertEqual(r.plan, {'a': 1})


class FingerprintTests(unittest.TestCase):
    def test_stable_and_order_independent(self):
        a = fingerprint('DISC', titles((7200, 100), (3000, 50)))
        b = fingerprint('DISC', titles((3000, 50), (7200, 100)))
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)
        int(a, 16)

    def test_short_titles_ignored(self):
        self.assertEqual(fingerprint('D', titles((7200, 1))),
                         fingerprint('D', titles((7200, 1), (30, 5), (119, 9))))
        self.assertEqual(legacy_fingerprint('D', titles((7200, 1))),
                         legacy_fingerprint('D', titles((7200, 1), (30, 5), (119, 9))))
        self.assertNotEqual(legacy_fingerprint('D', titles((7200, 1))),
                            legacy_fingerprint('D', titles((7200, 1), (120, 5))))

    def test_different_discs_differ(self):
        base = fingerprint('D', titles((7200, 100)))
        self.assertNotEqual(base, fingerprint('E', titles((7200, 100))))
        self.assertNotEqual(base, fingerprint('D', titles((7201, 100))))
        self.assertNotEqual(base, fingerprint('D', titles((7200, 101))))

    def test_same_main_title_different_extras(self):
        main = (5999, 4571738112)
        scan1 = titles(main, *[(300 + i, 1000 + i) for i in range(12)])
        scan2 = titles(*[(400 + i, 2000 + i) for i in range(17)], main)
        self.assertEqual(fingerprint('DISC_LABEL', scan1), fingerprint('DISC_LABEL', scan2))
        self.assertNotEqual(legacy_fingerprint('DISC_LABEL', scan1), legacy_fingerprint('DISC_LABEL', scan2))
        self.assertNotEqual(fingerprint('DISC_LABEL', scan1), fingerprint('OTHER', scan1))
        other = titles((5999, 4571738113), (300, 1000))
        self.assertNotEqual(fingerprint('DISC_LABEL', scan1), fingerprint('DISC_LABEL', other))

    def test_largest_by_size_ties_by_duration(self):
        a = fingerprint('D', titles((5000, 10), (6000, 10), (130, 3)))
        self.assertEqual(a, fingerprint('D', titles((6000, 10))))

    def test_no_titles_stable(self):
        a = fingerprint('D', [])
        self.assertEqual(a, fingerprint('D', titles((10, 99))))
        self.assertEqual(len(a), 16)
        self.assertNotEqual(a, fingerprint('E', []))

    def test_large_short_title_ignored(self):
        self.assertEqual(fingerprint('D', titles((7200, 1), (60, 10**12))), fingerprint('D', titles((7200, 1))))


class LegacyTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.l = Ledger(os.path.join(self.td.name, 'state'))

    def test_legacy_record_recognised(self):
        ts = titles((5999, 4571738112), (300, 5))
        new, old = fingerprint('L', ts), legacy_fingerprint('L', ts)
        self.assertNotEqual(new, old)
        self.assertTrue(self.l.should_process(new, old))
        self.l.put(Record(old, 'done'))
        self.assertTrue(self.l.should_process(new))
        self.assertFalse(self.l.should_process(new, old))
        self.assertEqual(self.l.get_any(new, old).fingerprint, old)
        self.assertIsNone(self.l.get_any(new))
        self.l.put(Record(new, 'failed'))
        self.assertEqual(self.l.get_any(new, old).fingerprint, new)


if __name__ == '__main__':
    unittest.main()
