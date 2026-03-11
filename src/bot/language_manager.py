"""
نظام دعم اللغات المتعددة
يدعم أكثر من 20 لغة لجميع أنواع المحتوى
"""
from typing import Dict, List, Optional
from dataclasses import dataclass
import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


@dataclass
class Language:
    """نموذج اللغة"""
    code: str           # كود اللغة (ar, en, es, etc.)
    name: str           # الاسم بالعربية
    name_en: str        # الاسم بالإنجليزية
    name_native: str    # الاسم باللغة الأصلية
    flag: str           # علم الدولة emoji
    rtl: bool = False   # من اليمين لليسار


class LanguageManager:
    """مدير اللغات"""
    
    # قائمة اللغات المدعومة
    LANGUAGES = {
        # العربية
        "ar": Language("ar", "العربية", "Arabic", "العربية", "🇸🇦", rtl=True),
        
        # الإنجليزية
        "en": Language("en", "الإنجليزية", "English", "English", "🇺🇸"),
        
        # الإسبانية
        "es": Language("es", "الإسبانية", "Spanish", "Español", "🇪🇸"),
        
        # البرتغالية (البرازيل)
        "pt": Language("pt", "البرتغالية", "Portuguese", "Português", "🇧🇷"),
        
        # الروسية
        "ru": Language("ru", "الروسية", "Russian", "Русский", "🇷🇺"),
        
        # الإندونيسية
        "id": Language("id", "الإندونيسية", "Indonesian", "Bahasa Indonesia", "🇮🇩"),
        
        # التايلاندية
        "th": Language("th", "التايلاندية", "Thai", "ไทย", "🇹🇭"),
        
        # الفيتنامية
        "vi": Language("vi", "الفيتنامية", "Vietnamese", "Tiếng Việt", "🇻🇳"),
        
        # الفلبينية (تاغالوغ)
        "tl": Language("tl", "الفلبينية", "Filipino", "Tagalog", "🇵🇭"),
        
        # الماليزية
        "ms": Language("ms", "الماليزية", "Malay", "Bahasa Melayu", "🇲🇾"),
        
        # الصينية المبسطة
        "zh": Language("zh", "الصينية", "Chinese", "中文", "🇨🇳"),
        
        # اليابانية
        "ja": Language("ja", "اليابانية", "Japanese", "日本語", "🇯🇵"),
        
        # الكورية
        "ko": Language("ko", "الكورية", "Korean", "한국어", "🇰🇷"),
        
        # الهندية
        "hi": Language("hi", "الهندية", "Hindi", "हिन्दी", "🇮🇳"),
        
        # البنغالية
        "bn": Language("bn", "البنغالية", "Bengali", "বাংলা", "🇧🇩"),
        
        # الأوردو
        "ur": Language("ur", "الأوردية", "Urdu", "اردو", "🇵🇰", rtl=True),
        
        # الفارسية
        "fa": Language("fa", "الفارسية", "Persian", "فارسی", "🇮🇷", rtl=True),
        
        # التركية
        "tr": Language("tr", "التركية", "Turkish", "Türkçe", "🇹🇷"),
        
        # الفرنسية
        "fr": Language("fr", "الفرنسية", "French", "Français", "🇫🇷"),
        
        # الألمانية
        "de": Language("de", "الألمانية", "German", "Deutsch", "🇩🇪"),
        
        # الإيطالية
        "it": Language("it", "الإيطالية", "Italian", "Italiano", "🇮🇹"),
        
        # البولندية
        "pl": Language("pl", "البولندية", "Polish", "Polski", "🇵🇱"),
        
        # الهولندية
        "nl": Language("nl", "الهولندية", "Dutch", "Nederlands", "🇳🇱"),
        
        # السويدية
        "sv": Language("sv", "السويدية", "Swedish", "Svenska", "🇸🇪"),
        
        # النرويجية
        "no": Language("no", "النرويجية", "Norwegian", "Norsk", "🇳🇴"),
        
        # الدنماركية
        "da": Language("da", "الدنماركية", "Danish", "Dansk", "🇩🇰"),
        
        # الفنلندية
        "fi": Language("fi", "الفنلندية", "Finnish", "Suomi", "🇫🇮"),
    }
    
    @classmethod
    def get_language(cls, code: str) -> Optional[Language]:
        """الحصول على لغة بواسطة الكود"""
        return cls.LANGUAGES.get(code)
    
    @classmethod
    def get_all_languages(cls) -> List[Language]:
        """الحصول على جميع اللغات"""
        return list(cls.LANGUAGES.values())
    
    @classmethod
    def get_languages_by_region(cls) -> Dict[str, List[Language]]:
        """تصنيف اللغات حسب المنطقة"""
        regions = {
            "الشرق الأوسط": ["ar", "fa", "ur", "tr"],
            "أوروبا": ["en", "es", "fr", "de", "it", "ru", "pl", "nl", "sv", "no", "da", "fi"],
            "آسيا": ["zh", "ja", "ko", "hi", "bn", "id", "th", "vi", "tl", "ms"],
            "أمريكا اللاتينية": ["es", "pt"],
        }
        
        result = {}
        for region, codes in regions.items():
            result[region] = [cls.LANGUAGES[code] for code in codes if code in cls.LANGUAGES]
        
        return result
    
    @classmethod
    def get_languages_keyboard_by_region(cls, page: int = 0, callback_prefix: str = "lang") -> InlineKeyboardMarkup:
        """إنشاء لوحة مفاتيح للغات مقسمة حسب المناطق"""
        regions = cls.get_languages_by_region()
        keyboard = []
        
        for region, languages in regions.items():
            # إضافة عنوان المنطقة كزر غير فعال
            keyboard.append([InlineKeyboardButton(f"─── {region} ───", callback_data="ignore")])
            
            row = []
            for lang in languages:
                row.append(InlineKeyboardButton(
                    f"{lang.flag} {lang.name}",
                    callback_data=f"{callback_prefix}:{lang.code}"
                ))
                if len(row) == 2:
                    keyboard.append(row)
                    row = []
            if row:
                keyboard.append(row)
                
        return InlineKeyboardMarkup(keyboard)
    
    @classmethod
    def get_language_name(cls, code: str, display_lang: str = "ar") -> str:
        """الحصول على اسم اللغة"""
        lang = cls.get_language(code)
        if not lang:
            return code
        
        if display_lang == "ar":
            return lang.name
        elif display_lang == "en":
            return lang.name_en
        else:
            return lang.name_native
    
    @classmethod
    def format_language_display(cls, code: str) -> str:
        """تنسيق عرض اللغة مع العلم"""
        lang = cls.get_language(code)
        if not lang:
            return code
        
        return f"{lang.flag} {lang.name} ({lang.name_native})"
    
    @classmethod
    def is_rtl(cls, code: str) -> bool:
        """التحقق من اتجاه اللغة"""
        lang = cls.get_language(code)
        return lang.rtl if lang else False
    
    @classmethod
    def get_popular_languages(cls) -> List[str]:
        """الحصول على اللغات الأكثر شيوعاً"""
        return ["ar", "en", "es", "pt", "id", "th", "vi", "hi", "ru", "tr"]
    
    @classmethod
    def search_languages(cls, query: str) -> List[Language]:
        """البحث عن لغات"""
        query = query.lower()
        results = []
        
        for lang in cls.LANGUAGES.values():
            if (query in lang.name.lower() or 
                query in lang.name_en.lower() or 
                query in lang.name_native.lower() or
                query in lang.code.lower()):
                results.append(lang)
        
        return results


