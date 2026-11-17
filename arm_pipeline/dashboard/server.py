"""HTTP server for the dashboard: JSON API + static files. Standard library only."""
from __future__ import annotations

import collections
import importlib
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .. import operator as ops
from ..ledger import Ledger
from .auth import COOKIE_NAME, Auth

log = logging.getLogger("arm_pipeline.dashboard")

MAX_BODY = 64 * 1024
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_CONNECTIONS = 64
MAX_CONNECTIONS_PER_IP = 8
HEADER_TIMEOUT = 3        # reading the request line + headers
SOCKET_TIMEOUT = 10       # reading the body, once the headers parsed
TMDB_RATE = 30            # requests per minute, global
TMDB_CACHE_TTL = 60
STATIC_FILES = frozenset({"index.html", "app.js", "style.css", "favicon.svg", "favicon.ico",
                          "icon.svg", "manifest.webmanifest"})
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml",
    ".png": "image/png", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}
_FP = r"([^/]+)"


class _HttpError(Exception):
    def __init__(self, status, message, headers=None):
        super().__init__(message)
        self.status, self.message, self.headers = status, message, headers or {}


def _bare_host(host_header: str) -> str:
    """Host header without its port (IPv6 literals keep their brackets stripped)."""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:].split("]", 1)[0]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _port_ok(host_header: str) -> bool:
    """If the Host header carries a port it must be plain digits <= 65535."""
    h = (host_header or "").strip()
    if h.startswith("["):
        rest = h.split("]", 1)[1] if "]" in h else None
        if rest is None:
            return False
        port = rest[1:] if rest.startswith(":") else (None if rest == "" else False)
    elif h.count(":") == 1:
        port = h.split(":", 1)[1]
    elif h.count(":") == 0:
        port = None
    else:
        return False
    if port is None:
        return True
    return port is not False and port.isascii() and port.isdigit() and int(port) <= 65535


def _is_any_bind(addr: str) -> bool:
    return addr in ("", "0.0.0.0", "::", "0:0:0:0:0:0:0:0")


def _ip_allowed(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.is_loopback or a.is_link_local:
        return True
    nets = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
    return any(a in ipaddress.ip_network(n) for n in nets)


def _ip_in_list(ip: str, spec: str) -> bool:
    """True if `ip` is loopback or matches one of the comma-separated IPs/CIDRs in `spec` (bad entries are ignored)."""
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.is_loopback:
        return True
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            if a in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, cfg, address=None, data=None, auth=None, ledger=None, static_dir=None,
                 drive_mod=None, events=None, clock=time.monotonic):
        self.cfg = cfg
        bind = cfg.dashboard_bind if address is None else address[0]
        if _is_any_bind(bind) and not cfg.dashboard_allow_any_bind:
            raise ValueError("refusing to bind to all interfaces; set ARM_DASHBOARD_BIND to a specific "
                             "address or ARM_DASHBOARD_ALLOW_ANY_BIND=1")
        self.auth = auth or Auth(cfg.dashboard_token, secure_cookie=cfg.dashboard_secure_cookie)
        self._data = data
        self.ledger = ledger or Ledger(cfg.state_path)
        self.static_dir = os.path.realpath(static_dir or STATIC_DIR)
        self.drive_mod = drive_mod
        self._events = events
        self.static_whitelist = frozenset()
        self._clock = clock
        hosts = {h.strip().lower() for h in (cfg.dashboard_allowed_hosts or "").split(",") if h.strip()}
        if not hosts:
            hosts = {"127.0.0.1", "localhost"}
            if not _is_any_bind(bind):
                hosts.add(bind.lower())
        self.allowed_hosts = hosts
        self._conn_sem = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._conn_lock = threading.Lock()
        self._conn_ips = collections.Counter()
        self._tmdb_lock = threading.Lock()
        self._tmdb_times = collections.deque()
        self._tmdb_cache = collections.OrderedDict()
        if address is None:
            address = (cfg.dashboard_bind, cfg.dashboard_port)
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)

    def data_module(self):
        return self._data if self._data is not None else importlib.import_module("arm_pipeline.dashboard.data")

    # connection caps (global + per client IP): over the limit => close immediately
    def process_request(self, request, client_address):
        ip = client_address[0]
        with self._conn_lock:
            if self._conn_ips[ip] >= MAX_CONNECTIONS_PER_IP:
                over = True
            else:
                over = False
                self._conn_ips[ip] += 1
        if over or not self._conn_sem.acquire(blocking=False):
            if not over:
                self._release_ip(ip)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(ip)
            raise

    def _release_ip(self, ip):
        with self._conn_lock:
            self._conn_ips[ip] -= 1
            if self._conn_ips[ip] <= 0:
                del self._conn_ips[ip]

    def _release(self, ip):
        self._conn_sem.release()
        self._release_ip(ip)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address[0])

    def audit(self, action, fp, ok, ip, text=None, **data):
        """One 'operator' event per action; never receives tokens or CSRF values."""
        result = "ok" if ok else "refused"
        msg = text or f"{action} {result}"
        try:
            ev = self._events
            if ev is None:
                from ..events import EventLog
                ev = self._events = EventLog(self.cfg.state_path)
            ev.emit("operator", msg, fp=fp, level="info" if ok else "warning",
                    who="dashboard", action=action, result=result, ip=ip, **data)
        except Exception:  # noqa: BLE001 - auditing must not break the request
            log.info("audit %s fp=%s result=%s ip=%s", action, fp, result, ip)

    def tmdb_search(self, text, year):
        """Rate limited (global) + cached TMDb proxy. Returns (status, payload)."""
        now = self._clock()
        key = (text.lower(), year)
        with self._tmdb_lock:
            hit = self._tmdb_cache.get(key)
            if hit and now - hit[0] < TMDB_CACHE_TTL:
                return 200, hit[1]
            while self._tmdb_times and now - self._tmdb_times[0] >= 60:
                self._tmdb_times.popleft()
            if len(self._tmdb_times) >= TMDB_RATE:
                return 429, None
            self._tmdb_times.append(now)
        try:
            res = self.data_module().tmdb_search(self.cfg, text, year=year)
        except Exception as e:  # noqa: BLE001 - never leak upstream URLs
            log.warning("tmdb lookup failed: %s", type(e).__name__)
            return 502, None
        with self._tmdb_lock:
            self._tmdb_cache[key] = (now, res)
            while len(self._tmdb_cache) > 100:
                self._tmdb_cache.popitem(last=False)
        return 200, res


