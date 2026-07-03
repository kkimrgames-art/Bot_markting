import os
import hashlib
import logging
import requests
import re
import random
import time
import threading
import unicodedata
from typing import Optional, Tuple, List, Dict, Any

from .config import Config
from ..bot.persistence import load_state, save_state, update_state
from .gemini_key_manager import get_key_manager
from .openrouter_manager import get_openrouter_manager
from .groq_manager import get_groq_manager
from .clarifai_manager import get_clarifai_manager
from .local_metadata import extract_source_metadata_context, generate_local_metadata_candidate

logger = logging.getLogger(__name__)


_AI_THROTTLE_LOCK = threading.Lock()
_AI_NEXT_ALLOWED_AT = 0.0


def _ai_throttle(cfg: Config) -> None:
    try:
        min_interval = float(os.getenv("AI_MIN_INTERVAL_SECONDS", "1.5") or "1.5")
    except Exception:
        min_interval = 1.5
    min_interval = max(0.0, min(30.0, min_interval))
    if min_interval <= 0:
        return
    global _AI_NEXT_ALLOWED_AT
    with _AI_THROTTLE_LOCK:
        now = time.monotonic()
        wait_s = _AI_NEXT_ALLOWED_AT - now
        if wait_s > 0:
            time.sleep(min(10.0, wait_s))
        _AI_NEXT_ALLOWED_AT = time.monotonic() + min_interval


def _is_ai_backed_off(cfg: Config) -> bool:
    try:
        st = load_state(cfg)
    except Exception:
        return False
    until = (st.get("ai") or {}).get("backoff_until")
    if not until:
        return False
    try:
        return float(until) > time.time()
    except Exception:
        return False


def _set_ai_backoff(cfg: Config, seconds: int) -> None:
    try:
        seconds = int(seconds)
    except Exception:
        seconds = 0
    seconds = max(0, min(3600, seconds))
    if seconds <= 0:
        return
    try:
        def _upd(st):
            ai = st.setdefault("ai", {})
            ai["backoff_until"] = time.time() + float(seconds)
        update_state(cfg, _upd)
    except Exception:
        return

def _contains_arabic(text: Optional[str]) -> bool:
    if not text:
        return False
    return bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]", text))


_LANG_SCRIPT_RANGES: Dict[str, List[Tuple[int, int]]] = {
    "ar": [(0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)],
    "fa": [(0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)],
    "ur": [(0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)],
    "th": [(0x0E00, 0x0E7F)],
    "hi": [(0x0900, 0x097F)],
    "bn": [(0x0980, 0x09FF)],
    "ta": [(0x0B80, 0x0BFF)],
    "te": [(0x0C00, 0x0C7F)],
    "kn": [(0x0C80, 0x0CFF)],
    "ml": [(0x0D00, 0x0D7F)],
    "mr": [(0x0900, 0x097F)],
    "gu": [(0x0A80, 0x0AFF)],
    "pa": [(0x0A00, 0x0A7F)],
    "ne": [(0x0900, 0x097F)],
    "si": [(0x0D80, 0x0DFF)],
    "my": [(0x1000, 0x109F), (0xA9E0, 0xA9FF), (0xAA60, 0xAA7F)],
    "km": [(0x1780, 0x17FF)],
    "lo": [(0x0E80, 0x0EFF)],
    "he": [(0x0590, 0x05FF)],
    "am": [(0x1200, 0x137F)],
    "ru": [(0x0400, 0x04FF)],
    "uk": [(0x0400, 0x04FF)],
    "bg": [(0x0400, 0x04FF)],
    "sr": [(0x0400, 0x04FF)],
    "mk": [(0x0400, 0x04FF)],
    "el": [(0x0370, 0x03FF)],
    "ka": [(0x10A0, 0x10FF), (0x2D00, 0x2D2F)],
    "hy": [(0x0530, 0x058F)],
    "ja": [(0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF)],
    "zh": [(0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF)],
    "ko": [(0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF)],
}


def _char_in_ranges(ch: str, ranges: List[Tuple[int, int]]) -> bool:
    cp = ord(ch)
    for lo, hi in ranges:
        if lo <= cp <= hi:
            return True
    return False


def _lang_primary(lang: Optional[str]) -> str:
    raw = (lang or "").strip().lower().replace("_", "-")
    return raw.split("-", 1)[0].strip() or "ar"


def _lang_requires_script_lock(lang: Optional[str]) -> bool:
    return _lang_primary(lang) in _LANG_SCRIPT_RANGES


def _is_allowed_hashtag_char(ch: str) -> bool:
    if ch == "_":
        return True
    if ch.isalnum():
        return True
    return unicodedata.category(ch).startswith("M")


def _tag_matches_target_script(tag: str, lang: str) -> bool:
    primary = _lang_primary(lang)
    ranges = _LANG_SCRIPT_RANGES.get(primary)
    if not ranges:
        return True
    body = (tag or "").lstrip("#")
    has_target_letter = False
    for ch in body:
        if ch == "_" or ch.isdigit():
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("M"):
            continue
        if ch.isalpha():
            if _char_in_ranges(ch, ranges):
                has_target_letter = True
                continue
            return False
    return has_target_letter


def _filter_hashtags_by_target_language(tags: List[str], lang: str) -> List[str]:
    if not _lang_requires_script_lock(lang):
        return list(tags or [])
    required_tag = (os.getenv("SHORTS_REQUIRED_TAG") or "").strip()
    if required_tag and not required_tag.startswith("#"):
        required_tag = "#" + required_tag
    required_key = _hashtag_key(required_tag) if required_tag else ""
    out: List[str] = []
    for tag in tags or []:
        if not isinstance(tag, str) or not tag.strip():
            continue
        if required_key and _hashtag_key(tag) == required_key:
            out.append(tag)
            continue
        if _tag_matches_target_script(tag, lang):
            out.append(tag)
    return out


_AR_HASHTAG_TOKENS = [
    "ماينكرافت",
    "لماينكرافت",
    "بيدروك",
    "مود",
    "مودات",
    "اضافة",
    "اضافات",
    "اضافه",
    "دايناصورات",
    "داينسورات",
    "داينسور",
    "داينصور",
    "ديناصورات",
    "دينسور",
    "دينصور",
    "شورتس",
    "العاب",
    "جوال",
    "تحميل",
    "تحديث",
]


def _fix_arabic_concatenated_hashtag_body(body: str) -> str:
    b = (body or "").strip()
    if not b:
        return b
    if "_" in b:
        return b
    if not _contains_arabic(b):
        return b
    if len(b) <= 18:
        return b

    parts: List[str] = []
    s = b
    guard = 0
    while s and guard < 40:
        guard += 1
        best_tok = None
        best_idx = None
        for tok in _AR_HASHTAG_TOKENS:
            try:
                idx = s.find(tok)
            except Exception:
                idx = -1
            if idx < 0:
                continue
            if best_idx is None or idx < best_idx or (idx == best_idx and best_tok is not None and len(tok) > len(best_tok)):
                best_tok = tok
                best_idx = idx

        if best_tok is None or best_idx is None:
            break

        if best_idx > 0:
            pre = s[:best_idx]
            if pre and len(pre) >= 2:
                parts.append(pre)
        parts.append(best_tok)
        s = s[best_idx + len(best_tok):]

    if s and len(s) >= 2:
        parts.append(s)

    parts = [p for p in (p.strip() for p in parts) if p]
    if len(parts) >= 2:
        return "_".join(parts)
    return b


def _sanitize_hashtag(tag: str, lang: str) -> str:
    t = (tag or "").strip()
    if not t:
        return ""
    if not t.startswith("#"):
        t = "#" + t
    t = t.replace("*", "").replace("`", "")
    body = t[1:]
    body = body.replace(":", "_")
    body = " ".join(body.split())

    is_ar = _contains_arabic(body) or (lang or "").strip().lower() in {"ar", "arabic"}
    if is_ar:
        body = _fix_arabic_concatenated_hashtag_body(body)
        body = body.replace(" ", "_")
        body = "".join(ch if _is_allowed_hashtag_char(ch) else "_" for ch in body)
        body = re.sub(r"_+", "_", body)
        max_len = 32
    else:
        body = body.replace(" ", "_")
        body = "".join(ch if _is_allowed_hashtag_char(ch) else "_" for ch in body)
        body = re.sub(r"_+", "_", body)
        max_len = 32

    body = body.strip("_")
    if not body or len(body) < 2:
        return ""
    if len(body) > max_len:
        body = body[:max_len].strip("_")
    return "#" + body


def _sanitize_hashtag_list(tags: List[str], lang: str, limit: int) -> List[str]:
    out: List[str] = []
    seen = set()
    for t in tags or []:
        if not isinstance(t, str):
            continue
        ht = _sanitize_hashtag(t, lang)
        if not ht:
            continue
        k = ht.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(ht)
        if len(out) >= limit:
            break
    return out


def _hashtag_key(ht: str) -> str:
    """Normalize hashtag to a stable key for deduping variants (case/underscores/suffixes)."""
    b = (ht or "").strip()
    if b.startswith("#"):
        b = b[1:]
    b = b.strip().lower()
    b = b.replace("_", "")
    # Deduplicate common suffix/prefix patterns that cause near-identical tags.
    # Example: minecraftshorts/minecraftshortvideos -> minecraft
    for suf in (
        "shorts", "short", "shortvideo", "shortvideos", "shortclip", "shortclips",
        "viral", "trending", "trend", "compilation",
        "reactions", "reaction", "gameplay", "gaming", "videos", "video",
        "youtubeshorts", "youtubeshort", "ytshorts",
        "2024", "2025", "2026",
    ):
        if b.endswith(suf) and len(b) > len(suf) + 3:
            b = b[: -len(suf)]
            break
    return b


_BLOCKED_HASHTAG_PHRASES = {
    "سلام عليكم", "السلام عليكم", "مرحبا", "اهلا", "أهلا", "اهلا بكم", "أهلا بكم",
    "welcome", "hello", "hi", "hey",
}

_BLOCKED_HASHTAG_TOKENS = {
    "سلام", "السلام", "عليكم", "مرحبا", "اهلا", "أهلا", "بكم", "welcome", "hello", "hi", "hey",
}

_BLOCKED_BRAND_HASHTAG_KEYS = {"modetaris"}


def _topic_supports_hashtag(topic_plain: str, phrase: str) -> bool:
    normalized_topic = _topic_to_plain_text(topic_plain).lower()
    normalized_phrase = _topic_to_plain_text(phrase).lower()
    if not normalized_topic or not normalized_phrase:
        return False
    if normalized_phrase in normalized_topic:
        return True
    phrase_words = [part for part in re.findall(r"[0-9A-Za-z\u0600-\u06FF]{2,}", normalized_phrase) if part]
    if len(phrase_words) < 2:
        return False
    topic_words = set(re.findall(r"[0-9A-Za-z\u0600-\u06FF]{2,}", normalized_topic))
    return bool(topic_words) and all(word in topic_words for word in phrase_words)


def _is_unwanted_hashtag_candidate(
    ht: str,
    *,
    topic_plain: str,
    topic_words: set[str],
    hard_generic: set[str],
    soft_generic: set[str],
) -> bool:
    key = _hashtag_key(ht)
    if not key:
        return True
    if key == "shorts":
        return False
    body = _topic_to_plain_text((ht or "").lstrip("#").replace("_", " "))
    if not body:
        return True
    words = {part.lower() for part in re.findall(r"[0-9A-Za-z\u0600-\u06FF]{2,}", body)}
    if not words:
        return True
    blocked_phrase_keys = {item.lower() for item in _BLOCKED_HASHTAG_PHRASES}
    blocked_token_keys = {item.lower() for item in _BLOCKED_HASHTAG_TOKENS}
    if any(blocked in body.lower() for blocked in blocked_phrase_keys) or any(word in blocked_token_keys for word in words):
        return not _topic_supports_hashtag(topic_plain, body)
    if any(word in _BLOCKED_BRAND_HASHTAG_KEYS for word in words):
        return not _topic_supports_hashtag(topic_plain, body)
    if words <= hard_generic or words <= soft_generic:
        return not bool(words & topic_words)
    return False


