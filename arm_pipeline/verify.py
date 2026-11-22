"""Fast, tiered verification of a freshly ripped MKV.

A full decode of a big 4K rip takes longer than the film itself, so the checks are tiered:

  T0  scan the MakeMKV log for read errors            (seconds)
  T1  ffprobe sanity: duration, size, streams         (seconds)
  T2  demux integrity: ffmpeg -c copy to null         (minutes, I/O bound)   } run
  T3  sampled decode of a few windows across the file (minutes, CPU bound)   } concurrently
  T4  full decode, ONLY when escalation is warranted  (slow)

A hard failure in T1-T3 fails immediately. A *warning-level* anomaly (or a suspicious MakeMKV
log, or cfg.verify_full_decode) escalates to T4 before a verdict is given.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .errors import VerifyError

# ---------------------------------------------------------------- data types


@dataclass
class Expect:
    duration_s: int          # expected feature duration in seconds (from the disc scan)
    min_size_bytes: int      # lower bound for the file size
    video_streams: int = 1
    min_audio: int = 1


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0
    skipped: bool = False


@dataclass
class VerifyResult:
    ok: bool
    checks: list[CheckResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    escalated: bool = False          # True if the full decode ran
    total_seconds: float = 0.0

    def summary(self) -> str:
        """One line for Slack."""
        if not self.ok:
            fails = [c for c in self.checks if not c.ok and not c.skipped]
            bad = next((c for c in fails if c.name.startswith("T4")), fails[0] if fails else None)   # the full decode is the verdict
            msg = f"FAILED: {bad.name}: {bad.detail}" if bad else "FAILED"
            return msg
        tiers = ", ".join(f"{c.name.split()[0]} {_coarse(c.seconds)}"
                          for c in self.checks if not c.skipped)
        kind = "full verify" if self.escalated else "quick verify"
        out = f"{kind} OK in {_fmt(self.total_seconds)} ({tiers})"
        if self.warnings:
            out += "; warnings: " + "; ".join(self.warnings)
        return out


def _fmt(s: float) -> str:
    s = int(round(s))
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def _coarse(s: float) -> str:
    return f"{int(round(s / 60))}m" if s >= 60 else f"{int(round(s))}s"


# ---------------------------------------------------------------- T0: MakeMKV log

_SUSPECT = re.compile(r"read error|failed|corrupt|bad sector|uncorrectable|error", re.I)
# Lines that merely report success ("0 failed", "completed successfully").
_SUCCESS = re.compile(r"\b0 (?:failed|errors?)\b|\bno errors?\b|successfully completed|completed successfully", re.I)


def scan_makemkv_log(text: str) -> list[str]:
    """Suspicious lines from a MakeMKV robot-mode log.

    MSG:3025 ("...was therefore skipped" short-title notices) is benign and ignored.
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("MSG:3025"):
            continue
        if _SUSPECT.search(line) and not _SUCCESS.search(line):
            out.append(line)
    return out


# ---------------------------------------------------------------- subprocess helpers

def _nice10():  # runs in the child between fork and exec
    os.nice(10)


def _run(cmd: list[str], timeout: float, low_priority: bool = True) -> subprocess.CompletedProcess:
    """Run a command with stdout discarded, stderr captured, a timeout, and (by default) nice 10."""
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, timeout=timeout,
                          preexec_fn=_nice10 if low_priority else None)


def _first_lines(data: bytes, n: int = 5) -> str:
    lines = data.decode("utf-8", "replace").strip().splitlines()
    return " | ".join(lines[:n])


