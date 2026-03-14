import hashlib
import json
import os
import random
import re
import time
from typing import Any, Dict, List, Optional


_CONFIG_CACHE: Dict[str, Any] = {"path": None, "mtime": None, "data": None}


def _default_config_path() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "local_metadata_templates.json"))


def load_local_metadata_config() -> Dict[str, Any]:
    path = os.getenv("LOCAL_METADATA_CONFIG_PATH") or _default_config_path()
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = None

    if _CONFIG_CACHE.get("path") == path and _CONFIG_CACHE.get("mtime") == mtime:
        return _CONFIG_CACHE.get("data") or {}

    data: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
    except Exception:
        data = {}

    _CONFIG_CACHE.update({"path": path, "mtime": mtime, "data": data})
    return data


def _clean_text(text: Optional[str]) -> str:
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split()).strip()


def _normalize_key(text: Optional[str]) -> str:
    raw = _clean_text(text).lower()
    raw = raw.replace("_", " ")
    raw = re.sub(r"[^0-9a-z\u0600-\u06FF\s]", " ", raw)
    return " ".join(raw.split())


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _merge_unique(*groups: Any) -> List[str]:
    seen = set()
    out: List[str] = []
    for group in groups:
        for item in _as_list(group):
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out


def _lang_list(node: Any, lang: str) -> List[str]:
    if isinstance(node, dict):
        return _merge_unique(node.get("default"), node.get(lang))
    return _as_list(node)


def _lang_map(node: Any, lang: str) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    if not isinstance(node, dict):
        return result
    for section in (node.get("default") or {}, node.get(lang) or {}):
        if not isinstance(section, dict):
            continue
        for key, value in section.items():
            result[str(key).strip()] = _merge_unique(result.get(str(key).strip()), value)
    return result


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_format(template: str, values: Dict[str, str]) -> str:
    class _Missing(dict):
        def __missing__(self, key: str) -> str:
            return ""

    try:
        return template.format_map(_Missing(values))
    except Exception:
        return template


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "in", "into", "is", "it", "of", "on", "or", "the", "this", "that", "to", "with",
    "عن", "على", "الى", "إلى", "في", "من", "مع", "هذا", "هذه", "ذلك", "تلك", "ثم", "الى", "إلى", "ل", "و", "او", "أو",
}

_HARD_GENERIC_TERMS = {
    "video", "videos", "clip", "clips", "short", "shorts", "viral", "trending", "watch", "now", "new", "best", "gaming", "game", "games", "reaction", "reactions",
    "فيديو", "فيديوهات", "مقطع", "مقاطع", "قصير", "قصيرة", "شورت", "شورتس", "ترند", "تريند", "فيرال", "جديد", "جديدة", "شاهد", "شاهدوا", "العاب", "ألعاب", "لعبة", "لقطة",
}

_SOFT_GENERIC_TERMS = {
    "mod", "mods", "addon", "addons", "tutorial", "guide", "gameplay", "build", "builds",
    "مود", "مودات", "اضافة", "إضافة", "اضافات", "إضافات", "شرح", "بناء", "بنايات",
}

_GENERIC_GREETING_PHRASES = {
    "سلام عليكم", "السلام عليكم", "مرحبا", "اهلا", "أهلا", "اهلا بكم", "أهلا بكم",
    "welcome", "hello", "hi", "hey",
}

_GENERIC_GREETING_TOKENS = {
    "سلام", "السلام", "عليكم", "مرحبا", "اهلا", "أهلا", "بكم", "welcome", "hello", "hi", "hey",
}

_BLOCKED_BRAND_TERMS = {"modetaris"}


def _tokenize_text(text: Optional[str]) -> List[str]:
    tokens = re.findall(r"\w{2,}", _clean_text(text), flags=re.UNICODE)
    out: List[str] = []
    for token in tokens:
        if any(ch.isalpha() for ch in token):
            out.append(token)
    return out


def _is_stopword(token: Optional[str]) -> bool:
    return _normalize_key(token) in _STOPWORDS


def _is_hard_generic(token: Optional[str]) -> bool:
    return _normalize_key(token) in _HARD_GENERIC_TERMS


def _is_soft_generic(token: Optional[str]) -> bool:
    return _normalize_key(token) in _SOFT_GENERIC_TERMS


