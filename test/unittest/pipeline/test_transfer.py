import hashlib
import os
import tempfile
import unittest
from unittest import mock

from arm_pipeline import transfer
from arm_pipeline.errors import TransferError


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.d = self.td.name
        self.data = os.urandom(300_000)
        self.src = os.path.join(self.d, 'src.mkv')
        with open(self.src, 'wb') as f:
            f.write(self.data)

    def test_round_trip(self):
        dst = os.path.join(self.d, 'out.mkv.part')
        size, h = transfer.copy_with_hash(self.src, dst, chunk=4096)
        self.assertEqual(size, len(self.data))
        self.assertEqual(h, hashlib.sha256(self.data).hexdigest())
        with open(dst, 'rb') as f:
            self.assertEqual(f.read(), self.data)
        transfer.verify_readback(dst, size, h)

    def test_mismatch_detected(self):
        dst = os.path.join(self.d, 'o.part')
        size, h = transfer.copy_with_hash(self.src, dst)
        with open(dst, 'r+b') as f:
            f.seek(1000)
            b = f.read(1)
            f.seek(1000)
            f.write(bytes([b[0] ^ 1]))
        with self.assertRaisesRegex(TransferError, 'hash'):
            transfer.verify_readback(dst, size, h)
        with self.assertRaisesRegex(TransferError, 'size'):
            transfer.verify_readback(dst, size + 1, h)

    def test_refusals(self):
        with self.assertRaises(TransferError):
            transfer.copy_with_hash(self.src, os.path.join(self.d, 'x.mkv'))
        dst = os.path.join(self.d, 'x.part')
        open(dst, 'w').write('keep')
        with self.assertRaises(TransferError):
            transfer.copy_with_hash(self.src, dst)
        self.assertEqual(open(dst).read(), 'keep')

    def test_cleanup_on_failure(self):
        dst = os.path.join(self.d, 'f.part')
        with mock.patch('arm_pipeline.transfer.os.fsync', side_effect=OSError('boom')):
            with self.assertRaises(TransferError):
                transfer.copy_with_hash(self.src, dst, chunk=1000)
        self.assertFalse(os.path.exists(dst))

    def test_cleanup_on_midcopy_failure(self):
        dst = os.path.join(self.d, 'm.part')

        def bad(done, total):
            raise RuntimeError('mid')
        with mock.patch('arm_pipeline.transfer.PROGRESS_INTERVAL', 0):
            with self.assertRaises(TransferError):
                transfer.copy_with_hash(self.src, dst, progress=bad, chunk=1000)
        self.assertFalse(os.path.exists(dst))

    def test_missing_source(self):
        dst = os.path.join(self.d, 'n.part')
        with self.assertRaises(TransferError):
            transfer.copy_with_hash(os.path.join(self.d, 'nope'), dst)
        self.assertFalse(os.path.exists(dst))

    def test_progress_bounded(self):
        calls = []
        dst = os.path.join(self.d, 'p.part')
        transfer.copy_with_hash(self.src, dst, progress=lambda a, b: calls.append((a, b)), chunk=100)
        self.assertLessEqual(len(calls), 1)   # 3000 chunks in far under 2s
        calls.clear()
        dst2 = os.path.join(self.d, 'p2.part')
        with mock.patch('arm_pipeline.transfer.PROGRESS_INTERVAL', 0):
            transfer.copy_with_hash(self.src, dst2, progress=lambda a, b: calls.append((a, b)), chunk=100_000)
        self.assertEqual(calls[-1][1], len(self.data))

    def test_stage_part(self):
        movies = os.path.join(self.d, 'movies')
        os.mkdir(movies)
        final = os.path.join(movies, "Movie (2000)", "Movie (2000).mkv")
        part = transfer.stage_part(self.src, final)
        self.assertEqual(part, final + '.part')
        self.assertTrue(os.path.isfile(part))
        self.assertFalse(os.path.exists(final))

    def test_stage_refuses(self):
        final = os.path.join(self.d, 'there.mkv')
        open(final, 'w').close()
        with self.assertRaises(TransferError):
            transfer.stage_part(self.src, final)
        with self.assertRaises(TransferError):
            transfer.stage_part(self.src, os.path.join(self.d, 'no', 'deep', 'f.mkv'))

    def test_stage_verify_failure_removes_part(self):
        final = os.path.join(self.d, 'v.mkv')
        with mock.patch('arm_pipeline.transfer.verify_readback', side_effect=TransferError('bad')):
            with self.assertRaises(TransferError):
                transfer.stage_part(self.src, final)
        self.assertFalse(os.path.exists(final + '.part'))


if __name__ == '__main__':
    unittest.main()
