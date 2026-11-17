"""Look at the existing movie library (a CIFS share: touch as little as possible)."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess

from arm_policy import library_match, quality

# (path, size, mtime_ns) -> Quality. A changed file gets a new key, so stale entries are never served.
_CACHE: dict = {}


def list_folders(movies_dir: str) -> list[str]:
    """Names of immediate subdirectories (no dotdirs), sorted. One directory read, no per-file stat of contents."""
    out = []
    with os.scandir(movies_dir) as it:
        for e in it:
            if not e.name.startswith(".") and e.is_dir():
                out.append(e.name)
    return sorted(out)


def probe_quality(path: str, ffprobe: str = "ffprobe", timeout_s: int = 120) -> quality.Quality:
    """Quality of one file via ffprobe. ANY failure -> empty Quality() (the policy then asks a human). Never raises."""
    try:
        if os.path.getsize(path) == 0:
            return quality.Quality()
        r = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
                           capture_output=True, timeout=timeout_s, check=False)
        if r.returncode != 0:
            return quality.Quality()
        return quality.from_ffprobe(json.loads(r.stdout.decode("utf-8", "replace")))
    except Exception:       # noqa: BLE001 - contract: never raise (timeouts, missing binary, bad JSON, ...)
        return quality.Quality()


def _probe_cached(path: str, ffprobe: str) -> quality.Quality:
    try:
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime_ns)
    except OSError:
        return quality.Quality()
    q = _CACHE.get(key)
    if q is None:
        q = probe_quality(path, ffprobe)
        if q.res_tier >= 0:                 # don't cache failures: they may be transient (timeout, share hiccup)
            _CACHE[key] = q
    return dataclasses.replace(q, notes=list(q.notes))


def build_library(cfg, title: str, year: int | None) -> dict:
    """{folder: [(filename, Quality), ...]} for arm_policy.decision.decide().
    Phase 1 matches by folder NAME only; phase 2 probes video files in the matched folder(s). All other folders -> []."""
    folders = list_folders(cfg.movies_dir)
    library = {f: [] for f in folders}
    match = library_match.find_match(title, year, folders)
    if match.status == "ambiguous":
        wanted = [f for f, _ in match.alternatives]
    elif match.status in ("exact", "likely"):
        wanted = [match.folder]
    else:
        wanted = []
    for folder in wanted:
        d = os.path.join(cfg.movies_dir, folder)
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        # Non-video files are listed with an empty Quality so decide() still sees them (e.g. name collisions).
        videos = set(library_match.video_files(names))
        library[folder] = [(n, _probe_cached(os.path.join(d, n), cfg.ffprobe) if n in videos else quality.Quality())
                           for n in names]
    return library


def folder_file_state(movies_dir: str, folder: str) -> dict:
    """Classify a folder's files so the pipeline can spot leftovers of an interrupted run."""
    state = {"videos": [], "hidden_old": [], "parts": [], "other": []}
    names = sorted(os.listdir(os.path.join(movies_dir, folder)))
    videos = set(library_match.video_files(names))
    for n in names:
        low = n.lower()
        if low.endswith(".arm-old"):
            state["hidden_old"].append(n)
        elif low.endswith(".part"):
            state["parts"].append(n)
        elif n in videos:
            state["videos"].append(n)
        else:
            state["other"].append(n)
    return state
