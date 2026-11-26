"""Turn ledger + live status into a prioritised list of things a human should look at (stdlib only).

Table-driven: each rule is a function ``rule(ctx) -> list[issue]`` registered in RULES. A rule that raises becomes an
info issue "Check failed: <rule>" instead of breaking the page.
"""
from __future__ import annotations

import re
import time

from ..ledger import ACTIVE_STATES, Ledger
from . import data

SEV_RANK = {"critical": 0, "warning": 1, "info": 2}
WORKING = data.WORKING_STATES
TERMINAL3 = ("done", "skipped", "failed")
NON_TERMINAL = ("awaiting_identity", "awaiting_approval", "failed")
IDENTITY_CRITICAL_S = 24 * 3600
STUCK_S = 3 * 3600
SCRATCH_WARN, SCRATCH_CRIT = 85, 95


class Ctx:
    def __init__(self, cfg, status, records, now):
        self.cfg, self.status, self.records, self.now = cfg, status, records, now

    @property
    def drive(self):
        return self.status.get("drive") or {}

    @property
    def servers(self):
        return self.status.get("servers") or {}


def _slug(s) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:60] or "unknown"


def _issue(id_, severity, title, detail="", fp=None, since=None, actions=None) -> dict:
    return {"id": id_, "severity": severity, "title": title, "detail": detail, "fp": fp, "since": since,
            "actions": actions or []}


def _action(id_, label, fp, confirm=False, needs_input=None, path=None) -> dict:
    return {"id": id_, "label": label, "method": "POST", "path": path or f"/api/jobs/{fp}/{id_}",
            "body": {}, "confirm": confirm, "needs_input": needs_input}


def _since(rec):
    """When the record entered its current state (history), else its last update."""
    for h in reversed(rec.history or []):
        try:
            if h[1] == rec.state:
                return float(h[0])
        except (IndexError, TypeError, ValueError):
            continue
    return rec.updated


def _name(rec) -> str:
    return rec.title or rec.label or rec.fingerprint


def _plan(rec) -> dict:
    return rec.plan if isinstance(rec.plan, dict) else {}


def _fmt_existing(ex) -> str:
    if isinstance(ex, dict):
        for k in ("folder", "path", "file", "name", "title"):
            if ex.get(k):
                return str(ex[k])
        return ", ".join(f"{k}={v}" for k, v in sorted(ex.items()))[:200]
    return str(ex)[:200] if ex else ""


def _fmt_age(s) -> str:
    h = s / 3600
    return f"{h / 24:.1f} days" if h >= 48 else f"{h:.0f} h"


# --------------------------------------------------------------------------- rules

def rule_service(c):
    if (c.status.get("service") or {}).get("active") is False:
        return [_issue("service-down", "critical", "The ripper service is not running",
                       f"systemd reports '{(c.status.get('service') or {}).get('state', 'unknown')}'. "
                       "No discs will be processed until it is started.")]
    return []


def rule_identity(c):
    out = []
    for r in c.records:
        if r.state != "awaiting_identity":
            continue
        since = _since(r)
        detail = "; ".join(str(x) for x in _plan(r).get("reasons") or []) or r.message or "Identity needed."
        if c.drive.get("disc_present"):
            detail += " The disc is still in the drive."
        acts = [_action("identify", "Identify", r.fingerprint, needs_input="identify"),
                _action("skip", "Skip", r.fingerprint, confirm=True)]
        if any(o.fingerprint != r.fingerprint and o.label and o.label == r.label for o in c.records):
            acts.append(_action("dismiss", "Dismiss", r.fingerprint, confirm=True))
        sev = "critical" if c.now - since > IDENTITY_CRITICAL_S else "warning"
        out.append(_issue(f"identity-{r.fingerprint}", sev, f"Needs identification: {_name(r)}", detail,
                          r.fingerprint, since, acts))
    return out


def rule_approval(c):
    out = []
    limit = c.cfg.approval_timeout_days * 86400 * 0.8
    for r in c.records:
        if r.state != "awaiting_approval":
            continue
        p, since = _plan(r), _since(r)
        bits = []
        ex = _fmt_existing(p.get("existing"))
        if ex:
            bits.append(f"Will replace: {ex}")
        bits += [str(x) for x in p.get("reasons") or []]
        sev = "critical" if c.now - since > limit else "warning"
        out.append(_issue(f"approval-{r.fingerprint}", sev, f"Waiting for approval: {_name(r)}",
                          ". ".join(bits) or r.message, r.fingerprint, since,
                          [_action("approve", "Approve", r.fingerprint),
                           _action("reject", "Reject", r.fingerprint, confirm=True)]))
    return out


