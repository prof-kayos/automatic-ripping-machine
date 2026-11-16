"""Pick the main-movie title from a parsed disc scan.

Policy (owner): main movie only; if the disc has an extended/director's cut, rip that one instead.
When unsure the answer is ``ambiguous`` -- the caller must ask a human on Slack, never guess.
The TMDb runtime (when known) is a HARD gate: a title far outside [runtime-10, runtime+45] min is never picked.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MIN_FEATURE_S = 60 * 60          # titles shorter than this are not a feature
MIN_EPISODE_S = 15 * 60          # only titles this long count as the parts of a 'play all' title
SAME_MOVIE_OVERLAP = 0.80        # shared-segment fraction (of the smaller title) => same movie
SAME_MOVIE_DURATION_RATIO = 0.75  # shorter/longer; a cut differs by far less than 25%
AMBIGUOUS_RATIO_NO_RUNTIME = 0.60  # without a runtime: any sizeable second feature => ask
LOOP_RATIO = 1.4                 # a repeated-segment title this much longer than its source is a loop
NEAR_EQUAL_S = 120               # durations within 2 min are duplicates, not distinct cuts
RUNTIME_BELOW_MIN = 10           # chosen title may be at most this many min shorter than TMDb runtime
RUNTIME_ABOVE_MIN = 45           # ... or this many longer (extended cuts)
RUNTIME_CLEAR_GAP_MIN = 5        # a cluster must be this much closer to the runtime than the next


@dataclass
class Selection:
    status: str                              # ok | ambiguous | no_candidate | tv_like
    title: object = None                     # chosen Title (status == ok)
    is_extended: bool = False
    variants: list = field(default_factory=list)    # all cut variants in the chosen cluster
    candidates: list = field(default_factory=list)  # one best Title per cluster, longest first
    warnings: list = field(default_factory=list)
    reason: str = ""


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _overlap(a, b) -> float:
    sa, sb = a.segment_set, b.segment_set
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def _same_movie(a, b) -> bool:
    longer, shorter = (a, b) if a.duration_s >= b.duration_s else (b, a)
    if longer.duration_s == 0:
        return False
    if shorter.duration_s / longer.duration_s < SAME_MOVIE_DURATION_RATIO:
        return False
    if a.segment_set and b.segment_set:
        return _overlap(a, b) >= SAME_MOVIE_OVERLAP
    return abs(a.duration_s - b.duration_s) <= NEAR_EQUAL_S


def _is_compound(t, others) -> bool:
    """A 'play all' title: it wholly contains >=2 other disjoint, episode-length titles."""
    if not t.segment_set:
        return False
    contained = [o for o in others
                 if o is not t and o.segment_set and MIN_EPISODE_S <= o.duration_s < t.duration_s
                 and len(o.segment_set & t.segment_set) / len(o.segment_set) >= 0.9]
    disjoint = []
    for o in sorted(contained, key=lambda x: -x.duration_s):
        if all(not (o.segment_set & d.segment_set) for d in disjoint):
            disjoint.append(o)
    return len(disjoint) >= 2


def _is_loop_duplicate(t, others) -> bool:
    """Repeated-segment title that is just another title played more than once (seamless-branching loop)."""
    if not t.has_repeated_segments:
        return False
    return any(o is not t and o.segment_set and o.duration_s * LOOP_RATIO <= t.duration_s
               and len(o.segment_set & t.segment_set) / len(o.segment_set) >= 0.9 for o in others)


def _cluster(titles) -> list:
    clusters: list = []
    for t in sorted(titles, key=lambda x: -x.duration_s):
        for c in clusters:
            if any(_same_movie(t, m) for m in c):
                c.append(t)
                break
        else:
            clusters.append([t])
    return clusters


def _best_of_variant_group(cluster):
    """Longest cut wins (extended preferred); among near-equal durations prefer no repeated segments, then
    fewest segments (not an obfuscated playlist), then most chapters, then largest size."""
    longest = max(t.duration_s for t in cluster)
    top = [t for t in cluster if longest - t.duration_s <= NEAR_EQUAL_S]
    top.sort(key=lambda t: (t.has_repeated_segments, t.segment_count or len(t.segment_map) or 10 ** 6,
                            -t.chapters, -t.size_bytes))
    return top[0]


def select_main_title(titles, expected_runtime_min=None, min_len_s=MIN_FEATURE_S) -> Selection:
    sel = Selection(status="no_candidate")
    titles = [t for t in (titles or []) if t is not None]
    expected = _num(expected_runtime_min)
    if not titles:
        sel.reason = "scan returned no titles"
        return sel

    long_titles = [t for t in titles if t.duration_s >= min_len_s]
    if not long_titles:
        mids = [t for t in titles if MIN_EPISODE_S <= t.duration_s < min_len_s]
        if len(mids) >= 3:
            sel.status = "tv_like"
            sel.reason = f"{len(mids)} titles of 15-60 min and no feature-length title (episodic disc)"
        else:
            sel.reason = f"no title >= {min_len_s // 60} min"
        return sel

    candidates = [t for t in long_titles if not _is_compound(t, titles)]
    dropped = len(long_titles) - len(candidates)
    if dropped:
        sel.warnings.append(f"ignored {dropped} 'play all' title(s) that contain other titles")
    loops = [t for t in candidates if _is_loop_duplicate(t, titles)]
    if loops:
        sel.warnings.append(f"ignored {len(loops)} title(s) with repeated segments "
                            "(looped/obfuscated duplicate of another title)")
        candidates = [t for t in candidates if t not in loops]
    if not candidates:
        sel.reason = "only compound/looped titles"
        return sel

    if expected is not None:
        lo, hi = expected - RUNTIME_BELOW_MIN, expected + RUNTIME_ABOVE_MIN
        fitting = [t for t in candidates if lo <= t.duration_s / 60 <= hi]
        if not fitting:
            sel.status = "ambiguous"
            sel.reason = (f"no feature-length title within [{lo:.0f}, {hi:.0f}] min of the TMDb runtime "
                          f"({expected:.0f} min)")
            return sel
        if len(fitting) < len(candidates):
            sel.warnings.append(f"{len(candidates) - len(fitting)} feature-length title(s) outside the "
                                "TMDb runtime window ignored")
        candidates = fitting

    clusters = _cluster(candidates)
    sel.candidates = [_best_of_variant_group(c) for c in clusters]

    chosen_cluster = clusters[0]
    if len(clusters) > 1:
        if expected is not None:
            # With a TMDb runtime, the cluster CLOSEST to it wins, but only if clearly closer than the next.
            dists = sorted((min(abs(t.duration_s / 60 - expected) for t in c), i) for i, c in enumerate(clusters))
            if dists[1][0] - dists[0][0] >= RUNTIME_CLEAR_GAP_MIN:
                chosen_cluster = clusters[dists[0][1]]
                sel.warnings.append(f"{len(clusters) - 1} other feature-length cluster(s) ignored "
                                    "(chosen by TMDb runtime)")
            else:
                sel.status = "ambiguous"
                sel.reason = "several feature-length clusters equally close to the TMDb runtime"
                return sel
        else:
            top_d = max(t.duration_s for t in clusters[0])
            close = [c for c in clusters if max(t.duration_s for t in c) >= AMBIGUOUS_RATIO_NO_RUNTIME * top_d]
            if len(close) > 1:
                sel.status = "ambiguous"
                sel.reason = (f"{len(close)} feature-length clusters of similar length and no TMDb runtime "
                              "to decide (double feature, multi-disc, or obfuscated disc)")
                return sel
            sel.warnings.append(f"{len(clusters) - 1} shorter feature-length cluster(s) ignored")

    chosen = _best_of_variant_group(chosen_cluster)
    sel.variants = sorted(chosen_cluster, key=lambda t: -t.duration_s)
    shortest = min(t.duration_s for t in chosen_cluster)
    sel.is_extended = chosen.duration_s - shortest > NEAR_EQUAL_S
    sel.title = chosen
    sel.status = "ok"
    return sel
