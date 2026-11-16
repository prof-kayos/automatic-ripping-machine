"""Synthetic MakeMKV robot-output builders for the policy tests (no real disc content)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from arm_policy.makemkv_scan import parse_scan, parse_segment_map  # noqa: E402


def hms(seconds):
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def title_lines(idx, minutes, segments, chapters=20, size_gb=30, name=None, size="1920x1080",
                audio=(("eng", "TrueHD"),), subs=("eng",), source=None):
    secs = int(minutes * 60)
    out = [
        f'TINFO:{idx},2,0,"{name or f"Title {idx}"}"',
        f'TINFO:{idx},8,0,"{chapters}"',
        f'TINFO:{idx},9,0,"{hms(secs)}"',
        f'TINFO:{idx},11,0,"{int(size_gb * 1e9)}"',
        f'TINFO:{idx},16,0,"{source or f"{idx:05d}.mpls"}"',
        f'TINFO:{idx},25,0,"{len(parse_segment_map(segments))}"',
        f'TINFO:{idx},26,0,"{segments}"',
        f'SINFO:{idx},0,1,6201,"Video"',
        f'SINFO:{idx},0,6,0,"MPEG-4 AVC"',
        f'SINFO:{idx},0,19,0,"{size}"',
    ]
    n = 1
    for lang, codec in audio:
        out += [f'SINFO:{idx},{n},1,6202,"Audio"', f'SINFO:{idx},{n},3,0,"{lang}"',
                f'SINFO:{idx},{n},6,0,"{codec}"']
        n += 1
    for lang in subs:
        out += [f'SINFO:{idx},{n},1,6203,"Subtitles"', f'SINFO:{idx},{n},3,0,"{lang}"']
        n += 1
    return out


def scan(*title_specs, disc_name="TEST_DISC"):
    lines = [f'CINFO:2,0,"{disc_name}"']
    for spec in title_specs:
        lines += title_lines(**spec)
    return parse_scan("\n".join(lines))
