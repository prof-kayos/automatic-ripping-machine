#!/usr/bin/env python3
"""Dev-only mock of the ARM dashboard API (stdlib only, fake data).

    python3 tools/dashboard_mock.py [--port 8099] [--scenario attention]

Open http://127.0.0.1:8099/?scenario=busy to switch scenario (remembered in a
cookie).  Scenarios: attention, busy, clear, down.  Login token: "demo".
Binds to loopback only; never use it for anything but UI development.
"""
import argparse
import json
import os
import secrets
import time
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "arm_pipeline" / "dashboard" / "static"
TOKEN = "demo"
SCENARIOS = ("attention", "busy", "clear", "down")
SESSIONS = {}
RESOLVED = set()     # issue ids acted on, per process
T0 = time.time()
DEFAULT = os.environ.get("DASHBOARD_MOCK_SCENARIO", "attention")


def now():
    return time.time()


def jobs_for(scn):
    n = now()
    j = [
        dict(fp="fp-say1", label="SAYANYTHING", title="", state="awaiting_identity", message="Could not identify disc",
             created=n - 7300, updated=n - 7200, age_s=7200, identity=None, action="", target_folder="", existing="",
             needs_approval=False, warnings=[]),
        dict(fp="fp-say2", label="SAYANYTHING", title="", state="awaiting_identity", message="Duplicate of fp-say1",
             created=n - 3700, updated=n - 3600, age_s=3600, identity=None, action="", target_folder="", existing="",
             needs_approval=False, warnings=[]),
        dict(fp="fp-bad", label="DARK_<b>CRYSTAL</b>", title="The Dark Crystal", state="failed", message="Transfer failed: share unavailable",
             created=n - 90000, updated=n - 86400, age_s=86400, identity={"tmdb_id": 11}, action="rip",
             target_folder="Movies/The Dark Crystal (1982)", existing="", needs_approval=False, warnings=["low disc quality"]),
        dict(fp="fp-appr", label="BLADE_RUNNER", title="Blade Runner (1982)", state="awaiting_approval", message="Better copy exists",
             created=n - 1900, updated=n - 1800, age_s=1800, identity={"tmdb_id": 78}, action="replace",
             target_folder="Movies/Blade Runner (1982)", existing="Blade Runner (1982).mkv (4.1 GB)", needs_approval=True,
             warnings=["existing file is smaller"]),
        dict(fp="fp-ok", label="TOY_STORY", title="Toy Story (1995)", state="done", message="Imported", created=n - 200000,
             updated=n - 190000, age_s=190000, identity={"tmdb_id": 862}, action="rip", target_folder="Movies/Toy Story (1995)",
             existing="", needs_approval=False, warnings=[]),
    ]
    if scn == "busy":
        j.insert(0, dict(fp="fp-live", label="ALIENS", title="Aliens (1986)", state="ripping", message="Ripping title 1",
                         created=n - 900, updated=n - 3, age_s=900, identity={"tmdb_id": 679}, action="rip",
                         target_folder="Movies/Aliens (1986)", existing="", needs_approval=False, warnings=[]))
    if scn in ("clear", "down"):
        j = [x for x in j if x["state"] == "done"]
    return j


def issues_for(scn):
    n = now()
    out = []
    if scn == "attention":
        out = [
            dict(id="i-say1", severity="warning", title="Disc SAYANYTHING needs a title",
                 detail="The label has no word separators; search for the movie and resume.", fp="fp-say1", since=n - 7200,
                 actions=[dict(id="identify", label="Identify and resume", method="POST", path="/api/jobs/fp-say1/identify", body={}, confirm=False, needs_input="identify"),
                          dict(id="skip", label="Skip", method="POST", path="/api/jobs/fp-say1/skip", body={}, confirm=True),
                          dict(id="eject", label="Eject disc", method="POST", path="/api/drive/eject", body={}, confirm=True)]),
            dict(id="i-say2", severity="warning", title="Duplicate record: SAYANYTHING", detail="Same disc was recorded twice.",
                 fp="fp-say2", since=n - 3600,
                 actions=[dict(id="identify", label="Identify and resume", method="POST", path="/api/jobs/fp-say2/identify", body={}, confirm=False, needs_input="identify"),
                          dict(id="dismiss", label="Dismiss", method="POST", path="/api/jobs/fp-say2/dismiss", body={}, confirm=False)]),
            dict(id="i-bad", severity="critical", title="Failed: The Dark Crystal", detail="Transfer failed: share unavailable",
                 fp="fp-bad", since=n - 86400,
                 actions=[dict(id="retry", label="Retry", method="POST", path="/api/jobs/fp-bad/retry", body={}, confirm=False),
                          dict(id="dismiss", label="Dismiss", method="POST", path="/api/jobs/fp-bad/dismiss", body={}, confirm=True)]),
            dict(id="i-appr", severity="info", title="Approval needed: Blade Runner (1982)", detail="A copy already exists and is smaller.",
                 fp="fp-appr", since=n - 1800,
                 actions=[dict(id="approve", label="Approve replace", method="POST", path="/api/jobs/fp-appr/approve", body={}, confirm=True),
                          dict(id="reject", label="Reject", method="POST", path="/api/jobs/fp-appr/reject", body={}, confirm=False)]),
        ]
    elif scn == "down":
        out = [dict(id="i-share", severity="critical", title="Media share unreachable", detail="Transfers are paused until the share is back.",
                    fp="", since=n - 600, actions=[]),
               dict(id="i-svc", severity="critical", title="Ripper service is not running", detail="State: failed", fp="", since=n - 900, actions=[])]
    return [i for i in out if i["id"] not in RESOLVED]