def _ffmpeg_check(name: str, cmd: list[str], timeout: float) -> CheckResult:
    """Run an ffmpeg command; ANY stderr text at -v error or a nonzero exit is a failure."""
    t0 = time.monotonic()
    try:
        p = _run(cmd, timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(name, False, f"timed out after {int(timeout)}s", time.monotonic() - t0)
    except OSError as e:
        return CheckResult(name, False, f"cannot run {cmd[0]}: {e}", time.monotonic() - t0)
    dt = time.monotonic() - t0
    err = _significant_stderr(p.stderr)
    if p.returncode != 0 or err.strip():
        return CheckResult(name, False, f"exit {p.returncode}: {_first_lines(err)}", dt)
    return CheckResult(name, True, "", dt)


# Output of the *null muxer* about timestamps is not a media error: older ffmpeg (4.4, Ubuntu 22.04) prints
# "[null @ ...] Application provided invalid, non monotonically increasing dts to muxer" at the start of a seeked
# window for perfectly good files. Decoder/demuxer errors ([hevc @], [h264 @], [matroska,webm @], ...) stay fatal.
_BENIGN = (b"[null @", b"non monotonically increasing dts")


def _significant_stderr(data: bytes) -> bytes:
    keep = []
    for line in data.splitlines():
        if all(token in line for token in _BENIGN):
            continue
        keep.append(line)
    return b"\n".join(keep)


# ---------------------------------------------------------------- T1: ffprobe

def probe(path: str, ffprobe: str = "ffprobe") -> dict:
    """Parsed `ffprobe -show_format -show_streams -of json`. Raises VerifyError on failure."""
    cmd = [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", path]
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise VerifyError(f"ffprobe could not run: {e}") from e
    if p.returncode != 0:
        raise VerifyError(f"ffprobe failed (exit {p.returncode}): {_first_lines(p.stderr)}")
    try:
        return json.loads(p.stdout)
    except ValueError as e:
        raise VerifyError(f"ffprobe returned unparseable output: {e}") from e


def _t1(info: dict, size: int, expect: Expect) -> tuple[list[str], list[str]]:
    """Return (hard failures, warnings) from probe data."""
    fails, warns = [], []
    streams = info.get("streams", [])
    try:
        dur = float(info.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        dur = None

    # duration: within max(3s, 0.2%) is fine; under 1% off is a warning; more is a failure
    if dur is None:
        fails.append("no duration in container")
    else:
        diff = abs(dur - expect.duration_s)
        tol = max(3.0, 0.002 * expect.duration_s)
        if diff > tol:
            msg = f"duration {dur:.0f}s vs expected {expect.duration_s}s"
            (warns if diff <= 0.01 * expect.duration_s else fails).append(msg)

    # size: at or above the floor is fine; 80-100% of it warns; below 80% fails
    if size < expect.min_size_bytes:
        pct = 100.0 * size / expect.min_size_bytes if expect.min_size_bytes else 0
        msg = f"size {size} B is {pct:.0f}% of the {expect.min_size_bytes} B minimum"
        (warns if pct >= 80 else fails).append(msg)

    nv = sum(1 for s in streams if s.get("codec_type") == "video")
    na = sum(1 for s in streams if s.get("codec_type") == "audio")
    if nv != expect.video_streams:
        fails.append(f"{nv} video streams, expected {expect.video_streams}")
    if na < expect.min_audio:
        fails.append(f"{na} audio streams, expected at least {expect.min_audio}")
    for s in streams:
        if s.get("codec_name") in (None, "", "unknown"):
            fails.append(f"stream #{s.get('index')} has unknown codec")
    return fails, warns


# ---------------------------------------------------------------- T3: sample windows

def sample_windows(duration_s: float, windows: int, seconds: int) -> list[tuple[float, float]]:
    """(start, length) windows evenly spread over the file.

    The first starts at 0; the last ends ~2s before the end. If the file is too short for
    `windows` non-overlapping windows the count is reduced (down to one).
    """
    if duration_s <= 0 or windows < 1 or seconds < 1:
        return []
    usable = duration_s - 2.0                    # keep clear of the (often ragged) last 2 seconds
    if usable <= seconds:                        # short file: one window from 0
        return [(0.0, float(max(min(seconds, usable), min(duration_s, 1.0))))]
    n = max(1, min(windows, int(usable // seconds)))
    last_start = usable - seconds
    if n == 1:
        return [(0.0, float(seconds))]
    step = last_start / (n - 1)
    return [(round(i * step, 3), float(seconds)) for i in range(n)]


# ---------------------------------------------------------------- orchestration

PREROLL_S = 30.0          # how much earlier a flagged sampled window is re-tested


def verify_rip(path: str, expect: Expect, cfg, makemkv_log_text: str = "", progress=None) -> VerifyResult:
    """Run the tiers. Never raises for a failed check; VerifyError only for a missing file."""
    say = progress or (lambda _m: None)
    if not os.path.isfile(path):
        raise VerifyError(f"file not found: {path}")
    start = time.monotonic()
    checks: list[CheckResult] = []
    warnings: list[str] = []
    notes: list[str] = []                      # shown to the owner but do NOT trigger the full decode

    def finish(ok: bool, escalated: bool = False) -> VerifyResult:
        return VerifyResult(ok, checks, warnings + notes, escalated, time.monotonic() - start)

    # T0
    t0 = time.monotonic()
    suspicious = scan_makemkv_log(makemkv_log_text) if makemkv_log_text else []
    checks.append(CheckResult("T0 makemkv log", True,
                              f"{len(suspicious)} suspicious line(s)" if suspicious else "",
                              time.monotonic() - t0))
    if suspicious:
        warnings.append(f"MakeMKV log: {len(suspicious)} suspicious line(s), e.g. {suspicious[0][:120]}")

    # T1
    say("verify T1: ffprobe")
    t1 = time.monotonic()
    try:
        info = probe(path, cfg.ffprobe)
        fails, warns = _t1(info, os.path.getsize(path), expect)
        dur = float(info.get("format", {}).get("duration") or 0)
    except VerifyError as e:
        fails, warns, dur = [str(e)], [], 0.0
    checks.append(CheckResult("T1 probe", not fails, "; ".join(fails), time.monotonic() - t1))
    warnings.extend(warns)
    if fails:
        return finish(False)

    # T2 + T3 concurrently (I/O-bound demux vs CPU-bound decode)
    say("verify T2 demux + T3 sampled decode")
    size = os.path.getsize(path)
    t2_timeout = max(600.0, size / 5e6)              # assume at least 5 MB/s
    wins = sample_windows(dur or expect.duration_s, cfg.sample_windows, cfg.sample_seconds)

    def t2() -> CheckResult:
        return _ffmpeg_check("T2 demux", [cfg.ffmpeg, "-v", "error", "-nostdin", "-i", path,
                                          "-map", "0", "-c", "copy", "-f", "null", "-"], t2_timeout)

    def one_window(w: tuple[float, float]) -> CheckResult:
        s, n = w
        return _ffmpeg_check(f"window@{s:.0f}s", [
            cfg.ffmpeg, "-v", "error", "-nostdin", "-xerror", "-threads", "0",
            "-ss", f"{s:.3f}", "-t", f"{n:.3f}", "-i", path,
            "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], 900.0)

    flagged: list[tuple[float, float]] = []

    def t3() -> CheckResult:
        t = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as ex:      # bounded parallelism
            results = list(ex.map(one_window, wins))
        flagged.extend(w for w, r in zip(wins, results) if not r.ok)
        bad = [r for r in results if not r.ok]
        detail = f"{bad[0].name}: {bad[0].detail}" if bad else f"{len(results)} windows"
        return CheckResult("T3 sampled decode", not bad, detail, time.monotonic() - t)

    with ThreadPoolExecutor(max_workers=2) as ex:
        f2, f3 = ex.submit(t2), ex.submit(t3)
        r2, r3 = f2.result(), f3.result()
    checks.extend([r2, r3])
    if not r2.ok:
        return finish(False)
    if not r3.ok:
        # A sampled window starts after a seek, where a decoder can complain about the first frames (e.g. HEVC "First slice in a
        # frame missing") even in a sound file. Real damage stays at the same place in the file whatever the seek point, a seek
        # artifact does not. So re-test each flagged window starting PREROLL_S earlier (a different keyframe, with the original span
        # now decoded mid-stream): clean => seek artifact (warning only); still bad => confirm with the full decode (T4).
        say("verify T3 retry: flagged window(s) from an earlier seek point")
        again = []
        for s0, n0 in flagged:
            a = max(0.0, s0 - PREROLL_S)
            again.append(one_window((a, n0 + (s0 - a))))
        if again and all(c.ok for c in again):
            spans = ", ".join(f"{s0:.0f}s" for s0, _ in flagged)
            checks.append(CheckResult("T3 retry", True, f"clean from an earlier seek: {len(flagged)} window(s)", 0.0))
            notes.append(f"sampled decode flagged window(s) at {spans} only at the seek start (clean when decoded from "
                            f"{PREROLL_S:.0f}s earlier): {r3.detail[:120]}")
            r3 = CheckResult("T3 sampled decode", True, "seek-start noise only", r3.seconds)
            checks[-2] = r3                                    # the retry cleared it
        else:
            warnings.append(f"sampled decode flagged {r3.detail[:160]} and it persisted from an earlier seek; "
                            "confirming with a full decode")

    # T4: escalation only
    if cfg.verify_full_decode or warnings:
        why = "configured" if cfg.verify_full_decode and not warnings else ("a sampled window was flagged" if not r3.ok else "warnings present")
        say(f"verify T4 full decode ({why})")
        timeout = max(3600.0, 4.0 * (dur or expect.duration_s) + 600.0)
        r4 = _ffmpeg_check("T4 full decode", [cfg.ffmpeg, "-v", "error", "-xerror", "-nostdin", "-i", path,
                                              "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], timeout)
        checks.append(r4)
        return finish(r4.ok, escalated=True)
    checks.append(CheckResult("T4 full decode", True, "not needed", skipped=True))
    return finish(True)
