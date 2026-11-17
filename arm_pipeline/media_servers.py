"""Plex and Jellyfin clients (stdlib only). Tokens never appear in URLs, logs or exceptions."""
from __future__ import annotations

import json
import posixpath
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import ServerError

TIMEOUT_S = 15


def to_server_path(local: str, local_root: str, server_root: str) -> str:
    """Translate a path under local_root into the same path under server_root (pure)."""
    root = posixpath.normpath(local_root) if local_root.strip("/") else "/"
    path = posixpath.normpath(local)               # also collapses '..' and trailing slash
    if root == "/":
        rel = path.lstrip("/")
    elif path == root:
        rel = ""
    elif path.startswith(root + "/"):
        rel = path[len(root) + 1:]
    else:
        raise ServerError(f"path outside library root: {local!r}")
    base = server_root.rstrip("/")
    return f"{base}/{rel}" if rel else (base or "/")


def _http(method: str, url: str, headers: dict, body: bytes | None = None, what: str = "request"):
    """Return (status, bytes). Non-2xx / network errors become ServerError without URL or token."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        raise ServerError(f"{what}: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ServerError(f"{what}: {type(e).__name__}") from None


def _wait(check, present: bool, timeout_s, poll_s, sleep, now) -> bool:
    """Poll check() until it equals `present` or timeout. Transient ServerErrors just mean 'try again'."""
    deadline = now() + timeout_s
    while True:
        try:
            if check() == present:
                return True
        except ServerError:
            pass
        if now() >= deadline:
            return False
        sleep(poll_s)


class Plex:
    def __init__(self, cfg, api_base: str | None = None):
        self.cfg = cfg
        self.base = (api_base or cfg.plex_url).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.base and self.cfg.plex_token)

    def _headers(self) -> dict:
        return {"X-Plex-Token": self.cfg.plex_token, "Accept": "application/json"}

    def scan_path(self, local_dir: str) -> None:
        """Ask Plex to refresh just this folder (not the whole library)."""
        server_dir = to_server_path(local_dir, self.cfg.movies_dir, self.cfg.plex_movies_path)
        q = urllib.parse.urlencode({"path": server_dir}, quote_via=urllib.parse.quote)
        _http("GET", f"{self.base}/library/sections/{self.cfg.plex_section_id}/refresh?{q}",
              self._headers(), what="plex refresh")

    def items(self, title_query: str) -> list[dict]:
        # An empty query lists the whole section: wait_for_file then matches by FILE PATH, which does not depend on
        # how Plex spells the title (it can differ from the folder name).
        q = urllib.parse.urlencode({"title": title_query}, quote_via=urllib.parse.quote) if title_query else ""
        _, raw = _http("GET", f"{self.base}/library/sections/{self.cfg.plex_section_id}/all" + (f"?{q}" if q else ""),
                       self._headers(), what="plex items")
        out = []
        for m in json.loads(raw).get("MediaContainer", {}).get("Metadata", []) or []:
            media = (m.get("Media") or [{}])[0]
            part = (media.get("Part") or [{}])[0]
            out.append({"key": m.get("ratingKey"), "title": m.get("title"), "year": m.get("year"),
                        "file": part.get("file"), "res": media.get("videoResolution"),
                        "codec": media.get("videoCodec"), "added": m.get("addedAt"),
                        # every file of the item, so wait_for_file sees multi-version movies
                        "files": [p.get("file") for md in m.get("Media") or [] for p in md.get("Part") or []]})
        return out

    def wait_for_file(self, title_query: str, server_file_path: str, present: bool, timeout_s: int,
                      poll_s: float = 5, sleep=time.sleep, now=time.monotonic) -> bool:
        def has_file():
            return any(server_file_path in (it["files"] or []) for it in self.items(title_query))
        return _wait(has_file, present, timeout_s, poll_s, sleep, now)


class Jellyfin:
    UPDATE_TYPES = ("Created", "Modified", "Deleted")
    PAGE = 500
    MAX_PAGES = 20

    def __init__(self, cfg, api_base: str | None = None):
        self.cfg = cfg
        self.base = (api_base or cfg.jellyfin_url).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.base and self.cfg.jellyfin_token)

    def _headers(self) -> dict:
        # NOT X-Emby-Token: that returns 401 on Jellyfin 12.x
        return {"Authorization": f'MediaBrowser Token="{self.cfg.jellyfin_token}"',
                "Accept": "application/json"}

    def _server_path(self, local: str) -> str:
        return to_server_path(local, self.cfg.movies_dir, self.cfg.jellyfin_movies_path)

    def push(self, local_path: str, update_type: str) -> None:
        if update_type not in self.UPDATE_TYPES:
            raise ServerError(f"invalid update_type {update_type!r}")
        body = json.dumps({"Updates": [{"Path": self._server_path(local_path),
                                        "UpdateType": update_type}]}).encode()
        headers = dict(self._headers(), **{"Content-Type": "application/json"})
        status, _ = _http("POST", f"{self.base}/Library/Media/Updated", headers, body, what="jellyfin push")
        if status != 204:
            raise ServerError(f"jellyfin push: unexpected HTTP {status}")

    def find_by_path(self, local_file: str) -> dict | None:
        """Find a movie by exact server path. We page through the library instead of searching by
        title because Jellyfin's title can differ from the folder name."""
        target = self._server_path(local_file)
        fields = "Path,ProviderIds,MediaStreams,DateCreated"
        for page in range(self.MAX_PAGES):
            q = urllib.parse.urlencode({"Recursive": "true", "IncludeItemTypes": "Movie", "Fields": fields,
                                        "StartIndex": page * self.PAGE, "Limit": self.PAGE})
            _, raw = _http("GET", f"{self.base}/Items?{q}", self._headers(), what="jellyfin items")
            data = json.loads(raw)
            items = data.get("Items") or []
            for it in items:
                if it.get("Path") == target:
                    return self._summary(it)
            if len(items) < self.PAGE or (page + 1) * self.PAGE >= data.get("TotalRecordCount", 0):
                break
        return None

    @staticmethod
    def _summary(it: dict) -> dict:
        video = next((s for s in it.get("MediaStreams") or [] if s.get("Type") == "Video"), {})
        return {"id": it.get("Id"), "name": it.get("Name"), "year": it.get("ProductionYear"),
                "path": it.get("Path"), "res": video.get("Height"), "codec": video.get("Codec"),
                "range": video.get("VideoRangeType") or video.get("VideoRange"),
                "tmdb": (it.get("ProviderIds") or {}).get("Tmdb")}

    def wait_for_path(self, local_file: str, present: bool, timeout_s: int, poll_s: float = 10,
                      sleep=time.sleep, now=time.monotonic) -> bool:
        return _wait(lambda: self.find_by_path(local_file) is not None, present, timeout_s, poll_s, sleep, now)
