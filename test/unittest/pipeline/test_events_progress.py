import json
import os
import shutil
import tempfile
import threading
import unittest

from arm_pipeline.events import EventLog
from arm_pipeline.progress import Progress, parse_prgv, prgv_percent


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)


class PrgvTests(unittest.TestCase):
    def test_single(self):
        self.assertEqual(parse_prgv(b'...PRGV:19002,19002,65536\r'), (19002, 19002, 65536))

    def test_multi_last_wins(self):
        t = 'PRGC:1,2,"x"\nPRGV:1,2,100\rPRGV:50,60,100\r\nMSG:foo\nPRGV:70,80,100\n'
        self.assertEqual(parse_prgv(t), (70, 80, 100))

    def test_garbage_empty(self):
        self.assertIsNone(parse_prgv('nothing here PRGV:x,y'))
        self.assertIsNone(parse_prgv(b''))
        self.assertIsNone(parse_prgv(b'\xff\xfe'))
        self.assertIsNone(parse_prgv(None))

    def test_percent(self):
        self.assertAlmostEqual(prgv_percent((1, 1, 200)), 0.5)
        self.assertEqual(prgv_percent((300, 1, 200)), 100.0)
        self.assertIsNone(prgv_percent((1, 1, 0)))
        self.assertIsNone(prgv_percent(None))


class ProgressTests(Base):
    def test_rate_eta(self):
        c = Clock()
        p = Progress(self.d, clock=c)
        p.update('fp', 'rip', 0, 1000)
        c.t += 10
        p.update('fp', 'rip', 100, 1000)
        r = p.read()
        self.assertAlmostEqual(r['rate_bps'], 10.0)
        self.assertAlmostEqual(r['eta_s'], 90.0)
        self.assertAlmostEqual(r['percent'], 10.0)
        self.assertEqual(r['started'], 1000.0)
        self.assertFalse(r['stale'])

    def test_no_total_no_eta_and_window(self):
        c = Clock()
        p = Progress(self.d, clock=c)
        for i in range(10):
            p.update('fp', 'x', i * 10)
            c.t += 1
        r = p.read()
        self.assertIsNone(r['eta_s'])
        self.assertLessEqual(len(r['samples']), 6)
        self.assertAlmostEqual(r['rate_bps'], 10.0)

    def test_phase_change_resets(self):
        c = Clock()
        p = Progress(self.d, clock=c)
        p.update('fp', 'a', 1, 10)
        c.t += 5
        p.update('fp', 'b', 2, 10, percent=33.0, note='n')
        r = p.read()
        self.assertEqual(r['started'], 1005.0)
        self.assertIsNone(r['rate_bps'])
        self.assertEqual(r['percent'], 33.0)

    def test_stale_and_missing_and_corrupt(self):
        c = Clock()
        p = Progress(self.d, clock=c)
        self.assertIsNone(p.read())
        p.update('fp', 'a')
        self.assertTrue(p.read(now=1000 + 121)['stale'])
        self.assertFalse(p.read(now=1000 + 100)['stale'])
        with open(p.path, 'w') as f:
            f.write('{"fp": ')
        self.assertIsNone(p.read())

    def test_clear(self):
        p = Progress(self.d)
        p.update('a', 'x')
        p.clear('b')
        self.assertIsNotNone(p.read())
        p.clear('a')
        self.assertIsNone(p.read())
        p.update('a', 'x')
        p.clear()
        self.assertIsNone(p.read())
        p.clear()  # no file: fine

    def test_never_raises(self):
        Progress('/proc/nonexistent/x').update('a', 'b', 1, 2)
        Progress('/proc/nonexistent/x').clear('a')

    def test_atomic_concurrent(self):
        p = Progress(self.d)
        p.update('fp', 'a', 0, 10)
        stop = threading.Event()
        bad = []

        def writer():
            i = 0
            while not stop.is_set():
                i += 1
                p.update('fp', 'a', i, 10 ** 9, note='x' * 2000)

        def reader():
            for _ in range(1500):
                try:
                    with open(p.path, encoding='utf-8') as f:
                        json.load(f)
                except FileNotFoundError:
                    pass
                except ValueError as e:
                    bad.append(e)

        w = threading.Thread(target=writer)
        w.start()
        reader()
        stop.set()
        w.join()
        self.assertEqual(bad, [])


