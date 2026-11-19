"""Operator actions shared by the CLI and the web dashboard (no HTTP, no printing).

Every function returns {"ok": bool, "message": str, ...} and never raises for an expected refusal
(wrong state, bad fingerprint). The daemon is only ever signalled through files in cfg.state_path.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import unicodedata

from . import drive as _drive

_FP_RE = re.compile(r"^[0-9a-f]{16}$")
WORKING_STATES = ("ripping", "verifying", "transferring", "placing")


def valid_fp(fp) -> bool:
    return isinstance(fp, str) and _FP_RE.match(fp) is not None and len(fp) == 16


def _res(ok: bool, message: str, **extra) -> dict:
    d = {"ok": ok, "message": message}
    d.update(extra)
    return d


def _check(led, fp):
    """Return (record, refusal) -- refusal is a result dict or None."""
    if not valid_fp(fp):
        return None, _res(False, "that is not a disc fingerprint (16 hex characters)")
    return led.get(fp), None


def _secure_dir(path) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _state_subdir(cfg, name) -> str:
    _secure_dir(cfg.state_path)
    d = os.path.join(cfg.state_path, name)
    _secure_dir(d)
    return d


def _touch(cfg, kind, fp) -> None:
    """Create the empty command file kind/fp exclusively, never following a symlink.

    Raises ValueError if the target is a symlink or not a regular file. An existing regular file is fine
    (the action was already recorded). The opposite command file is removed so both never coexist.
    """
    d = _state_subdir(cfg, kind)
    other = os.path.join(cfg.state_path, "reject" if kind == "approve" else "approve", fp)
    path = os.path.join(d, fp)
    if os.path.islink(path):
        raise ValueError("refusing to use a symlink as a command file")
    try:
        os.unlink(other)
    except FileNotFoundError:
        pass
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError("refusing to use a non-regular file as a command file")
        return
    except OSError as e:
        raise ValueError(f"cannot create command file: {e.strerror}")
    os.close(fd)


def _signal(cfg, led, fp, kind) -> dict:
    rec, bad = _check(led, fp)
    if bad:
        return bad
    if rec is None or rec.state != "awaiting_approval":
        return _res(False, f"nothing to {kind}: that disc is not waiting for approval "
                           f"(state: {rec.state if rec else 'unknown'})")
    try:
        _touch(cfg, kind, fp)
    except ValueError as e:
        return _res(False, str(e))
    return _res(True, f"{kind} recorded")


def approve(cfg, led, fp) -> dict:
    return _signal(cfg, led, fp, "approve")


def reject(cfg, led, fp) -> dict:
    return _signal(cfg, led, fp, "reject")


_BAD_TITLE_CHARS = set('/\\')   # path separators only; ':' '?' etc. are real title characters, dropped from the FOLDER by canonical_folder
_BAD_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")
_YEAR_MSG = "year must be a number between 1888 and next year"


def _trim_tail(t: str) -> str:
    try:
        from arm_policy.library_match import trim_title_tail
        return trim_title_tail(t)
    except ImportError:
        return t.rstrip(". ")


def clean_title(title) -> str:
    """Validate and normalise an owner-supplied movie title. Pure; raises ValueError (safe message).

    Rules: text only; NFC-normalised; no control/format/private/unassigned/surrogate/line-separator
    characters (bidi overrides, zero-width characters, BOM ...); no whitespace other than the plain
    ASCII space; no / or \\ (other punctuation such as : ? * is a real title character and is replaced in the FOLDER name by canonical_folder: ':' '/' '|' become ' - ', ? * " < > are dropped); must not start with a dot; at most 200 characters and
    255 UTF-8 bytes. Leading/trailing spaces and trailing dots are then stripped explicitly, except a closing
    ellipsis glued to a word ("Say Anything..." is kept, giving the folder "Say Anything... (1989)"). The result
    must be non-empty and must yield a non-empty library folder name.
    """
    if not isinstance(title, str):
        raise ValueError("title must be text")
    t = unicodedata.normalize("NFC", title)
    for ch in t:
        if unicodedata.category(ch) in _BAD_CATEGORIES:
            raise ValueError("title must not contain control or invisible characters")
        if ch.isspace() and ch != " ":
            raise ValueError("title may only contain plain spaces as whitespace")
    bad = sorted({c for c in t if c in _BAD_TITLE_CHARS})
    if bad:
        raise ValueError("title must not contain any of: " + " ".join(bad))
    t = _trim_tail(t.strip(" "))
    if not t:
        raise ValueError("title must not be empty")
    if t.startswith("."):
        raise ValueError("title must not start with a dot")
    if len(t) > 200 or len(t.encode("utf-8")) > 255:
        raise ValueError("title is too long (max 200 characters and 255 bytes)")
    try:
        from arm_policy.library_match import canonical_folder
        folder = canonical_folder(t, None)
    except ImportError:
        folder = t if re.search(r"\w", t) else ""
    if not folder:
        raise ValueError("title does not make a usable folder name")
    return t


def _whole_number(v, what):
    """int from an int, an integral-looking float or a digit string; ValueError on anything else."""
    if isinstance(v, bool) or v is None:
        raise ValueError(what)
    try:
        if isinstance(v, float):
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError(what)
        return int(v)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(what)


def identify(cfg, led, fp, title, year, tmdb_id=None) -> dict:
    rec, bad = _check(led, fp)
    if bad:
        return bad
    if rec is None or rec.state != "awaiting_identity":
        return _res(False, "identify only applies to a disc waiting for an answer (awaiting_identity)")
    try:
        t = clean_title(title)
    except ValueError as e:
        return _res(False, str(e))
    try:
        y = _whole_number(year, _YEAR_MSG)
    except ValueError as e:
        return _res(False, str(e))
    if not 1888 <= y <= time.gmtime().tm_year + 1:
        return _res(False, _YEAR_MSG)
    data = {"title": t, "year": y}
    if tmdb_id is not None and tmdb_id != "":
        try:
            tid = _whole_number(tmdb_id, "tmdb_id must be a positive integer")
        except ValueError as e:
            return _res(False, str(e))
        if tid <= 0 or tid > 2 ** 53:
            return _res(False, "tmdb_id must be a positive integer")
        data["tmdb_id"] = tid
    tmp = None
    try:
        d = _state_subdir(cfg, "identify")
        target = os.path.join(d, fp + ".json")
        if os.path.islink(target):
            return _res(False, "refusing to overwrite a symlink in the identify directory")
        if os.path.lexists(target) and not os.path.isfile(target):
            return _res(False, "an unexpected non-file exists where the answer must be written")
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        tmp = None
    except OSError as e:
        return _res(False, f"could not record the answer: {e.strerror or 'file error'}")
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return _res(True, f"identified as {t} ({y})", title=t, year=y)


def retry(cfg, led, fp) -> dict:
    rec, bad = _check(led, fp)
    if bad:
        return bad
    if rec is None:
        return _res(False, "unknown disc")
    if rec.state not in ("failed", "rejected", "skipped"):
        return _res(False, f"retry only applies to failed, rejected or skipped discs, not {rec.state}")
    was = rec.state
    led.retry(fp)
    _remove_rip_dir(cfg, fp)            # the old partial rip would only eat scratch space the new attempt needs
    try:
        _touch(cfg, "rescan", fp)       # tells the daemon to look at the drive again even though nothing was re-inserted
    except ValueError:
        pass                            # best effort: the record is cleared either way
    return _res(True, ("skip undone" if was == "skipped" else "record cleared")
                + "; if the disc is still in the drive it is picked up again within a few seconds, otherwise on its next insertion")


def skip(cfg, led, fp, eject_fn=None, drive_mod=None) -> dict:
    rec, bad = _check(led, fp)
    if bad:
        return bad
    if rec is None or rec.state != "awaiting_identity":
        return _res(False, "skip only applies to a disc waiting for an answer (awaiting_identity)")
    led.set_state(fp, "skipped", "skipped by owner")
    dm = drive_mod or _drive
    ejected = None
    try:
        if dm.disc_present(cfg.sr_dev):
            ejected = (eject_fn or dm.eject)(cfg)
    except Exception as e:  # drive trouble must not undo the skip
        return _res(True, f"skipped; eject failed: {e}", ejected=None)
    return _res(True, "skipped" + ("; ejected" if ejected else ""), ejected=ejected)


def dismiss(cfg, led, fp) -> dict:
    rec, bad = _check(led, fp)
    if bad:
        return bad
    if rec is None or rec.state not in ("awaiting_identity", "failed"):
        return _res(False, "dismiss only applies to a stuck awaiting_identity or failed record")
    led.set_state(fp, "skipped", "dismissed by owner")
    return _res(True, "dismissed")


_FP16 = re.compile(r"^[0-9a-f]{16}$")
_IN_USE = ("scanning", "awaiting_identity", "awaiting_approval", "ripping", "verifying", "transferring", "placing")


def _rip_dir_bytes(path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _remove_rip_dir(cfg, name) -> int:
    """Delete scratch/rips/<name> (a 16-hex fingerprint folder, never a symlink); returns the bytes freed."""
    path = os.path.join(cfg.scratch_dir, "rips", name)
    if not _FP16.match(name) or os.path.islink(path) or not os.path.isdir(path):
        return 0
    size = _rip_dir_bytes(path)
    shutil.rmtree(path, ignore_errors=True)
    return size


def clean_scratch(cfg, led=None) -> dict:
    """Free scratch space: remove every rip folder no live job needs (failed/rejected/skipped/done records and orphans).
    Refuses while a disc is being worked on, because a rip in flight lives in that directory."""
    from .ledger import Ledger
    led = led or Ledger(cfg.state_path)
    recs = led.all()
    if any(r.state in ("scanning",) + WORKING_STATES for r in recs):
        return _res(False, "refusing to clean scratch: a disc is being processed")
    keep = {r.fingerprint for r in recs if r.state in _IN_USE}
    freed, n = 0, 0
    try:
        names = os.listdir(os.path.join(cfg.scratch_dir, "rips"))
    except OSError:
        names = []
    for name in names:
        if name in keep:
            continue
        b = _remove_rip_dir(cfg, name)
        if b or _FP16.match(name):
            n += 1
        freed += b
    gb = round(freed / 2**30, 1)
    return _res(True, f"removed {n} leftover rip folder(s), freed {gb} GB", removed=n, freed_gb=gb)


def eject_now(cfg, led=None, drive_mod=None) -> dict:
    from .ledger import Ledger
    led = led or Ledger(cfg.state_path)
    busy = [r.fingerprint for r in led.all() if r.state in WORKING_STATES]
    if busy:
        return _res(False, "refusing to eject: a disc is being processed")
    dm = drive_mod or _drive
    try:
        ok, status = dm.eject(cfg)
    except Exception as e:
        return _res(False, f"eject failed: {e}")
    return _res(bool(ok), f"tray: {status}" if ok else f"eject did not complete (tray: {status})", status=status)
