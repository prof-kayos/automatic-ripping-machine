import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace as NS

from arm_pipeline import operator as ops
from arm_pipeline.config import Config
from arm_pipeline.dashboard import auth as auth_mod
from arm_pipeline.dashboard import server as srv
from arm_pipeline.dashboard.auth import Auth
from arm_pipeline.events import EventLog
from arm_pipeline.ledger import Ledger

TOKEN = "t" * 40
FP = "0123456789abcdef"


class FakeData:
    def __init__(self):
        self.tmdb_calls = 0

    def collect_status(self, cfg, now=None):
        return {"daemon": "up"}

    def list_jobs(self, cfg, limit=200):
        return [{"fp": FP}]

    def job_detail(self, cfg, fp):
        return {"fp": fp} if fp == FP else None

    def collect_issues(self, cfg, status=None, jobs=None):
        return [{"issue": "x", "status": status, "jobs": len(jobs)}]

    def tail_events(self, cfg, since=0.0, limit=100, fp=None):
        return [{"since": since, "limit": limit, "fp": fp}]

    def tmdb_search(self, cfg, q, year=None):
        self.tmdb_calls += 1
        return [{"q": q, "year": year}]


class FakeDrive:
    def __init__(self):
        self.ejects = 0

    def disc_present(self, dev):
        return True

    def eject(self, cfg):
        self.ejects += 1
        return True, "TRAY_OPEN"


class Base(unittest.TestCase):
    def make_cfg(self, **kw):
        return replace(Config(state_dir=os.path.join(self.td.name, "state"), dashboard_token=TOKEN,
                              dashboard_port=0), **kw)

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.static = os.path.join(self.td.name, "static")
        os.makedirs(self.static)
        for n, c in (("index.html", "<html>hi</html>"), ("app.js", "//js"), ("style.css", "a{}"),
                     (".secret", "s"), ("other.txt", "o")):
            with open(os.path.join(self.static, n), "w") as f:
                f.write(c)
        with open(os.path.join(self.td.name, "config.py"), "w") as f:
            f.write("SECRET")
        self.data = FakeData()
        self.drive = FakeDrive()
        self.start()

    def start(self, **cfgkw):
        self.cfg = self.make_cfg(**cfgkw)
        self.ledger = Ledger(self.cfg.state_path)
        self.httpd = srv.DashboardServer(self.cfg, address=("127.0.0.1", 0), data=self.data,
                                         static_dir=self.static, drive_mod=self.drive)
        self.port = self.httpd.server_address[1]
        t = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        t.start()
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close()))

    def req(self, method, path, body=None, headers=None, raw=None, origin=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        if method == "POST":
            if origin and "Origin" not in h:
                h["Origin"] = f"http://127.0.0.1:{self.port}"
            if raw is None and "Content-Type" not in h:
                h["Content-Type"] = "application/json"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else
                                            (b"{}" if method == "POST" else None))
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        b = r.read()
        c.close()
        return r, b

    def jreq(self, *a, **k):
        r, b = self.req(*a, **k)
        try:
            return r.status, json.loads(b), r
        except ValueError:
            return r.status, b, r

    def login(self, token=TOKEN):
        st, js, r = self.jreq("POST", "/api/login", {"token": token})
        if st != 200:
            return None
        cookie = r.getheader("Set-Cookie").split(";")[0]
        return {"Cookie": cookie, "X-CSRF-Token": js["csrf"]}

    def put(self, state, fp=FP):
        self.ledger.set_state(fp, state, "m")