# نصوص الدعوة (CTA) بلغات مختلفة
CTA_TEXTS = {
    "ar": "مود موجود على\nتطبيق Modetaris\nالرابط في البايو",
    "en": "Mod available on\nModetaris app\nLink in bio",
    "es": "Mod disponible en\napp Modetaris\nEnlace en bio",
    "pt": "Mod disponível no\napp Modetaris\nLink na bio",
    "ru": "Мод доступен в\nModetaris app\nСсылка в био",
    "id": "Mod tersedia di\naplikasi Modetaris\nLink di bio",
    "th": "มีมอดให้โหลด في\nแอป Modetaris\nลิงก์อยู่ในไبيو",
    "vi": "Mod có sẵn trên\nứng dụng Modetaris\nLink trong tiểu sử",
    "tl": "Available ang mod\nsa Modetaris app\nLink sa bio",
    "ms": "Mod tersedia di\naplikasi Modetaris\nLink di bio",
    "zh": "Mod可在\nModetaris应用下载\n链接在简介",
    "ja": "Modは\nModetarisアプリで\nリンクはバイオに",
    "ko": "Modetaris 앱에서\n모드 이용 가능\n바이오 링크 확인",
    "hi": "Mod Modetaris\nऐप पर उपलब्ध है\nबायो में लिंक है",
    "bn": "Modetaris অ্যাপে\nমোড পাওয়া যাবে\nবায়োতে লিঙ্ক দেওয়া আছে",
    "ur": "موڈ Modetaris\nایپ پر دستیاب ہے\nبایو میں لنک دیکھیں",
    "fa": "مود در اپلیکیشن\nModetaris موجود است\nلینک در بایو",
    "tr": "Mod Modetaris\nuygulamasında mevcut\nBağlantı profilde",
    "fr": "Mod disponible sur\nl'app Modetaris\nLien en bio",
    "de": "Mod in der\nModetaris-App\nLink in der Bio",
    "it": "Mod disponibile\nsull'app Modetaris\nLink in bio",
    "pl": "Mod dostępny w\naplikacji Modetaris\nLink w bio",
    "nl": "Mod beschikbaar in\nModetaris-app\nLink in bio",
    "sv": "Mod tillgänglig i\nModetaris-appen\nLänk i bio",
    "no": "Mod tilgjengelig i\nModetaris-appen\nLenke i bio",
    "da": "Mod tilgængelig i\nModetaris-appen\nLink i bio",
    "fi": "Mod saatavilla\nModetaris-sovelluksessa\nLinkki biossa",
}