def _decode_only_failure(r) -> bool:
    m = r.message or ""
    return ("verification FAILED" in m and ("T3 sampled decode" in m or "T4 full decode" in m)
            and "T1 probe" not in m and "T2 demux" not in m)


def rule_failed(c):
    out = []
    for r in c.records:
        if r.state != "failed":
            continue
        acts = [_action("retry", "Retry", r.fingerprint)]
        if _decode_only_failure(r) and (r.paths or {}).get("outdir"):
            acts.append(_action("accept_verify", "Accept and place kept rip", r.fingerprint, confirm=(
                "Accept this rip despite the decode warning and place the kept file? Only the decoder complained; the "
                "container and the copy checksum are re-proven first. No re-rip.")))
        acts.append(_action("dismiss", "Dismiss", r.fingerprint, True))
        out.append(_issue(f"failed-{r.fingerprint}", "critical", f"Failed: {_name(r)}", r.message or "Job failed.",
                          r.fingerprint, _since(r), acts))
    return out


def rule_duplicates(c):
    groups = {}
    for r in c.records:
        if r.state in NON_TERMINAL and r.label:
            groups.setdefault(r.label, []).append(r)
    out = []
    for label, recs in groups.items():
        if len(recs) < 2:
            continue
        recs.sort(key=lambda r: r.updated, reverse=True)
        newest = recs[0]
        out.append(_issue(f"duplicate-{_slug(label)}", "warning", f"Duplicate records for {label}",
                          f"{len(recs)} unresolved records share this label; keeping the newest "
                          f"({newest.fingerprint}) and offering to dismiss the rest.",
                          newest.fingerprint, min(r.created for r in recs),
                          [_action("dismiss", f"Dismiss {r.fingerprint[:8]} ({r.state})", r.fingerprint, True)
                           for r in recs[1:]]))
    return out


def rule_stuck(c):
    cur = c.status.get("current") or {}
    out = []
    for r in c.records:
        if r.state not in WORKING or c.now - r.updated <= STUCK_S:
            continue
        if cur and cur.get("fp") == r.fingerprint and not cur.get("stale"):
            continue                                         # something is demonstrably making progress
        out.append(_issue(f"stuck-{r.fingerprint}", "critical", "Job looks stuck",
                          f"{_name(r)} has been '{r.state}' with no update for {_fmt_age(c.now - r.updated)} "
                          "and nothing is ripping.", r.fingerprint, r.updated))
    return out


def rule_share(c):
    sh = c.status.get("share") or {}
    if sh.get("ok") is False:
        return [_issue("share-down", "critical", "The movie share is not reachable",
                       "The canary file check failed (share stale or unmounted). The ripper pauses until it recovers.",
                       None, sh.get("checked"))]
    return []


def rule_drive(c):
    out = []
    tray, present = c.drive.get("tray"), c.drive.get("disc_present")
    if tray == "UNKNOWN":
        out.append(_issue("drive-unknown", "warning", "Cannot read the drive state",
                          "The optical drive did not answer; check the device passthrough."))
    active = (c.status.get("service") or {}).get("active")
    if present and active and not any(r.state in ACTIVE_STATES for r in c.records):
        newest = max(c.records, key=lambda r: r.updated, default=None)
        if newest is not None and newest.state in TERMINAL3:
            out.append(_issue("disc-finished", "info", "Disc still in the drive after it finished",
                              f"'{_name(newest)}' is {newest.state}; the disc can be ejected.", newest.fingerprint,
                              newest.updated,
                              [_action("eject", "Eject", None, True, path="/api/drive/eject")]))
        else:
            out.append(_issue("disc-waiting", "info", "Disc present, waiting for the next poll",
                              "A disc is in the drive and no job is active; it will be picked up shortly."))
    return out


SCRATCH_MIN_FREE_GB = 90            # a 4K disc is about 77 GB and a rip needs the title size + 10%


