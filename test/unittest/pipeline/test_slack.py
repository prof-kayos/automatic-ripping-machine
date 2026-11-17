import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from arm_pipeline.config import Config
from arm_pipeline.slack import Notifier, Ticket


class Fake(BaseHTTPRequestHandler):
    """Mimics the Slack webhook and the two Web API methods we use."""
    log = []
    fail_webhook = 0          # number of 500s to return before succeeding
    reactions = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        Fake.log.append((self.path, dict(self.headers), data))
        if self.path.startswith("/hook"):
            if Fake.fail_webhook > 0:
                Fake.fail_webhook -= 1
                return self._send(500, b"oops")
            return self._send(200, b"ok")
        if self.path == "/chat.postMessage":
            return self._send(200, {"ok": True, "channel": data["channel"], "ts": "111.222"})
        self._send(404, {"ok": False})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        Fake.log.append((self.path, dict(self.headers), None))
        if u.path == "/reactions.get":
            return self._send(200, {"ok": True, "message": {"reactions": Fake.reactions}})
        self._send(404, {"ok": False})


class SlackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Fake.log, Fake.fail_webhook, Fake.reactions = [], 0, []

    def notifier(self, **kw):
        cfg = Config(slack_url=kw.pop("slack_url", self.base + "/hook/SECRET"),
                     slack_bot_token="xoxb-test", slack_channel_id="C1", slack_owner_id=kw.pop("owner", "UOWNER"))
        return Notifier(cfg, api_base=self.base, sleep=lambda s: None)

    def test_webhook_success_and_truncation(self):
        self.assertTrue(self.notifier().post("x" * 5000))
        self.assertEqual(len(Fake.log[0][2]["text"]), 3000)

    def test_retry_then_success(self):
        Fake.fail_webhook = 2
        self.assertTrue(self.notifier().post("hi"))
        self.assertEqual(len(Fake.log), 3)

    def test_total_failure_returns_false(self):
        Fake.fail_webhook = 99
        self.assertFalse(self.notifier().post("hi"))
        self.assertEqual(len(Fake.log), 3)

    def test_unreachable_does_not_raise(self):
        cfg = Config(slack_url="http://127.0.0.1:1/hook")
        self.assertFalse(Notifier(cfg, sleep=lambda s: None).post("hi"))

    def test_no_url_returns_false(self):
        self.assertFalse(self.notifier(slack_url="").post("hi"))

    def test_url_not_logged(self):
        Fake.fail_webhook = 99
        with self.assertLogs("arm_pipeline.slack", level="WARNING") as cm:
            self.notifier().post("hi")
        self.assertNotIn("SECRET", "\n".join(cm.output))

    def ticket(self):
        t = self.notifier().ask_approval("replace?")
        self.assertEqual(t, Ticket("C1", "111.222"))
        return t

    def test_ask_sends_bearer(self):
        self.ticket()
        self.assertEqual(Fake.log[0][1]["Authorization"], "Bearer xoxb-test")

    def test_ask_without_bot_returns_none(self):
        cfg = Config(slack_url="x")
        self.assertIsNone(Notifier(cfg, api_base=self.base).ask_approval("q"))

    def poll(self, reactions, owner="UOWNER"):
        Fake.reactions = reactions
        return self.notifier(owner=owner).poll_approval(Ticket("C1", "111.222"))

    def test_approved_by_owner(self):
        self.assertEqual(self.poll([{"name": "white_check_mark", "users": ["UOWNER"]}]), "approved")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(Fake.log[-1][0]).query)
        self.assertEqual(q["full"], ["true"])
        self.assertEqual(q["timestamp"], ["111.222"])

    def test_non_owner_ignored(self):
        self.assertEqual(self.poll([{"name": "+1", "users": ["UOTHER"]}]), "pending")

    def test_rejected_by_owner(self):
        self.assertEqual(self.poll([{"name": "x", "users": ["UOTHER", "UOWNER"]}]), "rejected")

    def test_skin_tone_suffix(self):
        self.assertEqual(self.poll([{"name": "+1::skin-tone-3", "users": ["UOWNER"]}]), "approved")

    def test_missing_owner_never_approves(self):
        with self.assertLogs("arm_pipeline.slack", level="WARNING"):
            self.assertEqual(self.poll([{"name": "+1", "users": ["UOWNER"]}], owner=""), "pending")

    def test_network_error_pending(self):
        n = Notifier(Config(slack_bot_token="t", slack_owner_id="U"), api_base="http://127.0.0.1:1")
        self.assertEqual(n.poll_approval(Ticket("C", "1")), "pending")