def optimize_hashtags(
    tags: List[str],
    lang: str,
    topic: Optional[str] = None,
    limit_title: int = 8,
    limit_desc: int = 24,
) -> Tuple[List[str], List[str]]:
    """Return (title_tags, desc_tags) after sanitizing + deduping + ranking."""
    tl = (lang or "en").strip().lower() or "en"
    limit_title = max(3, min(int(limit_title or 8), 14))
    limit_desc = max(limit_title, min(int(limit_desc or 24), 40))

    cleaned = _sanitize_hashtag_list(tags or [], tl, 60)
    cleaned = _filter_hashtags_by_target_language(cleaned, tl)
    allow_global_english_core = not _lang_requires_script_lock(tl)
    topic_plain = _topic_to_plain_text(topic)
    topic_words = {
        part.lower()
        for part in re.findall(r"[0-9A-Za-z\u0600-\u06FF]{3,}", topic_plain)
        if len(part) >= 3
    }
    hard_generic = {"video", "videos", "clip", "clips", "gaming", "game", "games", "viral", "trending", "reaction", "reactions", "short", "shorts"}
    soft_generic = {"mod", "mods", "addon", "addons", "tutorial", "guide", "gameplay"}

    # Core tags are allowed only when present in the input or clearly supported by the topic.
    core: List[str] = []
    if allow_global_english_core and any(_hashtag_key(tag) == "shorts" for tag in cleaned):
        ht = _sanitize_hashtag("#shorts", tl)
        if ht and ht not in core:
            core.append(ht)
    if allow_global_english_core and (any(tok in topic_plain.lower() for tok in ["minecraft"]) or any(tok in topic_plain for tok in ["ماين", "كرافت"])):
        ht = _sanitize_hashtag("#minecraft", tl)
        if ht and ht not in core:
            core.append(ht)

    # Topic hashtag (best-effort)
    try:
        if topic and topic.strip():
            t = (topic or "").replace("#", "").strip()
            if tl in {"ar", "fa", "ur"}:
                # Keep it readable: first N words joined by underscore.
                parts = [p for p in " ".join(t.split()).split(" ") if p]
                parts = parts[:3]
                t2 = "_".join(parts)
            else:
                t2 = "".join(t.split())
            topic_ht = _sanitize_hashtag("#" + (t2 or "")[:32], tl)
            if topic_ht and not _is_unwanted_hashtag_candidate(
                topic_ht,
                topic_plain=topic_plain,
                topic_words=topic_words,
                hard_generic=hard_generic,
                soft_generic=soft_generic,
            ):
                core.append(topic_ht)
    except Exception:
        pass

    # Dedupe by normalized key (variants)
    uniq: List[str] = []
    seen_keys = set()
    for ht in core + cleaned:
        k = _hashtag_key(ht)
        if not k or k in seen_keys:
            continue
        seen_keys.add(k)
        uniq.append(ht)
        if len(uniq) >= limit_desc:
            break

    uniq = [
        ht for ht in uniq
        if not _is_unwanted_hashtag_candidate(
            ht,
            topic_plain=topic_plain,
            topic_words=topic_words,
            hard_generic=hard_generic,
            soft_generic=soft_generic,
        )
    ]

    # Rank: core first, then shorter tags, then rest

    def _rank(ht: str) -> Tuple[int, int, int, str]:
        low = (ht or "").lower()
        body = low.lstrip("#").replace("_", " ")
        words = {part.lower() for part in re.findall(r"[0-9A-Za-z\u0600-\u06FF]{3,}", body)}
        matches_topic = len(words & topic_words)
        generic_penalty = 0
        if words and words <= hard_generic:
            generic_penalty = 2
        elif words and words <= soft_generic:
            generic_penalty = 1
        core_bonus = -1 if _hashtag_key(ht) in {"shorts", "minecraft"} else 0
        return (generic_penalty, core_bonus - matches_topic, len(ht), low)

    uniq_sorted = sorted(uniq, key=_rank)
    uniq_sorted = _filter_hashtags_by_target_language(uniq_sorted, tl)
    title_tags = uniq_sorted[:limit_title]
    desc_tags = uniq_sorted[:limit_desc]
    return title_tags, desc_tags

def _fallback_title_and_tags(hint_title: Optional[str], lang: str = "ar") -> Tuple[str, List[str]]:
    tl = _lang_primary(lang)
    fallback_kw = _fallback_keywords("", tl)
    if tl == "ar":
        default_title = "رد فعل قصير"
    elif tl == "en":
        default_title = "Short Reaction"
    else:
        default_title = " ".join([kw for kw in fallback_kw[:2] if kw]).strip() or "Video"
    base = (hint_title or default_title).strip()
    if len(base) > 80:
        base = base[:77] + "…"
    if tl == "th":
        hashtags = ["#คลิปสั้น", "#รีแอคชั่น"]
    elif tl == "ar":
        hashtags = ["#شورتس", "#رد_فعل"]
    elif _lang_requires_script_lock(tl):
        hashtags = [f"#{kw.replace(' ', '_')}" for kw in _fallback_keywords(base, tl)[:2]]
    else:
        hashtags = ["#shorts", "#reaction"]
    return base, hashtags


def _collapse_ws(text: Optional[str]) -> str:
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split()).strip()


def _extract_hashtags_from_text(text: Optional[str]) -> List[str]:
    return re.findall(r"#[^\s#]+", text or "")


def _strip_hashtags_from_text(text: Optional[str]) -> str:
    cleaned = re.sub(r"#[^\s#]+", " ", text or "")
    cleaned = re.sub(r"(?i)^(title|description|hashtags)\s*:\s*", "", cleaned).strip()
    return _collapse_ws(cleaned).strip("-|:,.،؛•")


def _is_hashtag_only_text(text: Optional[str]) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if not _extract_hashtags_from_text(raw):
        return False
    remainder = _strip_hashtags_from_text(raw)
    remainder = re.sub(r"[-|:,.،؛!؟•_]+", " ", remainder)
    return not remainder.strip()


def _topic_to_plain_text(topic: Optional[str]) -> str:
    text = (topic or "").replace("#", " ").replace("_", " ")
    text = "".join(ch if (ch.isspace() or ch.isalnum() or unicodedata.category(ch).startswith("M")) else " " for ch in text)
    return _collapse_ws(text)


def _heuristic_hashtags(topic: Optional[str], lang: str, limit: int = 14) -> List[str]:
    tl = (lang or "ar").strip().lower() or "ar"
    plain = _topic_to_plain_text(topic)
    signals = extract_source_metadata_context(
        hint_title=plain,
        source_description="",
        lang=tl,
        content_type="",
        max_keywords=max(limit, 6),
        max_hashtags=max(limit, 6),
    )
    raw_tags: List[str] = list(signals.get("hashtags") or [])
    for kw in _fallback_keywords(plain, tl)[:6]:
        raw_tags.append("#" + kw.replace(" ", "_"))

    title_tags, desc_tags = optimize_hashtags(raw_tags, tl, topic=plain or topic, limit_title=4, limit_desc=limit)
    return desc_tags or title_tags


def _compose_seo_title(raw_title: Optional[str], fallback_topic: Optional[str], title_tags: List[str], lang: str) -> str:
    tl = (lang or "ar").strip().lower() or "ar"
    title = _collapse_ws((raw_title or "").replace("*", "").replace("`", "").replace('"', ""))
    title = _strip_hashtags_from_text(title)
    if not title or len(title) < 8 or _is_hashtag_only_text(raw_title):
        title = _topic_to_plain_text(fallback_topic)
    if not title:
        title = "فيديو قصير جديد" if tl.startswith("ar") else "New short video"
    if len(title) > 78:
        title = title[:78].rstrip(" -|:,.،؛")

    inline_tags: List[str] = []
    title_l = title.lower()
    for ht in title_tags or []:
        body = (ht or "").lstrip("#").replace("_", " ").lower()
        if body and body not in title_l:
            inline_tags.append(ht)
        if len(inline_tags) >= 2:
            break

    if inline_tags:
        candidate = f"{title} {' '.join(inline_tags)}".strip()
        if len(candidate) <= 95:
            title = candidate
    return title.strip()


def _compose_seo_description(raw_desc: Optional[str], title: str, topic: Optional[str], tags: List[str], lang: str) -> str:
    tl = (lang or "ar").strip().lower() or "ar"
    desc = (raw_desc or "").replace("*", "").replace("`", "").strip()
    desc = re.sub(r"(?i)^description\s*:\s*", "", desc).strip()
    text_only = _strip_hashtags_from_text(desc)

    if not text_only or len(text_only) < 24 or _is_hashtag_only_text(desc):
        topic_text = _topic_to_plain_text(topic) or _strip_hashtags_from_text(title) or title
        if tl.startswith("ar"):
            text_only = f"شاهد هذا الشورت عن {topic_text} مع أبرز اللقطات والتفاصيل والكلمات المفتاحية المهمة."
        elif tl.startswith("en"):
            text_only = f"Watch this short about {topic_text} with the best moments, details, and searchable highlights."
        else:
            text_only = topic_text

    desc_tags = _sanitize_hashtag_list(_extract_hashtags_from_text(desc) + list(tags or []), tl, 18)
    if desc_tags:
        return f"{text_only}\n\n{' '.join(desc_tags[:10])}".strip()
    return text_only.strip()


def _fallback_metadata_bundle(topic: Optional[str], lang: str) -> Tuple[str, List[str], str]:
    tags = _heuristic_hashtags(topic, lang, limit=14)
    title = _compose_seo_title(topic, topic, tags[:2], lang)
    desc = _compose_seo_description("", title, topic, tags, lang)
    return title, tags, desc


def _make_title_more_distinct(title: str, tags: List[str], lang: str) -> str:
    base = _collapse_ws(title)
    if not base:
        return base
    for ht in tags or []:
        body = (ht or "").lstrip("#").replace("_", " ").strip()
        if body and body.lower() not in base.lower():
            candidate = f"{base} {ht}".strip()
            if len(candidate) <= 95:
                return candidate
    tl = (lang or "").strip().lower()
    if tl.startswith("ar"):
        suffix = "شورتس"
    elif tl.startswith("en"):
        suffix = "Shorts"
    else:
        suffix = (_fallback_keywords("", tl) or ["video"])[0]
    candidate = f"{base} | {suffix}".strip()
    return candidate[:95].rstrip()


def _normalize_local_title(title: Optional[str], topic: Optional[str], lang: str) -> str:
    tl = (lang or "ar").strip().lower() or "ar"
    text = (title or "").replace("*", "").replace("`", "").replace('"', "")
    text = _strip_hashtags_from_text(text)
    text = _collapse_ws(text).strip("-|:,.،؛•")
    if len(text) < 8:
        topic_text = _topic_to_plain_text(topic)
        if topic_text:
            if tl.startswith("ar"):
                text = f"فيديو جديد عن {topic_text}"
            elif tl.startswith("en"):
                text = f"New video about {topic_text}"
            else:
                text = topic_text
        else:
            if tl.startswith("ar"):
                text = "فيديو قصير جديد"
            elif tl.startswith("en"):
                text = "New short video"
            else:
                text = " ".join((_fallback_keywords("", tl) or ["video"])[:2]).strip() or "Video"
    if len(text) > 96:
        text = text[:96].rstrip(" -|:,.،؛•")
    return text


def _clean_local_description_text(raw_desc: Optional[str]) -> str:
    text = (raw_desc or "").replace("*", "").replace("`", "")
    text = re.sub(r"#[^\s#]+", " ", text)
    lines = [" ".join(line.split()).strip(" -|:,.،؛•") for line in text.splitlines()]
    lines = [line for line in lines if line]
    if lines:
        return "\n".join(lines).strip()
    return _collapse_ws(text).strip(" -|:,.،؛•")


def _make_plain_title_more_distinct(title: str, topic: Optional[str], lang: str) -> str:
    base = _normalize_local_title(title, topic, lang)
    tl = (lang or "ar").strip().lower() or "ar"
    suffixes = ["لقطة جديدة", "زاوية مختلفة", "تفصيل إضافي"] if tl.startswith("ar") else ["fresh take", "new angle", "extra detail"]
    base_l = base.lower()
    for suffix in suffixes:
        if suffix.lower() in base_l:
            continue
        candidate = f"{base} | {suffix}".strip()
        if len(candidate) <= 96:
            return candidate
    return base[:96].rstrip()


def _normalize_local_description(
    raw_desc: Optional[str],
    title: str,
    topic: Optional[str],
    tags: List[str],
    lang: str,
    min_hashtags: int = 4,
    max_hashtags: int = 7,
) -> str:
    tl = (lang or "ar").strip().lower() or "ar"
    min_hashtags = max(1, min(int(min_hashtags or 4), 10))
    max_hashtags = max(min_hashtags, min(int(max_hashtags or 7), 12))
    text_only = _clean_local_description_text(raw_desc)
    if len(text_only) < 24:
        topic_text = _topic_to_plain_text(topic) or _strip_hashtags_from_text(title) or title
        if tl.startswith("ar"):
            text_only = f"شاهد هذا الفيديو عن {topic_text} مع أبرز النقاط والتفاصيل المهمة بشكل مختصر وواضح."
        else:
            text_only = f"Watch this video about {topic_text} with a quick and clear summary of the most important details."
    desc_tags = _sanitize_hashtag_list(_extract_hashtags_from_text(raw_desc) + list(tags or []), tl, max_hashtags)
    if len(desc_tags) < min_hashtags:
        desc_tags = _sanitize_hashtag_list(desc_tags + _heuristic_hashtags(topic or title, tl, limit=max_hashtags + 4), tl, max_hashtags)
    if desc_tags:
        return f"{text_only}\n\n{' '.join(desc_tags[:max_hashtags])}".strip()
    return text_only.strip()


def _keywords_from_hashtags(tags: List[str], topic: Optional[str], lang: str, limit: int = 12) -> List[str]:
    out: List[str] = []
    seen = set()
    for tag in tags or []:
        keyword = _collapse_ws((tag or "").lstrip("#").replace("_", " "))
        key = keyword.lower()
        if not keyword or key in seen:
            continue
        seen.add(key)
        out.append(keyword)
        if len(out) >= limit:
            return out
    for keyword in _fallback_keywords(_topic_to_plain_text(topic or ""), lang):
        key = keyword.lower()
        if key in seen or key in {"video", "videos", "clip", "clips", "فيديو", "فيديوهات", "مقطع", "مقاطع", "short", "shorts", "شورت", "شورتس"}:
            continue
        seen.add(key)
        out.append(keyword)
        if len(out) >= limit:
            break
    return out


