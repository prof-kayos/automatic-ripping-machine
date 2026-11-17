"""Copy a verified rip to the NAS: one read of the source, then one read-back of the destination."""
from __future__ import annotations

import hashlib
import os
import time

from .errors import TransferError

PROGRESS_INTERVAL = 2.0


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def copy_with_hash(src: str, dst_part: str, progress=None, chunk=8 * 1024 * 1024):
    if not dst_part.endswith('.part'):
        raise TransferError(f'destination must end with .part: {dst_part}')
    if os.path.lexists(dst_part):
        raise TransferError(f'destination already exists: {dst_part}')
    created = False
    try:
        total = os.path.getsize(src)
        h = hashlib.sha256()
        done = 0
        last = time.monotonic()
        with open(src, 'rb') as fi, open(dst_part, 'xb') as fo:
            created = True
            while True:
                buf = fi.read(chunk)
                if not buf:
                    break
                h.update(buf)
                fo.write(buf)
                done += len(buf)
                now = time.monotonic()
                if progress and now - last >= PROGRESS_INTERVAL:
                    last = now
                    progress(done, total)
            fo.flush()
            os.fsync(fo.fileno())
        if done != total:
            raise TransferError(f'source size changed during copy ({done} != {total})')
        return done, h.hexdigest()
    except BaseException as e:
        if created:
            _remove(dst_part)
        if isinstance(e, TransferError):
            raise
        if isinstance(e, Exception):
            raise TransferError(f'copy failed: {e}') from e
        raise


def verify_readback(dst_part: str, size: int, sha256_hex: str, progress=None) -> None:
    try:
        actual = os.path.getsize(dst_part)
        if actual != size:
            raise TransferError(f'size mismatch on read-back: {actual} != {size}')
        h = hashlib.sha256()
        done = 0
        last = time.monotonic()
        with open(dst_part, 'rb') as f:
            while True:
                buf = f.read(8 * 1024 * 1024)
                if not buf:
                    break
                h.update(buf)
                done += len(buf)
                now = time.monotonic()
                if progress and now - last >= PROGRESS_INTERVAL:
                    last = now
                    progress(done, size)
    except TransferError:
        raise
    except OSError as e:
        raise TransferError(f'read-back failed: {e}') from e
    if h.hexdigest() != sha256_hex:
        raise TransferError('hash mismatch on read-back')


def stage_part(src: str, final_path: str, progress=None) -> str:
    if os.path.lexists(final_path):
        raise TransferError(f'final path already exists: {final_path}')
    parent = os.path.dirname(final_path)
    if not os.path.isdir(parent):
        if not os.path.isdir(os.path.dirname(parent)):
            raise TransferError(f'parent folder and its parent missing: {parent}')
        try:
            os.mkdir(parent)
        except OSError as e:
            raise TransferError(f'cannot create folder {parent}: {e}') from e
    part = final_path + '.part'
    size, digest = copy_with_hash(src, part, progress)
    try:
        verify_readback(part, size, digest, progress)
    except TransferError:
        _remove(part)
        raise
    return part