# نص "اقرأ أول تعليق" بلغات مختلفة (لمحتوى ماينكرافت)
READ_FIRST_COMMENT_TEXTS = {
    "ar": "اقرأ أول تعليق",
    "en": "Read the first comment",
    "es": "Lee el primer comentario",
    "pt": "Leia o primeiro comentário",
    "ru": "Читай первый комментарий",
    "id": "Baca komentar pertama",
    "th": "อ่านคอมเมนต์แรก",
    "vi": "Đọc bình luận đầu tiên",
    "tl": "Basahin ang unang komento",
    "ms": "Baca komen pertama",
    "zh": "看第一条评论",
    "ja": "最初のコメントを見てね",
    "ko": "첫 번째 댓글을 확인하세요",
    "hi": "पहला कमेंट पढ़ें",
    "bn": "প্রথম کمেন্টটি পড়ুন",
    "ur": "پہلا کمنٹ پڑھیں",
    "fa": "اولین کامنت را بخوانید",
    "tr": "İlk yorumu oku",
    "fr": "Lis le premier commentaire",
    "de": "Lies den ersten Kommentar",
    "it": "Leggi il primo commento",
    "pl": "Przeczytaj pierwszy komentarz",
    "nl": "Lees de eerste reactie",
    "sv": "Läs första kommentaren",
    "no": "Les den første kommentaren",
    "da": "Læs den eerste kommentar",
    "fi": "Lue ensimmäinen kommentti",
}

# نص "رابط التحميل في البايو" بلغات مختلفة
DOWNLOAD_LINK_IN_BIO_TEXTS = {
    "ar": "رابط التحميل في البايو",
    "en": "Download link in bio",
    "es": "Enlace de descarga en la bio",
    "pt": "Link de download na bio",
    "ru": "Ссылка на скачивание в био",
    "id": "Link unduhan di bio",
    "th": "ลิงก์ดาวน์โหลด فيไบโอ",
    "vi": "Link tải xuống trong tiểu sử",
    "tl": "Link sa pag-download nasa bio",
    "ms": "Pautan muat turun di bio",
    "zh": "下载链接在简介",
    "ja": "ダウンロードリンクはバイオに",
    "ko": "다운로드 링크는 바이오에",
    "hi": "डाउनلود लिंक बायो में है",
    "bn": "ডাউনলোড লিঙ্ক বায়োতে আছে",
    "ur": "ڈاؤن لوڈ لنک بائیو में है",
    "fa": "لینک دانلود در بیو",
    "tr": "İndirme bağlantısı profilde",
    "fr": "Lien de téléchargement en bio",
    "de": "Download-Link in der Bio",
    "it": "Link per il download nella bio",
    "pl": "Link do pobrania w bio",
    "nl": "Downloadlink in de bio",
    "sv": "Nedladdningslänk i bion",
    "no": "Nedlastingslenke i bio",
    "da": "Download-link i bio",
    "fi": "Latauslinkki biossa",
}


def get_cta_text(language_code: str) -> str:
    """الحصول على نص الدعوة باللغة المحددة"""
    return CTA_TEXTS.get(language_code, CTA_TEXTS["en"])