def _generate_local_platform_metadata(
    cfg: Config,
    video_path: str,
    hint_title: Optional[str],
    lang: str,
    platform: str,
    channel_key: str,
    channel_name: Optional[str] = None,
    source_description: Optional[str] = None,
    content_type: Optional[str] = None,
    source_context: Optional[Dict[str, Any]] = None,
) -> tuple[str, list[str], str, bool]:
    target_lang = _normalize_lang_code(lang)
    topic = hint_title or source_description or str(content_type or "").replace("_", " ")
    try:
        history = _get_meta_history(cfg, channel_key, target_lang) if channel_key else []
    except Exception:
        history = []

    try:
        max_attempts = int(os.getenv("LOCAL_METADATA_MAX_ATTEMPTS", "4") or 4)
    except Exception:
        max_attempts = 4
    max_attempts = max(1, min(6, max_attempts))

    last_result: Optional[tuple[str, list[str], str, bool]] = None
    had_similarity_rejection = False
    for attempt in range(max_attempts):
        candidate = generate_local_metadata_candidate(
            video_path=video_path,
            hint_title=hint_title or "",
            source_description=source_description or "",
            lang=target_lang,
            channel_key=channel_key or "",
            channel_name=channel_name or "",
            content_type=content_type or "",
            source_context=source_context,
            attempt=attempt,
        )
        min_tags = candidate.get("min_hashtags") or 4
        max_tags = candidate.get("max_hashtags") or 7
        tags = _sanitize_hashtag_list(
            list(candidate.get("hashtags") or []) + _heuristic_hashtags(topic or hint_title, target_lang, limit=max(int(max_tags) + 4, 10)),
            target_lang,
            max(int(max_tags or 7), int(min_tags or 4)),
        )
        title = _normalize_local_title(candidate.get("title"), topic, target_lang)
        desc = _normalize_local_description(candidate.get("description"), title, topic, tags, target_lang, int(min_tags), int(max_tags))

        def _dominant_language_ok(text: str, tl: str) -> bool:
            if not text:
                return False
            primary = _lang_primary(tl)
            ranges = _LANG_SCRIPT_RANGES.get(primary)
            if ranges:
                any_target = False
                for ch in text:
                    if not ch.isalpha():
                        continue
                    if _char_in_ranges(ch, ranges):
                        any_target = True
                        continue
                    cp = ord(ch)
                    if cp < 128:
                        return False
                return any_target
            if primary.startswith("en"):
                return not _contains_arabic(text)
            return True

        def _enforce_lang_bundle(title_in: str, tags_in: List[str], desc_in: str, tl: str) -> tuple[str, List[str], str]:
            tl = _normalize_lang_code(tl)
            needs_fix = False
            try:
                if not _dominant_language_ok(title_in, tl) or not _dominant_language_ok(desc_in, tl):
                    needs_fix = True
                else:
                    joined = (title_in or "") + "\n" + (desc_in or "")
                    if tl and not tl.startswith("en") and _is_mostly_english(joined):
                        needs_fix = True
            except Exception:
                needs_fix = False

            if not needs_fix:
                fixed_tags = _filter_hashtags_by_target_language(list(tags_in or []), tl)
                return title_in, fixed_tags, desc_in

            plain_title = _strip_hashtags_from_text(title_in)
            plain_desc = _strip_hashtags_from_text(desc_in)
            key_wo_hash = [t[1:] if isinstance(t, str) and t.startswith("#") else str(t or "") for t in (tags_in or [])]

            tr_title = None
            tr_desc = None
            tr_keys = None
            try:
                tr_batch = translate_batch(cfg, {"title": plain_title, "desc": plain_desc}, tl)
                tr_title = tr_batch.get("title")
                tr_desc = tr_batch.get("desc")
            except Exception:
                tr_title = None
                tr_desc = None
            try:
                tr_keys = translate_keywords(cfg, key_wo_hash, tl)
            except Exception:
                tr_keys = None

            out_title = (tr_title or translate_text(cfg, plain_title, tl) or title_in).strip()
            out_desc = (tr_desc or translate_text(cfg, plain_desc, tl) or desc_in).strip()
            out_tags = [f"#{k.strip().replace(' ', '')}" for k in (tr_keys or key_wo_hash) if k and str(k).strip()]
            out_tags = _filter_hashtags_by_target_language(_sanitize_hashtag_list(out_tags, tl, 24), tl)

            if not _dominant_language_ok(out_title, tl) or not _dominant_language_ok(out_desc, tl):
                fb_title, fb_tags, fb_desc = _fallback_metadata_bundle(topic or hint_title or "", tl)
                out_title = _normalize_local_title(fb_title, topic, tl)
                out_desc = _normalize_local_description(fb_desc, out_title, topic, fb_tags, tl)
                out_tags = _filter_hashtags_by_target_language(_sanitize_hashtag_list(fb_tags, tl, 24), tl)
            return out_title, out_tags, out_desc

        title, tags, desc = _enforce_lang_bundle(title, tags, desc, target_lang)
        last_result = (title, tags, desc, True)
        if not history or not _is_meta_too_similar(title, desc, history):
            if channel_key:
                _append_meta_history(cfg, channel_key, target_lang, title, desc)
            return last_result
        had_similarity_rejection = True
        logger.warning(f"♻️ Local metadata rejected due to similarity (Attempt {attempt+1}/{max_attempts})")

    if not last_result:
        fallback_title, fallback_tags, fallback_desc = _fallback_metadata_bundle(topic, target_lang)
        title = _normalize_local_title(fallback_title, topic, target_lang)
        desc = _normalize_local_description(fallback_desc, title, topic, fallback_tags, target_lang)
        if channel_key:
            _append_meta_history(cfg, channel_key, target_lang, title, desc)
        return title, fallback_tags, desc, True

    title, tags, desc, _ = last_result
    if had_similarity_rejection:
        title = _make_plain_title_more_distinct(title, topic, target_lang)
        desc = _normalize_local_description(desc, title, topic, tags, target_lang)
    if channel_key:
        _append_meta_history(cfg, channel_key, target_lang, title, desc)
    return title, tags, desc, True


def _metadata_cache_key(platform: str, channel_key: str, lang: str, video_path: str, hint_title: Optional[str]) -> str:
    try:
        size = os.path.getsize(video_path) if video_path and os.path.exists(video_path) else 0
    except Exception:
        size = 0
    parts = [
        "v3",
        (platform or "youtube").strip().lower(),
        (channel_key or "").strip().lower(),
        (lang or "ar").strip().lower(),
        os.path.basename(video_path or ""),
        str(size),
        _topic_to_plain_text(hint_title),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8", errors="ignore")).hexdigest()


def _get_cached_platform_metadata(cfg: Config, platform: str, channel_key: str, lang: str, video_path: str, hint_title: Optional[str]) -> Optional[Tuple[str, List[str], str, bool]]:
    key = _metadata_cache_key(platform, channel_key, lang, video_path, hint_title)
    try:
        st = load_state(cfg)
    except Exception:
        return None

    entry = ((st.get("ai_platform_metadata_cache") or {}).get(key) or {})
    if not isinstance(entry, dict):
        return None
    title = (entry.get("title") or "").strip()
    desc = (entry.get("description") or "").strip()
    tags = entry.get("tags") or []
    if not title or not isinstance(tags, list):
        return None
    return title, tags, desc, True


def _store_cached_platform_metadata(cfg: Config, platform: str, channel_key: str, lang: str, video_path: str, hint_title: Optional[str], title: str, tags: List[str], description: str) -> None:
    key = _metadata_cache_key(platform, channel_key, lang, video_path, hint_title)
    now = time.time()

    def _upd(st):
        cache = st.setdefault("ai_platform_metadata_cache", {})
        cache[key] = {
            "title": title,
            "tags": list(tags or []),
            "description": description,
            "updated_at": now,
        }
        if len(cache) > 120:
            ordered = sorted(cache.items(), key=lambda item: float((item[1] or {}).get("updated_at") or 0.0), reverse=True)
            st["ai_platform_metadata_cache"] = dict(ordered[:120])

    try:
        update_state(cfg, _upd)
    except Exception:
        pass


def _split_and_dedupe_api_keys(raw: str) -> List[str]:
    keys: List[str] = []
    seen = set()
    for token in (raw or "").replace("\n", ",").replace("\r", ",").split(","):
        key = token.strip()
        if not key or key in seen:
            continue
        keys.append(key)
        seen.add(key)
    return keys


def _load_mistral_api_keys(cfg: Config) -> List[str]:
    env_keys = _split_and_dedupe_api_keys(
        (os.getenv("MISTRAL_API_KEYS") or "") + "," + (cfg.MISTRAL_API_KEY or "")
    )
    if env_keys:
        return env_keys

    try:
        from .supabase_storage import load_mistral_state

        state_keys = load_mistral_state().get("keys", {})
        if isinstance(state_keys, dict):
            remote_keys = _split_and_dedupe_api_keys("\n".join(state_keys.keys()))
            if remote_keys:
                return remote_keys
    except Exception:
        pass

    try:
        st = load_state(cfg)
        ai_manager = st.get("ai_manager") if isinstance(st, dict) else {}
        provider_state = ai_manager.get("mistral") if isinstance(ai_manager, dict) else {}
        raw_keys = (provider_state.get("active_keys") or provider_state.get("keys") or []) if isinstance(provider_state, dict) else []
        if isinstance(raw_keys, list):
            return _split_and_dedupe_api_keys("\n".join(str(k or "").strip() for k in raw_keys))
    except Exception:
        pass

    return []


def _mistral_endpoint(cfg: Config) -> Optional[str]:
    # Deprecated in favor of OpenRouterManager, but kept for legacy config reference if needed
    if cfg.MISTRAL_PROXY_URL:
        return cfg.MISTRAL_PROXY_URL.rstrip("/") + "/v1/chat/completions"
    if _load_mistral_api_keys(cfg):
        return "https://api.mistral.ai/v1/chat/completions"
    return None


def _normalize_mistral_model_name(model: str) -> str:
    m = (model or "").strip()
    if not m:
        return m

    key = m.strip().lower().replace("_", "-")
    key = re.sub(r"\s+", " ", key)

    latest_map = {
        "mistral-large-latest": "mistral-large-2512",
        "mistral-large-3-latest": "mistral-large-2512",
        "ministral-3b-latest": "ministral-3b-2512",
        "ministral-8b-latest": "ministral-8b-2512",
        "ministral-14b-latest": "ministral-14b-2512",
    }
    if key in latest_map:
        return latest_map[key]

    display_map = {
        "mistral large 3": "mistral-large-2512",
        "mistral-large 3": "mistral-large-2512",
        "mistral large 2.1": "mistral-large-2411",
        "mistral-large 2.1": "mistral-large-2411",
        "mistral small 3.2": "mistral-small-2506",
        "mistral-small 3.2": "mistral-small-2506",
        "mistral medium 3.1": "mistral-medium-2508",
        "mistral-medium 3.1": "mistral-medium-2508",
        "mistral medium 3": "mistral-medium-2505",
        "mistral-medium 3": "mistral-medium-2505",
        "magistral medium 1.2": "magistral-medium-2509",
        "magistral-medium 1.2": "magistral-medium-2509",
        "magistral small 1.2": "magistral-small-2509",
        "magistral-small 1.2": "magistral-small-2509",
        "ministral 3 14b": "ministral-14b-2512",
        "ministral 3 8b": "ministral-8b-2512",
        "ministral 3 3b": "ministral-3b-2512",
        "mistral nemo 12b": "open-mistral-nemo",
    }
    if key in display_map:
        return display_map[key]

    return m


def _mistral_models_list() -> List[str]:
    """Build the Mistral model list priority:
       1. User-saved models via ai_models_store
       2. MISTRAL_MODEL env (single)
       3. Default
    """
    models: List[str] = []
    try:
        from . import ai_models_store
        saved = ai_models_store.get_models("mistral")
        if saved:
            models.extend(saved)
    except Exception:
        pass
    if not models:
        single_env = (os.getenv("MISTRAL_MODEL") or "").strip()
        if single_env:
            models = [single_env]
        else:
            models = ["mistral-large-latest"]
    return models


def _call_mistral_chat(cfg: Config, prompt: str, system_prompt: Optional[str] = None) -> Tuple[Optional[str], int, Optional[int], str]:
    """Call Mistral API.

    Iterates through all available Mistral keys × all configured models.
    Returns the first successful response. On model-level failures (404, 400,
    deprecation), tries the next model with the same key. On key-level
    failures (invalid_key, rate_limit, quota_exhausted), tries the next key.
    """
    try:
        if _is_ai_backed_off(cfg):
            return None, 0, None, "backoff"

        mistral_api_keys = _load_mistral_api_keys(cfg)
        endpoint = _mistral_endpoint(cfg)
        if not endpoint or not mistral_api_keys:
            return None, 0, None, "no_key"

        models = _mistral_models_list()
        # Normalize each model name
        models = [_normalize_mistral_model_name(m) for m in models if (m or "").strip()]
        # De-duplicate preserving order
        seen = set()
        deduped: List[str] = []
        for m in models:
            if m and m not in seen:
                seen.add(m)
                deduped.append(m)
        models = deduped or ["mistral-large-latest"]

        try:
            max_tokens = int(os.getenv("MISTRAL_MAX_TOKENS", "420") or 420)
        except Exception:
            max_tokens = 420
        max_tokens = max(64, min(1200, max_tokens))

        try:
            temperature = float(os.getenv("MISTRAL_TEMPERATURE", "0.8") or 0.8)
        except Exception:
            temperature = 0.8
        temperature = max(0.0, min(1.5, temperature))

        try:
            max_keys = int(os.getenv("MISTRAL_MAX_KEYS_PER_REQUEST", "2") or "2")
        except Exception:
            max_keys = 2
        max_keys = max(1, min(5, max_keys))

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        timeout = int(os.getenv("MISTRAL_TIMEOUT", "25") or 25)

        last_status = 0
        last_retry_after: Optional[int] = None
        last_category = "empty"

        tried_keys = set()
        for _ in range(min(max_keys, len(mistral_api_keys))):
            # Rotate to next key
            api_key = None
            for k in mistral_api_keys:
                if k not in tried_keys:
                    api_key = k
                    break
            if not api_key:
                break
            tried_keys.add(api_key)

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }

            for model in models:
                logger.info(f"🤖 Trying Mistral (Key: ...{api_key[-8:]}, Model: {model})")
                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                try:
                    resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
                except requests.exceptions.Timeout:
                    last_status, last_retry_after, last_category = 0, None, "timeout"
                    continue  # Try next model
                except requests.exceptions.RequestException:
                    last_status, last_retry_after, last_category = 0, None, "network"
                    continue  # Try next model

                status = int(getattr(resp, "status_code", 0) or 0)
                retry_after = None
                try:
                    ra = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
                    if ra:
                        retry_after = int(float(str(ra).strip()))
                except Exception:
                    retry_after = None

                if status == 200:
                    try:
                        data = resp.json() or {}
                    except Exception:
                        data = {}
                    choices = data.get("choices") or []
                    if choices:
                        msg = (choices[0].get("message") or {})
                        out = (msg.get("content") or "").strip()
                        if out:
                            return out, status, retry_after, "ok"
                    last_status, last_retry_after, last_category = status, retry_after, "empty"
                    continue  # Try next model

                # Error handling
                err_text = ""
                try:
                    j = resp.json() or {}
                    err_text = (j.get("message") or (j.get("error") or {}).get("message") or "")
                except Exception:
                    try:
                        err_text = (resp.text or "")[:500]
                    except Exception:
                        err_text = ""
                et = err_text.lower() if err_text else ""

                last_status, last_retry_after, last_category = status, retry_after, "other"

                if status in {401, 403}:
                    last_category = "invalid_key"
                    # Try next key
                    break
                if status == 429:
                    last_category = "quota_exhausted" if ("quota" in et or "exceeded" in et) else "rate_limit"
                    # Try next key
                    break
                if status >= 500:
                    last_category = "transient"
                    # Try next model
                    continue
                if status == 400:
                    last_category = "bad_request"
                    # Model likely unsupported — try next model
                    continue
                # Other 4xx — try next model
                continue

            # If we broke out of the model loop due to key-level error, try next key
            if last_category in {"invalid_key", "quota_exhausted", "rate_limit"}:
                continue
            # If we got an OK we would have returned already.
            # If all models failed with bad_request / transient, no point trying same key set
            # — but try next key anyway for transient cases.

        return None, last_status, last_retry_after, last_category
    except Exception:
        return None, 0, None, "exception"

