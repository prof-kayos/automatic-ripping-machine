"""Token login, per-IP throttling and in-memory sessions with CSRF tokens."""
from __future__ import annotations

import collections
import hashlib
import hmac
import secrets
import threading
import time

COOKIE_NAME = "arm_session"
MIN_TOKEN_LEN = 32
SESSION_TTL = 12 * 3600
MAX_SESSIONS = 20
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60
MAX_TRACKED_IPS = 1000
GLOBAL_FREE_FAILURES = 25        # failures (any client) within GLOBAL_WINDOW before the global backoff starts
GLOBAL_MAX_BACKOFF = 30          # deliberately small: a LAN client must not be able to lock the owner out
GLOBAL_WINDOW = 900


class Auth:
    def __init__(self, token: str, clock=time.monotonic, secure_cookie: bool = False):
        if not token or len(token) < MIN_TOKEN_LEN:
            raise ValueError(f"DASHBOARD_TOKEN is required and must be at least {MIN_TOKEN_LEN} characters")
        self._digest = hashlib.sha256(token.encode("utf-8")).digest()
        self._clock = clock
        self.secure_cookie = secure_cookie
        self._lock = threading.Lock()
        self._fails = collections.OrderedDict()   # ip -> (count, locked_until), oldest first
        self._gfails = collections.deque()      # timestamps of recent failures, any client
        self._guntil = 0.0
        self._sessions = {}     # sid -> (csrf, expires)

    # --- throttling
    def locked_seconds(self, ip: str) -> int:
        """Seconds remaining on a lockout for this client (0 = not locked)."""
        with self._lock:
            now = self._clock()
            left = max(self._guntil - now, self._fails.get(ip, (0, 0))[1] - now)
            return int(left) + 1 if left > 0 else 0

    def fail_count(self, ip: str) -> int:
        with self._lock:
            return self._fails.get(ip, (0, 0))[0]

    def check_token(self, candidate) -> bool:
        if not isinstance(candidate, str):
            return False
        d = hashlib.sha256(candidate.encode("utf-8", "replace")).digest()
        return hmac.compare_digest(d, self._digest)

    def login(self, ip: str, candidate):
        """Return (session_id, csrf) on success, or (None, retry_after_seconds_or_0) on failure."""
        left = self.locked_seconds(ip)
        if left:
            return None, left
        ok = self.check_token(candidate)
        now = self._clock()
        with self._lock:
            if not ok:
                count, until = self._fails.get(ip, (0, 0))
                if until and until <= now:       # lockout expired: start over
                    count = 0
                count += 1
                until = now + LOCKOUT_SECONDS if count >= MAX_FAILURES else 0
                self._fails.pop(ip, None)
                self._fails[ip] = (count, until)
                while len(self._fails) > MAX_TRACKED_IPS:
                    self._fails.popitem(last=False)          # evict oldest
                self._gfails.append(now)
                while self._gfails and now - self._gfails[0] > GLOBAL_WINDOW:
                    self._gfails.popleft()
                over = len(self._gfails) - GLOBAL_FREE_FAILURES
                if over >= 0:
                    self._guntil = now + min(GLOBAL_MAX_BACKOFF, 2 ** over)
                    until = max(until, self._guntil)
                return None, (int(until - now) if until else 0)
            self._fails.pop(ip, None)
            self._purge(now)
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda k: self._sessions[k][1])
                del self._sessions[oldest]
            sid = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            self._sessions[sid] = (csrf, now + SESSION_TTL)
            return sid, csrf

    def _purge(self, now):
        for k in [k for k, v in self._sessions.items() if v[1] <= now]:
            del self._sessions[k]

    # --- sessions
    def session_csrf(self, sid):
        """CSRF token for a valid session id, else None."""
        if not sid:
            return None
        with self._lock:
            ent = self._sessions.get(sid)
            if not ent:
                return None
            if ent[1] <= self._clock():
                del self._sessions[sid]
                return None
            return ent[0]

    def check_csrf(self, sid, supplied) -> bool:
        csrf = self.session_csrf(sid)
        if csrf is None or not isinstance(supplied, str):
            return False
        return hmac.compare_digest(csrf.encode(), supplied.encode("utf-8", "replace"))

    def logout(self, sid) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def cookie_header(self, sid: str) -> str:
        c = f"{COOKIE_NAME}={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}"
        return c + ("; Secure" if self.secure_cookie else "")

    def clear_cookie_header(self) -> str:
        c = f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
        return c + ("; Secure" if self.secure_cookie else "")
