"""Persistent activity log: append-only JSON lines, size-rotated, safe for multiple writers/readers."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)

LEVELS = ('info', 'warning', 'error')
MAX_TEXT = 2000
_CTRL = re.compile(r'[\x00-\x1f\x7f-\x9f]')
_SECRETS = (
    re.compile(r'xox[abprs]-[A-Za-z0-9-]{10,}'),
    re.compile(r'hooks\.slack\.com/\S+'),
    re.compile(r'(?i)((?:x-plex-token|api_key|apikey|token)=)[^\s&"\'<>]+'),
)


def sanitize(text) -> str:
    s = _CTRL.sub(' ', str(text))
    for i, rx in enumerate(_SECRETS):
        s = rx.sub((lambda m: m.group(1) + '[redacted]') if i == 2 else '[redacted]', s)
    return s[:MAX_TEXT]


def _clean(v):
    if isinstance(v, str):
        return sanitize(v)
    if isinstance(v, dict):
        return {sanitize(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return sanitize(v)


class EventLog:
    def __init__(self, state_dir, max_bytes=5_000_000, keep=3):
        self.dir = os.path.expanduser(str(state_dir))
        self.path = os.path.join(self.dir, 'events.jsonl')
        self.max_bytes = max_bytes
        self.keep = max(0, int(keep))
        self._lock = threading.Lock()
        try:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
        except OSError as e:
            log.warning('events: cannot create %s: %s', self.dir, e)

    def emit(self, kind, text, fp=None, level='info', **data) -> None:
        try:
            if level not in LEVELS:
                level = 'info'
            ev = {'ts': time.time(), 'level': level, 'kind': sanitize(kind),
                  'fp': None if fp is None else sanitize(fp),
                  'text': sanitize(text), 'data': _clean(data)}
            line = (json.dumps(ev, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
            with self._lock:
                self._rotate_if_needed()
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, line)
                finally:
                    os.close(fd)
        except Exception as e:  # noqa: BLE001 - emit must never raise
            try:
                log.warning('events: emit failed: %s', e)
            except Exception:  # noqa: BLE001
                pass

    def _rotated(self, n):
        return f'{self.path}.{n}'

    def _rotate_if_needed(self):
        try:
            if os.stat(self.path).st_size <= self.max_bytes:
                return
        except FileNotFoundError:
            return
        if self.keep <= 0:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            return
        for n in range(self.keep, 0, -1):
            src = self.path if n == 1 else self._rotated(n - 1)
            try:
                os.replace(src, self._rotated(n))
            except FileNotFoundError:
                pass

    def _read_backwards(self, path):
        """Yield decoded complete-ish lines (newest first) from one file."""
        try:
            f = open(path, 'rb')
        except OSError:
            return
        with f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            buf = b''
            while pos > 0:
                step = min(65536, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                parts = buf.split(b'\n')
                buf = parts[0]
                for ln in reversed(parts[1:]):
                    yield ln
            yield buf

    def tail(self, limit=100, since=0.0, fp=None) -> list:
        out = []
        if limit <= 0:
            return out
        files = [self.path] + [self._rotated(n) for n in range(1, self.keep + 1)]
        for path in files:
            older = False
            for raw in self._read_backwards(path):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw.decode('utf-8'))
                    ts = float(ev['ts'])
                except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                    continue
                if not isinstance(ev, dict):
                    continue
                if ts <= since:
                    older = True
                    break
                if fp is not None and ev.get('fp') != fp:
                    continue
                out.append(ev)
                if len(out) >= limit:
                    break
            if len(out) >= limit or older:
                break
        out.reverse()
        return out