def _call_gemini_with_key(
    prompt: str,
    api_key: str,
    model: Optional[str] = None,
    timeout: int = 20,
) -> Tuple[Optional[str], int, Optional[int], str]:
    try:
        mdl = (model or os.getenv("GEMINI_MODEL") or "gemini-1.5-flash-latest").strip()
        api_ver = (os.getenv("GEMINI_API_VERSION") or "v1beta").strip()
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.8, "maxOutputTokens": 1000},
        }
        headers = {"Content-Type": "application/json"}

        url = f"https://generativelanguage.googleapis.com/{api_ver}/models/{mdl}:generateContent?key={api_key}"
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        status = int(getattr(resp, "status_code", 0) or 0)
        retry_after = None
        try:
            ra = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
            if ra:
                retry_after = int(float(str(ra).strip()))
        except Exception:
            retry_after = None
        if status == 404 and api_ver != "v1":
            api_ver = "v1"
            url = f"https://generativelanguage.googleapis.com/{api_ver}/models/{mdl}:generateContent?key={api_key}"
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            status = int(getattr(resp, "status_code", 0) or 0)
            try:
                ra = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
                if ra:
                    retry_after = int(float(str(ra).strip()))
            except Exception:
                retry_after = None
        if status >= 400:
            err_text = ""
            try:
                j = resp.json() or {}
                err_text = ((j.get("error") or {}).get("message") or "")
            except Exception:
                try:
                    err_text = (resp.text or "")[:500]
                except Exception:
                    err_text = ""

            et = err_text.lower() if err_text else ""
            if status in {401, 403}:
                return None, status, retry_after, "invalid_key"
            if status == 429:
                if "quota" in et or "exceeded" in et:
                    return None, status, retry_after, "quota_exhausted"
                return None, status, retry_after, "rate_limit"
            if status >= 500:
                return None, status, retry_after, "transient"
            if status == 400:
                return None, status, retry_after, "bad_request"
            return None, status, retry_after, "other"

        data = resp.json() or {}
        candidates = data.get("candidates") or []
        if not candidates:
            return None, status, retry_after, "empty"
        content = (candidates[0].get("content") or {}).get("parts") or []
        if not content:
            return None, status, retry_after, "empty"
        texts = [p.get("text") for p in content if isinstance(p, dict) and p.get("text")]
        out = "\n".join(texts).strip() if texts else None
        return out, status, retry_after, ("ok" if out else "empty")
    except requests.exceptions.Timeout:
        return None, 0, None, "timeout"
    except requests.exceptions.RequestException:
        return None, 0, None, "network"
    except Exception:
        return None, 0, None, "exception"


def _call_gemini(cfg: Config, prompt: str) -> Optional[str]:
    """
    استدعاء Gemini مع نظام إدارة المفاتيح الاحترافي 🆕
    يدعم قائمة نماذج متعددة يحدها المستخدم (via Telegram UI) — عند فشل أحد
    النماذج (rate limit / quota / 404 / 400) ينتقل تلقائياً إلى النموذج التالي.
    """
    try:
        if _is_ai_backed_off(cfg):
            return None
        key_manager = get_key_manager()

        max_attempts = int(os.getenv("GEMINI_MAX_ATTEMPTS", "2") or "2")
        base_sleep = float(os.getenv("GEMINI_RETRY_BASE_SLEEP", "0.2") or "0.2")

        # Build the model list priority:
        #   1. User-saved models from ai_models_store (Telegram UI)
        #   2. GEMINI_MODEL_LIST env var
        #   3. GEMINI_MODEL env var (single)
        #   4. Hardcoded defaults
        models: List[str] = []
        try:
            from . import ai_models_store
            saved = ai_models_store.get_models("gemini")
            if saved:
                models.extend(saved)
        except Exception:
            pass

        if not models:
            model_list_env = os.getenv("GEMINI_MODEL_LIST") or ""
            env_models = [m.strip() for m in model_list_env.replace("\n", ",").split(",") if m.strip()]
            if env_models:
                models = env_models
            else:
                single_env = (os.getenv("GEMINI_MODEL") or "").strip()
                if single_env:
                    models = [single_env]
                else:
                    models = [
                        "gemini-1.5-flash",
                        "gemini-1.5-flash-8b",
                        "gemini-1.5-flash-latest",
                    ]

        tried = set()

        for attempt in range(max(1, max_attempts)):
            _ai_throttle(cfg)
            api_key = key_manager.get_next_key()
            if not api_key:
                break
            if api_key in tried:
                continue
            tried.add(api_key)

            result = None
            status = 0
            retry_after = None
            category = "empty"

            # Try each model with this key; break only on key-level failures
            # (invalid_key, rate_limit, quota_exhausted). On model-level failures
            # (404 Not Found / 400 Bad Request) — try the NEXT model with same key.
            for mdl in models:
                result, status, retry_after, category = _call_gemini_with_key(
                    prompt,
                    api_key,
                    model=mdl,
                    timeout=int(os.getenv("GEMINI_TIMEOUT", "25") or "25"),
                )
                if result:
                    key_manager.mark_request(api_key, success=True, status_code=status, error_category="ok")
                    return result

                # Key-level failures → don't try other models with this key
                if category in {"rate_limit", "quota_exhausted", "invalid_key"}:
                    break

                # bad_request / 404 / 500 / transient / network / timeout / empty
                # → try the next model with the same key
                logger.info(
                    f"🔄 Gemini model '{mdl}' returned category={category} (status={status}); "
                    f"trying next model if available."
                )

            # Mark the key with the final failure category
            key_manager.mark_request(api_key, success=False, status_code=status, error_category=category, retry_after_seconds=retry_after)

            if category in {"bad_request"}:
                break

            if category in {"rate_limit", "quota_exhausted"}:
                try:
                    _set_ai_backoff(cfg, int(os.getenv("AI_BACKOFF_SECONDS", "120") or "120"))
                except Exception:
                    pass
                break

            delay = base_sleep * (2 ** min(attempt, 6))
            if retry_after is not None:
                delay = max(delay, float(retry_after))
            delay = delay + random.random() * min(1.0, delay)
            time.sleep(min(8.0, delay))
    except Exception as e:
        logger.error(f"Gemini call error: {e}")
    return None