class EventTests(Base):
    def test_order_since_limit_fp(self):
        e = EventLog(self.d)
        for i in range(10):
            e.emit('rip', f'e{i}', fp='a' if i % 2 else 'b', n=i)
        t = e.tail()
        self.assertEqual([x['text'] for x in t], [f'e{i}' for i in range(10)])
        self.assertEqual(t[3]['data'], {'n': 3})
        self.assertEqual([x['text'] for x in e.tail(limit=3)], ['e7', 'e8', 'e9'])
        self.assertEqual([x['text'] for x in e.tail(fp='a')], ['e1', 'e3', 'e5', 'e7', 'e9'])
        self.assertEqual([x['text'] for x in e.tail(limit=2, fp='a')], ['e7', 'e9'])
        cut = t[6]['ts']
        self.assertTrue(all(x['ts'] > cut for x in e.tail(since=cut)))
        self.assertEqual(e.tail(since=t[-1]['ts']), [])
        self.assertEqual(e.tail(limit=0), [])

    def test_levels_and_tilde(self):
        e = EventLog(self.d)
        e.emit('k', 't', level='error')
        e.emit('k', 't', level='bogus')
        self.assertEqual([x['level'] for x in e.tail()], ['error', 'info'])

    def test_corrupt_and_partial(self):
        e = EventLog(self.d)
        e.emit('k', 'one')
        with open(e.path, 'ab') as f:
            f.write(b'garbage\n{"ts": "x"}\n\xff\xfe\n[1]\n{"ts": 5, "kind": "k"')
        e.emit('k', 'two')
        # the emit after a partial line is glued to it; the garbage must not break tail
        texts = [x['text'] for x in e.tail()]
        self.assertEqual(texts[0], 'one')
        e2 = EventLog(self.d)
        with open(e2.path, 'ab') as f:
            f.write(b'\n')
        e2.emit('k', 'three')
        self.assertEqual(e2.tail()[-1]['text'], 'three')

    def test_rotation(self):
        e = EventLog(self.d, max_bytes=500, keep=3)
        for i in range(100):
            e.emit('k', f'event-{i:03d}', pad='p' * 20)
        names = sorted(os.listdir(self.d))
        self.assertEqual(names, ['events.jsonl', 'events.jsonl.1', 'events.jsonl.2', 'events.jsonl.3'])
        t = e.tail(limit=1000)
        texts = [x['text'] for x in t]
        self.assertEqual(texts, sorted(texts))
        self.assertEqual(texts[-1], 'event-099')
        self.assertGreater(len(texts), 8)
        small = e.tail(limit=5)
        self.assertEqual([x['text'] for x in small], texts[-5:])
        # spans files
        with open(e.path) as fh:
            in_main = sum(1 for _ in fh)
        self.assertGreater(len(e.tail(limit=in_main + 2)), in_main)

    def test_scrub_and_control(self):
        e = EventLog(self.d)
        # token-shaped strings are assembled at run time so no secret-looking literal sits in the source file
        bot = 'xo' + 'xb-' + '1234567890-abcdefghij'
        user = 'xo' + 'xp-' + '1234567890-zzzzzzzz'
        hook = 'https://hooks' + '.slack' + '.com/services/T/B/X'
        e.emit('slack', f'tok {bot} and {hook} '
               'http://h/x?api_key=SECRET1&a=1 token=abc X-Plex-Token=zzz9 ok\x00\x1b[31m\r\nend',
               note=user, nested={'u': 'token=q'})
        with open(e.path) as fh:
            raw = fh.read()
        for s in ('xo' + 'xb-', 'hooks' + '.slack.com', 'SECRET1', 'abc ', 'zzz9', 'xo' + 'xp-', 'token=q'):
            self.assertNotIn(s, raw)
        self.assertIn('[redacted]', raw)
        self.assertEqual(len(raw.splitlines()), 1)
        ev = e.tail()[0]
        self.assertFalse(any(ord(ch) < 32 for ch in ev['text']))
        self.assertIn('a=1', ev['text'])

    def test_truncate(self):
        e = EventLog(self.d)
        e.emit('k', 'x' * 5000)
        self.assertEqual(len(e.tail()[0]['text']), 2000)

    def test_never_raises(self):
        EventLog('/proc/nonexistent/x').emit('k', 't')
        e = EventLog(self.d)
        e.emit('k', 'x', obj=object(), bad=float('nan'))
        e.emit('k', 't', fp=None, **{'a': {1, 2}})
        os.chmod(self.d, 0o500)
        try:
            e.emit('k', 'denied')
            e.tail()
        finally:
            os.chmod(self.d, 0o700)
        self.assertEqual(EventLog('/proc/nonexistent/x').tail(), [])

    def test_threads(self):
        e = EventLog(self.d)

        def run(name):
            for i in range(200):
                e.emit('k', f'{name}-{i}', fp=name)

        ts = [threading.Thread(target=run, args=(n,)) for n in 'ab']
        [t.start() for t in ts]
        [t.join() for t in ts]
        with open(e.path, "rb") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 400)
        for ln in lines:
            json.loads(ln)
        self.assertEqual(len(e.tail(limit=1000)), 400)
        self.assertEqual(len(e.tail(limit=1000, fp='a')), 200)


if __name__ == '__main__':
    unittest.main()