def get_read_first_comment_text(language_code: str) -> str:
    """الحصول على نص "اقرأ أول تعليق" بلغة القناة.

    لو لم تكن اللغة مدعومة في الجدول، نرجع النص الإنجليزي كافتراضي.
    """
    return READ_FIRST_COMMENT_TEXTS.get(language_code, READ_FIRST_COMMENT_TEXTS["en"])


def get_download_link_in_bio_text(language_code: str) -> str:
    """الحصول على نص "رابط التحميل في البايو" بلغة القناة."""
    return DOWNLOAD_LINK_IN_BIO_TEXTS.get(language_code, DOWNLOAD_LINK_IN_BIO_TEXTS["en"])


# أمثلة على prompts للذكاء الاصطناعي بلغات مختلفة
AI_PROMPTS = {
    "ar": "اكتب عنواناً احترافياً جذاباً لفيديو شورتس عن {topic} باللغة العربية مع 5 هاشتاقات مناسبة",
    "en": "Write a professional catchy title for a YouTube Shorts video about {topic} in English with 5 relevant hashtags",
    "es": "Escribe un título profesional atractivo para un video de YouTube Shorts sobre {topic} en español con 5 hashtags relevantes",
    "pt": "Escreva um título profissional atraente para um vídeo do YouTube Shorts sobre {topic} em português com 5 hashtags relevantes",
    "ru": "Напиши профессиональный привлекательный заголовок для видео YouTube Shorts о {topic} на русском языке с 5 релевантными хэштегами",
    "id": "Tulis judul profesional yang menarik untuk video YouTube Shorts tentang {topic} dalam bahasa Indonesia dengan 5 hashtag yang relevan",
    "th": "เขียนชื่อเรื่องที่น่าสนใจสำหรับวิดีโอ YouTube Shorts เกี่ยวกับ {topic} เป็นภาษาไทยพร้อม 5 แฮชแท็กที่เกี่ยวข้อง",
    "vi": "Viết tiêu đề chuyên nghiệp hấp dẫn cho video YouTube Shorts về {topic} bằng tiếng Việt với 5 hashtag liên quan",
    "hi": "{topic} के बारे में YouTube Shorts वीडियो के लिए 5 प्रासंगिक हैशटैग के साथ हिंदी में एक पेशेवर आकर्षक शीर्षक लिखें",
    "zh": "为关于{topic}的YouTube Shorts视频写一个专业吸引人的中文标题，并附上5个相关标签",
    "ja": "{topic}についてのYouTube Shortsビデオの日本語でプロフェッショナルで魅力的なタイトルを5つの関連ハッシュタグと共に書いてください",
    "ko": "{topic}에 대한 YouTube Shorts 영상의 한국어로 전문적이고 매력적인 제목을 5개의 관련 해시태그와 함께 작성하세요",
}


def get_ai_prompt(language_code: str, topic: str) -> str:
    """الحصول على prompt للذكاء الاصطناعي باللغة المحددة"""
    template = AI_PROMPTS.get(language_code, AI_PROMPTS["en"])
    return template.format(topic=topic)


def _contains_arabic(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]", text or ""))


def _normalize_topic_for_lang(topic: str, lang: str) -> str:
    t = (topic or "").strip()
    if not t:
        return "Minecraft Mod"

    if lang in {"ar", "fa", "ur"}:
        return t

    if _contains_arabic(t):
        fallback_topics = {
            "tr": "Minecraft modu",
            "en": "Minecraft mod",
            "es": "Mod de Minecraft",
            "pt": "Mod do Minecraft",
            "ru": "Мод Minecraft",
            "id": "Mod Minecraft",
            "th": "มอด Minecraft",
            "vi": "Mod Minecraft",
            "tl": "Minecraft mod",
            "ms": "Mod Minecraft",
            "zh": "Minecraft 模组",
            "ja": "Minecraft MOD",
            "ko": "Minecraft 모드",
            "hi": "Minecraft मॉड",
            "bn": "Minecraft মড",
            "fr": "Mod Minecraft",
            "de": "Minecraft Mod",
            "it": "Mod Minecraft",
            "pl": "Mod Minecraft",
            "nl": "Minecraft mod",
            "sv": "Minecraft mod",
            "no": "Minecraft mod",
            "da": "Minecraft mod",
            "fi": "Minecraft mod",
        }
        return fallback_topics.get(lang, "Minecraft Mod")

    return t