def _generate_content_with_failover(cfg: Config, prompt: str) -> Optional[str]:
    """
    Robust generation strategy with automatic service detection:
    - Skips services without API keys
    - Skips providers/keys currently in backoff (via ai_quota_tracker)
    - Tries available services in smart order (most available keys first)
    - Falls back gracefully between providers
    - Marks provider/key failures so they're not retried until their backoff expires
    """
    from . import ai_quota_tracker

    if _is_ai_backed_off(cfg):
        logger.info("⏸️ AI is in global backoff mode, skipping generation.")
        return None

    # Periodic cleanup of expired entries (cheap — only mutates if needed)
    try:
        ai_quota_tracker.cleanup_expired()
    except Exception:
        pass

    order = (os.getenv("AI_PROVIDER_ORDER") or "smart").strip().lower()
    valid_orders = {"mistral_first", "openrouter_first", "groq_first",
                    "clarifai_first", "gemini_first", "all", "smart"}
    if order == "gemini_first":
        # Gemini is now fully supported — gemini_first is a valid mode
        pass
    elif order not in valid_orders:
        order = "smart"

    system_prompt = "You are a creative social media manager specializing in Minecraft and YouTube Shorts. Output exactly what is requested."

    # ─── Gather available keys per provider ───
    orm = get_openrouter_manager()
    groqm = get_groq_manager()
    clarqm = get_clarifai_manager()
    gemini_km = get_key_manager()

    openrouter_keys = list(orm.api_keys or [])
    groq_keys = list(groqm.api_keys or [])
    clarifai_keys = list(clarqm.api_keys or [])
    gemini_keys = list(gemini_km.api_keys or gemini_km.keys or [])
    mistral_keys = _load_mistral_api_keys(cfg)

    # ─── Helper: provider key availability check ───
    def _has_openrouter_keys():
        return bool(ai_quota_tracker.get_available_keys("openrouter", openrouter_keys))

    def _has_groq_keys():
        return bool(ai_quota_tracker.get_available_keys("groq", groq_keys))

    def _has_clarifai_keys():
        return bool(ai_quota_tracker.get_available_keys("clarifai", clarifai_keys))

    def _has_gemini_keys():
        return bool(ai_quota_tracker.get_available_keys("gemini", gemini_keys))

    def _has_mistral_keys():
        if (os.getenv("DISABLE_MISTRAL") or "").strip().lower() in {"1", "true", "yes", "on"}:
            return False
        return bool(ai_quota_tracker.get_available_keys("mistral", mistral_keys))

    # ─── Try functions: mark quota tracker on success/failure ───
    def _try_mistral():
        if not _has_mistral_keys():
            logger.debug("⏭️ Skipping Mistral (no available keys)")
            return None
        _ai_throttle(cfg)
        content, status, retry_after, category = _call_mistral_chat(cfg, prompt, system_prompt=system_prompt)
        if content:
            ai_quota_tracker.mark_provider_success("mistral")
            # Mistral keys are managed internally — also mark success for the first key
            # (the manager handles its own rotation, but we track provider-level state here)
            logger.info("✅ Mistral successful.")
            return content
        logger.info(f"⚠️ Mistral failed (category={category}).")
        if category in {"rate_limit", "quota_exhausted"}:
            ai_quota_tracker.mark_provider_failure("mistral", error_category=category)
        elif category in {"invalid_key", "transient", "network", "timeout"}:
            ai_quota_tracker.mark_provider_failure("mistral", error_category=category)
        return None

    def _try_openrouter():
        if not _has_openrouter_keys():
            logger.debug("⏭️ Skipping OpenRouter (no available keys)")
            return None
        _ai_throttle(cfg)
        content = orm.completion_with_fallback(prompt, system_prompt=system_prompt)
        if content:
            ai_quota_tracker.mark_provider_success("openrouter")
            logger.info("✅ OpenRouter successful.")
            return content
        logger.info("⚠️ OpenRouter failed (all keys/models exhausted).")
        # Heuristic: if all keys/models failed, treat as quota-exhausted
        ai_quota_tracker.mark_provider_failure("openrouter", error_category="quota_exhausted")
        return None

    def _try_groq():
        if not _has_groq_keys():
            logger.debug("⏭️ Skipping Groq (no available keys)")
            return None
        _ai_throttle(cfg)
        content = groqm.completion_with_fallback(
            prompt,
            system_prompt=system_prompt,
            model=os.getenv("GROQ_MODEL") or None,  # None → use saved models list
        )
        if content:
            ai_quota_tracker.mark_provider_success("groq")
            logger.info("✅ Groq successful.")
            return content
        logger.info("⚠️ Groq failed (all keys/models exhausted).")
        ai_quota_tracker.mark_provider_failure("groq", error_category="quota_exhausted")
        return None

    def _try_clarifai():
        if not _has_clarifai_keys():
            logger.debug("⏭️ Skipping Clarifai (no available keys)")
            return None
        _ai_throttle(cfg)
        content = clarqm.completion_with_fallback(
            prompt,
            system_prompt=system_prompt,
            model=os.getenv("CLARIFAI_MODEL") or None,
        )
        if content:
            ai_quota_tracker.mark_provider_success("clarifai")
            logger.info("✅ Clarifai successful.")
            return content
        logger.info("⚠️ Clarifai failed (all keys/models exhausted).")
        ai_quota_tracker.mark_provider_failure("clarifai", error_category="quota_exhausted")
        return None

    def _try_gemini():
        if not _has_gemini_keys():
            logger.debug("⏭️ Skipping Gemini (no available keys)")
            return None
        _ai_throttle(cfg)
        content = _call_gemini(cfg, prompt)
        if content:
            ai_quota_tracker.mark_provider_success("gemini")
            logger.info("✅ Gemini successful.")
            return content
        logger.info("⚠️ Gemini failed (all keys/models exhausted).")
        ai_quota_tracker.mark_provider_failure("gemini", error_category="quota_exhausted")
        return None

    # ─── Provider order resolution ───
    provider_funcs = {
        "openrouter": _try_openrouter,
        "groq":       _try_groq,
        "clarifai":   _try_clarifai,
        "mistral":    _try_mistral,
        "gemini":     _try_gemini,
    }

    # Build the ordered list of providers to try
    if order == "smart":
        # Smart mode: use quota_tracker to prioritize providers
        provider_keys_map = {
            "openrouter": openrouter_keys,
            "groq":       groq_keys,
            "clarifai":   clarifai_keys,
            "mistral":    mistral_keys,
            "gemini":     gemini_keys,
        }
        # Only consider providers that have at least one key configured
        candidate_providers = [p for p, keys in provider_keys_map.items() if keys]
        if not candidate_providers:
            logger.error("❌ No AI services have API keys configured!")
            return None

        # Filter to providers that are NOT currently blocked
        available_providers = [p for p in candidate_providers
                               if ai_quota_tracker.is_provider_available(p)]
        if not available_providers:
            logger.warning("⏸️ All configured providers are currently in backoff. Skipping AI generation.")
            return None

        # Get smart order (most available keys first, with quota penalty)
        ordered = ai_quota_tracker.get_smart_provider_order(available_providers, provider_keys_map)
        if not ordered:
            logger.warning("⏸️ No providers with available keys right now. Skipping AI generation.")
            return None

        logger.info(f"🤖 Smart AI provider order: {ordered}")

        for prov_name in ordered:
            try:
                content = provider_funcs[prov_name]()
                if content:
                    return content
            except Exception as e:
                logger.error(f"❌ {prov_name} raised exception: {e}")
                ai_quota_tracker.mark_provider_failure(prov_name, error_category="other")
                continue

    else:
        # Explicit ordering modes (still skip providers in backoff)
        order_map = {
            "mistral_first":     ["mistral", "openrouter", "groq", "clarifai", "gemini"],
            "openrouter_first":  ["openrouter", "groq", "clarifai", "mistral", "gemini"],
            "groq_first":        ["groq", "openrouter", "clarifai", "mistral", "gemini"],
            "clarifai_first":    ["clarifai", "openrouter", "groq", "mistral", "gemini"],
            "gemini_first":      ["gemini", "openrouter", "groq", "clarifai", "mistral"],
            "all":               ["openrouter", "groq", "clarifai", "mistral", "gemini"],
        }
        ordered = order_map.get(order, order_map["all"])
        for prov_name in ordered:
            # Skip if provider is in backoff
            if not ai_quota_tracker.is_provider_available(prov_name):
                logger.debug(f"⏭️ Skipping {prov_name} (in provider-level backoff)")
                continue
            try:
                content = provider_funcs[prov_name]()
                if content:
                    return content
            except Exception as e:
                logger.error(f"❌ {prov_name} raised exception: {e}")
                ai_quota_tracker.mark_provider_failure(prov_name, error_category="other")
                continue

    logger.error("❌ All AI generation methods failed.")
    try:
        _set_ai_backoff(cfg, int(os.getenv("AI_BACKOFF_SECONDS", "300") or "300"))
    except Exception:
        pass
    return None


def _style_index(seed: str, n: int = 8) -> int:
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % n


def generate_title_and_hashtags(cfg: Config, video_path: str, hint_title: Optional[str] = None, lang: Optional[str] = None, override_key: Optional[str] = None) -> tuple[str, list[str], bool]:
    base_title, base_tags = _fallback_title_and_tags(hint_title, lang or os.getenv("GEN_LANG", "ar"))

    target_lang = _normalize_lang_code(lang or os.getenv("GEN_LANG", "ar"))
    seed = f"{os.path.basename(video_path)}|{base_title}|{target_lang}"
    sidx = _style_index(seed, 8)
    # Content mode context (games/minecraft)
    try:
        st = load_state(cfg)
    except Exception:
        st = {}
    content_mode = (st.get("content_mode") or os.getenv("CONTENT_MODE") or "").strip().lower()
    extra = []
    if content_mode in {"games", "minecraft"}:
        extra.append("Focus on gaming / Minecraft audience.")
        extra.append("Include very popular game-related hashtags.")
        if content_mode == "minecraft":
            extra.append("Strongly favor Minecraft-related hashtags like #minecraft, #minecraftshorts, etc.")
    extra_guidance = ("\n- " + "\n- ".join(extra)) if extra else ""
    prompt = (
        f"TargetLanguage: {target_lang}\n"
        "Task: Create a high-SEO set of 8-12 UNIQUE hashtags to be used as the YouTube Shorts TITLE. The title MUST be only hashtags separated by spaces (no normal words). Use ONLY the target language hashtags and do NOT mix hashtags from any other language.\n"
        f"InputHint: {base_title}\n"
        "Constraints:\n"
        "- Title line: ONLY hashtags, no plain text, no quotes.\n"
        "- Tags MUST be unique: do not repeat the same root word with small variations (avoid duplicates like #MinecraftShorts #MinecraftShortVideos together).\n"
        "- Prefer short, high-engagement tags related to the video topic (e.g. Minecraft, gaming, reactions).\n"
        "- If Arabic, use simple dialect hashtags without tashkil; you may also mix English game names.\n"
        f"{extra_guidance}\n"
        f"StyleIndex: {sidx}\n"
        "Styles:\n"
        "0) Hook question that invites curiosity.\n"
        "1) Strong statement with a power verb and clear subject.\n"
        "2) Light colloquial tone appropriate to the language/dialect.\n"
        "3) Honest teaser with hype but no exaggeration.\n"
        "4) Bracket tag at the start like [Reaction] / [رد فعل] depending on language.\n"
        "5) One subtle emoji at the end (optional).\n"
        "6) Two short phrases split by — or : for rhythm.\n"
        "7) Playful wordplay/pun if culturally appropriate.\n"
        "Return exactly two lines:\n"
        "Title: <title>\nHashtags: #shorts #tag2 #tag3 #tag4 #tag5"
    )

    # Use robust failover strategy
    content = _generate_content_with_failover(cfg, prompt)
    
    if not content:
        return base_title, base_tags, False

    # استخراج بسيط
    title_line = None
    tags_line = None
    for line in content.splitlines():
        line = line.strip()
        if line.lower().startswith("title:"):
            title_line = line.split(":", 1)[1].strip()
        elif line.lower().startswith("hashtags:"):
            tags_line = line.split(":", 1)[1].strip()
    title = title_line or base_title
    tags = base_tags
    if tags_line:
        tags = [t for t in tags_line.split() if t.startswith("#")]
        if not tags:
            tags = base_tags
    # Language enforcement: if target language is English but output appears Arabic, translate hashtags
    if (target_lang or "").lower().startswith("en"):
        if _contains_arabic(title) or any(_contains_arabic(t) for t in tags):
            try:
                # Translate keywords (without '#') then rebuild
                from .ai import translate_keywords  # self-import safe in same module
                key_wo_hash = [t[1:] if t.startswith("#") else t for t in tags]
                tr_keys = translate_keywords(cfg, key_wo_hash, "en") or key_wo_hash
                tags = [f"#{k.strip().replace(' ', '')}" for k in tr_keys if k and k.strip()]
                if tags:
                    title = " ".join(tags[:14])
            except Exception:
                pass
    try:
        title_tags, desc_tags = optimize_hashtags(tags, target_lang, topic=base_title, limit_title=10, limit_desc=24)
        tags = desc_tags
        title = " ".join(title_tags)
        if len(title) > 95:
            title = " ".join(title_tags[:8])
    except Exception:
        pass
    return title, tags, True



# Helper to detect if text is predominantly English
def _is_mostly_english(text: str) -> bool:
    if not text: return False
    # Remove common symbols and numbers
    cleaned = ''.join(c for c in text if c.isalpha() or c.isspace())
    if not cleaned: return False
    # Count ascii letters
    ascii_count = sum(1 for c in cleaned if ord(c) < 128)
    return (ascii_count / len(cleaned)) > 0.8


def _normalize_lang_code(lang: Optional[str]) -> str:
    raw = (lang or "").strip().lower()
    if not raw:
        return "ar"
    raw = raw.replace("_", "-")
    primary = raw.split("-", 1)[0].strip()
    return primary or "ar"


def _normalize_meta_text(text: Optional[str]) -> str:
    try:
        s = (text or "").strip().lower()
        if not s:
            return ""
        # Preserve hashtag bodies so hashtag-only titles/descriptions can be compared for similarity.
        # Example: "#MinecraftShorts #Funny" -> "minecraftshorts funny"
        s = re.sub(r"#([^\s]+)", r"\1", s)
        s = "".join(ch if (ch.isspace() or ch.isalnum() or ch == "_" or unicodedata.category(ch).startswith("M")) else " " for ch in s)
        s = re.sub(r"\s+", " ", s).strip()
        return s
    except Exception:
        return (text or "").strip().lower()


def _jaccard_similarity(a: str, b: str) -> float:
    try:
        a = _normalize_meta_text(a)
        b = _normalize_meta_text(b)
        if not a or not b:
            return 0.0
        sa = set(a.split())
        sb = set(b.split())
        if not sa or not sb:
            return 0.0
        inter = len(sa & sb)
        union = len(sa | sb)
        return float(inter) / float(union) if union else 0.0
    except Exception:
        return 0.0


def _is_meta_too_similar(title: str, desc: str, items: List[Dict[str, Any]]) -> bool:
    nt = _normalize_meta_text(title)
    nd = _normalize_meta_text(desc)
    if not nt:
        return True
    for it in items or []:
        try:
            ot = _normalize_meta_text(it.get("title"))
            od = _normalize_meta_text(it.get("desc"))
        except Exception:
            ot = ""
            od = ""
        if ot and nt == ot:
            return True
        if ot and (nt in ot or ot in nt) and len(nt) >= 24 and len(ot) >= 24:
            return True
        if _jaccard_similarity(nt, ot) >= 0.82:
            return True
        if nd and od and _jaccard_similarity(nd, od) >= 0.88:
            return True
    return False


def _get_meta_history(cfg: Config, channel_key: str, lang: str) -> List[Dict[str, Any]]:
    try:
        st = load_state(cfg)
    except Exception:
        return []
    try:
        root = st.get("ai_metadata_history") or {}
        shorts = root.get("shorts") or {}
        key = f"{(channel_key or '').strip()}::{(lang or '').strip().lower()}"
        items = shorts.get(key) or []
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _append_meta_history(cfg: Config, channel_key: str, lang: str, title: str, desc: str) -> None:
    try:
        limit = int(os.getenv("AI_META_HISTORY_LIMIT", "60") or 60)
    except Exception:
        limit = 60
    limit = max(5, min(300, limit))
    key = f"{(channel_key or '').strip()}::{(lang or '').strip().lower()}"

    def _upd(st):
        root = st.setdefault("ai_metadata_history", {})
        shorts = root.setdefault("shorts", {})
        items = shorts.get(key)
        if not isinstance(items, list):
            items = []
        items.append({"title": title, "desc": desc, "ts": time.time()})
        if len(items) > limit:
            items = items[-limit:]
        shorts[key] = items

    try:
        update_state(cfg, _upd)
    except Exception:
        try:
            st = load_state(cfg)
            _upd(st)
            save_state(st, cfg)
        except Exception:
            pass

