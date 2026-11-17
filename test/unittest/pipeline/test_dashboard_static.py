"""Static checks on the dashboard frontend (CSP-safety, API contract, helpers)."""
import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parents[3] / "arm_pipeline" / "dashboard" / "static"
NODE = shutil.which("node")

# Endpoints the frontend may call (the fixed API contract).  Dynamic segments
# are normalised to <fp>.
ALLOWED = {
    "/api/login", "/api/session", "/api/logout", "/api/status", "/api/issues",
    "/api/jobs", "/api/jobs/<fp>", "/api/events", "/api/tmdb/search",
    "/api/jobs/<fp>/approve", "/api/jobs/<fp>/reject", "/api/jobs/<fp>/retry",
    "/api/jobs/<fp>/skip", "/api/jobs/<fp>/dismiss", "/api/jobs/<fp>/identify",
    "/api/drive/eject",
}


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.problems, self.refs = [], []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "script" and "src" not in d:
            self.problems.append("inline <script>")
        if tag == "style":
            self.problems.append("<style> element")
        for k in d:
            if k == "style" or k.startswith("on"):
                self.problems.append("attribute %s on <%s>" % (k, tag))
        for k in ("src", "href"):
            if k in d and tag != "a":
                self.refs.append(d[k])


class TestHtml(unittest.TestCase):
    def setUp(self):
        self.html = (STATIC / "index.html").read_text()
        self.p = _Scan()
        self.p.feed(self.html)

    def test_no_inline_script_style_handlers(self):
        self.assertEqual(self.p.problems, [])

    def test_only_local_or_data_refs(self):
        for ref in self.p.refs:
            self.assertTrue(ref.startswith("data:") or not re.match(r"^(\w+:)?//|^\w+:", ref), ref)
            if not ref.startswith("data:"):
                self.assertTrue((STATIC / ref).is_file(), ref)

    def test_no_external_urls_anywhere(self):
        for name in ("index.html", "app.js", "style.css"):
            txt = (STATIC / name).read_text()
            txt = txt.replace("http://www.w3.org/2000/svg", "")
            self.assertNotRegex(txt, r"https?://", name)

    def test_landmarks(self):
        for needle in ("<header", "<main", 'lang="en"'):
            self.assertIn(needle, self.html)


class TestJsSource(unittest.TestCase):
    def setUp(self):
        self.js = (STATIC / "app.js").read_text()

    def test_no_dangerous_apis(self):
        for pat in (r"innerHTML", r"outerHTML", r"insertAdjacentHTML", r"\beval\s*\(",
                    r"document\.write", r"new\s+Function", r"setTimeout\s*\(\s*['\"]",
                    r"createContextualFragment", r"\.cssText", r"setAttribute\(\s*['\"]style"):
            self.assertIsNone(re.search(pat, self.js), pat)

    def test_endpoints_in_contract(self):
        found = set()
        for m in re.finditer(r"""['"`](/api/[^'"`]*)['"`]""", self.js):
            path = m.group(1).split("?")[0].rstrip("/")
            found.add(path)
        self.assertTrue(found)
        # '/api/jobs/' + fp [+ '/identify'] is built by concatenation
        norm = set()
        for p in found:
            norm.add("/api/jobs/<fp>" if p == "/api/jobs" and "'/api/jobs/'" in self.js else p)
            norm.add(p)
        for m in re.finditer(r"'/api/jobs/'\s*\+\s*[^;)]*?(?:\+\s*'(/\w+)')?\s*[,)]", self.js):
            if m.group(1):
                norm.add("/api/jobs/<fp>" + m.group(1))
        for p in norm:
            self.assertIn(p, ALLOWED, p)

    def test_csrf_header_used(self):
        self.assertIn("X-CSRF-Token", self.js)


class TestCss(unittest.TestCase):
    def test_dark_and_light_sets(self):
        css = (STATIC / "style.css").read_text()
        self.assertIn("prefers-color-scheme: dark", css)
        self.assertIn('[data-theme="dark"]', css)
        self.assertIn('[data-theme="light"]', css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertGreaterEqual(css.count("--bg:"), 3)
        self.assertNotIn("@import", css)
        self.assertNotRegex(css, r"url\(\s*['\"]?(?!data:)(https?:|//)")


@unittest.skipUnless(NODE, "node not available")
class TestNode(unittest.TestCase):
    def run_js(self, expr):
        code = "const m=require(%s);console.log(JSON.stringify(%s))" % (json.dumps(str(STATIC / "app.js")), expr)
        r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_syntax(self):
        r = subprocess.run([NODE, "--check", str(STATIC / "app.js")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_relTime(self):
        out = self.run_js("[m.relTime(1000,998),m.relTime(1000,950),m.relTime(1000,1000-600),"
                          "m.relTime(100000,100000-7200),m.relTime(500000,0),m.relTime(1,null)]")
        self.assertEqual(out, ["just now", "50 s ago", "10 min ago", "2 h ago", "5 d ago", "–"])

    def test_formatters(self):
        out = self.run_js("[m.fmtMinSec(5999),m.fmtDuration(3725),m.fmtDuration(95),m.fmtDuration(null),"
                          "m.fmtRate(38.44),m.fmtPct(42.4),m.fmtPct(140),m.fmtGb(7.25)]")
        self.assertEqual(out, ["99:59", "1 h 02 m", "1 m 35 s", "–", "38.4 MB/s", "42%", "100%", "7.3"])

    def test_sort_and_labels(self):
        out = self.run_js("[m.sortIssues([{severity:'info',since:1},{severity:'critical',since:5},{severity:'warning',since:2}]).map(x=>x.severity),"
                          "m.prettyLabel('SAY_ANYTHING.1989'),m.parseYear('1989'),m.parseYear('abc'),"
                          "m.longestTitle({titles:[{duration_s:5},{duration_s:5999}]}),m.matchesRuntime(100,5999),m.matchesRuntime(60,5999),"
                          "m.stateLevel('failed'),m.stateLevel('awaiting_identity'),m.stateLevel('done')]")
        self.assertEqual(out, [["critical", "warning", "info"], "SAY ANYTHING 1989", 1989, None, 5999, True, False,
                               "bad", "warn", "ok"])

    def test_backoff(self):
        out = self.run_js("[m.backoffDelay(4000,0),m.backoffDelay(4000,2),m.backoffDelay(4000,20)]")
        self.assertEqual(out, [4000, 16000, 60000])


if __name__ == "__main__":
    unittest.main()
