"""Combine scan + identity + library state into one auditable Plan (pure function, no I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import library_match, quality, selection, title_select

# actions
SKIP = "skip"                        # nothing to do (already have equal/better, or not a movie disc)
RIP_NEW = "rip_new"                  # movie not in library: rip, verify, move in
RIP_REPLACE = "rip_replace"          # disc better: rip to staging, then replace per approval policy
ASK = "ask"                          # a human decides (Slack); nothing is changed
ASK_IDENTITY = "ask_identity"        # cannot tell which movie this is


@dataclass
class Identity:
    title: str
    year: int | None
    runtime_min: int | None = None
    original_language: str | None = None   # ISO 639-1 as TMDb reports it
    tmdb_id: int | None = None
    confidence: str = "high"               # high | low


@dataclass
class Plan:
    action: str
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    title_index: int | None = None
    is_extended: bool = False
    selection_string: str = ""
    target_folder: str = ""
    target_filename_stem: str = ""
    existing_file: str = ""
    needs_approval: bool = False           # replacement must wait for a Slack reaction
    foreign: bool = False


def decide(scan, identity: Identity | None, library: dict, auto_replace: bool = False) -> Plan:
    """library: {folder_name: [(filename, Quality), ...]} -- the files in each existing folder.
    A file whose quality could not be probed must be passed as an empty ``quality.Quality()`` (-> ask)."""
    if identity is None or identity.confidence != "high" or not isinstance(identity.title, str) \
            or not identity.title.strip():
        return Plan(ASK_IDENTITY, ["movie identity unknown or low confidence"])
    year = library_match._coerce_year(identity.year)
    if year is None:
        return Plan(ASK_IDENTITY, ["release year unknown: cannot safely match against the library"])
    new_folder = library_match.canonical_folder(identity.title, year)
    if not new_folder:
        return Plan(ASK_IDENTITY, ["title has no usable characters for a folder name"])
    library = library or {}

    sel = title_select.select_main_title(scan.titles, expected_runtime_min=identity.runtime_min)
    plan = Plan(SKIP, warnings=list(sel.warnings))
    if sel.status == "tv_like":
        plan.reasons.append("episodic/TV-style disc: " + sel.reason)
        return plan
    if sel.status != "ok":
        plan.action = ASK
        plan.reasons.append(f"main title not determined ({sel.status}): {sel.reason}")
        return plan

    t = sel.title
    plan.title_index = t.index
    plan.is_extended = sel.is_extended
    plan.foreign = selection.is_foreign(identity.original_language)
    fallback = selection.needs_audio_fallback(t, identity.original_language)
    if fallback:
        plan.warnings.append("no English/original-language audio tagged on chosen title: keeping ALL audio")
    if selection.language_unmapped(identity.original_language):
        plan.warnings.append(f"original language {identity.original_language!r} not mapped to MakeMKV codes: "
                             "keeping ALL audio")
    plan.selection_string = selection.build_selection_string(identity.original_language, audio_fallback=fallback)

    match = library_match.find_match(identity.title, year, list(library))

    if match.status == "ambiguous":
        plan.action = ASK
        plan.reasons.append("several library folders match this movie: "
                            + ", ".join(f for f, _ in match.alternatives))
        return plan

    if match.status == "none":
        plan.action = RIP_NEW
        plan.target_folder = new_folder
        plan.target_filename_stem = new_folder
        plan.reasons.append("movie not in library")
        return plan

    entries = library[match.folder]
    files = library_match.video_files([f for f, _ in entries])
    quals = {f: q for f, q in entries if f in files}
    plan.target_folder = match.folder
    if not files:
        stem = match.folder
        taken = {f.lower() for f, _ in entries}
        if f"{stem}.mkv".lower() in taken:
            plan.action = ASK
            plan.reasons.append("library folder has a file with the target name that is not a recognised "
                                "video file; refusing to write over it")
            return plan
        plan.action = RIP_NEW
        plan.target_filename_stem = stem
        plan.reasons.append("library folder exists but has no video file")
        return plan
    if len(files) > 1:
        plan.action = ASK
        plan.reasons.append(f"library folder has {len(files)} video files: {', '.join(files)}")
        return plan

    existing_name = files[0]
    plan.existing_file = existing_name
    plan.target_filename_stem = existing_name.rsplit(".", 1)[0]
    existing_q = quals[existing_name]
    if not isinstance(existing_q, quality.Quality):
        existing_q = quality.Quality()        # missing probe result -> compare() asks, never replaces
    verdict = quality.compare(quality.from_disc_title(t), existing_q)
    plan.reasons.extend(verdict.reasons)
    if verdict.action == quality.SKIP:
        plan.action = SKIP
    elif verdict.action == quality.ASK:
        plan.action = ASK
    else:
        plan.action = RIP_REPLACE
        # Approval policy: ask-first unless auto_replace is on AND the folder match was exact.
        plan.needs_approval = not (auto_replace and match.status == "exact" and plan.existing_file)
        if match.status == "likely":
            plan.warnings.append(f"folder matched fuzzily ({match.score}): {match.folder}")
    return plan
