"""Orchestrator: one disc -> scan -> identify -> decide -> rip -> verify -> stage -> place/replace -> notify -> eject.

Design rules (owner policy):
  * the disc is only ejected once its data is safely off it (or it was skipped / failed); a disc that needs a human
    answer stays in the drive;
  * the library is never changed until the new file is verified AND checksum-staged on the share as ``*.part``
    (ignored by Plex/Jellyfin);
  * a replacement waits for approval unless ``auto_replace`` is on and the folder matched exactly;
  * replace order: hide old -> scan both servers -> place new -> scan again -> confirm -> delete hidden old,
    so the movie shows up as NEW in "Recently Added";
  * every failure posts one Slack message naming the step, and the ledger blocks re-ripping the same disc.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
import shutil
import subprocess
import re
import threading
import time
import unicodedata

from arm_policy import decision, library_match, makemkv_scan

from . import drive as drive_mod
from . import library, metadata, swap, transfer, verify
from .errors import PipelineError, SwapError
from .events import EventLog
from .ledger import Ledger, Record, fingerprint, legacy_fingerprint
from .media_servers import Jellyfin, Plex, to_server_path
from .progress import Progress, parse_prgv, prgv_percent
from .slack import Notifier

log = logging.getLogger("arm.pipeline")
_FP = re.compile(r"^[0-9a-f]{16}$")
_SECRET = re.compile(r"(TOKEN|KEY|URL|LICENSE|SECRET|PASSWORD)", re.I)


def _read(path: str) -> str:
    with open(path, errors="replace") as fh:
        return fh.read()


def _fmt_bytes(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def _fmt_dur(s: int) -> str:
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


class _Tee:
    """Wraps the notifier: every message posted to Slack is also written to the event log the dashboard reads."""

    def __init__(self, inner, events, current_fp):
        self._inner, self._events, self._current_fp = inner, events, current_fp

    def post(self, text):
        level = "error" if text.startswith(":x:") else "warning" if text.startswith(":warning:") else "info"
        self._events.emit("slack", text, fp=self._current_fp(), level=level)
        return self._inner.post(text)

    def __getattr__(self, name):                 # ask_approval, poll_approval, ...
        return getattr(self._inner, name)


class Pipeline:
    CANARY_TIMEOUT_S = 10

    def __init__(self, cfg, notifier=None, plex=None, jelly=None, tmdb=None, ledger=None, drive=None):
        self.cfg = cfg
        self.events = EventLog(cfg.state_path)
        self.progress = Progress(cfg.state_path)
        self._current_fp = None
        self.slack = _Tee(notifier or Notifier(cfg), self.events, lambda: self._current_fp)
        self.plex = plex or Plex(cfg)
        self.jelly = jelly or Jellyfin(cfg)
        self.tmdb = tmdb or metadata.Tmdb(cfg)
        self.ledger = ledger or Ledger(cfg.state_path)
        self.drive = drive or drive_mod
        self.stop_requested = False              # set by the daemon's SIGTERM handler
        self._canary_thread = None
        self._current_fp = None
        for d in ("identify", "approve", "reject", "scans"):
            os.makedirs(os.path.join(cfg.state_path, d), mode=0o700, exist_ok=True)

    # ------------------------------------------------------------------ small helpers
    def _state_file(self, kind: str, fp: str, ext: str = "") -> str:
        if not _FP.match(fp):
            raise PipelineError(f"invalid disc fingerprint {fp!r}", "state")
        return os.path.join(self.cfg.state_path, kind, fp + ext)

    def canary_ok(self) -> bool:
        """The share can hang (stale CIFS) or be an empty unmounted directory: prove it is the real mount.
        A stat on a dead CIFS mount enters uninterruptible sleep and cannot be killed, so it runs in a daemon thread
        that we simply stop waiting for; while one is still stuck we do not start another."""
        if self._canary_thread is not None and self._canary_thread.is_alive():
            return False
        path = os.path.join(self.cfg.movies_dir, self.cfg.canary_name)
        box = {"ok": False}

        def probe():
            try:
                os.stat(path)
                box["ok"] = True
            except OSError:
                pass
        t = threading.Thread(target=probe, daemon=True)
        t.start()
        t.join(self.CANARY_TIMEOUT_S)
        if t.is_alive():
            self._canary_thread = t
            return False
        self._canary_thread = None
        return box["ok"]

    def _run_makemkv(self, args: list, log_path: str, timeout: int | None, on_tick=None) -> int:
        env = {k: v for k, v in os.environ.items() if not _SECRET.search(k)}   # MakeMKV needs none of our secrets
        with open(log_path, "wb") as fh:
            p = subprocess.Popen([self.cfg.makemkvcon, "-r", *args], stdout=fh, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, env=env)
            t0 = last_tick = time.monotonic()
            while p.poll() is None:
                if on_tick and time.monotonic() - last_tick >= 5:
                    last_tick = time.monotonic()
                    try:
                        on_tick()
                    except Exception:                                   # noqa: BLE001 - progress must never break a rip
                        log.exception("progress tick failed")
                if self.stop_requested:
                    p.terminate()
                    try:
                        p.wait(30)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()
                    raise PipelineError("interrupted by a service stop", "rip")
                if timeout and time.monotonic() - t0 > timeout:
                    p.kill()
                    p.wait()
                    raise PipelineError(f"makemkvcon timed out after {timeout}s", "rip")
                time.sleep(0.25)
            return p.returncode

    def _set_selection_string(self, sel: str) -> None:
        """MakeMKV honours app_DefaultSelectionString from settings.conf (verified on real discs)."""
        path = os.path.expanduser("~/.MakeMKV/settings.conf")
        lines = []
        if os.path.exists(path):
            lines = [ln for ln in _read(path).splitlines() if not ln.startswith("app_DefaultSelectionString")]
        lines.append(f'app_DefaultSelectionString="{sel}"')
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _finish(self, fp: str, state: str, message: str, eject: bool, **fields) -> None:
        tail = ""
        if eject:
            ok, st = self.drive.eject(self.cfg)
            tail = f" Disc ejected." if ok else f" EJECT NOT CONFIRMED (tray {st}) - please check the drive."
        self.ledger.set_state(fp, state, message, **fields)
        self.progress.clear(fp)
        self.slack.post(message + tail)

    # ------------------------------------------------------------------ entry points
    def scan_disc(self):
        os.makedirs(self.cfg.scratch_dir, exist_ok=True)
        log_path = os.path.join(self.cfg.scratch_dir, "scan.log")
        rc = self._run_makemkv(["--cache=16", "info", "disc:0"], log_path, self.cfg.scan_timeout_s)
        text = _read(log_path)
        if rc != 0 or "TCOUNT" not in text and "TINFO" not in text:
            raise PipelineError(f"disc scan failed (exit {rc}); unreadable disc or drive not in LibreDrive mode", "scan")
        return makemkv_scan.parse_scan(text), text

    def handle_new_disc(self) -> str:
        """Called once per inserted disc. Never raises: a crash marks the record failed and posts ONE Slack message,
        so the daemon cannot loop on the same disc."""
        self._current_fp = None
        try:
            return self._handle_new_disc()
        except Exception as e:                                         # noqa: BLE001
            log.exception("crash while handling a disc")
            msg = f":x: ARM crashed while handling the disc: {type(e).__name__}: {e}. Disc left in the drive."
            fp = self._current_fp
            if fp and self.ledger.get(fp):
                self.ledger.set_state(fp, "failed", msg)
            self.slack.post(msg)
            return "failed"

    def _handle_new_disc(self) -> str:
        if not self.canary_ok():
            self.slack.post(":warning: ARM: the movies share failed its canary check; not processing the disc.")
            return "share-unavailable"
        try:
            disc, text = self.scan_disc()
        except PipelineError as e:
            self.slack.post(f":x: ARM failed at {e.step}: {e}. Disc left in the drive.")
            return "scan-failed"
        label = disc.attrs.get(32, "") or disc.name
        fp = fingerprint(label, disc.titles)
        legacy = legacy_fingerprint(label, disc.titles)
        self._current_fp = fp
        if not self.ledger.should_process(fp, legacy_fp=legacy):
            rec = self.ledger.get_any(fp, legacy)
            state = rec.state if rec else "?"
            tail = ""
            if state in ("done", "skipped", "failed", "rejected"):
                ok, st = self.drive.eject(self.cfg)
                tail = " Disc ejected." if ok else f" Eject not confirmed (tray {st})."
            self.slack.post(f"ARM: disc '{disc.name or label}' was already handled (state: {state}). Ignoring it."
                            f" Use `python3 -m arm_pipeline retry {fp}` to process it again.{tail}")
            return "already-seen"
        with open(self._state_file("scans", fp, ".txt"), "w") as fh:
            fh.write(text)
        self.ledger.put(Record(fingerprint=fp, state="scanning", label=label, title=disc.name,
                               created=time.time(), updated=time.time()))
        self.slack.post(f":cd: ARM: disc detected: {disc.name or label} ({len(disc.titles)} titles). Working on it.")
        return self._plan_and_run(fp, disc, identity_override=None)

    # ------------------------------------------------------------------ planning
    def _identity(self, disc, override):
        if override:
            return override
        longest = max((t.duration_s for t in disc.titles), default=0)
        title, year = metadata.parse_disc_name(disc.attrs.get(32, ""), disc.name)
        return self.tmdb.identify(title, year, longest // 60 if longest else None) if self.tmdb.configured else None

    def _prefer_library_spelling(self, identity, disc):
        """TMDb may spell a title differently from the library (regional titles). If the TMDb title matches no folder
        but the title parsed from the disc does, use the disc's spelling so the existing movie is found instead of a
        duplicate being created."""
        try:
            folders = library.list_folders(self.cfg.movies_dir)
            if library_match.find_match(identity.title, identity.year, folders).status != "none":
                return identity
            alt, _ = metadata.parse_disc_name(disc.attrs.get(32, ""), disc.name)
            if alt and library_match.find_match(alt, identity.year, folders).status != "none":
                return dataclasses.replace(identity, title=alt)
        except Exception:                                              # noqa: BLE001 - best effort only
            log.exception("library spelling check failed")
        return identity

    def _plan_and_run(self, fp: str, disc, identity_override) -> str:
        try:
            identity = self._identity(disc, identity_override)
            if identity and not identity_override:
                identity = self._prefer_library_spelling(identity, disc)
            lib = library.build_library(self.cfg, identity.title, identity.year) if identity else {}
            plan = decision.decide(disc, identity, lib, auto_replace=self.cfg.auto_replace)
        except Exception as e:                                         # noqa: BLE001
            self._finish(fp, "failed", f":x: ARM failed at planning: {e}", eject=False)
            return "failed"
        self.ledger.set_state(fp, "scanning", "; ".join(plan.reasons), plan={"action": plan.action,
                              "reasons": plan.reasons, "warnings": plan.warnings, "title_index": plan.title_index,
                              "target_folder": plan.target_folder, "stem": plan.target_filename_stem,
                              "existing": plan.existing_file, "selection": plan.selection_string,
                              "extended": plan.is_extended, "needs_approval": plan.needs_approval,
                              "identity": {"title": identity.title, "year": identity.year} if identity else None})
        if plan.existing_file:
            try:
                st = os.stat(os.path.join(self.cfg.movies_dir, plan.target_folder, plan.existing_file))
                self.ledger.set_state(fp, "scanning", "", plan={**self.ledger.get(fp).plan,
                                      "existing_stat": [st.st_size, st.st_mtime_ns]})
            except OSError:
                pass
        name = f"{identity.title} ({identity.year})" if identity and identity.year else (disc.name or fp)
        why = "; ".join(plan.reasons + plan.warnings) or "-"
        if plan.action == decision.SKIP:
            self._finish(fp, "skipped", f":fast_forward: ARM skipped *{name}*: {why}.", eject=True)
            return "skipped"
        if plan.action in (decision.ASK, decision.ASK_IDENTITY):
            self.ledger.set_state(fp, "awaiting_identity", why)
            self.slack.post(f":question: ARM needs a human for *{disc.name or fp}*: {why}. The disc stays in the drive. "
                            f"Answer with `python3 -m arm_pipeline identify {fp} \"Title (Year)\"` "
                            f"or skip it with `python3 -m arm_pipeline skip {fp}`.")
            return "awaiting-identity"
        return self._rip_verify_stage(fp, disc, plan, name, identity)

    # ------------------------------------------------------------------ rip + verify + stage
    def _rip_verify_stage(self, fp, disc, plan, name, identity) -> str:
        cfg = self.cfg
        title = next(t for t in disc.titles if t.index == plan.title_index)
        outdir = os.path.join(cfg.scratch_dir, "rips", fp)
        final = os.path.join(cfg.movies_dir, plan.target_folder, plan.target_filename_stem + ".mkv")
        part = final + ".part"
        try:
            need = int(title.size_bytes * 1.1)
            free = shutil.disk_usage(cfg.scratch_dir).free
            if free < need:
                raise PipelineError(f"scratch has {_fmt_bytes(free)} free, need about {_fmt_bytes(need)}", "rip")
            if os.path.exists(final) and plan.action == decision.RIP_NEW:
                raise PipelineError(f"target file already exists: {final}", "stage")
            st = library.folder_file_state(cfg.movies_dir, plan.target_folder) \
                if os.path.isdir(os.path.join(cfg.movies_dir, plan.target_folder)) else {}
            if st.get("parts") or st.get("hidden_old"):
                raise PipelineError(f"leftovers from an interrupted run in '{plan.target_folder}' "
                                    f"(parts={st.get('parts')}, hidden={st.get('hidden_old')}); resolve by hand first",
                                    "stage")
            os.makedirs(outdir, exist_ok=True)
            self._set_selection_string(plan.selection_string)
            self.ledger.set_state(fp, "ripping", f"ripping title {title.index} ({_fmt_dur(title.duration_s)}, "
                                  f"{_fmt_bytes(title.size_bytes)})", paths={"outdir": outdir, "final": final, "part": part})
            self.slack.post(f":arrow_forward: ARM ripping *{name}*: title {title.index}, {_fmt_dur(title.duration_s)}, "
                            f"{_fmt_bytes(title.size_bytes)}{' (extended cut)' if plan.is_extended else ''}.")
            t0 = time.time()
            log_path = os.path.join(outdir, "rip.log")
            def rip_tick():
                pct = None
                try:
                    with open(log_path, "rb") as fh:
                        fh.seek(0, os.SEEK_END)
                        fh.seek(max(0, fh.tell() - 4096))
                        pct = prgv_percent(parse_prgv(fh.read().decode("utf-8", "replace")))
                except OSError:
                    pass
                done = sum(os.path.getsize(os.path.join(outdir, f)) for f in os.listdir(outdir)
                           if f.lower().endswith(".mkv"))
                self.progress.update(fp, "ripping", done_bytes=done, total_bytes=title.size_bytes, percent=pct,
                                     note=f"title {title.index}")
            rip_tick()
            rc = self._run_makemkv(["--progress=-same", "mkv", "disc:0", str(title.index), outdir], log_path, None,
                                   on_tick=rip_tick)
            mkvs = sorted(f for f in os.listdir(outdir) if f.lower().endswith(".mkv"))
            if rc != 0 or len(mkvs) != 1:
                raise PipelineError(f"rip failed (exit {rc}, {len(mkvs)} output files)", "rip")
            ripped = os.path.join(outdir, mkvs[0])
            rip_s = int(time.time() - t0)

            # verify (tiered, fast) and stage on the share concurrently: they use different resources
            self.ledger.set_state(fp, "verifying", "verifying and staging")
            self.progress.update(fp, "verifying", note="quick checks + copy to the share")
            expect = verify.Expect(duration_s=title.duration_s, min_size_bytes=int(title.size_bytes * 0.5))
            res: dict = {}

            def do_verify():
                try:
                    res["v"] = verify.verify_rip(ripped, expect, cfg, makemkv_log_text=_read(log_path))
                except Exception as e:                                  # noqa: BLE001
                    res["verify_err"] = e

            def do_stage():
                try:
                    folder = os.path.dirname(final)
                    if not os.path.isdir(folder):
                        os.mkdir(folder)                # the share root must exist; never create parents of parents
                    size, digest = transfer.copy_with_hash(
                        ripped, part, progress=lambda done, total: self.progress.update(
                            fp, "copying", done_bytes=done, total_bytes=total, note="copy to the share"))  # one read...
                    transfer.verify_readback(part, size, digest)              # ...and one read-back of the copy
                    res["size"], res["sha"] = size, digest
                except Exception as e:                                  # noqa: BLE001
                    res["stage_err"] = e
            th = [threading.Thread(target=do_verify), threading.Thread(target=do_stage)]
            [x.start() for x in th]
            [x.join() for x in th]

            def drop_part():
                if os.path.exists(part):
                    os.remove(part)
            if "stage_err" in res:
                drop_part()
                raise res["stage_err"]
            if "verify_err" in res:
                drop_part()
                raise PipelineError(f"verification could not run: {type(res['verify_err']).__name__}: "
                                    f"{res['verify_err']}", "verify")
            v = res["v"]
            if not v.ok:
                drop_part()
                raise PipelineError("verification FAILED: " + v.summary(), "verify")
            verify_line = v.summary()
        except PipelineError as e:
            self._finish(fp, "failed", f":x: ARM failed at {e.step} for *{name}*: {e}. Scratch kept for inspection.",
                         eject=True)
            return "failed"
        except Exception as e:                                         # noqa: BLE001
            log.exception("unexpected failure")
            self._finish(fp, "failed", f":x: ARM unexpected failure for *{name}*: {type(e).__name__}: {e}", eject=True)
            return "failed"

        size = res["size"]
        info = (f"{_fmt_bytes(size)}, {_fmt_dur(title.duration_s)}, ripped in {rip_s // 60}m; {verify_line}; "
                "copy checksum-verified on the share")
        self.ledger.set_state(fp, "transferring", info, paths={"outdir": outdir, "final": final, "part": part,
                                                               "ripped": ripped, "info": info, "part_size": size,
                                                               "part_sha256": res["sha"]})
        if plan.action == decision.RIP_REPLACE and plan.needs_approval:
            return self._ask_approval(fp, plan, name, info)
        return self._place(fp, plan, name, identity, eject=True)

    # ------------------------------------------------------------------ approval
    def _ask_approval(self, fp, plan, name, info) -> str:
        text = (f":scales: ARM wants to REPLACE the existing copy of *{name}* ({plan.existing_file}) with the new rip: "
                f"{'; '.join(plan.reasons)}. New file: {info}. React with :white_check_mark: to approve or :x: to reject"
                f" (or run `python3 -m arm_pipeline approve {fp}` / `reject {fp}`). Nothing in the library has changed; "
                "the new file is staged as .part.")
        ticket = self.slack.ask_approval(text)
        if ticket is None:
            self.slack.post(text)
        for kind in ("approve", "reject"):                  # an answer given BEFORE this request must not count
            try:
                os.remove(self._state_file(kind, fp))
            except FileNotFoundError:
                pass
        rec = self.ledger.get(fp)
        paths = dict(rec.paths)
        if ticket is not None:
            paths["ticket"] = {"channel": ticket.channel, "ts": ticket.ts}
        self.ledger.set_state(fp, "awaiting_approval", "waiting for approval", paths=paths)
        self.progress.clear(fp)
        ok, st = self.drive.eject(self.cfg)           # the data is safely off the disc: free the drive
        if not ok:
            self.slack.post(f":warning: ARM: eject not confirmed (tray {st}).")
        return "awaiting-approval"

    def poll_pending(self) -> None:
        """Between discs: resume approvals and identity answers; expire old requests."""
        for rec in self.ledger.active():
            try:
                if rec.state == "awaiting_approval":
                    self._poll_approval(rec)
            except Exception as e:                                     # noqa: BLE001
                log.exception("poll failed for %s", rec.fingerprint)
                self.slack.post(f":x: ARM error while resuming {rec.fingerprint}: {e}")

    def _poll_approval(self, rec) -> None:
        fp = rec.fingerprint
        verdict = "pending"
        if os.path.exists(self._state_file("reject", fp)):          # if both files exist, reject wins
            verdict = "rejected"
        elif os.path.exists(self._state_file("approve", fp)):
            verdict = "approved"
        elif rec.paths.get("ticket"):
            verdict = self.slack.poll_approval(metadata_ticket(rec.paths["ticket"]))
        if verdict == "pending":
            if time.time() - rec.updated > self.cfg.approval_timeout_days * 86400:
                verdict = "rejected"
            else:
                return
        for kind in ("approve", "reject"):
            try:
                os.remove(self._state_file(kind, fp))
            except FileNotFoundError:
                pass
        name = rec.plan.get("identity") or {}
        name = f"{name.get('title', rec.title)} ({name.get('year', '?')})"
        if verdict == "rejected":
            part = rec.paths.get("part", "")
            if part and os.path.exists(part):
                os.remove(part)
            shutil.rmtree(rec.paths.get("outdir", ""), ignore_errors=True)
            self._finish(fp, "rejected", f":no_entry_sign: ARM: replacement of *{name}* was rejected or timed out; "
                         "staged file removed, library unchanged.", eject=False)
            return
        plan = decision.Plan(action=decision.RIP_REPLACE, target_folder=rec.plan["target_folder"],
                             target_filename_stem=rec.plan["stem"], existing_file=rec.plan["existing"])
        self._place(fp, plan, name, None, eject=False, recheck=True)   # disc already ejected; re-prove the staged file

    # ------------------------------------------------------------------ placement (new + replace)
    def _server_sync(self, folder: str, final: str, present: bool, deleted_file: str | None = None) -> list:
        """Tell BOTH servers about a change and wait until each reflects it. A failure on one server never prevents
        notifying the other; all failures are raised together afterwards. Returns notes about anything skipped."""
        cfg, notes, errors = self.cfg, [], []
        target = deleted_file or final
        word = "show the new" if present else "drop the old"
        if self.plex.configured:
            try:
                self.plex.scan_path(folder)
                sp = to_server_path(target, cfg.movies_dir, cfg.plex_movies_path)
                if not self.plex.wait_for_file("", sp, present, cfg.server_wait_s, poll_s=10):
                    errors.append(f"Plex did not {word} file within {cfg.server_wait_s}s")
            except Exception as e:                                     # noqa: BLE001
                errors.append(f"Plex: {type(e).__name__}: {e}")
        else:
            notes.append("Plex not notified (no PLEX_TOKEN configured)")
        if self.jelly.configured:
            try:
                self.jelly.push(folder, "Created" if present else "Deleted")
                if not self.jelly.wait_for_path(target, present, cfg.server_wait_s):
                    errors.append(f"Jellyfin did not {word} file within {cfg.server_wait_s}s")
            except Exception as e:                                     # noqa: BLE001
                errors.append(f"Jellyfin: {type(e).__name__}: {e}")
        else:
            notes.append("Jellyfin not notified (no JELLYFIN_TOKEN configured)")
        if errors:
            raise SwapError("; ".join(errors))
        return notes

    def _place(self, fp, plan, name, identity, eject: bool = True, recheck: bool = False) -> str:
        rec = self.ledger.get(fp)
        final, part = rec.paths["final"], rec.paths["part"]
        folder = os.path.dirname(final)
        replace = plan.action == decision.RIP_REPLACE
        old = os.path.join(self.cfg.movies_dir, plan.target_folder, plan.existing_file) if replace else None
        self.ledger.set_state(fp, "placing", "placing in library")
        state, notes, verb = None, [], "ADDED"
        try:
            # --- everything before the new file is in place: any failure rolls back and leaves the library as it was
            if recheck:      # the staged file may have waited days for approval: prove it and the old file again
                exp = rec.plan.get("existing_stat")
                if replace and exp:
                    st = os.stat(old)
                    if [st.st_size, st.st_mtime_ns] != exp:
                        raise PipelineError("the existing file changed since this replacement was requested; "
                                            "nothing was replaced", "swap")
                transfer.verify_readback(part, rec.paths["part_size"], rec.paths["part_sha256"])
            if replace:
                verb = "REPLACED"
                state = swap.replace_begin(old, part, final)
                self.ledger.set_state(fp, "placing", "old file hidden", paths={**rec.paths, "replace": state.__dict__})
                notes += self._server_sync(folder, final, present=False, deleted_file=old)
                swap.replace_place(state)
            else:
                swap.place_new(part, final)
        except Exception as e:                                         # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            if state is not None:
                try:
                    msg += " | rollback: " + swap.replace_rollback(state)
                    try:                                  # the servers were told the old file was gone: tell them it is back
                        self._server_sync(folder, old, present=True)
                        msg += " | servers re-synced"
                    except Exception as e3:                          # noqa: BLE001
                        msg += f" | servers NOT re-synced ({e3}): rescan Plex/Jellyfin by hand"
                except Exception as e2:                                # noqa: BLE001
                    msg += f" | ROLLBACK FAILED ({e2}); check {folder} by hand"
            elif os.path.exists(part):
                try:
                    os.remove(part)                       # a staged file that failed its re-check is unusable
                except OSError:
                    pass
            self._finish(fp, "failed", f":x: ARM failed placing *{name}*: {msg}", eject=eject)
            return "failed"
        # --- the new file IS in the library from here on: never report failure or remove it
        confirmed = True
        try:
            notes += self._server_sync(folder, final, present=True)
        except Exception as e:                                         # noqa: BLE001
            confirmed = False
            notes.append(f"WARNING: servers did not confirm the new file ({e}); rescan them by hand"
                         + (f"; the old file is kept hidden as {state.hidden_path}" if state else ""))
        if state is not None and confirmed:
            swap.replace_finish(state)
        shutil.rmtree(rec.paths.get("outdir", ""), ignore_errors=True)
        extra = ("; " + "; ".join(notes)) if notes else ""
        shows = "Plex and Jellyfin show it as new" if confirmed else "it is in the library"
        self._finish(fp, "done", f":white_check_mark: ARM {verb} *{name}* - {rec.paths.get('info', '')}. {shows}{extra}.",
                     eject=eject)
        return "done"

    # ------------------------------------------------------------------ resume an identity answer
    def resume_identity(self, rec, disc_present: bool) -> str | None:
        fp = rec.fingerprint
        path = self._state_file("identify", fp, ".json")
        if not os.path.lexists(path):
            return None
        if not disc_present:
            return None
        try:
            ans = _read_command_file(path)
            title, year = _clean_identity(ans)
        except PipelineError as e:
            try:
                os.remove(path)
            except OSError:
                pass
            self.slack.post(f":warning: ARM ignored an invalid identity answer for {fp}: {e}. "
                            "The disc is still waiting for a valid one.")
            return None
        os.remove(path)
        text = _read(self._state_file("scans", fp, ".txt"))
        disc = makemkv_scan.parse_scan(text)
        runtime, lang = None, None
        tmdb_id = ans.get("tmdb_id") if isinstance(ans.get("tmdb_id"), int) else None
        if tmdb_id and self.tmdb.configured:          # a TMDb pick brings runtime + language (better title gating)
            try:
                det = self.tmdb._get(f"/movie/{tmdb_id}", {}) or {}
                runtime = det.get("runtime") or None
                lang = det.get("original_language") or None
            except Exception:                                          # noqa: BLE001 - optional enrichment
                log.exception("TMDb detail lookup failed for the owner's pick")
        ident = decision.Identity(title=title, year=year, runtime_min=runtime, original_language=lang,
                                  tmdb_id=tmdb_id, confidence="high")
        self.ledger.set_state(fp, "scanning", f"identity set by owner: {ident.title} ({ident.year})")
        self.events.emit("operator", f"identity set to {title} ({year})", fp=fp)
        return self._plan_and_run(fp, disc, identity_override=ident)


_BAD_NAME_CHARS = set('/\\:*?"<>|')


def _read_command_file(path: str) -> dict:
    """Read a JSON command file written by another process: refuse symlinks/non-regular files/foreign owners/big files."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise PipelineError(f"cannot open the answer file ({type(e).__name__})", "identity") from None
    try:
        st = os.fstat(fd)
        import stat as _stat
        if not _stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > 4096:
            raise PipelineError("the answer file is not a small regular file owned by this user", "identity")
        data = json.loads(os.read(fd, 4096).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise PipelineError("the answer file is not valid JSON", "identity") from None
    finally:
        os.close(fd)
    if not isinstance(data, dict):
        raise PipelineError("the answer file must be a JSON object", "identity")
    return data


def _clean_identity(ans: dict) -> tuple:
    """The title becomes a LIBRARY FOLDER NAME, so this is the trust boundary: validate, never silently 'fix' -- with one
    explicit normalisation (trailing dots/spaces are stripped, because a folder cannot end with them and a TMDb title such
    as 'Say Anything...' must still be usable). The dashboard applies the same rules (operator.clean_title); a test keeps
    the two in step."""
    title = ans.get("title")
    year = ans.get("year")
    if not isinstance(title, str):
        raise PipelineError("title must be text", "identity")
    # categories first: encoding a lone surrogate would raise, so look at the characters before measuring bytes
    for c in title:
        cat = unicodedata.category(c)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") or (cat == "Zs" and c != " "):
            raise PipelineError("title contains control, invisible or unusual whitespace characters", "identity")
        if c in _BAD_NAME_CHARS:
            raise PipelineError("title contains characters that cannot be in a folder name", "identity")
    title = unicodedata.normalize("NFC", title).strip().rstrip(". ").strip()
    if not title or len(title) > 200 or len(title.encode("utf-8")) > 255:
        raise PipelineError("title is empty or too long", "identity")
    if title.startswith("."):          # '/' and '\\' are already refused, so '..' cannot form a path component
        raise PipelineError("title must not start with a dot", "identity")
    if isinstance(year, bool) or not isinstance(year, int) or not (1888 <= year <= datetime.date.today().year + 1):
        raise PipelineError("year must be a whole number between 1888 and next year", "identity")
    if not library_match.canonical_folder(title, year):
        raise PipelineError("title has no usable characters for a folder name", "identity")
    return title, year


def metadata_ticket(d: dict):
    from .slack import Ticket
    return Ticket(channel=d["channel"], ts=d["ts"])
