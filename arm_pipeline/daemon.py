"""Poll loop: wake on a newly inserted disc, resume approvals/identity answers, guard the share.

Disc detection is polling (udev events do not reach an unprivileged LXC): the tray-status ioctl is cheap and does
not spin the drive up. A disc is processed once when it appears (or at start-up if one is already inserted); a
disc left in the drive for a human answer is not re-scanned every cycle.
"""
from __future__ import annotations

import fcntl
import logging
import os
import shutil
import signal
import time

from . import drive as drive_mod
from . import swap
from .errors import DriveError
from .pipeline import Pipeline

log = logging.getLogger("arm.daemon")
TERMINAL = ("done", "skipped", "failed", "rejected")
RIPPING_STATES = ("scanning", "ripping", "verifying", "transferring", "placing")


def recover_interrupted(pipe: Pipeline) -> int:
    """A restart mid-job leaves records in a working state with nothing running. Never silently re-rip. If the crash
    happened inside a replace, the old movie may be sitting hidden as `.arm-old` with nothing in its place: put it
    back (single file rename) FIRST, then report. Staged `.part` files are kept for a human."""
    n = 0
    for rec in pipe.ledger.all():
        if rec.state not in RIPPING_STATES:
            continue
        note = ""
        st = rec.paths.get("replace")
        if st:
            try:
                note = " Replace rollback: " + swap.replace_rollback(swap.ReplaceState(**st))
            except Exception as e:                                   # noqa: BLE001
                note = f" REPLACE ROLLBACK FAILED ({e}): check the movie folder by hand NOW."
        pipe.ledger.set_state(rec.fingerprint, "failed", f"interrupted by a restart while {rec.state}")
        pipe.slack.post(f":warning: ARM was restarted while '{rec.title or rec.fingerprint}' was {rec.state}; the job "
                        f"is marked failed (never auto-resumed, staged files kept).{note} Re-insert the disc and run "
                        f"`python3 -m arm_pipeline retry {rec.fingerprint}` to try again.", needs_action=True)
        n += 1
    return n


def cleanup_scratch(pipe: Pipeline, max_age_s: int = 86400) -> int:
    """Remove rip directories that no active/failed job needs (finished, skipped, rejected, or orphaned)."""
    root = os.path.join(pipe.cfg.scratch_dir, "rips")
    if not os.path.isdir(root):
        return 0
    keep = {r.fingerprint for r in pipe.ledger.all() if r.state not in ("done", "skipped", "rejected")}
    n = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if name in keep or time.time() - os.path.getmtime(path) < max_age_s:
            continue
        shutil.rmtree(path, ignore_errors=True)
        n += 1
    return n


def acquire_lock(cfg):
    """One daemon per state directory (also protects the ledger from a second instance)."""
    os.makedirs(cfg.state_path, mode=0o700, exist_ok=True)
    fh = open(os.path.join(cfg.state_path, "daemon.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("another arm_pipeline daemon already holds the lock")
    return fh


def run(cfg, pipe: Pipeline | None = None, stop=None, sleep=time.sleep) -> None:
    lock = acquire_lock(cfg)                                  # noqa: F841 - held for the process lifetime
    pipe = pipe or Pipeline(cfg)
    stop = stop or (lambda: False)
    running = {"stop": False}

    def on_signal(*_):
        running["stop"] = True
        pipe.stop_requested = True                            # makes a running rip terminate cleanly

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    recover_interrupted(pipe)
    cleanup_scratch(pipe)
    pipe.events.emit("daemon", "pipeline started")
    pipe.slack.post(":robot_face: ARM pipeline started.")
    prev_present, share_alerted, drive_alerted = False, False, False
    while not (running["stop"] or stop()):
        try:
            if pipe.canary_ok():
                share_alerted = False
                pipe.poll_pending()
            elif not share_alerted:
                pipe.slack.post(":warning: ARM: the movies share failed its canary check (stale or unmounted?). "
                                "Pausing until it recovers; a container restart may be needed.", needs_action=True)
                share_alerted = True
            try:
                present = drive_mod.disc_present(cfg.sr_dev)
                drive_alerted = False
            except DriveError as e:
                present = False
                if not drive_alerted:
                    pipe.slack.post(f":warning: ARM cannot read the drive: {e}", needs_action=True)
                    drive_alerted = True
            if present and not share_alerted:
                waiting = [r for r in pipe.ledger.active() if r.state == "awaiting_identity"]
                if not prev_present:
                    prev_present = True                   # BEFORE handling: a crash must never re-trigger this disc
                    pipe.handle_new_disc()
                for rec in waiting:                      # an identity answer arrived while the disc stayed in
                    pipe.resume_identity(rec, True)
            elif present:
                pass                                      # share down: keep "not handled" so it is processed on recovery
            else:
                prev_present = False
        except Exception:                                # noqa: BLE001 - the loop must survive anything
            log.exception("poll cycle failed")
        sleep(cfg.poll_seconds)
    pipe.events.emit("daemon", "pipeline stopping")
    pipe.slack.post(":stop_sign: ARM pipeline stopping.")