def status_for(scn):
    n = now()
    down = scn == "down"
    cur = None
    if scn == "busy":
        el = (n - T0) % 600
        pct = 42 + (el / 600) * 10
        cur = dict(fp="fp-live", title="Aliens (1986)", state="ripping", phase="Ripping title 1", percent=round(pct, 1),
                   eta_s=int(1900 * (1 - pct / 100)), rate_mbps=38.4, done_gb=round(pct * .27, 1), total_gb=27.0, stale=False)
    counts = {"done": 1} if scn in ("clear", "down") else {"awaiting_identity": 2, "failed": 1, "awaiting_approval": 1, "done": 1}
    return dict(now=n, service=dict(active=not down, state="failed" if down else "active"),
                drive=dict(tray="closed", disc_present=scn in ("attention", "busy")),
                share=dict(ok=not down, checked=n - 20),
                scratch=dict(free_gb=412.5, total_gb=900.0, used_pct=54.2 if scn != "down" else 92.0, rips_gb=27.0 if scn == "busy" else 0),
                servers=dict(plex=dict(configured=True, reachable=not down), jellyfin=dict(configured=False, reachable=False),
                             tmdb=dict(configured=True), slack=dict(webhook=True, bot_approvals=scn == "attention")),
                current=cur, counts=counts, version="mock-0")