class Handler(BaseHTTPRequestHandler):
    server_version = ""
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = HEADER_TIMEOUT

    # ---- logging: never include query strings, headers or cookies
    def log_message(self, fmt, *args):
        pass

    def log_request(self, code="-", size="-"):
        path = urlsplit(self.path).path if hasattr(self, "path") else "-"
        log.info("%s %s %s %s", self.client_address[0], getattr(self, "command", "-"), path, code)

    def log_error(self, fmt, *args):
        log.info("%s request error: %s", self.client_address[0], fmt % args if args else fmt)

    # ---- low-level responses
    def _send(self, status, body=b"", ctype="application/json", headers=None, api=True):
        self.log_request(status)
        self.send_response_only(status)           # no Server banner
        self.send_header("Date", self.date_time_string())
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", PERMISSIONS_POLICY)
        if api:
            self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj, headers=None):
        self._send(status, json.dumps(obj, default=str).encode("utf-8"), headers=headers)

    def _error(self, status, message, headers=None):
        self._json(status, {"ok": False, "error": message}, headers)

    # ---- request helpers
    def _client_ip(self):
        return self.client_address[0]

    def _check_host(self):
        host = self.headers.get("Host", "")
        if not host or not _port_ok(host) or _bare_host(host) not in self.server.allowed_hosts:
            raise _HttpError(421, "unrecognised Host header")
        spec = (getattr(self.server.cfg, "dashboard_allowed_clients", "") or "").strip()
        if spec:      # an explicit client list wins: only those addresses (plus loopback) may connect
            if not _ip_in_list(self._client_ip(), spec):
                raise _HttpError(403, "client address not permitted")
        elif not self.server.cfg.dashboard_allow_public and not _ip_allowed(self._client_ip()):
            raise _HttpError(403, "client address not permitted")

    def _sid(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME:
                return v
        return None

    def _require_auth(self):
        sid = self._sid()
        if self.server.auth.session_csrf(sid) is None:
            raise _HttpError(401, "authentication required")
        return sid

    def _check_origin(self):
        """POST only: Origin is REQUIRED and must be scheme://Host of an allowed host."""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if not origin or origin == "null":
            raise _HttpError(403, "missing or invalid Origin")
        parts = urlsplit(origin)
        if (parts.scheme not in ("http", "https") or parts.netloc.lower() != host.lower()
                or _bare_host(host) not in self.server.allowed_hosts or parts.path not in ("", "/")):
            raise _HttpError(403, "origin mismatch")
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs.lower() not in ("same-origin", "none"):
            raise _HttpError(403, "cross-site request refused")

    def _read_json(self):
        if self.headers.get("Transfer-Encoding") is not None:
            raise _HttpError(400, "Transfer-Encoding is not supported")
        cls = self.headers.get_all("Content-Length") or []
        if not cls:
            raise _HttpError(411, "Content-Length required")
        if len(cls) > 1:
            raise _HttpError(400, "duplicate Content-Length")
        if not re.fullmatch(r"[0-9]{1,10}", cls[0]):
            raise _HttpError(400, "bad Content-Length")
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self.close_connection = True
            raise _HttpError(415, "content type must be application/json")
        try:
            n = int(cls[0])
        except ValueError:
            self.close_connection = True
            raise _HttpError(400, "bad Content-Length")
        if n < 0:
            raise _HttpError(400, "bad Content-Length")
        if n > MAX_BODY:
            self.close_connection = True
            raise _HttpError(413, "request body too large")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _HttpError(400, "invalid JSON")
        if not isinstance(body, dict):
            raise _HttpError(400, "JSON body must be an object")
        return body

    @staticmethod
    def _result(res):
        if res.get("ok"):
            return 200, res
        out = dict(res)
        out["ok"] = False
        out["error"] = res.get("message", "refused")
        return 400, out

    # ---- routing
    def _routes(self):
        # (method, regex, handler, needs_auth, needs_csrf)
        return [
            ("POST", r"/api/login", self.h_login, False, False),
            ("GET", r"/api/session", self.h_session, False, False),
            ("POST", r"/api/logout", self.h_logout, False, False),
            ("GET", r"/api/status", self.h_status, True, False),
            ("GET", r"/api/issues", self.h_issues, True, False),
            ("GET", r"/api/jobs", self.h_jobs, True, False),
            ("GET", r"/api/jobs/" + _FP, self.h_job, True, False),
            ("GET", r"/api/events", self.h_events, True, False),
            ("GET", r"/api/tmdb/search", self.h_tmdb, True, False),
            ("POST", r"/api/jobs/" + _FP + r"/(approve|reject|retry|skip|dismiss)", self.h_action, True, True),
            ("POST", r"/api/jobs/" + _FP + r"/identify", self.h_identify, True, True),
            ("POST", r"/api/drive/eject", self.h_eject, True, True),
        ]

    def _dispatch(self, method):
        parts = urlsplit(self.path)
        path = parts.path
        try:
            self.connection.settimeout(SOCKET_TIMEOUT)     # headers parsed; allow the body more time
            self._check_host()
            if method == "GET" and (path == "/" or path.startswith("/static/")):
                return self._static(path)
            if not path.startswith("/api/"):
                raise _HttpError(404, "not found")
            matched_path = False
            for m, rx, handler, need_auth, need_csrf in self._routes():
                mo = re.fullmatch(rx, path)
                if not mo:
                    continue
                matched_path = True
                if m != method:
                    continue
                if method == "POST":
                    self._check_origin()
                sid = self._require_auth() if need_auth else None
                if need_csrf:
                    if not self.server.auth.check_csrf(sid, self.headers.get("X-CSRF-Token")):
                        raise _HttpError(403, "missing or invalid CSRF token")
                body = self._read_json() if method == "POST" else None
                return handler(mo.groups(), parse_qs(parts.query), body, sid)
            if matched_path:
                raise _HttpError(405, "method not allowed", {"Allow": "GET, POST"})
            raise _HttpError(404, "not found")
        except _HttpError as e:
            if self.command == "POST" and e.status in (401, 403, 404, 405):
                self.close_connection = True
            self._error(e.status, e.message, e.headers)
        except (TimeoutError, ConnectionError):
            self.close_connection = True
        except Exception:
            log.exception("unhandled error serving %s %s", method, path)
            self._error(500, "internal error")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_HEAD(self):
        self._send(405, b"", headers={"Allow": "GET, POST"}, api=False)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD

    # ---- static files
    def _static(self, path):
        rel = "index.html" if path == "/" else unquote(path[len("/static/"):])
        if rel not in STATIC_FILES and rel not in self.server.static_whitelist:
            raise _HttpError(404, "not found")
        root = self.server.static_dir
        full = os.path.join(root, rel)
        # refuse symlinks (the file itself or any directory in the way)
        if os.path.realpath(full) != full or os.path.commonpath([root, full]) != root:
            raise _HttpError(404, "not found")
        if not os.path.isfile(full):
            raise _HttpError(404, "not found")
        ctype = CONTENT_TYPES.get(os.path.splitext(full)[1].lower())
        if ctype is None:
            raise _HttpError(404, "not found")
        with open(full, "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype=ctype, headers={"Cache-Control": "no-cache"}, api=False)

    # ---- handlers: auth
    def h_login(self, g, q, body, sid):
        ip = self._client_ip()
        auth = self.server.auth
        new_sid, info = auth.login(ip, body.get("token"))
        if new_sid is None:
            n = auth.fail_count(ip)
            log.warning("login failed from %s (failures from this client: %s)", ip, n)
            self.server.audit("login", None, False, ip, text="login failed", failures=n)
            if info:
                raise _HttpError(429, "too many failed attempts; try again later", {"Retry-After": str(info)})
            raise _HttpError(401, "invalid token")
        self.server.audit("login", None, True, ip, text="login ok")
        self._json(200, {"ok": True, "csrf": info}, headers={"Set-Cookie": auth.cookie_header(new_sid)})

    def h_session(self, g, q, body, sid):
        csrf = self.server.auth.session_csrf(self._sid())
        self._json(200, {"authenticated": True, "csrf": csrf} if csrf else {"authenticated": False})

    def h_logout(self, g, q, body, sid):
        self.server.auth.logout(self._sid())
        self._json(200, {"ok": True}, headers={"Set-Cookie": self.server.auth.clear_cookie_header()})

    # ---- handlers: data
    def h_status(self, g, q, body, sid):
        self._json(200, self.server.data_module().collect_status(self.server.cfg))

    def h_issues(self, g, q, body, sid):
        d = self.server.data_module()
        cfg = self.server.cfg
        status = d.collect_status(cfg)
        jobs = d.list_jobs(cfg)
        self._json(200, d.collect_issues(cfg, status=status, jobs=jobs))

    def h_jobs(self, g, q, body, sid):
        self._json(200, self.server.data_module().list_jobs(self.server.cfg))

    def h_job(self, g, q, body, sid):
        fp = g[0]
        if not ops.valid_fp(fp):
            raise _HttpError(400, "that is not a disc fingerprint (16 hex characters)")
        detail = self.server.data_module().job_detail(self.server.cfg, fp)
        if detail is None:
            raise _HttpError(404, "unknown job")
        self._json(200, detail)

    @staticmethod
    def _qs(q, name, default=None):
        v = q.get(name)
        return v[0] if v else default

    def h_events(self, g, q, body, sid):
        try:
            since = float(self._qs(q, "since", "0"))
            limit = max(1, min(500, int(self._qs(q, "limit", "100"))))
        except ValueError:
            raise _HttpError(400, "since must be a number and limit an integer")
        fp = self._qs(q, "fp")
        if fp is not None and not ops.valid_fp(fp):
            raise _HttpError(400, "that is not a disc fingerprint (16 hex characters)")
        self._json(200, self.server.data_module().tail_events(self.server.cfg, since=since, limit=limit, fp=fp))

    def h_tmdb(self, g, q, body, sid):
        text = " ".join((self._qs(q, "q", "") or "").split())[:100]
        if not text:
            raise _HttpError(400, "q is required")
        year = self._qs(q, "year")
        if year in (None, ""):
            year = None
        elif not (year.isascii() and year.isdigit() and len(year) == 4):
            raise _HttpError(400, "year must be four digits")
        else:
            year = int(year)
        status, res = self.server.tmdb_search(text, year)
        if status == 429:
            raise _HttpError(429, "search rate limit reached; try again shortly", {"Retry-After": "10"})
        if status != 200:
            raise _HttpError(502, "title lookup failed")
        self._json(200, res)

    # ---- handlers: operator actions
    def _fp_or_400(self, fp):
        if not ops.valid_fp(fp):
            raise _HttpError(400, "that is not a disc fingerprint (16 hex characters)")

    def h_action(self, g, q, body, sid):
        fp, action = g
        self._fp_or_400(fp)
        s = self.server
        if action == "skip":
            res = ops.skip(s.cfg, s.ledger, fp, drive_mod=s.drive_mod)
        else:
            res = getattr(ops, action)(s.cfg, s.ledger, fp)
        self.server.audit(action, fp, res.get("ok"), self._client_ip(), text=f"{action}: {res.get('message', '')}")
        status, out = self._result(res)
        self._json(status, out)

    def h_identify(self, g, q, body, sid):
        fp = g[0]
        self._fp_or_400(fp)
        s = self.server
        res = ops.identify(s.cfg, s.ledger, fp, body.get("title"), body.get("year"), body.get("tmdb_id"))
        self.server.audit("identify", fp, res.get("ok"), self._client_ip(),
                          text=f"identify: {res.get('message', '')}")
        status, out = self._result(res)
        self._json(status, out)

    def h_eject(self, g, q, body, sid):
        s = self.server
        res = ops.eject_now(s.cfg, s.ledger, drive_mod=s.drive_mod)
        self.server.audit("eject", None, res.get("ok"), self._client_ip(), text=f"eject: {res.get('message', '')}")
        status, out = self._result(res)
        self._json(status, out)


def serve(cfg, data=None) -> None:
    """Run the dashboard until SIGTERM/SIGINT. Raises ValueError if the token is missing/short."""
    httpd = DashboardServer(cfg, data=data)
    stop = threading.Event()

    def _stop(signum, frame):
        if not stop.is_set():
            stop.set()
            threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("dashboard listening on %s:%s", *httpd.server_address[:2])
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        log.info("dashboard stopped")