def _topic_supports_phrase(phrase: Optional[str], *texts: Optional[str]) -> bool:
    phrase_key = _normalize_key(phrase)
    if not phrase_key:
        return False
    phrase_words = [word for word in phrase_key.split() if word]
    if not phrase_words:
        return False
    for text in texts:
        haystack = _normalize_key(text)
        if not haystack:
            continue
        if phrase_key in haystack:
            return True
        hay_words = set(haystack.split())
        if len(phrase_words) >= 2 and all(word in hay_words for word in phrase_words):
            return True
    return False


def _should_keep_metadata_phrase(
    phrase: Optional[str],
    *,
    title_text: str = "",
    context_title: str = "",
) -> bool:
    key = _normalize_key(phrase)
    if not key:
        return False
    words = [word for word in key.split() if word]
    if not words:
        return False
    if key in _STOPWORDS:
        return False
    if len(words) == 1 and (key in _HARD_GENERIC_TERMS or key in _SOFT_GENERIC_TERMS):
        return False
    if any(blocked in key for blocked in _GENERIC_GREETING_PHRASES) or any(word in _GENERIC_GREETING_TOKENS for word in words):
        return _topic_supports_phrase(key, title_text, context_title)
    if any(word in _BLOCKED_BRAND_TERMS for word in words):
        return _topic_supports_phrase(key, title_text, context_title)
    return True


def _filter_grounded_metadata_phrases(
    items: List[str],
    *,
    title_text: str = "",
    context_title: str = "",
) -> List[str]:
    return [
        item for item in (items or [])
        if _should_keep_metadata_phrase(item, title_text=title_text, context_title=context_title)
    ]


def _phrase_specificity(phrase: Optional[str]) -> int:
    words = [_normalize_key(word) for word in _tokenize_text(phrase) if _normalize_key(word)]
    if not words:
        return 0
    strong = sum(1 for word in words if word not in _STOPWORDS and word not in _HARD_GENERIC_TERMS and word not in _SOFT_GENERIC_TERMS)
    soft = sum(1 for word in words if word not in _STOPWORDS and word not in _HARD_GENERIC_TERMS and word in _SOFT_GENERIC_TERMS)
    if strong <= 0 and soft <= 0:
        return 0
    bonus = 1 if len(words) >= 2 and strong >= 1 else 0
    return (strong * 3) + soft + bonus


def _add_phrase_candidate(phrase: str, score: float, score_map: Dict[str, float], phrase_map: Dict[str, str]) -> None:
    clean = _clean_text(phrase).strip("-|:,.،؛•_")
    key = _normalize_key(clean)
    if not clean or not key:
        return
    spec = _phrase_specificity(clean)
    if spec <= 0:
        return
    current = score_map.get(key)
    if current is None or score > current:
        score_map[key] = float(score)
        phrase_map[key] = clean


def _collect_source_hashtag_phrases(text: Optional[str], score_map: Dict[str, float], phrase_map: Dict[str, str], base_weight: float) -> None:
    for raw in re.findall(r"#[^\s#]+", text or ""):
        phrase = raw.lstrip("#").replace("_", " ")
        spec = _phrase_specificity(phrase)
        if spec <= 0:
            continue
        _add_phrase_candidate(phrase, base_weight + (spec * 0.7), score_map, phrase_map)


def _collect_scored_phrases(text: Optional[str], score_map: Dict[str, float], phrase_map: Dict[str, str], base_weight: float) -> None:
    cleaned = re.sub(r"#[^\s#]+", " ", text or "")
    tokens = [tok for tok in _tokenize_text(cleaned) if not _is_stopword(tok)]
    if not tokens:
        return
    tokens = tokens[:18]
    for size in (3, 2, 1):
        if len(tokens) < size:
            continue
        for idx in range(0, len(tokens) - size + 1):
            words = tokens[idx:idx + size]
            if not words:
                continue
            if size > 1 and (_is_hard_generic(words[0]) or _is_hard_generic(words[-1])):
                continue
            phrase = " ".join(words)
            spec = _phrase_specificity(phrase)
            if size == 1 and spec < 2:
                continue
            score = float(base_weight) + ((size - 1) * 1.15) + (spec * 0.45)
            if size >= 2 and any(_is_soft_generic(word) for word in words):
                score += 0.25
            _add_phrase_candidate(phrase, score, score_map, phrase_map)