def generate_title_desc_hashtags(cfg: Config, video_path: str, hint_title: Optional[str] = None, lang: Optional[str] = None, override_key: Optional[str] = None) -> tuple[str, list[str], str, bool]:
    base_title, base_tags = _fallback_title_and_tags(hint_title, lang or os.getenv("GEN_LANG", "ar"))
    target_lang = _normalize_lang_code(lang or os.getenv("GEN_LANG", "ar"))
    fallback_title, fallback_tags, fallback_desc = _fallback_metadata_bundle(base_title, target_lang)
    seed = f"{os.path.basename(video_path)}|{base_title}|{target_lang}|{(override_key or '').strip()}"
    sidx = _style_index(seed, 8)
    try:
        st = load_state(cfg)
    except Exception:
        st = {}
    content_mode = (st.get("content_mode") or os.getenv("CONTENT_MODE") or "").strip().lower()
    from .prompt_templates import PromptTemplates
    
    prompt = PromptTemplates.get_shorts_prompt_instruction(
        topic=base_title,
        title=base_title,
        lang=target_lang,
        style_seed=seed
    )

    content = _generate_content_with_failover(cfg, prompt)
    
    title = fallback_title or base_title
    tags = fallback_tags or base_tags
    desc = fallback_desc or base_title
    if not content:
        return title, tags, desc, False

    title_line = None
    tags_line = None
    desc_line = None
    for line in content.splitlines():
        line = line.strip()
        if line.lower().startswith("title:"):
            title_line = line.split(":", 1)[1].strip()
        elif line.lower().startswith("hashtags:"):
            tags_line = line.split(":", 1)[1].strip()
        elif line.lower().startswith("description:"):
            desc_line = line.split(":", 1)[1].strip()
    title = (title_line or fallback_title or base_title).strip()
    if tags_line:
        tks = [t for t in tags_line.split() if t.startswith("#")]
        if tks:
            tags = tks
    title_inline_tags = _extract_hashtags_from_text(title)
    if desc_line:
        desc = desc_line
    else:
        desc = fallback_desc
    desc_inline_tags = _extract_hashtags_from_text(desc)
    combined_seed_tags = list(tags or []) + title_inline_tags + desc_inline_tags + _heuristic_hashtags(base_title, target_lang, limit=14)
    if combined_seed_tags:
        _, tags = optimize_hashtags(combined_seed_tags, target_lang, topic=base_title, limit_title=4, limit_desc=18)

    tl = (target_lang or "").lower().strip()
    if tl:
        try:
            plain_title = _strip_hashtags_from_text(title)
            plain_desc = _strip_hashtags_from_text(desc)
            key_wo_hash = [t[1:] if isinstance(t, str) and t.startswith("#") else str(t or "") for t in (tags or [])]

            if tl.startswith("en"):
                need_fix = _contains_arabic(title) or _contains_arabic(desc) or any(_contains_arabic(t) for t in tags)
                if need_fix:
                    tr_batch = translate_batch(cfg, {"title": plain_title, "desc": plain_desc}, "en") if 'translate_batch' in globals() else {}
                    title = tr_batch.get("title") or translate_text(cfg, plain_title, "en") or title
                    desc = tr_batch.get("desc") or translate_text(cfg, plain_desc, "en") or desc
                    tr_keys = translate_keywords(cfg, key_wo_hash, "en") if 'translate_keywords' in globals() else key_wo_hash
                    tags = [f"#{k.strip().replace(' ', '')}" for k in (tr_keys or key_wo_hash) if k and k.strip()]
            else:
                tr_batch = translate_batch(cfg, {"title": plain_title, "desc": plain_desc}, tl) if 'translate_batch' in globals() else {}
                title = tr_batch.get("title") or translate_text(cfg, plain_title, tl) or title
                desc = tr_batch.get("desc") or translate_text(cfg, plain_desc, tl) or desc
                tr_keys = translate_keywords(cfg, key_wo_hash, tl) if 'translate_keywords' in globals() else key_wo_hash
                tags = [f"#{k.strip().replace(' ', '')}" for k in (tr_keys or key_wo_hash) if k and k.strip()]
        except Exception:
            pass

        tags = _filter_hashtags_by_target_language(_sanitize_hashtag_list(tags or [], tl, 24), tl)

    # Validate format: if model didn't follow expected schema, treat as failure so callers can fallback
    title = (title or "").replace("*", "").strip()
    desc = (desc or "").replace("*", "").strip()
    parsed_title_tags = _sanitize_hashtag_list(_extract_hashtags_from_text(title), tl or target_lang, 14)
    if isinstance(tags, list) and tags:
        tags = _sanitize_hashtag_list([t for t in tags if isinstance(t, str)], tl or target_lang, 24)
    if desc:
        try:
            d_tags = re.findall(r"#[^\s]+", desc)
            if d_tags:
                fixed = _sanitize_hashtag_list(d_tags, tl or target_lang, 40)
                if fixed:
                    desc = re.sub(r"#[^\s]+", "", desc)
                    desc = " ".join(desc.split())
                    tags = _sanitize_hashtag_list(list(tags or []) + fixed, tl or target_lang, 24)
        except Exception:
            pass

    if not isinstance(tags, list) or len([t for t in tags if isinstance(t, str) and t.startswith("#")]) < 3:
        tags = fallback_tags or base_tags

    try:
        title_tags, desc_tags = optimize_hashtags((parsed_title_tags or []) + (tags or []) + _heuristic_hashtags(base_title, tl or target_lang, limit=14), tl or target_lang, topic=base_title, limit_title=4, limit_desc=18)
        tags = desc_tags
        title = _compose_seo_title(title, base_title, title_tags, tl or target_lang)
        desc = _compose_seo_description(desc, title, base_title, tags, tl or target_lang)
    except Exception:
        pass

    if _is_hashtag_only_text(title):
        title = fallback_title
    if _is_hashtag_only_text(desc) or len(_strip_hashtags_from_text(desc)) < 20:
        desc = _compose_seo_description(fallback_desc, title, base_title, tags, tl or target_lang)
    if not tags:
        tags = fallback_tags or base_tags
    return title, tags, desc, True


def generate_platform_metadata(
    cfg: Config,
    video_path: str,
    hint_title: Optional[str],
    lang: str,
    platform: str,
    channel_key: str,
    channel_name: Optional[str] = None,
    source_description: Optional[str] = None,
    content_type: Optional[str] = None,
    source_context: Optional[Dict[str, Any]] = None,
) -> tuple[str, list[str], str, bool]:
    """
    توليد بيانات الفيديو حسب المنصة:
    - يعتمد افتراضياً على مُولّد محلي non-AI
    - YouTube: عنوان + وصف + هاشتاقات
    - Facebook/Instagram: عنوان فقط
    """
    platform = (platform or "youtube").strip().lower()
    target_lang = _normalize_lang_code(lang)

    cached = _get_cached_platform_metadata(cfg, platform, channel_key, target_lang, video_path, hint_title)
    if cached:
        return cached

    title, tags, desc, ok = _generate_local_platform_metadata(
        cfg,
        video_path,
        hint_title,
        lang,
        platform,
        channel_key,
        channel_name,
        source_description,
        content_type,
        source_context,
    )
    if platform in ("facebook", "instagram"):
        title = title[:60].rstrip()
        tags = []
        desc = ""
    if ok:
        _store_cached_platform_metadata(cfg, platform, channel_key, target_lang, video_path, hint_title, title, tags, desc)
    return title, tags, desc, ok


def generate_unique_shorts_title_desc_hashtags(
    cfg: Config,
    video_path: str,
    hint_title: Optional[str],
    lang: str,
    channel_key: str,
    channel_name: Optional[str] = None,
    max_attempts: int = 3,
) -> tuple[str, list[str], str, bool]:
    try:
        max_attempts = int(max_attempts)
    except Exception:
        max_attempts = 3
    max_attempts = max(1, min(6, max_attempts))

    history = _get_meta_history(cfg, channel_key, lang)
    if not history:
        max_attempts = min(max_attempts, 2)

    last_ok = None
    had_similarity_rejection = False
    for attempt in range(max_attempts):
        salt = f"{channel_key}|{channel_name or ''}|{attempt}|{random.randint(0, 10**9)}"
        title, tags, desc, ok = generate_title_desc_hashtags(
            cfg,
            video_path,
            hint_title=hint_title,
            lang=lang,
            override_key=salt,
        )
        last_ok = ok
        if not ok:
            continue
        if not _is_meta_too_similar(title, desc, history):
            _append_meta_history(cfg, channel_key, lang, title, desc)
            return title, tags, desc, True
        
        had_similarity_rejection = True
        logger.warning(f"♻️ Metadata rejected due to similarity (Attempt {attempt+1}/{max_attempts}) - Trying fresh generation...")

    if not had_similarity_rejection:
        return title, tags, desc, bool(last_ok)

    title, tags, desc, ok = generate_title_desc_hashtags(
        cfg,
        video_path,
        hint_title=hint_title,
        lang=lang,
        override_key=f"{channel_key}|fallback|{random.randint(0, 10**9)}",
    )
    if ok:
        title = _make_title_more_distinct(title, tags, lang)
        _append_meta_history(cfg, channel_key, lang, title, desc)
        return title, tags, desc, True
    return title, tags, desc, bool(last_ok)


def translate_text(cfg: Config, text: str, target_lang: str) -> Optional[str]:
    """
    Translate text to target language using AI.
    Returns None if translation fails.
    """
    if not text or not text.strip():
        return text
        
    prompt = (
        f"Task: Translate the following text to {target_lang}. "
        "Maintain the original tone, emoji usage, and meaning. "
        "Do not explain the translation, just return the translated text.\n\n"
        f"Text: {text}"
    )
    
    translated = _generate_content_with_failover(cfg, prompt)
    return translated.strip() if translated else None

def translate_batch(cfg: Config, texts: Dict[str, str], target_lang: str) -> Dict[str, str]:
    """
    Translate a batch of texts to target language.
    Returns a dictionary of original_key -> translated_text.
    Falls back to original text if translation fails.
    """
    if not texts:
        return {}
        
    # Group small texts into one prompt to save API calls
    keys = list(texts.keys())
    values = list(texts.values())
    
    prompt = (
        f"Task: Translate the following {len(values)} items to {target_lang}. "
        "Return the result as a STRICT valid JSON object where keys are the indices (0, 1, 2...) and values are the translations.\n"
        "Do NOT use Markdown. Do NOT wrap in ```json.\n"
        "Maintain emojis and tone.\n\n"
        "Items:\n"
    )
    
    for i, val in enumerate(values):
        prompt += f"{i}: {val}\n"
        
    response = _generate_content_with_failover(cfg, prompt)
    
    results = {}
    import json
    import re
    
    # Try to parse JSON
    try:
        if response:
            # Clean up potential markdown code blocks
            clean_json = re.sub(r'```json\s*|\s*```', '', response).strip()
            # Handle potential non-JSON output from weaker models
            if clean_json.startswith("{"):
                data = json.loads(clean_json)
                for i, key in enumerate(keys):
                    idx_str = str(i)
                    if idx_str in data:
                        results[key] = data[idx_str]
                    else:
                        results[key] = values[i] # Fallback
            else:
                # If model didn't return JSON, fallback to manual splitting if possible or just original
                # For now, simplistic fallback
                logger.warning(f"AI did not return JSON for batch translation to {target_lang}")
                for key, val in texts.items():
                    results[key] = val
        else:
            # Total failure
            for key, val in texts.items():
                results[key] = val
                
    except Exception as e:
        logger.error(f"Batch translation error: {e}")
        for key, val in texts.items():
            results[key] = val
            
    return results


def translate_keywords(cfg: Config, keywords: List[str], target_lang: str) -> List[str]:
    """
    Translate a list of keywords/tags to target language.
    Optimizes keywords for SEO in the target language.
    Falls back to original keywords if translation fails.
    
    Args:
        cfg: Configuration object
        keywords: List of keywords to translate
        target_lang: Target language code (e.g., 'en', 'ar', 'fr')
        
    Returns:
        List of translated keywords optimized for YouTube SEO
    """
    if not keywords:
        return []
    
    # If target language is same as assumed base, return as is
    # Note: This is a simple heuristic; ideally we'd detect the source language
    
    prompt = (
        f"Task: Translate the following YouTube video keywords/tags to {target_lang}. "
        "Optimize them for YouTube SEO in the target language. "
        "Keep keywords relevant and searchable. "
        "You may adjust phrasing for better search performance in the target language, "
        "but maintain the core meaning.\n\n"
        "Return ONLY the translated keywords, one per line, without numbering or extra text.\n\n"
        "Keywords:\n" + "\n".join(f"- {kw}" for kw in keywords)
    )
    
    response = _generate_content_with_failover(cfg, prompt)
    
    if not response:
        logger.warning(f"Failed to translate keywords to {target_lang}, using original")
        return keywords
    
    # Parse response - expecting one keyword per line
    translated = []
    for line in response.splitlines():
        line = line.strip()
        # Remove common prefixes like "-", "*", numbers, etc.
        line = line.lstrip("-*•➤►123456789 .)")
        if line and not line.startswith("#"):  # Skip empty lines and comments
            # Remove hashtag if present
            if line.startswith("#"):
                line = line[1:]
            translated.append(line.strip())
    
    # Fallback to original if parsing failed
    if not translated:
        logger.warning(f"Failed to parse translated keywords for {target_lang}, using original")
        return keywords
        
    return translated


