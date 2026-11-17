"""Match a disc's movie (title + year from TMDb/ARM) to a ``Title (Year)`` folder in the library.

Library folders are messy in practice: trailing dots ("28 Days Later. (2002)"), doubled years
("A Charlie Brown Christmas (Short 1965) (1965)"), typos. Matching is token-aware:
  * numerals must agree exactly (roman numerals, "Part 2", "Vol. 2" all become digits), so sequels never match
    the original ("Toy Story 2" != "Toy Story", "Back to the Future Part II" != "...Part III");
  * short tokens must match exactly (so "Alien" != "Aliens"); only long tokens tolerate a typo;
  * a different year is a different movie (remakes); one year off is tolerated with a penalty.
Anything not clearly unique is ``ambiguous`` and anything not clearly a match is ``none`` -- never a guess.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".iso",
              ".webm", ".vob", ".flv", ".divx", ".ogv", ".img"}
LIKELY_SCORE = 0.90
AMBIGUOUS_GAP = 0.04
TOKEN_TYPO_RATIO = 0.85
TOKEN_TYPO_MIN_LEN = 6
LENGTH_MISMATCH_RATIO = 0.96
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}
_ROMAN_LAST_ONLY = {"v": "5", "x": "10"}
_PART_WORDS = {"part", "vol", "volume", "chapter"}
_ARTICLES = {"the", "a", "an"}


@dataclass
class Match:
    status: str                      # exact | likely | ambiguous | none
    folder: str = ""
    score: float = 0.0
    alternatives: list = field(default_factory=list)
    reason: str = ""


def _coerce_year(year):
    if isinstance(year, bool):
        return None
    if isinstance(year, int):
        return year if 1800 <= year <= 2100 else None
    if isinstance(year, str) and re.fullmatch(r"\s*(1[89]|20)\d{2}\s*", year):
        return int(year)
    return None


def normalize_title(text) -> str:
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[\W_]+", " ", text)          # \W is unicode-aware: non-Latin letters are kept
    return re.sub(r"\s+", " ", text).strip()


def tokens(text) -> list:
    toks = normalize_title(text).split()
    if len(toks) > 1 and toks[0] in _ARTICLES:
        toks = toks[1:]
    out = []
    for i, t in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if t in _PART_WORDS and (nxt.isdigit() or nxt in _ROMAN or nxt in _ROMAN_LAST_ONLY):
            continue
        if i > 0 and t in _ROMAN:
            t = _ROMAN[t]
        elif i > 0 and i == len(toks) - 1 and t in _ROMAN_LAST_ONLY:
            t = _ROMAN_LAST_ONLY[t]
        out.append(t)
    return out


def parse_folder(name: str):
    """'A Charlie Brown Christmas (Short 1965) (1965)' -> (title tokens string, 1965).
    Only TRAILING parenthesised groups are stripped, so '(500) Days of Summer (2009)' keeps its '(500)'."""
    if not isinstance(name, str):
        return "", None
    base = name.strip()
    year = None
    while True:
        m = re.search(r"\s*\(([^()]*)\)\s*$", base)
        if not m:
            break
        grp = m.group(1).strip()
        if year is None and re.fullmatch(r"(?:19|20)\d{2}", grp):
            year = int(grp)
        base = base[:m.start()]
    return " ".join(tokens(base)), year


def _tokens_compatible(a: list, b: list) -> bool:
    if not a or not b:
        return False
    if [t for t in a if t.isdigit()] != [t for t in b if t.isdigit()]:
        return False
    if len(a) == len(b):
        for x, y in zip(a, b):
            if x == y:
                continue
            if x.isdigit() or y.isdigit():
                return False
            if min(len(x), len(y)) >= TOKEN_TYPO_MIN_LEN and \
                    difflib.SequenceMatcher(None, x, y).ratio() >= TOKEN_TYPO_RATIO:
                continue
            return False
        return True
    return difflib.SequenceMatcher(None, " ".join(a), " ".join(b)).ratio() >= LENGTH_MISMATCH_RATIO


def find_match(title, year, folders) -> Match:
    want_tokens = tokens(title)
    year = _coerce_year(year)
    if not want_tokens:
        return Match("none", reason="no usable title")
    want = " ".join(want_tokens)
    scored = []
    for f in folders or []:
        ft, fy = parse_folder(f)
        ftoks = ft.split()
        if not _tokens_compatible(want_tokens, ftoks):
            continue
        score = difflib.SequenceMatcher(None, want, ft).ratio()
        if year and fy:
            if fy != year:
                if abs(fy - year) == 1:
                    score -= 0.03
                else:
                    continue                          # different year: a different movie (remake etc.)
        scored.append((score, f, fy))
    scored.sort(reverse=True)
    if not scored or scored[0][0] < LIKELY_SCORE:
        return Match("none", reason="no folder is a compatible title match",
                     alternatives=[(f, round(s, 3)) for s, f, _ in scored[:3]],
                     score=scored[0][0] if scored else 0.0)
    top = scored[0]
    close = [x for x in scored[1:] if top[0] - x[0] <= AMBIGUOUS_GAP and x[0] >= LIKELY_SCORE]

    def is_exact(x):
        return x[0] >= 0.999 and year is not None and x[2] == year
    # A single exact, year-matching folder is not made ambiguous by fuzzy near-duplicates (typo'd or leftover
    # folders); two exact ones still are.
    if close and is_exact(top) and not any(is_exact(x) for x in close):
        close = []
    if close:
        return Match("ambiguous", folder=top[1], score=top[0],
                     alternatives=[(f, round(s, 3)) for s, f, _ in [top] + close],
                     reason="more than one folder matches")
    exact = top[0] >= 0.999 and year is not None and top[2] == year
    return Match("exact" if exact else "likely", folder=top[1], score=round(top[0], 3))


_SAMPLE = re.compile(r"[-._]sample\.[a-z0-9]+$", re.I)


def video_files(names) -> list:
    out = []
    for n in names or []:
        low = n.lower()
        if n.startswith("."):
            continue
        if any(low.endswith(e) for e in VIDEO_EXTS) and "-trailer" not in low and not _SAMPLE.search(low):
            out.append(n)
    return out


def canonical_folder(title, year) -> str:
    """'Who Framed: Roger? Rabbit.' -> 'Who Framed Roger Rabbit (1988)'. Returns '' if no usable title."""
    if not isinstance(title, str):
        return ""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip().rstrip(". ")
    if not re.search(r"\w", safe):
        return ""
    y = _coerce_year(year)
    return f"{safe} ({y})" if y else safe