def rule_scratch(c):
    sc = c.status.get("scratch") or {}
    pct, free, rips = sc.get("used_pct"), sc.get("free_gb"), sc.get("rips_gb") or 0
    low = isinstance(free, (int, float)) and free < SCRATCH_MIN_FREE_GB
    full = isinstance(pct, (int, float)) and pct >= SCRATCH_WARN
    if not (low or full):
        return []
    sev = "critical" if (isinstance(pct, (int, float)) and pct >= SCRATCH_CRIT) else "warning"
    detail = (f"{free:.0f} GB free" if isinstance(free, (int, float)) else "low") + \
             f"; a 4K rip needs about {SCRATCH_MIN_FREE_GB} GB. Leftover rips hold {rips:.0f} GB."
    return [_issue("scratch-full", sev, "Scratch disk is low on space", detail, None, None,
                   [_action("clean-scratch", "Clean up leftovers", "", True, path="/api/scratch/clean")])]


def rule_integrations(c):
    s, out = c.servers, []
    for key, name in (("plex", "Plex"), ("jellyfin", "Jellyfin")):
        d = s.get(key) or {}
        if not d.get("configured"):
            out.append(_issue(f"{key}-not-configured", "info", f"{name} is not configured: new movies will not "
                              "appear until it rescans", f"Set {key.upper()}_URL and {key.upper()}_TOKEN."))
        elif d.get("reachable") is False:
            out.append(_issue(f"{key}-unreachable", "warning", f"{name} is not reachable",
                              f"The {name} server did not answer; placed movies may not be picked up."))
    if not (s.get("tmdb") or {}).get("configured"):
        out.append(_issue("tmdb-not-configured", "warning", "TMDb is not configured",
                          "Every disc will ask for an identity because titles cannot be looked up."))
    if not (s.get("slack") or {}).get("webhook"):
        out.append(_issue("slack-webhook-missing", "warning", "Slack notifications are not configured",
                          "Failures and approval requests will not be announced."))
    return out


def _part_checks(c, states):
    if (c.status.get("share") or {}).get("ok") is False:
        return [], {}                                        # share down: a missing file proves nothing
    recs = [r for r in c.records if r.state in states and (r.paths or {}).get("part")]
    return recs, data.fs_exists_many([r.paths["part"] for r in recs])


def rule_leftovers(c):
    recs, ex = _part_checks(c, ("failed", "rejected"))
    return [_issue(f"leftover-{r.fingerprint}", "warning", "Staged file left on the share",
                   f"{r.paths['part']} remains after '{_name(r)}' {r.state}.", r.fingerprint, _since(r))
            for r in recs if ex.get(r.paths["part"]) is True]


def rule_staged_missing(c):
    recs, ex = _part_checks(c, ("awaiting_approval",))
    return [_issue(f"staged-missing-{r.fingerprint}", "critical", "Staged file is gone while approval is pending",
                   f"{r.paths['part']} no longer exists; approving would fail. Reject and retry.",
                   r.fingerprint, _since(r), [_action("reject", "Reject", r.fingerprint, True)])
            for r in recs if ex.get(r.paths["part"]) is False]


RULES = [
    ("service", rule_service), ("identity", rule_identity), ("approval", rule_approval),
    ("failed", rule_failed), ("duplicates", rule_duplicates), ("stuck", rule_stuck), ("share", rule_share),
    ("drive", rule_drive), ("scratch", rule_scratch), ("integrations", rule_integrations),
    ("leftovers", rule_leftovers), ("staged_missing", rule_staged_missing),
]


def _sort_key(i):
    since = i.get("since")
    return (SEV_RANK.get(i["severity"], 9), since is None, since or 0, i["id"])


def collect_issues(cfg, status=None, jobs=None) -> list:
    """`jobs` is accepted for API symmetry; rules read the raw ledger records (they need plan/paths/history)."""
    try:
        if status is None:
            status = data.collect_status(cfg)
        now = status.get("now") or time.time()
        try:
            records = Ledger(cfg.state_path).all()
        except Exception:                                    # noqa: BLE001
            records = []
        ctx = Ctx(cfg, status, records, now)
    except Exception:                                        # noqa: BLE001
        return [_issue("check-failed-setup", "info", "Check failed: setup")]
    out = []
    for name, fn in RULES:
        try:
            out.extend(fn(ctx))
        except Exception:                                    # noqa: BLE001
            out.append(_issue(f"check-failed-{name}", "info", f"Check failed: {name}"))
    out.sort(key=_sort_key)
    return out
