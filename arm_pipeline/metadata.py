"""Identify the movie on a disc: clean the disc name, then confirm it against TMDb.

TMDb is only asked for facts (title, year, runtime, language); the choice between candidates is made here by a
transparent score so that anything ambiguous comes back as ``confidence='low'`` and a human is asked.
"""
from __future__ import annotations

import datetime
import difflib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from arm_policy import library_match
from arm_policy.decision import Identity

from .errors import MetadataError

# --------------------------------------------------------------------------- disc name parsing

# Trailing tokens that describe the media, not the movie. "Deluxe Edition" etc. are deliberately NOT here.
_NOISE = re.compile(r"^(uhd|4k|bd|bluray|blu-ray|dvd|disc\d*|disk\d*|d\d{1,2}|ws|fs|region\w*|r\d|"
                    r"widescreen|fullscreen)$", re.I)
_SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs"}
_YEAR = re.compile(r"^(19|20)\d{2}$")


def _max_bare_year() -> int:
    """A bare 4-digit token only counts as a year if plausible (so 'Blade Runner 2049' keeps its 2049)."""
    return datetime.date.today().year + 1


def _looks_human(name: str) -> bool:
    """Display names have spaces or mixed case; volume labels are SHOUTY_WITH_UNDERSCORES."""
    name = name.strip()
    if not name:
        return False
    return " " in name or (name != name.upper() and name != name.lower())


def _title_case(words: list[str]) -> str:
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        out.append(low if (i > 0 and low in _SMALL_WORDS) else (w if w != w.lower() and w != w.upper()
                                                                  else w.capitalize()))
    return " ".join(out)


def _clean(raw: str) -> tuple[str, int | None]:
    text = raw.replace("_", " ").replace(".", " ") if "_" in raw or " " not in raw.strip() else raw
    year = None
    # Parenthesised/bracketed year, e.g. "Blade Runner (1982)": always trusted (1900-2099).
    m = re.search(r"[(\[]\s*((?:19|20)\d{2})\s*[)\]]", text)
    if m:
        year = int(m.group(1))
        text = text[:m.start()] + " " + text[m.end():]
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    # Bare year token: not the first word (so "2001 A Space Odyssey" and "1917" stay titles).
    if year is None:
        for i in range(len(words) - 1, 0, -1):
            if _YEAR.match(words[i]) and int(words[i]) <= _max_bare_year():
                year = int(words[i])
                words = words[:i]          # anything after the year is edition/format junk
                break
    while len(words) > 1 and _NOISE.match(words[-1]):
        words.pop()
    if words and all(w == w.upper() or w == w.lower() for w in words):
        return _title_case(words), year
    return " ".join(words), year


def parse_disc_name(label: str, display_name: str = "") -> tuple[str, int | None]:
    """(title, year|None) from the MakeMKV volume label and the disc display name (CINFO:2)."""
    label, display_name = label or "", display_name or ""
    if _looks_human(display_name):
        title, year = _clean(display_name.strip())
        if title:
            # Year may only be present in the label ("..._1982").
            if year is None and label:
                year = _clean(label)[1]
            return title, year
    title, year = _clean(label.strip())
    return title, year


# --------------------------------------------------------------------------- TMDb

TIMEOUT_S = 15
TOP_N = 5
MIN_SIMILARITY = 0.6


def query_variants(title: str) -> list:
    """Search strings to try, most specific first: the title as given, without trademark symbols, then with up to
    three leading words dropped (publisher/series prefixes such as 'DC Universe Movie Batman: ...').
    Candidate scoring (title similarity + runtime + year) still decides; a wrong guess ends in 'low' -> ask."""
    clean = re.sub(r"[\u2122\u00ae\u00a9]", "", title or "")
    clean = re.sub(r"\s+", " ", clean).strip()
    out, words = [], clean.split()
    for q in [title.strip(), clean] + [" ".join(words[n:]) for n in (1, 2, 3) if len(words) - n >= 2]:
        if q and q not in out:
            out.append(q)
    return out


MAX_PREFIX_SEARCHES = 8
MAX_PREFIX_DETAILS = 12
_JOIN_MIN_LETTERS = 7


def joined_title_key(title) -> str:
    """Lower-cased title with every non-alphanumeric character removed ('Say Anything...' -> 'sayanything')."""
    return "".join(ch for ch in str(title or "").lower() if ch.isalnum())


def _joined_keys(title) -> set:
    """joined_title_key plus the same key after dropping a leading 'the'/'a' word."""
    t = str(title or "").strip()
    keys = {joined_title_key(t)}
    m = re.match(r"^(?:the|a)\W+(.+)$", t, re.I)
    if m:
        keys.add(joined_title_key(m.group(1)))
    keys.discard("")
    return keys


