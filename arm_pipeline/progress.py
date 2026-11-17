"""Live progress of the current long-running step: one atomically written JSON file."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time

log = logging.getLogger(__name__)

STALE_AFTER_S = 120
WINDOW = 6
_PRGV = re.compile(r'PRGV:(\d+),(\d+),(\d+)')


def parse_prgv(text):
    """Last 'PRGV:cur,total,max' triple in a MakeMKV robot-mode log tail, or None."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode('utf-8', 'replace')
    if not text:
        return None
    last = None
    for m in _PRGV.finditer(text):
        last = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return last


def prgv_percent(triple):
    if not triple:
        return None
    cur, _total, mx = triple
    if mx <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * cur / mx))


class Progress:
    def __init__(self, state_dir, clock=time.time):
        self.dir = os.path.expanduser(str(state_dir))
        self.path = os.path.join(self.dir, 'progress.json')
        self.clock = clock

    def _load(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                d = json.load(f)
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    def _write(self, d):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix='.tmp-progress-', suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(json.dumps(d))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def update(self, fp, phase, done_bytes=None, total_bytes=None, percent=None, note='') -> None:
        try:
            now = self.clock()
            prev = self._load()
            same = bool(prev) and prev.get('fp') == fp and prev.get('phase') == phase
            started = prev.get('started', now) if same else now
            samples = list(prev.get('samples', [])) if same else []
            rate = eta = None
            if done_bytes is not None:
                samples.append([now, done_bytes])
                samples = samples[-WINDOW:]
                if len(samples) >= 2:
                    dt = samples[-1][0] - samples[0][0]
                    db = samples[-1][1] - samples[0][1]
                    if dt > 0 and db >= 0:
                        rate = db / dt
                if rate and rate > 0 and total_bytes is not None:
                    eta = max(0.0, (total_bytes - done_bytes) / rate)
            if percent is None and done_bytes is not None and total_bytes:
                percent = max(0.0, min(100.0, 100.0 * done_bytes / total_bytes))
            self._write({'fp': fp, 'phase': phase, 'done_bytes': done_bytes,
                         'total_bytes': total_bytes, 'percent': percent, 'note': str(note)[:500],
                         'started': started, 'updated': now, 'rate_bps': rate, 'eta_s': eta,
                         'samples': samples})
        except Exception as e:  # noqa: BLE001 - must never raise
            try:
                log.warning('progress: update failed: %s', e)
            except Exception:  # noqa: BLE001
                pass

    def clear(self, fp=None) -> None:
        try:
            if fp is not None:
                cur = self._load()
                if cur is not None and cur.get('fp') != fp:
                    return
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            try:
                log.warning('progress: clear failed: %s', e)
            except Exception:  # noqa: BLE001
                pass

    def read(self, now=None):
        d = self._load()
        if d is None:
            return None
        now = self.clock() if now is None else now
        try:
            d['stale'] = (now - float(d.get('updated', 0))) > STALE_AFTER_S
        except (TypeError, ValueError):
            return None
        return d
