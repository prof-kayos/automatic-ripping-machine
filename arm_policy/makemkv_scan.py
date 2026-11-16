"""Parse MakeMKV robot-mode ``info`` output into plain dataclasses.

Line shapes (robot mode, https://forum.makemkv.com robot docs / apdefs.h):
  CINFO:<attrId>,<code>,"<value>"
  TINFO:<title>,<attrId>,<code>,"<value>"
  SINFO:<title>,<stream>,<attrId>,<code>,"<value>"

Attribute ids below are inferred from a mirror of apdefs.h and ARM's own wrapper and have NOT yet been
confirmed against a real disc -- every constant tagged VERIFY must be checked on the first real scan.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- attribute ids (VERIFY against a real disc) ---
A_TYPE = 1
A_NAME = 2
A_LANG = 3
A_CODEC_SHORT = 6
A_CODEC_LONG = 7
A_CHAPTERS = 8
A_DURATION = 9
A_SIZE_BYTES = 11
A_SOURCE_FILE = 16
A_VIDEO_SIZE = 19
A_SEGMENT_COUNT = 25
A_SEGMENT_MAP = 26
A_OUTPUT_FILE = 27

MAX_SEGMENT_RANGE = 100_000   # reject corrupt/hostile ranges that would exhaust memory
_TYPE_CODES = {"6201": "video", "6202": "audio", "6203": "subtitle"}
_TYPE_WORDS = {"video": "video", "audio": "audio", "subtitles": "subtitle", "subtitle": "subtitle"}

_LINE = re.compile(r'^(CINFO|TINFO|SINFO):(.*)$')
_VALUE = re.compile(r'^(?P<nums>[\d,]*),"(?P<val>.*)"\s*$')


@dataclass
class Stream:
    index: int
    kind: str = "unknown"          # video | audio | subtitle | unknown
    lang: str = ""                 # ISO 639-2 code as MakeMKV reports it, lowercased
    codec: str = ""
    video_size: str = ""           # "3840x2160" for video streams
    attrs: dict = field(default_factory=dict)

    @property
    def width(self) -> int:
        m = re.match(r"(\d+)x(\d+)", self.video_size)
        return int(m.group(1)) if m else 0

    @property
    def height(self) -> int:
        m = re.match(r"(\d+)x(\d+)", self.video_size)
        return int(m.group(2)) if m else 0


@dataclass
class Title:
    index: int
    name: str = ""
    duration_s: int = 0
    chapters: int = 0
    size_bytes: int = 0
    source_file: str = ""
    segment_count: int = 0
    segment_map: list = field(default_factory=list)   # ordered, duplicates preserved
    streams: list = field(default_factory=list)
    attrs: dict = field(default_factory=dict)

    @property
    def segment_set(self) -> frozenset:
        return frozenset(self.segment_map)

    @property
    def has_repeated_segments(self) -> bool:
        return len(self.segment_map) != len(set(self.segment_map))

    def streams_of(self, kind: str) -> list:
        return [s for s in self.streams if s.kind == kind]

    @property
    def video(self):
        v = self.streams_of("video")
        return v[0] if v else None


@dataclass
class Disc:
    name: str = ""
    attrs: dict = field(default_factory=dict)
    titles: list = field(default_factory=list)


def parse_duration(text: str) -> int:
    """'1:50:07' -> 6607 seconds; tolerant of 'M:SS' and bare seconds."""
    if not isinstance(text, str):
        return 0
    parts = [p for p in text.strip().split(":") if p != ""]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if not nums or len(nums) > 3 or any(n < 0 for n in nums):
        return 0
    if len(nums) > 1 and any(n >= 60 for n in nums[1:]):
        return 0
    secs = 0
    for n in nums:
        secs = secs * 60 + n
    return secs


def parse_segment_map(text: str) -> list:
    """'1-3,5,7-8' -> [1,2,3,5,7,8]. Order and duplicates preserved."""
    out: list = []
    if not isinstance(text, str):
        return out
    for tok in text.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "-" in tok:
            a, _, b = tok.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if abs(hi - lo) > MAX_SEGMENT_RANGE:
                continue
            out.extend(range(lo, hi + 1) if hi >= lo else range(lo, hi - 1, -1))
        else:
            try:
                out.append(int(tok))
            except ValueError:
                continue
    return out


def _to_int(text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        return 0


def parse_scan(text: str) -> Disc:
    disc = Disc()
    titles: dict = {}
    if not isinstance(text, str):
        return disc

    def title(i: int) -> Title:
        if i not in titles:
            titles[i] = Title(index=i)
        return titles[i]

    for raw in text.splitlines():
        m = _LINE.match(raw.strip())
        if not m:
            continue
        kind, rest = m.group(1), m.group(2)
        vm = _VALUE.match(rest)
        if not vm:
            continue
        nums = [n for n in vm.group("nums").split(",") if n != ""]
        val = vm.group("val").replace('\\"', '"')
        try:
            ints = [int(n) for n in nums]
        except ValueError:
            continue
        if kind == "CINFO" and len(ints) >= 1:
            disc.attrs[ints[0]] = val
            if ints[0] == A_NAME:
                disc.name = val
        elif kind == "TINFO" and len(ints) >= 2:
            t = title(ints[0])
            attr = ints[1]
            t.attrs[attr] = val
            if attr == A_NAME:
                t.name = val
            elif attr == A_CHAPTERS:
                t.chapters = _to_int(val)
            elif attr == A_DURATION:
                t.duration_s = parse_duration(val)
            elif attr == A_SIZE_BYTES:
                t.size_bytes = _to_int(val)
            elif attr == A_SOURCE_FILE:
                t.source_file = val
            elif attr == A_SEGMENT_COUNT:
                t.segment_count = _to_int(val)
            elif attr == A_SEGMENT_MAP:
                t.segment_map = parse_segment_map(val)
        elif kind == "SINFO" and len(ints) >= 3:
            t = title(ints[0])
            sidx, attr = ints[1], ints[2]
            stream = next((s for s in t.streams if s.index == sidx), None)
            if stream is None:
                stream = Stream(index=sidx)
                t.streams.append(stream)
            stream.attrs[attr] = val
            if attr == A_TYPE:
                code = str(ints[3]) if len(ints) >= 4 else ""
                stream.kind = _TYPE_CODES.get(code) or _TYPE_WORDS.get(val.strip().lower(), "unknown")
            elif attr == A_LANG:
                stream.lang = val.strip().lower()
            elif attr == A_CODEC_SHORT:
                stream.codec = val
            elif attr == A_VIDEO_SIZE:
                stream.video_size = val
    disc.titles = [titles[i] for i in sorted(titles)]
    # Fallback segment_count when only a map was given
    for t in disc.titles:
        if not t.segment_count and t.segment_map:
            t.segment_count = len(t.segment_map)
    return disc