def _sanitize_hashtag_for_lang(tag: str, lang: str) -> Optional[str]:
    ht = (tag or "").strip()
    if not ht:
        return None
    if not ht.startswith("#"):
        ht = "#" + ht

    if lang not in {"ar", "fa", "ur"}:
        if _contains_arabic(ht):
            return None

    return ht


def _topic_to_hashtag(topic: str, lang: str) -> str:
    t = (topic or "").strip()
    if not t:
        return "#MinecraftMod"
    t = " ".join(t.replace("\n", " ").replace("\r", " ").split())
    t = t.replace("#", "")
    if lang not in {"ar", "fa", "ur"}:
        if _contains_arabic(t):
            t = "Minecraft Mod"
    if lang == "tr":
        t = re.sub(r"[^0-9A-Za-zÇĞİÖŞÜçğıöşü ]+", " ", t)
    keep = []
    for ch in t:
        if ch.isalnum():
            keep.append(ch)
        elif ch.isspace():
            keep.append(" ")
    cleaned = "".join(keep).strip()
    if not cleaned:
        return "#MinecraftMod"
    parts = [p for p in cleaned.split(" ") if p]
    if not parts:
        return "#MinecraftMod"

    # Arabic/Persian/Urdu: keep hashtags readable by joining with underscores
    # and limiting number of words to avoid extremely long concatenations.
    if lang in {"ar", "fa", "ur"}:
        parts = parts[:3]
        joined = "_".join(parts)
        if not joined:
            return "#MinecraftMod"
        return "#" + joined[:32].strip("_")

    # Latin-ish languages: keep readability by joining with underscores.
    parts = parts[:4]
    joined = "_".join(parts)
    if not joined:
        return "#MinecraftMod"
    joined = re.sub(r"_+", "_", joined).strip("_")
    return "#" + joined[:40]


