"""Crash-safe per-disc state ledger: one atomically written JSON file per fingerprint."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

STATES = ('scanning', 'awaiting_identity', 'awaiting_approval', 'ripping', 'verifying',
          'transferring', 'placing', 'done', 'skipped', 'failed', 'rejected')
ACTIVE_STATES = ('scanning', 'awaiting_identity', 'awaiting_approval', 'ripping',
                 'verifying', 'transferring', 'placing')
MIN_TITLE_SECONDS = 120


@dataclass
class Record:
    fingerprint: str
    state: str
    label: str = ''
    title: str = ''
    plan: dict = field(default_factory=dict)
    paths: dict = field(default_factory=dict)
    message: str = ''
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    history: list = field(default_factory=list)   # [(ts, state, message)]


def legacy_fingerprint(disc_label: str, titles: list) -> str:
    """The ORIGINAL id: label + sorted (duration_s, size_bytes) of every title >= 120s.

    Unstable in practice (MakeMKV's scan of extras varies between runs), kept only so records created under it
    are still recognised."""
    items = sorted((int(t.duration_s), int(t.size_bytes)) for t in titles
                   if int(t.duration_s) >= MIN_TITLE_SECONDS)
    payload = json.dumps([disc_label or '', items], separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(payload.encode('ascii')).hexdigest()[:16]


def fingerprint(disc_label: str, titles: list) -> str:
    """Stable id for a disc: the volume label plus duration and size of the LARGEST title (by size_bytes; ties:
    longest duration), considering only titles >= 120s.

    Why not every title: scanning the same physical disc twice can report a different set of extras (13 titles
    then 18 on a real disc) while the main feature is identical, so hashing all titles gave one disc two ids."""
    main = None
    for t in titles:
        d, sz = int(t.duration_s), int(t.size_bytes)
        if d >= MIN_TITLE_SECONDS and (main is None or (sz, d) > (main[1], main[0])):
            main = (d, sz)
    payload = json.dumps([disc_label or '', list(main) if main else []],
                         separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(payload.encode('ascii')).hexdigest()[:16]


class Ledger:
    def __init__(self, state_dir):
        self.root = os.path.join(os.path.expanduser(str(state_dir)), 'ledger')
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def _path(self, fp: str) -> str:
        if not fp or '/' in fp or '\\' in fp or fp.startswith('.'):
            raise ValueError(f'bad fingerprint: {fp!r}')
        return os.path.join(self.root, fp + '.json')

    @staticmethod
    def _load(path: str) -> Record:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        d['history'] = [tuple(h) for h in d.get('history', [])]
        return Record(**d)

    def get(self, fp):
        path = self._path(fp)
        try:
            return self._load(path)
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, OSError) as e:
            log.warning('ledger: corrupt record %s skipped: %s', path, e)
            return None

    def put(self, rec: Record) -> None:
        if rec.state not in STATES:
            raise ValueError(f'unknown state: {rec.state!r}')
        now = time.time()
        prev = self.get(rec.fingerprint)
        if prev is None:
            if not rec.history or rec.history[-1][1] != rec.state:
                rec.history.append((now, rec.state, rec.message))
        elif prev.state != rec.state and (not rec.history or rec.history[-1][1] != rec.state):
            rec.history.append((now, rec.state, rec.message))
        rec.updated = now
        data = json.dumps(asdict(rec), indent=2, ensure_ascii=False)
        path = self._path(rec.fingerprint)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix='.tmp-', suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            dfd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def set_state(self, fp, state, message='', **fields) -> Record:
        rec = self.get(fp) or Record(fingerprint=fp, state=state)
        for k, v in fields.items():
            if not hasattr(rec, k) or k in ('fingerprint', 'history', 'created', 'updated'):
                raise ValueError(f'cannot set field {k!r}')
            setattr(rec, k, v)
        if rec.state != state:
            rec.history.append((time.time(), state, message))
        rec.state = state
        rec.message = message
        self.put(rec)
        return rec

    def all(self) -> list:
        out = []
        for name in sorted(os.listdir(self.root)):
            if not name.endswith('.json') or name.startswith('.'):
                continue
            path = os.path.join(self.root, name)
            try:
                out.append(self._load(path))
            except (ValueError, TypeError, OSError) as e:
                log.warning('ledger: corrupt record %s skipped: %s', path, e)
        return out

    def active(self) -> list:
        return [r for r in self.all() if r.state in ACTIVE_STATES]

    def get_any(self, fp, legacy_fp=None):
        """Record under the current id, else under the legacy id (if given), else None."""
        rec = self.get(fp)
        if rec is None and legacy_fp and legacy_fp != fp:
            rec = self.get(legacy_fp)
        return rec

    def should_process(self, fp, legacy_fp: str | None = None) -> bool:
        if os.path.exists(self._path(fp)):
            return False
        if legacy_fp and legacy_fp != fp and os.path.exists(self._path(legacy_fp)):
            return False
        return True

    def retry(self, fp) -> None:
        rec = self.get(fp)
        if rec is None:
            return
        if rec.state not in ('failed', 'rejected'):
            raise ValueError(f'retry only applies to failed/rejected, not {rec.state!r}')
        os.unlink(self._path(fp))