class AuthTests(Base):
    def test_login_success_cookie_flags(self):
        st, js, r = self.jreq("POST", "/api/login", {"token": TOKEN})
        self.assertEqual(st, 200)
        self.assertTrue(js["ok"] and js["csrf"])
        ck = r.getheader("Set-Cookie")
        for part in ("arm_session=", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(part, ck)
        self.assertNotIn("Secure", ck)

    def test_secure_cookie(self):
        a = Auth(TOKEN, secure_cookie=True)
        self.assertIn("Secure", a.cookie_header("x"))

    def test_login_failure_and_lockout(self):
        codes = [self.jreq("POST", "/api/login", {"token": "wrong"})[0] for _ in range(5)]
        self.assertEqual(codes, [401, 401, 401, 401, 429])      # the 5th failure starts the lockout
        st, js, r = self.jreq("POST", "/api/login", {"token": TOKEN})   # right token, but locked
        self.assertEqual(st, 429)
        self.assertTrue(r.getheader("Retry-After"))

    def test_session_endpoint_and_logout(self):
        st, js, _ = self.jreq("GET", "/api/session")
        self.assertEqual(js, {"authenticated": False})
        h = self.login()
        st, js, _ = self.jreq("GET", "/api/session", headers=h)
        self.assertTrue(js["authenticated"])
        self.assertEqual(js["csrf"], h["X-CSRF-Token"])
        st, js, _ = self.jreq("POST", "/api/logout", headers=h)
        self.assertEqual(st, 200)
        st, js, _ = self.jreq("GET", "/api/status", headers=h)
        self.assertEqual(st, 401)

    def test_each_login_new_session(self):
        a, b = self.login(), self.login()
        self.assertNotEqual(a["Cookie"], b["Cookie"])

    def test_session_required_for_get(self):
        for p in ("/api/status", "/api/issues", "/api/jobs", f"/api/jobs/{FP}", "/api/events",
                  "/api/tmdb/search?q=x"):
            st, js, _ = self.jreq("GET", p)
            self.assertEqual(st, 401, p)
            self.assertFalse(js["ok"])

    def test_authed_gets(self):
        h = self.login()
        self.assertEqual(self.jreq("GET", "/api/status", headers=h)[1], {"daemon": "up"})
        self.assertEqual(self.jreq("GET", "/api/jobs", headers=h)[1], [{"fp": FP}])
        self.assertEqual(self.jreq("GET", f"/api/jobs/{FP}", headers=h)[1], {"fp": FP})
        self.assertEqual(self.jreq("GET", "/api/jobs/fedcba9876543210", headers=h)[0], 404)
        self.assertEqual(self.jreq("GET", "/api/jobs/..%2Fx", headers=h)[0], 400)
        self.assertEqual(self.jreq("GET", "/api/issues", headers=h)[1][0]["jobs"], 1)
        ev = self.jreq("GET", f"/api/events?since=2&limit=9999&fp={FP}", headers=h)[1]
        self.assertEqual(ev, [{"since": 2.0, "limit": 500, "fp": FP}])
        self.assertEqual(self.jreq("GET", "/api/events?since=abc", headers=h)[0], 400)
        self.assertEqual(self.jreq("GET", "/api/events?fp=zz", headers=h)[0], 400)

    def test_csrf_required(self):
        h = self.login()
        self.put("awaiting_approval")
        for hh in ({"Cookie": h["Cookie"]}, {"Cookie": h["Cookie"], "X-CSRF-Token": "bad"}):
            st, js, _ = self.jreq("POST", f"/api/jobs/{FP}/approve", headers=hh)
            self.assertEqual(st, 403)
        self.assertFalse(os.path.exists(os.path.join(self.cfg.state_path, "approve", FP)))
        st, js, _ = self.jreq("POST", f"/api/jobs/{FP}/approve", headers=h)    # no session
        self.assertEqual(self.jreq("POST", f"/api/jobs/{FP}/approve")[0], 401)
        self.assertEqual(st, 200)

    def test_global_backoff_capped_and_owner_recovers(self):
        now = [0.0]
        a = Auth(TOKEN, clock=lambda: now[0])
        waits = []
        for i in range(auth_mod.GLOBAL_FREE_FAILURES + 20):          # failures from many different IPs
            sid, w = a.login(f"10.0.{i}.1", "bad")
            self.assertIsNone(sid)
            waits.append(w)
        self.assertTrue(all(w <= auth_mod.GLOBAL_MAX_BACKOFF for w in waits), waits)
        self.assertEqual(auth_mod.GLOBAL_MAX_BACKOFF, 30)
        self.assertEqual(waits[auth_mod.GLOBAL_FREE_FAILURES - 2], 0)   # no global delay before 25 failures
        self.assertGreater(waits[-1], 0)
        sid, left = a.login("10.200.0.1", TOKEN)       # correct token does NOT bypass the delay
        self.assertIsNone(sid)
        self.assertLessEqual(left, 30)
        now[0] += 31                                   # after the delay a clean IP gets in
        sid, _ = a.login("10.200.0.1", TOKEN)
        self.assertIsNotNone(sid)

    def test_two_ips_cannot_lock_out_owner(self):
        now = [0.0]
        a = Auth(TOKEN, clock=lambda: now[0])
        for ip in ("192.0.2.1", "192.0.2.2"):
            for _ in range(5):
                a.login(ip, "bad")
        sid, _ = a.login("192.0.2.3", TOKEN)
        self.assertIsNotNone(sid)

    def test_global_window_expires(self):
        now = [0.0]
        a = Auth(TOKEN, clock=lambda: now[0])
        for i in range(30):
            a.login(f"10.0.{i}.1", "bad")
        now[0] += auth_mod.GLOBAL_WINDOW + 40
        self.assertIsNotNone(a.login("10.9.9.9", TOKEN)[0])

    def test_ip_table_capped(self):
        a = Auth(TOKEN)
        for i in range(auth_mod.MAX_TRACKED_IPS + 50):
            a.login(f"h{i}", "bad")
        self.assertLessEqual(len(a._fails), auth_mod.MAX_TRACKED_IPS)

    def test_session_cap_and_ttl(self):
        now = [0.0]
        a = Auth(TOKEN, clock=lambda: now[0])
        sids = [a.login("1.1.1.1", TOKEN)[0] for _ in range(auth_mod.MAX_SESSIONS + 3)]
        self.assertEqual(len(a._sessions), auth_mod.MAX_SESSIONS)
        self.assertIsNone(a.session_csrf(sids[0]))
        now[0] += auth_mod.SESSION_TTL + 1
        self.assertIsNone(a.session_csrf(sids[-1]))

    def test_short_token_refused(self):
        with self.assertRaises(ValueError):
            Auth("x" * 31)
        Auth("x" * 32)
        with self.assertRaises(ValueError):
            srv.DashboardServer(self.make_cfg(dashboard_token="short"), address=("127.0.0.1", 0))


class HostOriginTests(Base):
    def test_wrong_host_rejected_everywhere(self):
        for path in ("/", "/api/session", "/static/app.js"):
            r, _ = self.req("GET", path, headers={"Host": "evil.example"})
            self.assertEqual(r.status, 421, path)
        r, _ = self.req("POST", "/api/login", {"token": TOKEN}, headers={"Host": "evil.example",
                                                                      "Origin": "http://evil.example"})
        self.assertEqual(r.status, 421)

    def test_localhost_host_ok(self):
        r, _ = self.req("GET", "/api/session", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(r.status, 200)

    def test_missing_origin_rejected(self):
        h = self.login()
        r, _ = self.req("POST", "/api/login", {"token": TOKEN}, origin=False)
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/api/logout", headers=h, origin=False)
        self.assertEqual(r.status, 403)

    def test_null_and_mismatched_origin_rejected(self):
        h = self.login()
        for o in ("null", "http://evil.example", f"http://localhost:{self.port}",
                  f"ftp://127.0.0.1:{self.port}", f"http://127.0.0.1:{self.port}/x"):
            hh = dict(h, Origin=o)
            r, _ = self.req("POST", f"/api/jobs/{FP}/approve", headers=hh)
            self.assertEqual(r.status, 403, o)

    def test_sec_fetch_site_cross_site_rejected_but_not_a_substitute(self):
        h = self.login()
        r, _ = self.req("POST", "/api/logout", headers=dict(h, **{"Sec-Fetch-Site": "cross-site"}))
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/api/logout", headers=dict(h, **{"Sec-Fetch-Site": "same-origin"}),
                        origin=False)
        self.assertEqual(r.status, 403)       # Origin still required
        r, _ = self.req("POST", "/api/logout", headers=dict(h, **{"Sec-Fetch-Site": "same-origin"}))
        self.assertEqual(r.status, 200)

    def test_source_ip_policy(self):
        for ip in ("127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.1", "169.254.1.1", "::1",
                   "fd12::1", "fe80::1", "::ffff:192.168.1.5"):
            self.assertTrue(srv._ip_allowed(ip), ip)
        for ip in ("8.8.8.8", "172.32.0.1", "2001:db8::1", "garbage", "::ffff:8.8.8.8"):
            self.assertFalse(srv._ip_allowed(ip), ip)

    def test_bind_policy(self):
        self.assertEqual(Config().dashboard_bind, "127.0.0.1")
        self.assertEqual(Config.from_env({}).dashboard_bind, "127.0.0.1")
        for b in ("0.0.0.0", "::"):
            with self.assertRaises(ValueError):
                srv.DashboardServer(self.make_cfg(dashboard_bind=b))
        s = srv.DashboardServer(self.make_cfg(dashboard_bind="0.0.0.0", dashboard_allow_any_bind=True,
                                              dashboard_allowed_hosts="arm.lan, ARM2"))
        self.addCleanup(s.server_close)
        self.assertEqual(s.allowed_hosts, {"arm.lan", "arm2"})

    def test_default_allowed_hosts_include_bind(self):
        s = srv.DashboardServer(self.make_cfg(), address=("127.0.0.1", 0))
        self.addCleanup(s.server_close)
        self.assertEqual(s.allowed_hosts, {"127.0.0.1", "localhost"})


class HardeningTests(Base):
    def test_security_headers_everywhere(self):
        for path in ("/", "/static/app.js", "/api/session", "/api/status", "/nope"):
            r, _ = self.req("GET", path)
            self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff", path)
            self.assertEqual(r.getheader("Referrer-Policy"), "no-referrer")
            self.assertEqual(r.getheader("Content-Security-Policy"), srv.CSP)
            self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))
            self.assertIn("camera=()", r.getheader("Permissions-Policy"))
            self.assertEqual(r.getheader("Connection"), "close")
            self.assertIsNone(r.getheader("Server"))
        r, _ = self.req("GET", "/api/session")
        self.assertEqual(r.getheader("Cache-Control"), "no-store")

    def test_static_serving(self):
        r, b = self.req("GET", "/")
        self.assertEqual((r.status, b), (200, b"<html>hi</html>"))
        self.assertTrue(r.getheader("Content-Type").startswith("text/html"))
        r, b = self.req("GET", "/static/app.js")
        self.assertTrue(r.getheader("Content-Type").startswith("text/javascript"))
        r, b = self.req("GET", "/static/style.css")
        self.assertTrue(r.getheader("Content-Type").startswith("text/css"))

    def test_static_refusals(self):
        for p in ("/static/../config.py", "/static/%2e%2e/config.py", "/static/%2e%2e%2fconfig.py",
                  "/static//etc/passwd", "/static/", "/static", "/static/.secret", "/static/other.txt",
                  "/static/..%5cconfig.py", "/static/app.js%00", "/static/nope.js", "/static/./app.js/",
                  "/static/%252e%252e/config.py"):
            r, b = self.req("GET", p)
            self.assertEqual(r.status, 404, p)
            self.assertNotIn(b"SECRET", b)

    def test_static_symlink_refused(self):
        os.remove(os.path.join(self.static, "style.css"))
        os.symlink(os.path.join(self.td.name, "config.py"), os.path.join(self.static, "style.css"))
        r, b = self.req("GET", "/static/style.css")
        self.assertEqual(r.status, 404)
        self.assertNotIn(b"SECRET", b)

    def test_oversize_body_and_content_type(self):
        r, _ = self.req("POST", "/api/login", raw=b"{" + b" " * 70000 + b"}",
                        headers={"Content-Type": "application/json"})
        self.assertEqual(r.status, 413)
        r, _ = self.req("POST", "/api/login", raw=b'{"token":"x"}', headers={"Content-Type": "text/plain"})
        self.assertEqual(r.status, 415)
        r, _ = self.req("POST", "/api/login", raw=b"not json", headers={"Content-Type": "application/json"})
        self.assertEqual(r.status, 400)
        r, _ = self.req("POST", "/api/login", raw=b"[1]", headers={"Content-Type": "application/json"})
        self.assertEqual(r.status, 400)

    def raw_exchange(self, data):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(data)
        out = b""
        try:
            while True:
                c = s.recv(4096)
                if not c:
                    break
                out += c
        except socket.timeout:
            pass
        s.close()
        return out

    def test_transfer_encoding_and_missing_length_rejected(self):
        base = (f"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                f"Origin: http://127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n")
        out = self.raw_exchange((base + "Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n").encode())
        self.assertIn(b" 400 ", out.split(b"\r\n")[0])
        out = self.raw_exchange((base + "\r\n").encode())
        self.assertIn(b" 411 ", out.split(b"\r\n")[0])

    def test_unknown_routes_and_methods(self):
        h = self.login()
        self.assertEqual(self.req("GET", "/api/nope", headers=h)[0].status, 404)
        self.assertEqual(self.req("GET", "/elsewhere")[0].status, 404)
        self.assertEqual(self.req("GET", "/api/login")[0].status, 405)
        self.assertEqual(self.req("POST", "/api/status", headers=h)[0].status, 405)
        self.assertEqual(self.req("GET", f"/api/jobs/{FP}/approve", headers=h)[0].status, 405)
        self.assertEqual(self.req("PUT", "/api/status", headers=h, raw=b"{}")[0].status, 405)
        self.assertEqual(self.req("DELETE", "/")[0].status, 405)

    def test_connection_cap(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        old = srv.MAX_CONNECTIONS
        srv.MAX_CONNECTIONS = 2
        self.addCleanup(setattr, srv, "MAX_CONNECTIONS", old)
        self.start()
        held = [socket.create_connection(("127.0.0.1", self.port)) for _ in range(2)]
        time.sleep(0.3)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        try:
            s.sendall(f"GET /api/session HTTP/1.0\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode())
            self.assertEqual(s.recv(100), b"")          # closed without a response
        except ConnectionResetError:
            pass
        finally:
            s.close()
            for h in held:
                h.close()
        time.sleep(0.3)
        self.assertEqual(self.req("GET", "/api/session")[0].status, 200)   # slots released

    def test_slowloris_per_ip_cap_legit_client_survives(self):
        socks = []
        for _ in range(40):
            try:
                c = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                c.sendall(b"GET /api/sess")                      # partial request line, never finished
                socks.append(c)
            except OSError:
                pass
        time.sleep(0.3)
        self.assertLessEqual(len(self.httpd._conn_ips), 1)
        self.assertLessEqual(max(self.httpd._conn_ips.values()), srv.MAX_CONNECTIONS_PER_IP)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5,
                                          source_address=("127.0.0.2", 0))
        conn.request("GET", "/api/session")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        for c in socks:
            c.close()

    def test_header_timeout_frees_slot(self):
        old = srv.Handler.timeout
        srv.Handler.timeout = 0.3
        self.addCleanup(setattr, srv.Handler, "timeout", old)
        c = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        c.sendall(b"GET /api/sess")
        self.assertEqual(c.recv(100), b"")                       # server gave up on the stalled headers
        c.close()
        time.sleep(0.2)
        self.assertEqual(dict(self.httpd._conn_ips), {})

    def test_body_gets_longer_timeout_after_headers(self):
        self.assertEqual(srv.HEADER_TIMEOUT, 3)
        self.assertEqual(srv.SOCKET_TIMEOUT, 10)
        self.assertEqual(srv.MAX_CONNECTIONS, 64)
        self.assertEqual(srv.MAX_CONNECTIONS_PER_IP, 8)
        self.assertEqual(srv.Handler.timeout, srv.HEADER_TIMEOUT)

    def test_x_frame_options(self):
        r, _ = self.req("GET", "/api/session")
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")

    def test_strict_content_length(self):
        base = (f"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                f"Origin: http://127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n")
        body = '{"token":"x"}'
        for cl in ("+5", "5 5", "0x5", "-1", "5.0", "\u0665".encode("utf-8").decode("latin-1")):
            out = self.raw_exchange((base + f"Content-Length: {cl}\r\n\r\n{body}").encode("latin-1"))
            self.assertIn(b" 400 ", out.split(b"\r\n")[0], cl)
        out = self.raw_exchange((base + f"Content-Length: {len(body)}\r\nContent-Length: {len(body)}"
                                 f"\r\n\r\n{body}").encode())
        self.assertIn(b" 400 ", out.split(b"\r\n")[0])
        out = self.raw_exchange((base + f"Content-Length: {len(body)}\r\n\r\n{body}").encode())
        self.assertIn(b" 401 ", out.split(b"\r\n")[0])           # a clean request still works

    def test_host_port_validation(self):
        for h in ("127.0.0.1:99999", "127.0.0.1:abc", "127.0.0.1:", "127.0.0.1:-1", "127.0.0.1:+80",
                  "[::1]:70000", "127.0.0.1:80:80"):
            r, _ = self.req("GET", "/api/session", headers={"Host": h})
            self.assertEqual(r.status, 421, h)
        self.assertTrue(srv._port_ok("localhost") and srv._port_ok("127.0.0.1:65535"))
        self.assertTrue(srv._port_ok("[::1]:8088") and srv._port_ok("[::1]"))
        self.assertFalse(srv._port_ok("x:65536"))

    def test_internal_error_is_generic(self):
        h = self.login()
        self.data.collect_status = lambda cfg, now=None: 1 / 0
        with self.assertLogs("arm_pipeline.dashboard", level="ERROR") as cm:
            st, js, _ = self.jreq("GET", "/api/status", headers=h)
        self.assertEqual((st, js), (500, {"ok": False, "error": "internal error"}))
        self.assertIn("ZeroDivisionError", "\n".join(cm.output))
        self.assertNotIn("ZeroDivisionError", json.dumps(js))


class TmdbTests(Base):
    def test_validation_cache_and_rate_limit(self):
        h = self.login()
        self.assertEqual(self.jreq("GET", "/api/tmdb/search", headers=h)[0], 400)
        self.assertEqual(self.jreq("GET", "/api/tmdb/search?q=x&year=19a5", headers=h)[0], 400)
        self.assertEqual(self.jreq("GET", "/api/tmdb/search?q=x&year=1995&x=1", headers=h)[1],
                         [{"q": "x", "year": 1995}])
        self.jreq("GET", "/api/tmdb/search?q=x&year=1995", headers=h)      # cached
        self.assertEqual(self.data.tmdb_calls, 1)
        long = self.jreq("GET", "/api/tmdb/search?q=" + "a" * 500, headers=h)[1]
        self.assertEqual(len(long[0]["q"]), 100)
        codes = [self.jreq("GET", f"/api/tmdb/search?q=m{i}", headers=h)[0] for i in range(40)]
        self.assertEqual(codes.count(200), srv.TMDB_RATE - 2)
        self.assertEqual(codes[-1], 429)

    def test_upstream_error_hides_detail(self):
        h = self.login()

        def boom(cfg, q, year=None):
            raise RuntimeError("https://api.example/secret?api_key=ZZZ")
        self.data.tmdb_search = boom
        st, js, _ = self.jreq("GET", "/api/tmdb/search?q=a", headers=h)
        self.assertEqual(st, 502)
        self.assertNotIn("ZZZ", json.dumps(js))

    def test_rate_window_expires(self):
        now = [0.0]
        self.httpd._clock = lambda: now[0]
        for i in range(srv.TMDB_RATE):
            self.assertEqual(self.httpd.tmdb_search(f"q{i}", None)[0], 200)
        self.assertEqual(self.httpd.tmdb_search("new", None)[0], 429)
        now[0] += 61
        self.assertEqual(self.httpd.tmdb_search("new", None)[0], 200)


class ActionTests(Base):
    def post(self, h, path, body=None):
        return self.jreq("POST", path, body, headers=h)

    def events(self):
        return EventLog(self.cfg.state_path).tail(100)

    def test_approve_reject_and_guard(self):
        h = self.login()
        st, js, _ = self.post(h, f"/api/jobs/{FP}/approve")
        self.assertEqual((st, js["ok"]), (400, False))
        self.assertIn("error", js)
        self.put("awaiting_approval")
        st, js, _ = self.post(h, f"/api/jobs/{FP}/approve")
        self.assertEqual((st, js["ok"]), (200, True))
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.state_path, "approve", FP)))
        st, js, _ = self.post(h, f"/api/jobs/{FP}/reject")
        self.assertEqual(st, 200)
        self.assertFalse(os.path.exists(os.path.join(self.cfg.state_path, "approve", FP)))

    def test_fp_validation(self):
        h = self.login()
        for bad in ("..%2Fx", "short", FP.upper(), "0123456789abcdeg", FP + "0"):
            st, js, _ = self.post(h, f"/api/jobs/{bad}/approve")
            self.assertEqual(st, 400, bad)
            st, js, _ = self.post(h, f"/api/jobs/{bad}/identify", {"title": "A", "year": 2000})
            self.assertEqual(st, 400, bad)
        self.assertFalse(os.path.exists(os.path.join(self.cfg.state_path, "x")))

    def test_identify(self):
        h = self.login()
        self.put("awaiting_identity")
        st, js, _ = self.post(h, f"/api/jobs/{FP}/identify", {"title": "Bad/Title", "year": 2000})
        self.assertEqual(st, 400)
        st, js, _ = self.post(h, f"/api/jobs/{FP}/identify", {"title": "Heat", "year": 1995, "tmdb_id": 949})
        self.assertEqual(st, 200)
        with open(os.path.join(self.cfg.state_path, "identify", FP + ".json")) as f:
            self.assertEqual(json.load(f), {"title": "Heat", "year": 1995, "tmdb_id": 949})

    def test_retry_skip_dismiss(self):
        h = self.login()
        self.put("ripping")
        for a in ("retry", "skip", "dismiss"):
            self.assertEqual(self.post(h, f"/api/jobs/{FP}/{a}")[0], 400, a)
        self.put("failed")
        self.assertEqual(self.post(h, f"/api/jobs/{FP}/dismiss")[0], 200)
        self.assertEqual(self.ledger.get(FP).message, "dismissed by owner")
        self.put("failed")
        self.assertEqual(self.post(h, f"/api/jobs/{FP}/retry")[0], 200)
        self.assertIsNone(self.ledger.get(FP))
        self.put("awaiting_identity")
        self.assertEqual(self.post(h, f"/api/jobs/{FP}/skip")[0], 200)
        self.assertEqual(self.drive.ejects, 1)
        self.assertEqual(self.ledger.get(FP).state, "skipped")

    def test_eject(self):
        h = self.login()
        self.put("ripping")
        st, js, _ = self.post(h, "/api/drive/eject")
        self.assertEqual((st, js["ok"], self.drive.ejects), (400, False, 0))
        self.put("done")
        st, js, _ = self.post(h, "/api/drive/eject")
        self.assertEqual((st, self.drive.ejects), (200, 1))

    def test_audit_events_and_no_secrets(self):
        h = self.login()
        self.jreq("POST", "/api/login", {"token": "wrong"})
        self.put("awaiting_approval")
        self.post(h, f"/api/jobs/{FP}/approve")
        self.post(h, f"/api/jobs/{FP}/retry")          # refused
        self.post(h, "/api/drive/eject")
        evs = [e for e in self.events() if e["kind"] == "operator"]
        actions = [(e["data"]["action"], e["data"]["result"]) for e in evs]
        self.assertEqual(actions, [("login", "ok"), ("login", "refused"), ("approve", "ok"),
                                   ("retry", "refused"), ("eject", "ok")])
        for e in evs:
            self.assertEqual(e["data"]["who"], "dashboard")
            self.assertEqual(e["data"]["ip"], "127.0.0.1")
        self.assertEqual(evs[2]["fp"], FP)
        with open(os.path.join(self.cfg.state_path, "events.jsonl")) as f:
            blob = f.read()
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(h["X-CSRF-Token"], blob)
        self.assertNotIn(h["Cookie"].split("=", 1)[1], blob)

    def test_access_log_has_no_secrets(self):
        with self.assertLogs("arm_pipeline.dashboard", level="INFO") as cm:
            h = self.login()
            self.jreq("POST", "/api/login?token=" + TOKEN, {"token": "wrong"})
            self.jreq("GET", "/api/status?x=secretquery", headers=h)
            self.jreq("POST", "/api/login", {"token": "wrongwrongwrong"})
        out = "\n".join(cm.output)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("wrongwrongwrong", out)
        self.assertNotIn("secretquery", out)
        self.assertNotIn(h["X-CSRF-Token"], out)
        self.assertNotIn(h["Cookie"].split("=", 1)[1], out)
        self.assertIn("/api/login", out)


if __name__ == "__main__":
    unittest.main()
