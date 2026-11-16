"""Score a disc title or an existing library file and decide replace / skip / ask.

Owner policy: one file per movie, keep the better one. The disc is always a RAW rip. Replace only when the
disc is better on the primary tier and worse on nothing; anything incomparable is ``ask`` (a human decides).
The score is deliberately a set of ordered tiers, not a single number, so reasons stay explainable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

REPLACE, SKIP, ASK = "replace", "skip", "ask"
CUT_TOLERANCE_S = 120


@dataclass
class Quality:
    res_tier: int = -1              # 0 SD, 1 720p, 2 1080p, 3 2160p, -1 unknown
    hdr: int = 0                    # 0 SDR, 1 HDR10/HLG, 2 Dolby Vision
    hdr_known: bool = True          # False => disc HDR layer is assumed, not measured
    audio: int = 0                  # see audio_rank
    video_codec: str = ""
    video_bitrate: int = 0          # bit/s, 0 unknown
    raw: bool = False               # untouched disc stream (remux/backup) vs re-encode
    duration_s: int = 0
    width: int = 0
    height: int = 0
    notes: list = field(default_factory=list)


def res_tier(width: int, height: int) -> int:
    """Width-based first (scope crops: 1920x800 is still 1080p), height as fallback."""
    if width >= 3500 or height >= 2000:
        return 3
    if width >= 1700 or height >= 1000:
        return 2
    if width >= 1200 or height >= 650:
        return 1
    if width > 0 or height > 0:
        return 0
    return -1


def audio_rank(codec_text: str, profile: str = "") -> int:
    s = f"{codec_text} {profile}".lower()
    if "truehd" in s or "atmos" in s or "dts-hd ma" in s or "dts-hd master" in s or "master audio" in s:
        return 5
    if "dts-hd" in s or "dts:x" in s or "flac" in s or "pcm" in s or "lpcm" in s:
        return 4
    if "eac3" in s or "e-ac-3" in s or "ddp" in s or "dd+" in s or s.strip() == "dts" or s.startswith("dts "):
        return 3
    if "ac3" in s or "ac-3" in s or s.strip() == "dd" or "dolby digital" in s:
        return 2
    if "aac" in s or "mp3" in s or "opus" in s or "vorbis" in s or "mp2" in s:
        return 1
    return 0


def _is_raw_class(tier: int, codec: str, bitrate: int) -> bool:
    """Heuristic: a re-encode has far less video bitrate than a disc stream of the same resolution."""
    codec = (codec or "").lower()
    if tier == 0:
        return True                               # DVD-class MPEG-2 is the disc stream; encodes at SD are rare
    if bitrate <= 0:
        return False
    floor = {1: 12_000_000, 2: 15_000_000, 3: 40_000_000}.get(tier, 15_000_000)
    return bitrate >= floor


def _int(x, default=0) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def from_ffprobe(probe) -> Quality:
    """probe = parsed ``ffprobe -v error -show_streams -show_format -of json`` output. Never raises."""
    q = Quality()
    if not isinstance(probe, dict):
        q.notes.append("no ffprobe data")
        return q
    streams = [s for s in (probe.get("streams") or []) if isinstance(s, dict)]
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if not video:
        q.notes.append("no video stream")
        return q
    q.width, q.height = _int(video.get("width")), _int(video.get("height"))
    q.res_tier = res_tier(q.width, q.height)
    q.video_codec = str(video.get("codec_name") or "")
    q.video_bitrate = _int(video.get("bit_rate"))
    fmt = probe.get("format") if isinstance(probe.get("format"), dict) else {}
    if not q.video_bitrate:
        total = _int(fmt.get("bit_rate"))
        if total:
            q.video_bitrate = int(total * 0.85)     # crude: video ~85% of container rate
            q.notes.append("video bitrate estimated from container bitrate")
    q.duration_s = _int(fmt.get("duration") or video.get("duration"))

    trc = str(video.get("color_transfer") or "").lower()
    side = " ".join(str(sd.get("side_data_type", "")) for sd in (video.get("side_data_list") or [])
                    if isinstance(sd, dict))
    if "dovi" in side.lower() or "dolby vision" in side.lower():
        q.hdr = 2
    elif trc in ("smpte2084", "arib-std-b67"):
        q.hdr = 1
    q.audio = max([audio_rank(str(s.get("codec_name") or ""), str(s.get("profile") or ""))
                   for s in streams if s.get("codec_type") == "audio"] or [0])
    q.raw = _is_raw_class(q.res_tier, q.video_codec, q.video_bitrate)
    return q


def from_disc_title(title) -> Quality:
    """Quality of what a rip of this MakeMKV title will produce (always a raw stream)."""
    q = Quality(raw=True, duration_s=title.duration_s)
    v = title.video
    if not v:
        q.notes.append("title has no video stream")
        return q
    q.width, q.height = v.width, v.height
    q.res_tier = res_tier(q.width, q.height)
    q.video_codec = v.codec
    if q.res_tier == 3:
        q.hdr, q.hdr_known = 1, False     # UHD BD mandates an HDR10 base layer; DV layer not visible in a scan
        q.notes.append("UHD: HDR10 assumed (Dolby Vision layer cannot be detected from a scan)")
    q.audio = max([audio_rank(s.codec) for s in title.streams_of("audio")] or [0])
    return q


@dataclass
class Verdict:
    action: str
    reasons: list = field(default_factory=list)


DURATION_PLAUSIBLE = 0.25      # existing vs disc runtime may differ by at most this fraction
AUDIO_CLEAR_UPGRADE = 2        # audio-only replacement needs at least this many ranks of improvement


def compare(disc: Quality, existing: Quality | None) -> Verdict:
    """existing=None means 'the movie is not in the library at all' (callers must not pass None for a
    file whose probe failed -- use an empty Quality() for that, which yields ASK).

    Replace ONLY when the disc is better on some tier and not worse on ANY tier; otherwise skip or ask.
    """
    if existing is None:
        return Verdict(REPLACE, ["no existing file"])
    if existing.res_tier < 0 or disc.res_tier < 0:
        return Verdict(ASK, ["could not determine quality of the "
                             + ("existing file" if existing.res_tier < 0 else "disc")])
    # Is the existing file plausibly the same film/cut as the disc title?
    if not disc.duration_s or not existing.duration_s:
        return Verdict(ASK, ["runtime of the existing file or the disc title is unknown"])
    if abs(existing.duration_s - disc.duration_s) / max(existing.duration_s, disc.duration_s) > DURATION_PLAUSIBLE:
        return Verdict(ASK, [f"existing file runtime {existing.duration_s}s vs disc {disc.duration_s}s differ "
                             "too much to be the same cut"])
    longer_existing = existing.duration_s - disc.duration_s > CUT_TOLERANCE_S

    if not disc.hdr_known and existing.hdr == 2:
        return Verdict(ASK, ["existing is Dolby Vision; the disc's DV layer cannot be detected from a scan"])
    if not disc.hdr_known and existing.res_tier == 3 and existing.hdr == 0:
        return Verdict(ASK, ["existing 4K file carries no HDR tags (untagged or tone-mapped); disc HDR is assumed"])

    tiers = (("resolution", disc.res_tier, existing.res_tier), ("HDR", disc.hdr, existing.hdr),
             ("audio", disc.audio, existing.audio))
    better = [f"{n} {d} vs existing {e}" for n, d, e in tiers if d > e]
    worse = [f"{n} {d} vs existing {e}" for n, d, e in tiers if d < e]
    first = next(((n, d, e) for n, d, e in tiers if d != e), None)
    if first and first[1] < first[2]:
        # existing wins on the highest-priority tier where they differ (resolution > HDR > audio): keep it
        return Verdict(SKIP, ["existing is better on the highest differing tier: " + "; ".join(worse)])
    if worse:
        # disc wins the top tier but is worse on a lower one: a human weighs the trade-off
        return Verdict(ASK, ["disc is better on [" + "; ".join(better) + "] but worse on [" + "; ".join(worse) + "]"])
    if not better:
        if disc.raw and not existing.raw:
            return Verdict(ASK, ["same resolution/HDR/audio but existing looks like a re-encode and the disc is a "
                                 "raw rip: needs a human call"])
        return Verdict(SKIP, ["existing is equal on every tier"])

    only_audio = better and all(b.startswith("audio") for b in better)
    if only_audio and disc.audio - existing.audio < AUDIO_CLEAR_UPGRADE:
        return Verdict(ASK, ["only a marginal audio difference: " + "; ".join(better)])
    if disc.res_tier == existing.res_tier and disc.hdr == existing.hdr and disc.raw and not existing.raw:
        return Verdict(ASK, ["same resolution/HDR; existing looks like a re-encode: " + "; ".join(better)])
    if longer_existing:
        return Verdict(ASK, [f"disc is better ({'; '.join(better)}), but the existing file is "
                             f"{existing.duration_s - disc.duration_s}s longer (possibly an extended cut)"])
    return Verdict(REPLACE, ["disc is better: " + "; ".join(better)])