if __name__ == "__main__":
    unittest.main()


class DashboardLinkTests(unittest.TestCase):
    URL = "http://ripper.example.invalid:8088/"

    def test_config_validation(self):
        from arm_pipeline.config import Config, validate_dashboard_url
        self.assertEqual(Config().dashboard_url, "")
        self.assertEqual(Config.from_env({"ARM_DASHBOARD_URL": self.URL}).dashboard_url, self.URL)
        self.assertEqual(validate_dashboard_url("  "), "")
        for bad in ("ftp://h/", "javascript:alert(1)", "http://", "//h/x", "http://u:p@h/", "http://u@h/",
                    "http://h/?a=1", "http://h/#x", "http://h/ x", "http://h/<x>", "http://h/a|b", "http://h:99999/"):
            with self.assertRaises(ValueError, msg=bad):
                Config(dashboard_url=bad)

    def test_link_appended_and_escaped(self):
        from arm_pipeline.slack import with_link
        out = with_link("hi <!channel> & co", self.URL)
        self.assertTrue(out.startswith("hi &lt;!channel&gt; &amp; co"))
        self.assertTrue(out.endswith(f"<{self.URL}|Open dashboard>"))
        self.assertEqual(with_link("x", "http://h/a&b"), "x\n<http://h/a&amp;b|Open dashboard>")

    def test_no_link_when_empty_and_no_double_append(self):
        from arm_pipeline.slack import with_link
        self.assertEqual(with_link("hello", ""), "hello")
        once = with_link("hello", self.URL)
        self.assertEqual(once.count("Open dashboard"), 1)
        plain = f"already see {self.URL} here"          # text that already carries the URL: no second link
        self.assertEqual(with_link(plain, self.URL), plain)

    def test_link_survives_truncation(self):
        from arm_pipeline.slack import MAX_TEXT, with_link
        out = with_link("a" * 5000, self.URL)
        self.assertLessEqual(len(out), MAX_TEXT)
        self.assertTrue(out.endswith("|Open dashboard>"))

    def test_post_and_approval_carry_link_events_text_untouched(self):
        sent = []

        class Resp:
            status = 200
            def __enter__(s): return s
            def __exit__(s, *a): pass
            def read(s): return b'{"ok": true, "ts": "1.2", "channel": "C1"}'

        class Op:
            def open(s, req, timeout=None):
                sent.append(json.loads(req.data)["text"])
                return Resp()

        cfg = Config(slack_url="http://hook.invalid/x", slack_bot_token="t", slack_channel_id="C1",
                     dashboard_url=self.URL)
        n = Notifier(cfg, opener=Op())
        self.assertTrue(n.post(":white_check_mark: ADDED movie"))          # success: never linked
        self.assertTrue(n.post(":robot_face: started", needs_action=False))
        self.assertTrue(n.post(":question: needs identify", needs_action=True))
        self.assertIsNotNone(n.ask_approval("approve?"))                   # approval: always linked
        link = f"<{self.URL}|Open dashboard>"
        self.assertEqual([t.endswith(link) for t in sent], [False, False, True, True])
        from arm_pipeline.pipeline import _Tee

        class Ev:
            def emit(s, *a, **k): s.a = a

        class Inner:
            def post(s, t, needs_action=False): s.t = t; s.na = needs_action
        ev, inner = Ev(), Inner()
        _Tee(inner, ev, lambda: None).post(":warning: x", needs_action=True)
        self.assertNotIn("dashboard", ev.a[1])
        self.assertTrue(inner.na)

    def test_finish_flags_only_intervention_states(self):
        from arm_pipeline.pipeline import Pipeline

        class Slack:
            def __init__(s): s.calls = []
            def post(s, t, needs_action=False): s.calls.append((t, needs_action))

        class Drive:
            ok = True
            def eject(s, cfg): return s.ok, "open"

        class Led:
            def set_state(s, *a, **k): pass

        class Prog:
            def clear(s, fp): pass
        p = Pipeline.__new__(Pipeline)
        p.cfg, p.slack, p.drive, p.ledger, p.progress = None, Slack(), Drive(), Led(), Prog()
        for state in ("done", "skipped"):
            p._finish("fp", state, "m", eject=True)
        for state in ("failed", "rejected"):
            p._finish("fp", state, "m", eject=False)
        Drive.ok = False
        p._finish("fp", "done", "m", eject=True)       # eject not confirmed -> human must look
        self.assertEqual([f for _, f in p.slack.calls], [False, False, True, True, True])