def _fallback_keywords(topic: str, lang: str = "ar") -> List[str]:
    """Generate smart fallback keywords when AI fails"""
    topic_plain = _topic_to_plain_text(topic)
    signals = extract_source_metadata_context(
        hint_title=topic_plain,
        source_description="",
        lang=lang,
        content_type="",
        max_keywords=8,
        max_hashtags=8,
    )
    keywords = [str(item).strip() for item in (signals.get("keywords") or []) if str(item or "").strip()]
    if keywords:
        return keywords[:12]

    if lang in ["ar", "arabic"]:
        return ["موضوع", "لقطة", "فيديو"]
    if lang in ["th", "thai"]:
        return ["หัวข้อ", "คลิป", "วิดีโอ"]
    if lang in ["hi", "hindi", "mr", "marathi", "ne", "nepali"]:
        return ["विषय", "क्लिप", "वीडियो"]
    if lang in ["bn", "bengali"]:
        return ["বিষয়", "ক্লিপ", "ভিডিও"]
    if lang in ["ta", "tamil"]:
        return ["தலைப்பு", "கிளிப்", "வீடியோ"]
    if lang in ["te", "telugu"]:
        return ["విషయం", "క్లిప్", "వీడియో"]
    if lang in ["kn", "kannada"]:
        return ["ವಿಷಯ", "ಕ್ಲಿಪ್", "ವೀಡಿಯೊ"]
    if lang in ["ml", "malayalam"]:
        return ["വിഷയം", "ക്ലിപ്പ്", "വീഡിയോ"]
    if lang in ["gu", "gujarati"]:
        return ["વિષય", "ક્લિપ", "વિડિઓ"]
    if lang in ["pa", "punjabi"]:
        return ["ਵਿਸ਼ਾ", "ਕਲਿੱਪ", "ਵੀਡੀਓ"]
    if lang in ["my", "burmese"]:
        return ["ခေါင်းစဉ်", "ကလစ်", "ဗီဒီယို"]
    if lang in ["km", "khmer"]:
        return ["ប្រធានបទ", "វីដេអូខ្លី", "វីដេអូ"]
    if lang in ["lo", "lao"]:
        return ["ຫົວຂໍ້", "ຄລິບ", "ວິດີໂອ"]
    if lang in ["am", "amharic"]:
        return ["ርዕስ", "ክሊፕ", "ቪዲዮ"]
    if lang in ["he", "hebrew"]:
        return ["נושא", "קליפ", "וידאו"]
    if lang in ["el", "greek"]:
        return ["θέμα", "κλιπ", "βίντεο"]
    if lang in ["ru", "uk", "bg", "sr", "mk"]:
        return ["тема", "клип", "видео"]
    if lang in ["hy", "armenian"]:
        return ["թեմա", "կլիպ", "տեսանյութ"]
    if lang in ["ka", "georgian"]:
        return ["თემა", "კლიპი", "ვიდეო"]
    if lang in ["fr", "french"]:
        return ["sujet", "clip", "video"]
    return ["topic", "clip", "video"]


def generate_seo_keywords(
    cfg: Config,
    video_topic: str,
    title: str,
    lang: str = "ar",
    count: int = 15,
    override_key: Optional[str] = None,
) -> tuple[List[str], bool]:
    """
    Generate SEO-optimized keywords for a video based on topic and title.
    
    Args:
        cfg: Configuration object
        video_topic: Brief description of the video topic
        title: Video title
        lang: Target language (ar, en, fr, etc.)
        count: Desired number of keywords (default: 15)
        
    Returns:
        Tuple of (keywords_list, success_status)
        
    Uses advanced AI to create highly effective keywords for YouTube search ranking.
    """
    if not video_topic and not title:
        return _fallback_keywords("", lang), False
    
    prompt = (
        f"Task: Generate {count} SEO-optimized keywords/tags for a YouTube video about Minecraft mods in {lang}.\n\n"
        f"Video Topic: {video_topic}\n"
        f"Video Title: {title}\n\n"
        f"UniqSeed: {(override_key or '').strip()}\n\n"
        "Requirements:\n"
        "- Generate keywords that will help the video rank high in YouTube search\n"
        "- Mix of short keywords (1-2 words) and long-tail keywords (3-4 words)\n"
        "- Include variations and related terms\n"
        "- Focus on searchable, popular terms related to Minecraft and mods\n"
        "- Use ONLY the target language; do NOT include keywords in other languages\n"
        "- DO NOT use hashtags (#), just plain keywords\n\n"
        "Return ONLY the keywords, one per line, without numbering or explanations.\n"
    )
    
    response = _generate_content_with_failover(cfg, prompt)
    
    if not response:
        logger.warning(f"AI keyword generation failed, using smart fallback for {lang}")
        return _fallback_keywords(video_topic or title, lang), False
    
    # Parse keywords
    keywords = []
    for line in response.splitlines():
        line = line.strip()
        # Remove numbering, bullets, etc.
        line = line.lstrip("-*•➤►123456789 .)")
        if line and not line.startswith("#"):
            # Remove any hashtags if present  
            if line.startswith("#"):
                line = line[1:]
            keywords.append(line.strip())
    
    if not keywords or len(keywords) < 3:
        logger.warning(f"Parsed keywords too few ({len(keywords)}), using fallback")
        return _fallback_keywords(video_topic or title, lang), False
    
    # Enforce language: if output is in wrong script/language, attempt to translate
    tl = (lang or "").lower().strip()
    try:
        if tl and not tl.startswith("ar"):
            if any(_contains_arabic(k) for k in keywords):
                keywords = translate_keywords(cfg, keywords, tl) or keywords
        if tl.startswith("en"):
            if any(_contains_arabic(k) for k in keywords):
                keywords = translate_keywords(cfg, keywords, "en") or keywords
        elif len(tl) >= 2 and not tl.startswith("en"):
            joined = " ".join(keywords)
            if _is_mostly_english(joined):
                keywords = translate_keywords(cfg, keywords, tl) or keywords
    except Exception:
        pass

    return keywords[:count], True