def events_for(scn):
    n = now()
    ev = [dict(ts=n - 7200, level="info", kind="disc", fp="fp-say1", text="Disc inserted: SAYANYTHING"),
          dict(ts=n - 7190, level="warning", kind="identify", fp="fp-say1", text="Could not identify SAYANYTHING (no separators in label)"),
          dict(ts=n - 86400, level="error", kind="transfer", fp="fp-bad", text="Transfer failed: share unavailable"),
          dict(ts=n - 190000, level="info", kind="import", fp="fp-ok", text="Imported Toy Story (1995)")]
    if scn == "busy":
        ev.append(dict(ts=n - 60, level="info", kind="rip", fp="fp-live", text="Ripping Aliens (1986): title 1"))
        ev.append(dict(ts=int(n // 5) * 5, level="info", kind="tick", fp="fp-live", text="Progress heartbeat"))
    if scn == "clear":
        ev = ev[-1:]
    return sorted(ev, key=lambda e: e["ts"])


TMDB = [
    dict(tmdb_id=2757, title="Say Anything...", year=1989, runtime=100, original_language="en",
         overview="A noble underachiever falls for the class valedictorian and wins over her skeptical father in this romantic comedy."),
    dict(tmdb_id=99999, title="Say Anything", year=2003, runtime=92, original_language="en", overview="An unrelated documentary short."),
    dict(tmdb_id=88888, title="Saying Nothing", year=1994, runtime=88, original_language="en", overview="Silence <script>alert(1)</script> speaks."),
]


def detail_for(scn, fp):
    rec = next((j for j in jobs_for(scn) if j["fp"] == fp), None)
    if not rec:
        return None
    rec = dict(rec)
    n = now()
    rec["history"] = [[n - 7300, "detected", "Disc inserted"], [n - 7200, rec["state"], rec["message"]]]
    rec["plan"] = dict(reasons=["Longest title is 99:59", "No existing file in library"], warnings=rec["warnings"])
    rec["scan"] = dict(disc_name=rec["label"], label=rec["label"], titles=[
        dict(index=1, duration_s=5999, size_gb=7.2, chapters=24, is_selected=True, video="MPEG2 720x480", audio_langs=["en"], sub_langs=["en", "es"]),
        dict(index=2, duration_s=312, size_gb=.4, chapters=2, is_selected=False, video="MPEG2", audio_langs=["en"], sub_langs=[]),
        dict(index=3, duration_s=1480, size_gb=1.1, chapters=6, is_selected=False, video="MPEG2", audio_langs=["en"], sub_langs=["en"])])
    rec["paths"] = {"staging": "/scratch/rips/" + fp}
    return rec


class H(BaseHTTPRequestHandler):
    server_version = "ARMMock/0"

    def log_message(self, *a):
        pass

    def _cookies(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return {k: v.value for k, v in c.items()}

    def _scn(self, q):
        s = (q.get("scenario") or [self._cookies().get("scenario", DEFAULT)])[0]
        return s if s in SCENARIOS else DEFAULT

    def _send(self, code, body, ctype="application/json", extra=()):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if os.environ.get("DASHBOARD_MOCK_NOAUTH") == "1":      # dev-only: skip the login to take screenshots
            return "mock-csrf"
        return SESSIONS.get(self._cookies().get("sid"))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        scn = self._scn(q)
        extra = [("Set-Cookie", "scenario=%s; Path=/; SameSite=Strict" % scn)] if "scenario" in q else []
        p = u.path
        if not p.startswith("/api/"):
            name = "index.html" if p == "/" else p.lstrip("/")
            f = (STATIC / name).resolve()
            if STATIC not in f.parents or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ct = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css"}.get(f.suffix, "application/octet-stream")
            return self._send(200, f.read_bytes(), ct, extra)
        if p == "/api/session":
            s = self._auth()
            return self._send(200, dict(authenticated=bool(s), **({"csrf": s} if s else {})), extra=extra)
        if not self._auth():
            return self._send(401, dict(ok=False, error="login required"))
        if p == "/api/status":
            return self._send(200, status_for(scn))
        if p == "/api/issues":
            return self._send(200, issues_for(scn))
        if p == "/api/jobs":
            return self._send(200, jobs_for(scn))
        if p.startswith("/api/jobs/"):
            d = detail_for(scn, urllib.parse.unquote(p[len("/api/jobs/"):]))
            return self._send(200, d) if d else self._send(404, dict(ok=False, error="no such job"))
        if p == "/api/events":
            since = float((q.get("since") or ["0"])[0] or 0)
            fp = (q.get("fp") or [""])[0]
            lim = int((q.get("limit") or ["100"])[0])
            ev = [e for e in events_for(scn) if e["ts"] >= since and (not fp or e["fp"] == fp)]
            return self._send(200, ev[-lim:])
        if p == "/api/tmdb/search":
            qq = (q.get("q") or [""])[0].lower().replace(" ", "")
            res = [r for r in TMDB if qq and qq[:3] in r["title"].lower().replace(" ", "")]
            return self._send(200, res)
        return self._send(404, dict(ok=False, error="not found"))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, dict(ok=False, error="bad json"))
        p = u.path
        if p == "/api/login":
            if body.get("token") == TOKEN:
                sid, csrf = secrets.token_hex(16), secrets.token_hex(16)
                SESSIONS[sid] = csrf
                return self._send(200, dict(ok=True, csrf=csrf), extra=[("Set-Cookie", "sid=%s; Path=/; HttpOnly; SameSite=Strict" % sid)])
            if body.get("token") == "throttle":
                return self._send(429, dict(ok=False, error="too many attempts"), extra=[("Retry-After", "30")])
            return self._send(401, dict(ok=False, error="bad token"))
        s = self._auth()
        if not s:
            return self._send(401, dict(ok=False, error="login required"))
        if self.headers.get("X-CSRF-Token") != s:
            return self._send(400, dict(ok=False, error="bad csrf"))
        if p == "/api/logout":
            SESSIONS.pop(self._cookies().get("sid"), None)
            return self._send(200, dict(ok=True))
        parts = p.strip("/").split("/")
        if p == "/api/drive/eject":
            return self._send(200, dict(ok=True, message="Eject requested (mock)"))
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] in ("approve", "reject", "retry", "skip", "dismiss", "identify"):
            fp, act = parts[2], parts[3]
            if act == "identify" and not str(body.get("title", "")).strip():
                return self._send(400, dict(ok=False, error="title is required"))
            for i in issues_for("attention"):
                if i["fp"] == fp:
                    RESOLVED.add(i["id"])
            msg = "%s ok for %s" % (act, fp)
            if act == "identify":
                msg = "Resuming as %s (%s)" % (body.get("title"), body.get("year"))
            return self._send(200, dict(ok=True, message=msg))
        return self._send(404, dict(ok=False, error="not found"))


def main():
    global DEFAULT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--scenario", default=DEFAULT, choices=SCENARIOS)
    a = ap.parse_args()
    DEFAULT = a.scenario
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print("mock dashboard on http://127.0.0.1:%d/  token: %s" % (a.port, TOKEN))
    srv.serve_forever()


if __name__ == "__main__":
    main()
