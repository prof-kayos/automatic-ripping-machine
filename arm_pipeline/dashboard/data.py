"""Read-only data layer for the dashboard (standard library only, Python 3.10+).

Cheap by design: the movie library lives on a slow network share, so it is never walked; only files named in
ledger records are stat'ed, and every share access runs in a thread with a timeout so a hung mount cannot hang
the dashboard. Nothing returned here contains tokens, API keys or URLs carrying credentials.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from collections import Counter

from .. import drive as drive_mod
from .. import metadata
from ..errors import DriveError, MetadataError
from ..ledger import Ledger

FS_TIMEOUT_S = 3.0              # every access to the share
HTTP_TIMEOUT_S = 3.0
REACH_TTL_S = 30.0
WORKING_STATES = ("scanning", "ripping", "verifying", "transferring", "placing")
STALE_LOG_S = 120.0
GIB = 1024 ** 3
_PRGV = re.compile(r"PRGV:(\d+),(\d+),(\d+)")   # PRGV:current,total,max -> percent = cur/max
_FP_OK = re.compile(r"^[0-9a-f]{16}$")
_REACH_CACHE: dict = {}
_REACH_LOCK = threading.Lock()


# --------------------------------------------------------------------------- timeout helpers

def _raw_stat(path):
    return os.stat(path)


def call_with_timeout(fn, *args, timeout=None):
    """Run fn(*args) in a daemon thread. Returns (finished, value); a hung call is abandoned, not waited for.
    An exception counts as finished with value None."""
    box = []

    def run():
        try:
            box.append(fn(*args))
        except Exception:                                    # noqa: BLE001
            box.append(None)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(FS_TIMEOUT_S if timeout is None else timeout)
    if t.is_alive() or not box:
        return False, None
    return True, box[0]


def _exists(path):
    try:
        _raw_stat(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


def fs_exists(path, timeout=None):
    """True / False, or None when it could not be determined (timeout or I/O error)."""
    if not path:
        return False
    ok, val = call_with_timeout(_exists, path, timeout=timeout)
    return val if ok else None


def fs_exists_many(paths, timeout=None) -> dict:
    """Stat several paths concurrently under ONE shared deadline: {path: True|False|None}."""
    deadline = time.monotonic() + (FS_TIMEOUT_S if timeout is None else timeout)
    out, threads = {}, []
    for p in paths:
        slot = {}
        out[p] = slot

        def run(p=p, slot=slot):
            slot["v"] = _exists(p)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return {p: (slot.get("v") if "v" in slot else None) for p, slot in out.items()}


# --------------------------------------------------------------------------- small helpers

def _ledger_records(cfg) -> list:
    try:
        return Ledger(cfg.state_path).all()
    except Exception:                                        # noqa: BLE001
        return []


def _lazy_progress(cfg):
    try:
        from ..progress import Progress
        return Progress(cfg.state_path)
    except Exception:                                        # noqa: BLE001 - ImportError or any init problem
        return None


def _lazy_eventlog(cfg):
    try:
        from ..events import EventLog
        return EventLog(cfg.state_path)
    except Exception:                                        # noqa: BLE001
        return None


def _version() -> str:
    try:
        import arm_pipeline
        v = getattr(arm_pipeline, "__version__", None)
        if v:
            return str(v)
    except Exception:                                        # noqa: BLE001
        pass
    return "dev"


def _hist_list(rec) -> list:
    return [list(h) for h in (rec.history or [])]


# --------------------------------------------------------------------------- status pieces

def _service_state():
    try:
        r = subprocess.run(["systemctl", "is-active", "arm-pipeline"], capture_output=True, text=True, timeout=3)
    except (FileNotFoundError, PermissionError):
        return None, "unavailable"
    except subprocess.SubprocessError:
        return None, "unknown"
    state = (r.stdout or "").strip() or "unknown"
    return state == "active", state


def _tray(cfg) -> str:
    def f():
        try:
            return drive_mod.tray_status(cfg.sr_dev)
        except DriveError:
            return "UNKNOWN"

    ok, val = call_with_timeout(f, timeout=3)
    val = val if ok else "UNKNOWN"
    return val if val in ("NO_DISC", "TRAY_OPEN", "NOT_READY", "DISC_OK") else "UNKNOWN"


def _scratch(cfg) -> dict:
    out = {"free_gb": None, "total_gb": None, "used_pct": None, "rips_gb": 0.0}
    try:
        u = shutil.disk_usage(cfg.scratch_dir)
        out.update(free_gb=round(u.free / GIB, 1), total_gb=round(u.total / GIB, 1),
                   used_pct=round(100.0 * u.used / u.total, 1) if u.total else None)
    except OSError:
        pass
    total = 0
    root = os.path.join(cfg.scratch_dir, "rips")
    try:
        for d in os.scandir(root):
            if d.is_dir(follow_symlinks=False):
                for f in os.scandir(d.path):
                    try:
                        if f.is_file(follow_symlinks=False):
                            total += f.stat().st_size
                    except OSError:
                        pass
    except OSError:
        pass
    out["rips_gb"] = round(total / GIB, 2)
    return out


def _http_get(url, timeout=HTTP_TIMEOUT_S):
    """Unauthenticated GET; True if the server answered at all (status < 500). Never raises, never sends tokens."""
    import urllib.error
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:                                        # noqa: BLE001
        return False


def _reachable(name, base, path):
    """Cached (30 s) reachability; None if not configured."""
    if not base:
        return None
    key = (name, base)
    now = time.monotonic()
    with _REACH_LOCK:
        hit = _REACH_CACHE.get(key)
        if hit and now - hit[0] < REACH_TTL_S:
            return hit[1]
    val = bool(_http_get(base.rstrip("/") + path, HTTP_TIMEOUT_S))
    with _REACH_LOCK:
        _REACH_CACHE[key] = (time.monotonic(), val)
    return val


def _servers(cfg) -> dict:
    res = {}
    probes = {"plex": (getattr(cfg, "plex_url", ""), "/identity"),
              "jellyfin": (getattr(cfg, "jellyfin_url", ""), "/System/Info/Public")}

    def probe(name):
        base, path = probes[name]
        res[name] = _reachable(name, base, path)

    threads = [threading.Thread(target=probe, args=(n,), daemon=True) for n in probes]
    for t in threads:
        t.start()
    for t in threads:
        t.join(HTTP_TIMEOUT_S + 2)
    out = {}
    for name, (base, _p) in probes.items():
        out[name] = {"configured": bool(base), "reachable": res.get(name) if base else None}
    out["tmdb"] = {"configured": bool(getattr(cfg, "tmdb_api_key", ""))}
    out["slack"] = {"webhook": bool(getattr(cfg, "slack_url", "")),
                    "bot_approvals": bool(getattr(cfg, "slack_bot_token", "")
                                          and getattr(cfg, "slack_channel_id", ""))}
    return out


def _round(v, n=2):
    return None if v is None else round(v, n)


def _fallback_progress(cfg, fp) -> dict | None:
    """No Progress file: parse the newest PRGV line of rip.log and size the mkv in the rip directory."""
    outdir = os.path.join(cfg.scratch_dir, "rips", fp)
    log = os.path.join(outdir, "rip.log")
    try:
        mtime = os.stat(log).st_mtime
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    pct = None
    last = None
    for m in _PRGV.finditer(tail):
        last = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if last is not None:
        try:
            from ..progress import prgv_percent
            pct = prgv_percent(last)
        except ImportError:
            pct = (max(0.0, min(100.0, 100.0 * last[0] / last[2])) if last[2] > 0 else None)
        if pct is not None:
            pct = round(pct, 1)
    done = 0
    try:
        for f in os.scandir(outdir):
            if f.name.lower().endswith(".mkv"):
                done += f.stat().st_size
    except OSError:
        pass
    return {"percent": pct, "done_bytes": done, "mtime": mtime}


def _title_size_gb(cfg, fp, idx):
    """Size of the selected title from the saved scan, in GiB; None if unknown."""
    if idx is None or not _FP_OK.match(fp or ""):
        return None
    try:
        from arm_policy import makemkv_scan
        with open(os.path.join(cfg.state_path, "scans", fp + ".txt"), encoding="utf-8", errors="replace") as f:
            disc = makemkv_scan.parse_scan(f.read())
        for t in disc.titles:
            if t.index == idx and t.size_bytes:
                return _round(t.size_bytes / GIB)
    except Exception:                                        # noqa: BLE001
        pass
    return None


def _current(cfg, records, now) -> dict | None:
    working = [r for r in records if r.state in WORKING_STATES]
    if not working:
        return None
    rec = max(working, key=lambda r: r.updated)
    base = {"fp": rec.fingerprint, "title": rec.title or rec.label or rec.fingerprint, "state": rec.state,
            "phase": rec.state, "percent": None, "eta_s": None, "rate_mbps": None,
            "done_gb": None, "total_gb": None, "stale": False}
    prog = None
    p = _lazy_progress(cfg)
    if p is not None:
        try:
            prog = p.read(now=now)
        except Exception:                                    # noqa: BLE001
            prog = None
    if isinstance(prog, dict) and prog.get("fp") == rec.fingerprint:
        done, total = prog.get("done_bytes"), prog.get("total_bytes")
        rate = prog.get("rate_bps")
        base.update(phase=prog.get("phase") or rec.state, percent=prog.get("percent"), eta_s=prog.get("eta_s"),
                    rate_mbps=_round(rate / 1e6) if isinstance(rate, (int, float)) else None,
                    done_gb=_round(done / GIB) if isinstance(done, (int, float)) else None,
                    total_gb=_round(total / GIB) if isinstance(total, (int, float)) else None,
                    stale=bool(prog.get("stale")))
        return base
    fb = _fallback_progress(cfg, rec.fingerprint)
    if fb:
        base.update(percent=fb["percent"], done_gb=_round(fb["done_bytes"] / GIB) if fb["done_bytes"] else None,
                    stale=(now - fb["mtime"]) > STALE_LOG_S)
        plan = rec.plan if isinstance(rec.plan, dict) else {}
        base["total_gb"] = _title_size_gb(cfg, rec.fingerprint, plan.get("title_index"))
    elif rec.state in ("ripping",):
        base["stale"] = (now - rec.updated) > STALE_LOG_S
    return base


def collect_status(cfg, now=None) -> dict:
    now = time.time() if now is None else float(now)
    active, state = _service_state()
    tray = _tray(cfg)
    canary = os.path.join(cfg.movies_dir, cfg.canary_name)
    share_ok = fs_exists(canary) is True
    records = _ledger_records(cfg)
    try:
        current = _current(cfg, records, now)
    except Exception:                                        # noqa: BLE001
        current = None
    return {
        "now": now,
        "service": {"active": active, "state": state},
        "drive": {"tray": tray, "disc_present": tray == "DISC_OK"},
        "share": {"ok": share_ok, "checked": now},
        "scratch": _scratch(cfg),
        "servers": _servers(cfg),
        "current": current,
        "counts": dict(Counter(r.state for r in records)),
        "version": _version(),
    }


def collect_issues(cfg, status=None, jobs=None):
    """Delegates to issues.collect_issues (imported lazily: issues imports this module)."""
    from . import issues
    return issues.collect_issues(cfg, status=status, jobs=jobs)


# --------------------------------------------------------------------------- jobs

def _job_row(rec, now) -> dict:
    plan = rec.plan if isinstance(rec.plan, dict) else {}
    return {
        "fp": rec.fingerprint, "label": rec.label or "", "title": rec.title or "", "state": rec.state,
        "message": (rec.message or "")[:300], "created": rec.created, "updated": rec.updated,
        "age_s": max(0.0, now - (rec.updated or now)),
        "identity": plan.get("identity"), "action": plan.get("action"),
        "target_folder": plan.get("target_folder"), "existing": plan.get("existing"),
        "needs_approval": bool(plan.get("needs_approval")),
        "warnings": list(plan.get("warnings") or []),
    }


def list_jobs(cfg, limit=200) -> list:
    now = time.time()
    recs = sorted(_ledger_records(cfg), key=lambda r: r.updated, reverse=True)
    return [_job_row(r, now) for r in recs[:max(0, int(limit))]]


def _scan_summary(cfg, fp, selected) -> dict | None:
    if not _FP_OK.match(fp or ""):
        return None
    try:
        from arm_policy import makemkv_scan
        with open(os.path.join(cfg.state_path, "scans", fp + ".txt"), encoding="utf-8", errors="replace") as f:
            disc = makemkv_scan.parse_scan(f.read())
        titles = []
        for t in disc.titles:
            titles.append({
                "index": t.index, "duration_s": t.duration_s, "size_gb": round(t.size_bytes / GIB, 2),
                "chapters": t.chapters, "is_selected": selected is not None and t.index == selected,
                "video": (t.video.video_size or t.video.codec) if t.video else "",
                "audio_langs": sorted({s.lang for s in t.streams_of("audio") if s.lang}),
                "sub_langs": sorted({s.lang for s in t.streams_of("subtitle") if s.lang}),
            })
        return {"disc_name": disc.name, "label": disc.attrs.get(2, disc.name) if hasattr(disc, "attrs") else "",
                "titles": titles}
    except Exception:                                        # noqa: BLE001
        return None


def job_detail(cfg, fp):
    try:
        rec = Ledger(cfg.state_path).get(fp)
    except Exception:                                        # noqa: BLE001
        return None
    if rec is None:
        return None
    plan = rec.plan if isinstance(rec.plan, dict) else {}
    paths = rec.paths if isinstance(rec.paths, dict) else {}
    safe = {k: paths[k] for k in ("outdir", "final", "part", "ripped") if k in paths}
    d = {"fp": rec.fingerprint, "fingerprint": rec.fingerprint, "state": rec.state, "label": rec.label,
         "title": rec.title, "plan": plan, "paths": safe, "message": rec.message, "created": rec.created,
         "updated": rec.updated, "history": _hist_list(rec), "reasons": list(plan.get("reasons") or [])}
    d["scan"] = _scan_summary(cfg, rec.fingerprint, plan.get("title_index"))
    return d


# --------------------------------------------------------------------------- events

def tail_events(cfg, since=0.0, limit=100, fp=None) -> list:
    since = float(since or 0.0)
    limit = max(0, int(limit))
    log = _lazy_eventlog(cfg)
    if log is not None and os.path.exists(os.path.join(cfg.state_path, "events.jsonl")):
        try:
            return list(log.tail(limit, since, fp))
        except TypeError:
            try:
                return list(log.tail(limit=limit, since=since, fp=fp))
            except Exception:                                # noqa: BLE001
                return []
        except Exception:                                    # noqa: BLE001
            return []
    out = []
    for rec in _ledger_records(cfg):
        if fp and rec.fingerprint != fp:
            continue
        name = rec.title or rec.label or rec.fingerprint
        for h in rec.history or []:
            try:
                ts, st, msg = float(h[0]), str(h[1]), str(h[2]) if len(h) > 2 else ""
            except (TypeError, ValueError, IndexError):
                continue
            if ts <= since:
                continue
            level = "error" if st == "failed" else "warn" if st in ("rejected", "awaiting_identity") else "info"
            out.append({"ts": ts, "level": level, "kind": "state", "fp": rec.fingerprint,
                        "text": f"{name}: {st}" + (f" - {msg}" if msg else ""), "data": {"state": st}})
    out.sort(key=lambda e: e["ts"])
    return out[-limit:] if limit else []


# --------------------------------------------------------------------------- TMDb

def tmdb_search(cfg, q, year=None) -> list:
    try:
        client = metadata.Tmdb(cfg)
        q = str(q or "").strip()[:100]
        if not client.configured or not q:
            return []
        params = {"query": q}
        if year:
            params["year"] = int(year)
        results = [r for r in ((client._get("/search/movie", params) or {}).get("results") or [])
                   if isinstance(r, dict) and r.get("id") is not None][:8]
        out = []
        for i, r in enumerate(results):
            runtime = None
            if i < 5:
                det = client._get(f"/movie/{int(r['id'])}", {}) or {}
                try:
                    runtime = int(det.get("runtime") or 0) or None
                except (TypeError, ValueError):
                    runtime = None
            m = re.match(r"^(\d{4})", str(r.get("release_date") or ""))
            out.append({"tmdb_id": r["id"], "title": str(r.get("title") or r.get("original_title") or ""),
                        "year": int(m.group(1)) if m else None, "runtime": runtime,
                        "original_language": r.get("original_language"),
                        "overview": str(r.get("overview") or "")[:160]})
        return out
    except MetadataError:
        return []
    except Exception:                                        # noqa: BLE001
        return []