def _normalize_keyword_kw(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(ch if (ch.isspace() or ch.isalnum() or ch == "_" or unicodedata.category(ch).startswith("M")) else " " for ch in s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _keywords_similarity(a: List[str], b: List[str]) -> float:
    sa = set(_normalize_keyword_kw(x) for x in (a or []) if _normalize_keyword_kw(x))
    sb = set(_normalize_keyword_kw(x) for x in (b or []) if _normalize_keyword_kw(x))
    if not sa or not sb:
        return 0.0
    return float(len(sa & sb)) / float(len(sa | sb)) if (sa | sb) else 0.0


def _get_keywords_history(cfg: Config, channel_key: str, lang: str) -> List[Dict[str, Any]]:
    try:
        st = load_state(cfg)
    except Exception:
        return []
    try:
        root = st.get("ai_metadata_history") or {}
        kw = root.get("shorts_keywords") or {}
        key = f"{(channel_key or '').strip()}::{(lang or '').strip().lower()}"
        items = kw.get(key) or []
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _append_keywords_history(cfg: Config, channel_key: str, lang: str, keywords: List[str]) -> None:
    try:
        limit = int(os.getenv("AI_KEYWORDS_HISTORY_LIMIT", "40") or 40)
    except Exception:
        limit = 40
    limit = max(5, min(200, limit))
    key = f"{(channel_key or '').strip()}::{(lang or '').strip().lower()}"

    def _upd(st):
        root = st.setdefault("ai_metadata_history", {})
        kw = root.setdefault("shorts_keywords", {})
        items = kw.get(key)
        if not isinstance(items, list):
            items = []
        items.append({"keywords": list(keywords or []), "ts": time.time()})
        if len(items) > limit:
            items = items[-limit:]
        kw[key] = items

    try:
        update_state(cfg, _upd)
    except Exception:
        pass


def generate_unique_seo_keywords(
    cfg: Config,
    video_topic: str,
    title: str,
    lang: str,
    channel_key: str,
    channel_name: Optional[str] = None,
    count: int = 15,
    max_attempts: int = 3,
) -> tuple[List[str], bool]:
    try:
        max_attempts = int(max_attempts)
    except Exception:
        max_attempts = 3
    max_attempts = max(1, min(6, max_attempts))

    history = _get_keywords_history(cfg, channel_key, lang)
    prev = [h.get("keywords") for h in (history or []) if isinstance(h, dict) and h.get("keywords")]

    last_ok = None
    for attempt in range(max_attempts):
        salt = f"{channel_key}|{channel_name or ''}|kw|{attempt}|{random.randint(0, 10**9)}"
        kws, ok = generate_seo_keywords(cfg, video_topic, title, lang, count, override_key=salt)
        last_ok = ok
        if not kws:
            continue
        too_similar = False
        for pk in prev[-15:]:
            if _keywords_similarity(kws, pk) >= 0.72:
                too_similar = True
                break
        if not too_similar:
            _append_keywords_history(cfg, channel_key, lang, kws)
            return kws, bool(ok)

    kws, ok = generate_seo_keywords(cfg, video_topic, title, lang, count, override_key=f"{channel_key}|kw|fallback")
    if kws:
        _append_keywords_history(cfg, channel_key, lang, kws)
    return kws, bool(ok or last_ok)


def _fallback_description(title: str, topic: str, lang: str = "ar", app_link: str = None) -> str:
    """وصف احتياطي في حال فشل الذكاء الاصطناعي"""
    if app_link is None:
        from .config import load_config
        app_link = load_config().APP_DOWNLOAD_URL
    
    if lang in ["ar", "arabic"]:
        desc = f"🎮 {title}\n\n"
        desc += f"في هذا الفيديو نستعرض {topic or 'مود رائع لماين كرافت'}.\n\n"
        desc += f"📌 عنوان الفيديو: {title}\n\n"
        desc += "💎 محتوى مميز:\n"
        desc += "- شرح كامل ومفصل\n"
        desc += "- جودة عالية\n"
        desc += "- محتوى حصري\n\n"
        desc += f"📱 حمل التطبيق:\n"
        desc += f"للحصول على المزيد من المودات، حمل تطبيقنا من هنا:\n{app_link}\n\n"
        desc += "🔥 هاشتاقات:\n"
        desc += "#ماين_كرافت #minecraft #mod #مود #gaming #shorts #ماين #العاب #شرح #تحميل #minecraft_mod #minecraft_shorts\n\n"
        desc += "🏷️ كلمات مفتاحية:\n"
        desc += "ماين كرافت, minecraft, mod, مود, minecraft mod, gaming, شرح مود, تحميل مود, ماين كرافت مودات, minecraft mods, العاب, shorts"
    elif lang in ["en", "english"]:
        desc = f"🎮 {title}\n\n"
        desc += f"In this video, we showcase {topic or 'an amazing Minecraft mod'}.\n\n"
        desc += f"📌 Video Title: {title}\n\n"
        desc += "💎 Featured Content:\n"
        desc += "- Complete detailed explanation\n"
        desc += "- High quality\n"
        desc += "- Exclusive content\n\n"
        desc += f"📱 Download Our App:\n"
        desc += f"Get more mods by downloading our app:\n{app_link}\n\n"
        desc += "🔥 Hashtags:\n"
        desc += "#minecraft #mod #minecraftmod #gaming #shorts #tutorial #download #gameplay #mcpe #minecraftshorts\n\n"
        desc += "🏷️ Keywords:\n"
        desc += "minecraft, mod, minecraft mod, gaming, shorts, tutorial, download, minecraft mods, gameplay, addon"
    else:
        # Default to English
        desc = _fallback_description(title, topic, "en", app_link)
    
    return desc


def generate_detailed_description(
    cfg: Config,
    video_topic: str,
    title: str,
    keywords: List[str],
    lang: str = "ar",
    channel_name: str = "",
    style_seed: Optional[str] = None
) -> tuple[str, bool]:
    """
    Generate a detailed, SEO-optimized video description with structured sections.
    
    Description structure:
    1. Introduction paragraph explaining the video topic
    2. "📌 عنوان الفيديو: {actual_title}"
    3. Relevant content section
    4. "📱 حمل التطبيق" section with app link
    5. Multiple hashtags
    6. Keywords separated by commas
   
    Args:
        cfg: Configuration object
        video_topic: Brief description of the video content
        title: Video title
        keywords: List of keywords for SEO
        lang: Target language
        channel_name: Channel name for style variation
        style_seed: Optional seed for style variation (auto-generated if None)
        
    Returns:
        Tuple of (description, success_status)
    """
    if not style_seed:
        style_seed = f"{channel_name}|{video_topic}|{lang}"
    
    sidx = _style_index(style_seed, 5)  # 5 different writing styles
    
    app_link = cfg.APP_DOWNLOAD_URL
    keywords_str = ", ".join(keywords) if keywords else ""
    
    from .prompt_templates import PromptTemplates
    
    prompt = PromptTemplates.get_dynamic_prompt_instruction(
        topic=video_topic,
        title=title,
        app_link=app_link,
        lang=lang,
        keywords=keywords,
        style_seed=style_seed
    )
    
    response = _generate_content_with_failover(cfg, prompt)
    
    if not response or len(response) < 100:  # Too short to be valid
        logger.warning(f"AI description generation failed or too short, using smart fallback for {lang}")
        return _fallback_description(title, video_topic, lang, app_link), False
    
    return response.strip(), True

def generate_description_from_mod_data(
    cfg: Config,
    raw_data: str,
    lang: str = "ar",
    title: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    channel_name: str = "",
) -> tuple[str, bool]:
    if not raw_data or len(raw_data.strip()) < 20:
        return _fallback_description(title or "Mod Video", "", lang), False
    kw_str = ", ".join(keywords or [])
    app_link = cfg.APP_DOWNLOAD_URL
    seed = f"{channel_name}|{lang}|{(title or '').strip()[:24]}|{len(raw_data)}"
    sidx = _style_index(seed, 5)
    from .prompt_templates import PromptTemplates
    
    prompt = PromptTemplates.get_mod_data_prompt_instruction(
        raw_data=raw_data,
        lang=lang,
        title=title,
        keywords=keywords,
        app_link=app_link,
        style_seed=seed
    )
    response = _generate_content_with_failover(cfg, prompt)
    if not response or len(response) < 120:
        return _fallback_description(title or "Mod Video", "", lang), False
    return response.strip(), True

def generate_long_video_title(
    cfg: Config,
    video_topic: str,
    keywords: List[str],
    lang: str = "en",
    mod_version: Optional[str] = None,
    year: Optional[str] = None,
    channel_name: str = "",
    style_seed: Optional[str] = None
) -> tuple[str, bool]:
    """
    توليد عنوان احترافي لفيديو مود طويل (ليس شورتس)، بصيغة جذابة وبشرية،
    مع دعم تضمين إصدار المود (مثل 1.21+) والسنة (مثل 2025) إن توفرت.
    
    أمثلة أسلوب:
    - Demon Slayer Mules Slayer (Early Access) Addon For Minecraft PE/Bedrock
    - Best Combine Zombie Apocalypse Addons For MCPE Survival Horror! Minecraft Bedrock
    - Best Combine Zombie Apocalypse Addons For MCPE 1.21+! | Survival Horror! (Minecraft Bedrock 2025)
    """
    try:
        if not style_seed:
            style_seed = f"{channel_name}|{video_topic}|{lang}|{mod_version or ''}|{year or ''}"
        sidx = _style_index(style_seed, 6)
        kw_str = ", ".join(keywords or [])
        version_hint = (mod_version or "").strip()
        year_hint = (year or "").strip()
        meta_line = ""
        if version_hint or year_hint:
            meta = []
            if version_hint:
                meta.append(f"Version: {version_hint}")
            if year_hint:
                meta.append(f"Year: {year_hint}")
            meta_line = " | ".join(meta)
        prompt = (
            f"Task: Write a professional, human-like YouTube LONG video TITLE in {lang} for a Minecraft mod/addon showcase.\n"
            f"Topic: {video_topic}\n"
            f"Keywords: {kw_str}\n"
            f"{'Meta: ' + meta_line if meta_line else ''}\n"
            "Constraints:\n"
            "- Length <= 100 characters.\n"
            "- Avoid clickbait and false claims; be engaging and specific.\n"
            "- Prefer structure like: <Hook/Best/Combine> <Mod Name/Theme> Addon(s) For MCPE/Minecraft Bedrock, optionally include version (e.g., 1.21+) and year in parentheses.\n"
            "- You may use separators like | or parentheses for clarity.\n"
            "- If version provided, include it as 1.21+ or similar where natural.\n"
            "- If year provided, include it in parentheses at the end like (Minecraft Bedrock 2025).\n"
            "- Return ONLY the title line, no extra text.\n"
            f"StyleIndex: {sidx}\n"
            "Styles:\n"
            "0) Best <Theme> Addons For MCPE | Minecraft Bedrock\n"
            "1) <Franchise/Theme> <Addon Name> For Minecraft PE/Bedrock\n"
            "2) Survival Horror / Adventure focus with clear value\n"
            "3) Early Access / Showcase wording when appropriate\n"
            "4) Combine multiple addons with 'Best Combine'\n"
            "5) Clean professional statement with subtle hype\n"
        )
        title = _generate_content_with_failover(cfg, prompt) or ""
        title = title.strip()
        if not title or len(title) < 10 or len(title) > 120:
            # فشل أو غير مناسب — توليد احتياطي بسيط
            base = (video_topic or "Minecraft Mod Addon").strip()
            if version_hint:
                base += f" {version_hint}+"
            ending = "Minecraft Bedrock"
            if year_hint:
                ending += f" {year_hint}"
            return (f"Best {base} Addons For MCPE | {ending}").strip(), False
        # قص إلى 100 كحد أقصى
        if len(title) > 100:
            title = title[:97].rstrip() + "…"
        return title, True
    except Exception:
        base = (video_topic or "Minecraft Mod Addon").strip()
        return (f"Best {base} For MCPE | Minecraft Bedrock").strip(), False


def translate_video_metadata_batch(
    cfg: Config,
    base_data: Dict, 
    target_lang: str,
) -> Dict:
    """
    Translates/Generates Title, Description, Keywords, and Texts in a SINGLE API call.
    Reduces API usage and improves consistency.
    """
    import json
    import re
    
    # Unpack data
    title = base_data.get("title", "")
    desc = base_data.get("description", "")
    keywords = base_data.get("keywords", []) or []
    texts = base_data.get("texts", {}) or {}
    topic = base_data.get("video_topic", "")
    mod_meta = base_data.get("mod_metadata", "")
    
    # Default result (fallback)
    result = {
        "title": title,
        "description": desc,
        "keywords": keywords,
        "texts": texts
    }

    prompt = (
        f"Task: Translate and optimize Youtube video metadata for a Minecraft Mod in {target_lang}.\n"
        "Return a strictly valid JSON object. Do not include markdown formatting.\n\n"
        "Input Data:\n"
        f"Title: {title}\n"
        f"Description: {desc}\n"
        f"Keywords: {keywords}\n"
        f"OverlayTexts: {json.dumps(texts, ensure_ascii=False)}\n"
        f"Topic: {topic}\n"
        f"ModInfo: {str(mod_meta)[:800] if mod_meta else 'None'}\n\n"
        "Requirements:\n"
        "1. title: Translate/Optimize title. Be catchy. If mod info/version is known, include it in a professional way (e.g. 1.21+).\n"
        "2. description: Translate description. If ModInfo is present, use it to enhance the description (list features).\n"
        "3. keywords: Translate and SEO optimize keywords (list of strings).\n"
        "4. texts: Translate overlay texts maintaining meaning and emoji (dictionary).\n\n"
        "Output JSON Structure:\n"
        "{\n"
        '  "title": "...",\n'
        '  "description": "...",\n'
        '  "keywords": ["...", "..."],\n'
        '  "texts": {"0_text": "...", "1_text": "..."}\n'
        "}"
    )
    
    response = _generate_content_with_failover(cfg, prompt)
    
    if response:
        try:
            clean = re.sub(r'```json\s*|\s*```', '', response).strip()
            # Handle list output mistake
            if clean.startswith("["):
                 logger.warning(f"Batch AI returned list instead of dict for {target_lang}")
            elif clean.startswith("{"):
                data = json.loads(clean)
                # Build result carefully to avoid overwriting with None
                if data.get("title"): result["title"] = data["title"]
                if data.get("description"): result["description"] = data["description"]
                if data.get("keywords") and isinstance(data["keywords"], list): result["keywords"] = data["keywords"]
                if data.get("texts") and isinstance(data["texts"], dict): 
                    # Merge texts
                    for k,v in data["texts"].items():
                        result["texts"][k] = v
            else:
                logger.warning(f"Batch AI response not JSON for {target_lang}: {clean[:50]}...")
        except Exception as e:
             logger.error(f"Batch AI parse error: {e}")
             
    return result


def generate_ai_metadata(
    cfg: Config,
    source_title: str,
    source_description: str = "",
    content_type: str = "minecraft_mods",
    target_lang: str = "ar",
    is_shorts: bool = True,
    channel_key: str = "",
    video_path: str = "",
    source_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    واجهة توافقية قديمة تُعيد الآن بيانات نهائية عبر المولّد المحلي non-AI.
    """
    title, hashtags, description, ok = generate_platform_metadata(
        cfg=cfg,
        video_path=video_path,
        hint_title=source_title,
        lang=target_lang,
        platform="youtube",
        channel_key=channel_key,
        channel_name="",
        source_description=source_description,
        content_type=content_type,
        source_context=source_context,
    )
    result = {
        "title": (title or source_title or "")[:100],
        "description": description or f"Auto-generated from: {source_title}",
        "hashtags": list(hashtags or []),
        "tags": _keywords_from_hashtags(hashtags, source_title or content_type, target_lang, limit=15),
        "source_context": source_context or {},
    }
    if not ok:
        result["title"] = _normalize_local_title(source_title, content_type or source_title, target_lang)
        result["description"] = _normalize_local_description(source_description, result["title"], source_title or content_type, hashtags, target_lang)
    logger.info(f"🧩 Local metadata generated: title={result['title'][:60]}... | tags={len(result.get('tags', []))}")
    return result


def _format_hashtags(hashtags: List[str], lang: str) -> List[str]:
    """
    تنسيق الهاشتاقات: تنظيف + فصل الكلمات المركبة بـ _
    """
    result = []
    seen = set()
    for tag in hashtags:
        tag = tag.strip()
        if not tag.startswith("#"):
            tag = f"#{tag}"
        
        # إزالة أحرف غير مسموحة (نحتفظ بالحروف والأرقام و _ فقط)
        body = tag[1:]  # بدون #
        body = re.sub(r'[^\w\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]', '_', body)
        body = re.sub(r'_+', '_', body).strip('_')
        
        if not body or len(body) < 2:
            continue
        
        # مفتاح للتكرار (بدون حساسية لحالة الأحرف)
        key = body.lower().replace('_', '')
        if key in seen:
            continue
        seen.add(key)
        
        result.append(f"#{body}")
    
    return result


def _extract_metadata_from_text(text: str) -> Optional[Dict[str, Any]]:
    """
    استخراج يدوي للبيانات إذا فشل تحليل JSON
    """
    if not text:
        return None
    
    result = {}
    lines = text.strip().splitlines()
    
    for line in lines:
        line = line.strip()
        lower = line.lower()
        if lower.startswith("title:"):
            result["title"] = line.split(":", 1)[1].strip()
        elif lower.startswith("description:"):
            result["description"] = line.split(":", 1)[1].strip()
        elif lower.startswith("hashtags:"):
            result["description"] = line.split(":", 1)[1].strip()
        elif lower.startswith("tags:"):
            tags_str = line.split(":", 1)[1].strip()
            # محاولة تحليل كقائمة
            tags_str = tags_str.strip("[]")
            result["tags"] = [t.strip().strip('"').strip("'") for t in tags_str.split(",") if t.strip()]
    
    return result if result else None


def _generate_fallback_hashtags(
    source_title: str,
    content_type: str,
    lang: str,
    count: int = 6
) -> List[str]:
    """
    توليد هاشتاقات احتياطية من عنوان المصدر ونوع المحتوى
    """
    hashtags = []
    
    # هاشتاقات من نوع المحتوى
    content_tags = {
        "minecraft_mods": {
            "ar": ["#ماينكرافت", "#مودات", "#مودات_ماينكرافت", "#بيدروك", "#ماين_كرافت", "#العاب"],
            "en": ["#Minecraft", "#Mods", "#Minecraft_Mods", "#Bedrock", "#Gaming", "#MCPE"],
            "fr": ["#Minecraft", "#Mods", "#Minecraft_Mods", "#Jeux", "#Bedrock"],
            "es": ["#Minecraft", "#Mods", "#Minecraft_Mods", "#Juegos", "#Bedrock"],
            "default": ["#Minecraft", "#Mods", "#Gaming", "#Bedrock", "#MCPE"],
        },
        "gaming": {
            "ar": ["#العاب", "#قيمنق", "#جيمنق", "#شورتس", "#لعبة"],
            "en": ["#Gaming", "#Games", "#Shorts", "#Gameplay", "#Gamer"],
            "default": ["#Gaming", "#Games", "#Shorts", "#Gameplay"],
        },
    }
    
    # الحصول على الهاشتاقات المناسبة
    type_tags = content_tags.get(content_type, content_tags.get("gaming", {}))
    lang_tags = type_tags.get(lang, type_tags.get("default", []))
    hashtags.extend(lang_tags)
    
    # استخراج كلمات مفتاحية من العنوان
    if source_title:
        words = source_title.split()
        for word in words:
            clean_word = re.sub(r'[^\w\u0600-\u06FF]', '', word)
            if clean_word and len(clean_word) >= 3 and not clean_word.isdigit():
                tag = f"#{clean_word}"
                if tag not in hashtags:
                    hashtags.append(tag)
    
    return hashtags[:count]