def build_shorts_fallback_metadata(topic: str, language_code: str) -> tuple[str, list[str], str]:
    lang = (language_code or "en").strip().lower()
    if lang not in LanguageManager.LANGUAGES:
        lang = "en"

    topic = _normalize_topic_for_lang((topic or "").strip() or "Minecraft Mod", lang)
    topic_hashtag = _topic_to_hashtag(topic, lang)

    hashtags_by_lang: Dict[str, List[str]] = {
        "ar": ["#ماينكرافت", "#مود", "#شورتس", "#العاب", "#Minecraft"],
        "en": ["#Minecraft", "#MinecraftMod", "#Mods", "#Shorts", "#Gaming"],
        "es": ["#Minecraft", "#Mods", "#Shorts", "#Videojuegos", "#MinecraftMod"],
        "pt": ["#Minecraft", "#Mods", "#Shorts", "#Jogos", "#MinecraftMod"],
        "ru": ["#Minecraft", "#Майнкраفت", "#Моды", "#Shorts", "#Игры"],
        "id": ["#Minecraft", "#ModMinecraft", "#Shorts", "#Game", "#Addon"],
        "th": ["#Minecraft", "#มายคราฟ", "#ม็อด", "#Shorts", "#เกม"],
        "vi": ["#Minecraft", "#Mod", "#Shorts", "#Game", "#Addon"],
        "tl": ["#Minecraft", "#Mod", "#Shorts", "#Laro", "#Gaming"],
        "ms": ["#Minecraft", "#Mod", "#Shorts", "#Permainan", "#Gaming"],
        "zh": ["#Minecraft", "#我的世界", "#模组", "#Shorts", "#游戏"],
        "ja": ["#Minecraft", "#マインクラフト", "#MOD", "#Shorts", "#ゲーム"],
        "ko": ["#Minecraft", "#마인크래프트", "#모드", "#Shorts", "#게임"],
        "hi": ["#Minecraft", "#माइनक्राफ्ट", "#मोड", "#Shorts", "#Gaming"],
        "bn": ["#Minecraft", "#মাইনক্রাফ্ট", "#মড", "#Shorts", "#গেম"],
        "ur": ["#Minecraft", "#مائنکرافٹ", "#مود", "#Shorts", "#Gaming"],
        "fa": ["#Minecraft", "#ماینکرفت", "#مود", "#Shorts", "#بازی"],
        "tr": ["#Minecraft", "#Mod", "#Shorts", "#Oyun", "#MinecraftMod"],
        "fr": ["#Minecraft", "#Mods", "#Shorts", "#JeuxVideo", "#MinecraftMod"],
        "de": ["#Minecraft", "#Mods", "#Shorts", "#Gaming", "#MinecraftMod"],
        "it": ["#Minecraft", "#Mod", "#Shorts", "#Videogiochi", "#MinecraftMod"],
        "pl": ["#Minecraft", "#Mody", "#Shorts", "#Gry", "#MinecraftMod"],
        "nl": ["#Minecraft", "#Mods", "#Shorts", "#Gaming", "#MinecraftMod"],
        "sv": ["#Minecraft", "#Mods", "#Shorts", "#Spel", "#MinecraftMod"],
        "no": ["#Minecraft", "#Mods", "#Shorts", "#Spill", "#MinecraftMod"],
        "da": ["#Minecraft", "#Mods", "#Shorts", "#Spil", "#MinecraftMod"],
        "fi": ["#Minecraft", "#Modit", "#Shorts", "#Pelit", "#MinecraftMod"],
    }

    base_tags = hashtags_by_lang.get(lang) or hashtags_by_lang["en"]

    tags: List[str] = []
    for ht in [topic_hashtag, *base_tags, "#Mod", "#Addon"]:
        ht2 = _sanitize_hashtag_for_lang(ht, lang)
        if not ht2:
            continue
        if ht2 not in tags:
            tags.append(ht2)
    tags = tags[:12]

    title = " ".join(tags[:8])

    desc_labels = {
        "ar": ("🎮 شورتس ماينكرافت مود", "الموضوع:", "هاشتاقات:"),
        "en": ("🎮 Minecraft Mod Shorts", "Topic:", "Hashtags:"),
        "es": ("🎮 Shorts de Mods de Minecraft", "Tema:", "Hashtags:"),
        "pt": ("🎮 Shorts de Mods do Minecraft", "Tema:", "Hashtags:"),
        "ru": ("🎮 Minecraft Моды Shorts", "Теما:", "Хэштеги:"),
        "id": ("🎮 Minecraft Mod Shorts", "Topik:", "Tagar:"),
        "th": ("🎮 Minecraft Mod Shorts", "หัวข้อ:", "แฮชแท็ก:"),
        "vi": ("🎮 Minecraft Mod Shorts", "Chủ đề:", "Hashtag:"),
        "tl": ("🎮 Minecraft Mod Shorts", "Paksa:", "Hashtags:"),
        "ms": ("🎮 Minecraft Mod Shorts", "Topik:", "Hashtag:"),
        "zh": ("🎮 我的世界 模组 Shorts", "主题：", "标签："),
        "ja": ("🎮 Minecraft MOD Shorts", "トピック：", "ハッシュタグ："),
        "ko": ("🎮 Minecraft 모드 Shorts", "주제:", "해시태그:"),
        "hi": ("🎮 Minecraft Mod Shorts", "विषय:", "हैशटैग:"),
        "bn": ("🎮 Minecraft Mod Shorts", "বিষয়:", "হ্যাشٹ্যাগ:"),
        "ur": ("🎮 Minecraft Mod Shorts", "موضوع:", "ہیش ٹیگز:"),
        "fa": ("🎮 Minecraft Mod Shorts", "موضوع:", "هشتگ‌ها:"),
        "tr": ("🎮 Minecraft Mod Shorts", "Konu:", "Etiketler:"),
        "fr": ("🎮 Minecraft Mods Shorts", "Sujet :", "Hashtags :"),
        "de": ("🎮 Minecraft Mods Shorts", "Thema:", "Hashtags:"),
        "it": ("🎮 Minecraft Mod Shorts", "Tema:", "Hashtag:"),
        "pl": ("🎮 Minecraft Mod Shorts", "Temat:", "Hashtagi:"),
        "nl": ("🎮 Minecraft Mod Shorts", "Onderwerp:", "Hashtags:"),
        "sv": ("🎮 Minecraft Mod Shorts", "Ämne:", "Hashtags:"),
        "no": ("🎮 Minecraft Mod Shorts", "Tema:", "Hashtags:"),
        "da": ("🎮 Minecraft Mod Shorts", "Emne:", "Hashtags:"),
        "fi": ("🎮 Minecraft Mod Shorts", "Aihe:", "Hashtagit:"),
    }
    headline, topic_label, tags_label = desc_labels.get(lang, desc_labels["en"])
    cta = get_cta_text(lang)
    desc = f"{headline}\n\n{topic_label} {topic}\n\n{cta}\n\n{tags_label} {' '.join(tags[:10])}"
    return title, tags, desc