def _select_distinct_phrases(score_map: Dict[str, float], phrase_map: Dict[str, str], limit: int) -> List[str]:
    ordered = sorted(
        score_map.items(),
        key=lambda item: (
            -float(item[1]),
            -_phrase_specificity(phrase_map.get(item[0]) or item[0]),
            -(len((phrase_map.get(item[0]) or item[0]).split())),
            len(phrase_map.get(item[0]) or item[0]),
        ),
    )
    chosen: List[str] = []
    chosen_sets: List[set[str]] = []
    for key, _ in ordered:
        phrase = _clean_text(phrase_map.get(key) or key)
        words = {_normalize_key(word) for word in _tokenize_text(phrase) if not _is_stopword(word)}
        words.discard("")
        if not phrase or not words:
            continue
        overlap = False
        for existing in chosen_sets:
            if words <= existing:
                overlap = True
                break
            common = words & existing
            if len(common) >= max(2, min(len(words), len(existing))):
                overlap = True
                break
        if overlap:
            continue
        chosen.append(phrase)
        chosen_sets.append(words)
        if len(chosen) >= limit:
            break
    return chosen


def _soft_generic_keywords(*parts: Optional[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for token in _tokenize_text(" ".join(_clean_text(part) for part in parts if part)):
        key = _normalize_key(token)
        if not key or key in seen or not _is_soft_generic(token):
            continue
        seen.add(key)
        out.append(token)
        if len(out) >= 4:
            break
    return out


def extract_source_metadata_context(
    *,
    hint_title: str = "",
    source_description: str = "",
    lang: str = "ar",
    content_type: str = "",
    channel_name: str = "",
    source_name: str = "",
    source_context: Optional[Dict[str, Any]] = None,
    max_keywords: int = 8,
    max_hashtags: int = 10,
) -> Dict[str, Any]:
    lang_key = (lang or "ar").strip().lower() or "ar"
    source_context = source_context or {}
    score_map: Dict[str, float] = {}
    phrase_map: Dict[str, str] = {}

    title_text = _clean_text(hint_title)
    desc_text = _clean_text(source_description)
    type_text = _clean_text(str(content_type or "").replace("_", " "))
    context_title = _clean_text(source_context.get("original_title") or source_context.get("title") or "")
    context_desc = _clean_text(source_context.get("original_description") or source_context.get("description") or "")

    for text, weight in (
        (title_text, 8.0),
        (context_title, 7.5),
        (desc_text, 4.5),
        (context_desc, 4.0),
        (type_text, 3.8),
    ):
        _collect_source_hashtag_phrases(text, score_map, phrase_map, weight + 0.6)
        _collect_scored_phrases(text, score_map, phrase_map, weight)

    selected_phrases = _select_distinct_phrases(score_map, phrase_map, max(max_keywords, max_hashtags) + 2)
    selected_phrases = _filter_grounded_metadata_phrases(
        selected_phrases,
        title_text=title_text,
        context_title=context_title,
    )
    keywords = _merge_unique(selected_phrases[:max_keywords])
    if len(keywords) < max_keywords:
        keywords = _merge_unique(
            keywords,
            _filter_grounded_metadata_phrases(
                _extract_keywords(title_text, desc_text, type_text),
                title_text=title_text,
                context_title=context_title,
            ),
        )
    if len(keywords) < 4:
        keywords = _merge_unique(
            keywords,
            _filter_grounded_metadata_phrases(
                _soft_generic_keywords(title_text, desc_text, type_text),
                title_text=title_text,
                context_title=context_title,
            ),
        )

    hashtags = [f"#{phrase.replace(' ', '_')}" for phrase in selected_phrases[:max_hashtags]]
    if len(hashtags) < min(4, max_hashtags):
        hashtags = _merge_unique(hashtags, [f"#{item.replace(' ', '_')}" for item in keywords[:max_hashtags]])

    topic = title_text or (selected_phrases[0] if selected_phrases else type_text)
    focus_words = _merge_unique([phrase for phrase in selected_phrases if len(phrase.split()) >= 2], keywords, [topic])

    return {
        "topic": topic or ("فيديو جديد" if lang_key.startswith("ar") else "New video"),
        "focus_words": focus_words[: max(max_keywords, 4)],
        "keywords": keywords[:max_keywords],
        "hashtags": hashtags[:max_hashtags],
        "source_name": _clean_text(source_name or channel_name),
        "context_summary": _clean_text(" ".join(part for part in [title_text, desc_text, type_text] if part)),
    }


def _extract_keywords(*parts: Optional[str]) -> List[str]:
    common = {
        "this", "that", "with", "from", "about", "video", "short", "shorts", "clip", "new",
        "هذا", "هذه", "عن", "على", "الى", "إلى", "في", "من", "مع", "شرح", "فيديو", "شورت", "شورتس",
    }
    out: List[str] = []
    seen = set()
    raw = " ".join(_clean_text(p) for p in parts if p)
    for word in re.findall(r"\w{3,}", raw, flags=re.UNICODE):
        if not any(ch.isalpha() for ch in word):
            continue
        key = word.casefold()
        if key in common or key in seen:
            continue
        seen.add(key)
        out.append(word)
        if len(out) >= 8:
            break
    return out


def _topic_matches(profile: Dict[str, Any], haystack: str) -> bool:
    for term in _as_list(profile.get("match_any")):
        normalized = _normalize_key(term)
        if normalized and normalized in haystack:
            return True
    return False


def _merge_profile(base: Dict[str, Any], extra: Dict[str, Any], lang: str) -> Dict[str, Any]:
    result = dict(base)
    for key in ["title_templates", "intro_lines", "body_templates", "outro_lines", "hooks", "focus_words", "keywords", "hashtags"]:
        result[key] = _merge_unique(result.get(key), _lang_list(extra.get(key), lang))
    merged_syn = dict(result.get("synonyms") or {})
    for syn_key, values in _lang_map(extra.get("synonyms"), lang).items():
        merged_syn[syn_key] = _merge_unique(merged_syn.get(syn_key), values)
    result["synonyms"] = merged_syn
    if "min_hashtags" in extra:
        result["min_hashtags"] = _safe_int(extra.get("min_hashtags"), result.get("min_hashtags") or 4)
    if "max_hashtags" in extra:
        result["max_hashtags"] = _safe_int(extra.get("max_hashtags"), result.get("max_hashtags") or 7)
    return result


def _build_profile(data: Dict[str, Any], lang: str, channel_key: str, source_text: str) -> Dict[str, Any]:
    profile = {
        "title_templates": _lang_list(data.get("title_templates"), lang),
        "intro_lines": _lang_list(data.get("intro_lines"), lang),
        "body_templates": _lang_list(data.get("body_templates"), lang),
        "outro_lines": _lang_list(data.get("outro_lines"), lang),
        "hooks": _lang_list(data.get("hooks"), lang),
        "focus_words": _lang_list(data.get("focus_words"), lang),
        "keywords": _lang_list(data.get("keywords"), lang),
        "hashtags": _lang_list(data.get("hashtags"), lang),
        "synonyms": _lang_map(data.get("synonyms"), lang),
        "min_hashtags": _safe_int(data.get("min_hashtags"), 4),
        "max_hashtags": _safe_int(data.get("max_hashtags"), 7),
    }
    haystack = _normalize_key(source_text)
    for item in data.get("topic_profiles") or []:
        if isinstance(item, dict) and _topic_matches(item, haystack):
            profile = _merge_profile(profile, item, lang)
    overrides = data.get("channel_overrides") or {}
    override = overrides.get(channel_key) if isinstance(overrides, dict) else None
    if isinstance(override, dict):
        profile = _merge_profile(profile, override, lang)
    profile["min_hashtags"] = max(1, min(profile["min_hashtags"], 10))
    profile["max_hashtags"] = max(profile["min_hashtags"], min(profile["max_hashtags"], 12))
    return profile


def _default_texts(lang: str) -> Dict[str, List[str]]:
    if lang.startswith("ar"):
        return {
            "title_templates": ["{hook} | {topic}", "{focus} في {topic}", "{topic} - {focus}"],
            "intro_lines": ["فيديو سريع عن {topic}.", "اليوم معنا {topic} بطريقة مختصرة.", "لقطة سريعة حول {topic}."],
            "body_templates": ["ركزنا هنا على {focus} مع تفاصيل مفيدة وسريعة.", "ستلاحظ {keyword} ولماذا هذا الجزء مهم.", "هذا المقطع مناسب إذا كنت مهتمًا بـ {topic_alt}."],
            "outro_lines": ["إذا أعجبك المحتوى شارك رأيك في التعليقات.", "للمزيد من المقاطع المشابهة تابع القناة.", "إذا ناسبك هذا الأسلوب أخبرنا بما تريد لاحقًا."],
            "hooks": ["شاهد الآن", "لا يفوتك", "لقطة اليوم"],
        }
    return {
        "title_templates": ["{hook} | {topic}", "{focus} in {topic}", "{topic} - {focus}"],
        "intro_lines": ["A quick video about {topic}.", "Here is a short look at {topic}.", "A fast clip focused on {topic}."],
        "body_templates": ["This clip highlights {focus} with a simple and clear angle.", "Watch for {keyword} and why it matters here.", "A compact take for anyone interested in {topic_alt}."],
        "outro_lines": ["If you enjoyed it, leave your opinion below.", "Follow for more clips like this.", "Tell us what you want to see next."],
        "hooks": ["Watch now", "Worth a look", "Quick highlight"],
    }


def _pick(rnd: random.Random, items: List[str], fallback: str) -> str:
    choices = [item for item in items if item]
    return rnd.choice(choices) if choices else fallback


def _remix_topic(topic: str, synonyms: Dict[str, List[str]], rnd: random.Random) -> str:
    words = [w for w in topic.split() if w.strip()]
    out: List[str] = []
    for word in words:
        options = synonyms.get(word) or synonyms.get(_normalize_key(word)) or []
        if options and rnd.random() < 0.45:
            out.append(_pick(rnd, options, word))
        else:
            out.append(word)
    return _clean_text(" ".join(out)) or topic


def generate_local_metadata_candidate(
    *,
    video_path: str = "",
    hint_title: str = "",
    source_description: str = "",
    lang: str = "ar",
    channel_key: str = "",
    channel_name: str = "",
    content_type: str = "",
    source_context: Optional[Dict[str, Any]] = None,
    attempt: int = 0,
) -> Dict[str, Any]:
    lang_key = (lang or "ar").strip().lower() or "ar"
    data = load_local_metadata_config()
    signals = extract_source_metadata_context(
        hint_title=hint_title,
        source_description=source_description,
        lang=lang_key,
        content_type=content_type,
        channel_name=channel_name,
        source_name=(source_context or {}).get("source_name") or channel_name,
        source_context=source_context,
    )
    topic = _clean_text(signals.get("topic") or hint_title or str(content_type).replace("_", " ") or ("فيديو جديد" if lang_key.startswith("ar") else "New video"))
    source_text = " ".join([topic, _clean_text(source_description), _clean_text(channel_name), str(content_type).replace("_", " ")])
    profile = _build_profile(data, lang_key, channel_key, source_text)
    defaults = _default_texts(lang_key)

    title_templates = _merge_unique(profile.get("title_templates"), defaults["title_templates"])
    intro_lines = _merge_unique(profile.get("intro_lines"), defaults["intro_lines"])
    body_templates = _merge_unique(profile.get("body_templates"), defaults["body_templates"])
    outro_lines = _merge_unique(profile.get("outro_lines"), defaults["outro_lines"])
    hooks = _merge_unique(profile.get("hooks"), defaults["hooks"])
    focus_words = _merge_unique(signals.get("focus_words"), profile.get("focus_words"), _extract_keywords(topic, source_description), [topic])
    keywords = _merge_unique(signals.get("keywords"), _extract_keywords(topic, source_description, content_type))
    hashtags = _merge_unique(signals.get("hashtags"), [f"#{item.replace(' ', '_')}" for item in keywords[:6]])

    seed = f"{topic}|{video_path}|{channel_key}|{attempt}|{time.time_ns()}|{random.randint(0, 10**9)}"
    rnd = random.Random(hashlib.sha256(seed.encode("utf-8")).hexdigest())

    topic_alt = _remix_topic(topic, profile.get("synonyms") or {}, rnd)
    hook = _pick(rnd, hooks, topic)
    focus = _pick(rnd, focus_words, topic_alt)
    keyword = _pick(rnd, keywords, focus)
    keyword2 = _pick(rnd, [item for item in keywords if item.casefold() != keyword.casefold()], topic_alt)
    values = {
        "topic": topic,
        "topic_alt": topic_alt,
        "focus": focus,
        "hook": hook,
        "keyword": keyword,
        "keyword2": keyword2,
        "channel": channel_name or channel_key,
    }

    title = _clean_text(_safe_format(_pick(rnd, title_templates, "{topic}"), values))
    intro = _clean_text(_safe_format(_pick(rnd, intro_lines, "{topic}"), values))
    body = _clean_text(_safe_format(_pick(rnd, body_templates, "{topic}"), values))
    outro = _clean_text(_safe_format(_pick(rnd, outro_lines, ""), values))

    body2 = ""
    if len(body_templates) > 1 and rnd.random() < 0.6:
        alternatives = [tpl for tpl in body_templates if tpl and tpl != body_templates[0]]
        body2 = _clean_text(_safe_format(_pick(rnd, alternatives, ""), values))
        if body2 == body:
            body2 = ""

    description_parts = [part for part in [intro, body, body2, outro] if part]
    description = "\n".join(description_parts)

    return {
        "title": title,
        "description": description,
        "hashtags": hashtags,
        "min_hashtags": profile.get("min_hashtags") or 4,
        "max_hashtags": profile.get("max_hashtags") or 7,
    }
