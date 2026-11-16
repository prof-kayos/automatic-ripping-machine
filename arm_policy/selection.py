"""Build the MakeMKV ``app_DefaultSelectionString`` (English-only, plus original audio for foreign films).

Grammar (developer post, forum.makemkv.com p=17398): comma-separated ``{+sel|-sel|=N}:{condition}`` tokens,
evaluated left to right. The strings produced here are ASSEMBLED FROM THAT GRAMMAR AND NOT YET RUN ON A
REAL DISC -- they must be verified with ffprobe on a test rip before being trusted.
"""
from __future__ import annotations

# ISO 639-1 (TMDb original_language) -> every ISO 639-2 code MakeMKV might report (B and T variants).
ISO1_TO_2 = {
    "en": ["eng"], "fr": ["fre", "fra"], "de": ["ger", "deu"], "es": ["spa"], "it": ["ita"],
    "ja": ["jpn"], "ko": ["kor"], "zh": ["chi", "zho"], "cn": ["chi", "zho"], "ru": ["rus"],
    "pt": ["por"], "sv": ["swe"], "da": ["dan"], "no": ["nor"], "nb": ["nor", "nob"], "fi": ["fin"],
    "nl": ["dut", "nld"], "pl": ["pol"], "cs": ["cze", "ces"], "hu": ["hun"], "tr": ["tur"],
    "he": ["heb"], "ar": ["ara"], "hi": ["hin"], "th": ["tha"], "el": ["gre", "ell"], "fa": ["per", "fas"],
    "is": ["ice", "isl"], "ro": ["rum", "ron"], "uk": ["ukr"], "vi": ["vie"], "id": ["ind"],
    "ta": ["tam"], "bn": ["ben"], "ca": ["cat"], "sr": ["srp"], "hr": ["hrv"], "sk": ["slo", "slk"],
    "sl": ["slv"], "bg": ["bul"], "lt": ["lit"], "lv": ["lav"], "et": ["est"], "ms": ["may", "msa"],
    "ur": ["urd"], "ml": ["mal"], "te": ["tel"], "kn": ["kan"], "mr": ["mar"], "af": ["afr"],
    "sq": ["alb", "sqi"], "hy": ["arm", "hye"], "ka": ["geo", "kat"], "mn": ["mon"], "ne": ["nep"],
    "tl": ["fil", "tgl"], "gl": ["glg"], "eu": ["baq", "eus"], "yue": ["chi", "zho", "yue"],
}
ISO1_TO_2["cn"] = ["chi", "zho", "yue"]


def original_language_codes(tmdb_original_language) -> list:
    if not isinstance(tmdb_original_language, str) or not tmdb_original_language:
        return []
    return ISO1_TO_2.get(tmdb_original_language.lower().strip(), [])


def is_foreign(tmdb_original_language) -> bool:
    lang = tmdb_original_language.lower().strip() if isinstance(tmdb_original_language, str) else ""
    return bool(lang) and lang != "en"


def language_unmapped(tmdb_original_language) -> bool:
    """Foreign (or unknown, e.g. TMDb 'xx') original language we cannot map to MakeMKV codes: keep ALL audio."""
    return is_foreign(tmdb_original_language) and not original_language_codes(tmdb_original_language)


def build_selection_string(tmdb_original_language: str | None = None,
                           audio_fallback: bool = False) -> str:
    """audio_fallback=True keeps ALL audio (used when the chosen title has no usable English audio)."""
    if language_unmapped(tmdb_original_language):
        audio_fallback = True
    audio_langs = ["eng"] + (original_language_codes(tmdb_original_language) if is_foreign(
        tmdb_original_language) else [])
    audio_cond = "|".join(audio_langs)
    parts = ["-sel:all", "+sel:video"]
    if audio_fallback:
        parts.append("+sel:audio")
    else:
        parts.append(f"+sel:(audio&({audio_cond}))" if len(audio_langs) > 1
                     else f"+sel:(audio&{audio_langs[0]})")
    parts.append("+sel:(subtitle&eng)")
    parts.append("=100:all")
    return ",".join(parts)


def needs_audio_fallback(title, tmdb_original_language: str | None = None) -> bool:
    """True when the English-only/original rule would leave the title with NO audio stream."""
    if language_unmapped(tmdb_original_language):
        return True
    wanted = {"eng"}
    if is_foreign(tmdb_original_language):
        wanted.update(original_language_codes(tmdb_original_language))
    audio = title.streams_of("audio")
    if not audio:
        return False
    return not any(s.lang in wanted for s in audio)