class Tmdb:
    def __init__(self, cfg, api_base: str = "https://api.themoviedb.org/3"):
        self._key = getattr(cfg, "tmdb_api_key", "") or ""
        self._base = api_base.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _get(self, path: str, params: dict) -> dict | None:
        """GET and decode JSON. Errors never include the URL (it carries the API key). 404 -> None."""
        # language=en-US: without it TMDb returns the UK/original title (e.g. 'Philosopher's Stone'), which would not
        # match a library filed under the US title
        q = urllib.parse.urlencode(dict(params, api_key=self._key, language="en-US"))
        try:
            with urllib.request.urlopen(f"{self._base}{path}?{q}", timeout=TIMEOUT_S) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise MetadataError(f"TMDb request {path} failed: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            # str(e) of URLError can embed the URL; report only the exception class.
            raise MetadataError(f"TMDb request {path} failed: {type(e).__name__}") from None
        if not isinstance(data, dict):
            raise MetadataError(f"TMDb request {path} returned unexpected data")
        return data

    def _search(self, title: str, year: int | None) -> list:
        params = {"query": title}
        if year:
            params["year"] = year
        res = (self._get("/search/movie", params) or {}).get("results") or []
        if not res and year:                       # year in a disc name is often wrong/absent on TMDb
            res = (self._get("/search/movie", {"query": title}) or {}).get("results") or []
        return [r for r in res if isinstance(r, dict) and r.get("id") is not None][:TOP_N]

    def identify(self, title: str, year: int | None, main_runtime_min: int | None) -> Identity | None:
        if not self.configured or not (title or "").strip():
            return None
        cands = []
        want = library_match.tokens(title)
        results = []
        for q in query_variants(title):             # first query that returns anything wins
            results = self._search(q, year)
            if results:
                break
        for r in results:
            det = self._get(f"/movie/{int(r['id'])}", {})
            if det is None:
                continue
            c = _candidate(det, r, want, year, main_runtime_min)
            if c:
                cands.append(c)
        if not cands:
            cands = self._identify_joined(title, main_runtime_min)
        if not cands:
            return None
        best = _pick(cands, main_runtime_min)
        others = [c["score"] for c in cands if c is not best]
        margin = best["score"] - max(others) if others else None
        confident = best["year"] is not None and (margin is None or margin >= 2)
        if confident and best.get("joined") and main_runtime_min and best["runtime"] \
                and abs(best["runtime"] - main_runtime_min) > 25:
            confident = False       # a squashed-label match needs a consistent runtime to be trusted
        return Identity(title=best["title"], year=best["year"], runtime_min=best["runtime"] or None,
                        original_language=best["lang"], tmdb_id=best["id"],
                        confidence="high" if confident else "low")


    def _identify_joined(self, title: str, main_rt) -> list:
        """Fallback for a label with no separators ('SAYANYTHING'): search TMDb by short prefixes of the token and
        keep results whose title, with all punctuation/spaces removed, equals the token. Bounded call budget."""
        token = (title or "").strip()
        if not token.isalpha() or len(token) < _JOIN_MIN_LETTERS:
            return []
        key = joined_title_key(token)
        found, seen_ids, last_err, ok = [], set(), None, False
        lengths = list(range(3, min(8, len(key) - 3) + 1))[:MAX_PREFIX_SEARCHES]
        for n in lengths:
            try:
                results = self._search(key[:n], None)
            except MetadataError as e:
                last_err = e
                continue
            ok = True
            for r in results:
                rid = int(r["id"])
                if rid in seen_ids:
                    continue
                names = (str(r.get("title") or ""), str(r.get("original_title") or ""))
                if any(key in _joined_keys(nm) for nm in names):
                    seen_ids.add(rid)
                    found.append(r)
        if not ok and last_err is not None:
            raise last_err
        cands = []
        for r in found[:MAX_PREFIX_DETAILS]:
            try:
                det = self._get(f"/movie/{int(r['id'])}", {})
            except MetadataError:
                continue
            if det is None:
                continue
            c = _candidate(det, r, [], None, main_rt, joined_key=key)
            if c:
                cands.append(c)
        return cands


def _candidate(det: dict, res: dict, want: list, year, main_rt, joined_key: str = "") -> dict | None:
    """Score one TMDb movie against what we know; None if the title is not plausibly the same."""
    ctitle = str(det.get("title") or res.get("title") or "")
    names = [ctitle, str(det.get("original_title") or res.get("original_title") or "")]
    sim = max(difflib.SequenceMatcher(None, " ".join(want), " ".join(library_match.tokens(n))).ratio()
              for n in names if n) if any(names) else 0.0
    joined = bool(joined_key) and any(joined_key in _joined_keys(n) for n in names if n)
    if joined_key and not joined:
        return None
    if sim < MIN_SIMILARITY and not joined:
        return None
    score = 4 if (sim >= 0.99 or joined) else 2 if sim >= 0.85 else 0
    m = re.match(r"^(\d{4})", str(det.get("release_date") or res.get("release_date") or ""))
    cyear = int(m.group(1)) if m else None
    if year and cyear:
        score += 3 if cyear == year else 1 if abs(cyear - year) == 1 else 0
    try:
        runtime = int(det.get("runtime") or 0)
    except (TypeError, ValueError):
        runtime = 0
    if main_rt and runtime:
        diff = abs(runtime - main_rt)
        score += 3 if diff <= 5 else 1 if diff <= 15 else -3 if diff > 30 else 0
    return {"id": det.get("id", res.get("id")), "title": ctitle, "year": cyear, "runtime": runtime,
            "lang": det.get("original_language") or res.get("original_language") or None,
            "score": score, "joined": joined, "norm": " ".join(library_match.tokens(ctitle))}


def _pick(cands: list, main_rt) -> dict:
    best = max(cands, key=lambda c: c["score"])        # max() keeps the first (TMDb relevance order) on ties
    if main_rt and best["runtime"] and abs(best["runtime"] - main_rt) > 25:
        # TMDb sometimes lists a special edition with a different runtime: prefer a same-title entry that
        # matches the disc runtime closely, and give it a clear lead so it can be 'high' confidence.
        alt = [c for c in cands if c is not best and c["norm"] == best["norm"] and c["runtime"]
               and abs(c["runtime"] - main_rt) <= 10]
        if alt:
            pick = max(alt, key=lambda c: c["score"])
            pick["score"] = max(c["score"] for c in cands) + 2
            return pick
    return best
