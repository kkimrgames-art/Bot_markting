import os
import logging
from dataclasses import dataclass
from typing import List
from dotenv import load_dotenv


_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


def get_project_root() -> str:
    return _PROJECT_ROOT


def resolve_project_path(path: str | None, default_relative: str | None = None) -> str | None:
    raw = (path or default_relative or "").strip()
    if not raw:
        return None
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.abspath(os.path.join(_PROJECT_ROOT, raw))


def _to_bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _to_int(val: str | None, default: int) -> int:
    try:
        return int(val) if val is not None and val != "" else default
    except ValueError:
        return default


def _to_float(val: str | None, default: float) -> float:
    if val is None or val == "":
        return default
    # Strip inline comments like "0.5  # note"
    cleaned = val.split("#", 1)[0].strip()
    try:
        return float(cleaned)
    except ValueError:
        return default


def _to_list(val: str | None) -> List[int]:
    if not val:
        return []
    items = []
    for part in val.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            items.append(int(part))
        except ValueError:
            # ignore non-integer entries
            pass
    return items


@dataclass
class Config:
    # AI / Mistral
    MISTRAL_API_KEY: str | None
    MISTRAL_PROXY_URL: str | None
    DISABLE_MISTRAL: bool
    AI_PROVIDER_ORDER: str

    # YouTube OAuth
    GOOGLE_CLIENT_ID: str | None
    GOOGLE_CLIENT_SECRET: str | None
    GOOGLE_REDIRECT_URI: str | None
    GOOGLE_OAUTH_PORT: int

    # Agent
    CHANNEL_LIST_PATH: str
    OUTPUT_DIR: str
    TEMP_DIR: str
    AUDIO_MODE: str
    CHUNK_SECONDS: int
    CHUNK_OVERLAP: float
    ACCOMP_THRESHOLD: float
    ACCOMP_REDUCTION: float
    SEPARATION_ENGINE: str
    MDX_MODEL_PATH: str | None
    EXTERNAL_SEP_API_URL: str | None
    EXTERNAL_SEP_API_KEY: str | None
    MUSIC_DETECTION_ENABLED: bool
    SKIP_IF_MUSIC: bool
    MUSIC_DETECTION_THRESHOLD: float
    AUDIO_SEPARATION_ENABLED: bool
    DEFAULT_TEST_URL: str | None
    YTDLP_FORCE_IPV4: bool

    # PIP
    REACTIONS_DIR: str
    PIP_SCALE: float
    PIP_MARGIN: int
    PIP_POSITION: str

    # Background Removal
    BACKGROUND_REMOVAL_ENABLED: bool
    BACKGROUND_DIR: str

    # Scheduler / Conditions
    RUN_DAILY_AT: str | None
    RUN_ONLY_ON_WIFI: bool
    RUN_ONLY_WHILE_CHARGING: bool

    # Termux / Performance
    ORT_THREADS: int
    QUANTIZE: bool

    # Telegram
    TELEGRAM_BOT_TOKEN: str | None
    LOCAL_BOT_API_URL: str | None
    TELEGRAM_API_ID: int | None
    TELEGRAM_API_HASH: str | None
    TELEGRAM_ALLOWED_USER_IDS: List[int]
    TG_MODE: str
    TELEGRAM_WEBHOOK_URL: str | None
    TELEGRAM_WEBHOOK_SECRET_PATH: str | None
    TELEGRAM_PERSISTENCE: str
    TELEGRAM_DB_PATH: str

    # Testing Mode
    SINGLE_VIDEO_MODE: bool
    SKIP_PROCESSED_VIDEOS: bool

    # Global Fonts
    GLOBAL_FONT_AR: str | None
    GLOBAL_FONT_EN: str | None

    # App Download
    APP_DOWNLOAD_URL: str

    # Supabase (Optional for local state but good for sync)
    SUPABASE_URL: str | None
    SUPABASE_KEY: str | None


_config_cache: Config | None = None


def load_config(force_reload: bool = False) -> Config:
    global _config_cache
    if force_reload:
        _config_cache = None
    if _config_cache is not None:
        return _config_cache

    # Load only the project-local .env to avoid cwd-dependent leakage.
    load_dotenv(dotenv_path=resolve_project_path(".env"), override=True)

    cfg = Config(
        # AI / Mistral
        MISTRAL_API_KEY=os.getenv("MISTRAL_API_KEY"),
        MISTRAL_PROXY_URL=os.getenv("MISTRAL_PROXY_URL"),
        DISABLE_MISTRAL=_to_bool(os.getenv("DISABLE_MISTRAL"), False),
        AI_PROVIDER_ORDER=os.getenv("AI_PROVIDER_ORDER", "smart"),
        # YouTube
        GOOGLE_CLIENT_ID=os.getenv("GOOGLE_CLIENT_ID"),
        GOOGLE_CLIENT_SECRET=os.getenv("GOOGLE_CLIENT_SECRET"),
        GOOGLE_REDIRECT_URI=os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/"),
        GOOGLE_OAUTH_PORT=_to_int(os.getenv("GOOGLE_OAUTH_PORT"), 8080),
        # Agent
        CHANNEL_LIST_PATH=os.getenv("CHANNEL_LIST_PATH", "spec/channels.txt"),
        OUTPUT_DIR=os.getenv("OUTPUT_DIR", "outputs"),
        TEMP_DIR=os.getenv("TEMP_DIR", ".temp"),
        AUDIO_MODE=os.getenv("AUDIO_MODE", "light"),
        CHUNK_SECONDS=_to_int(os.getenv("CHUNK_SECONDS"), 15),
        CHUNK_OVERLAP=_to_float(os.getenv("CHUNK_OVERLAP"), 1.0),
        ACCOMP_THRESHOLD=_to_float(os.getenv("ACCOMP_THRESHOLD"), 0.4),
        ACCOMP_REDUCTION=_to_float(os.getenv("ACCOMP_REDUCTION"), 0.25),
        SEPARATION_ENGINE=os.getenv("SEPARATION_ENGINE", "demucs"),
        MDX_MODEL_PATH=os.getenv("MDX_MODEL_PATH"),
        EXTERNAL_SEP_API_URL=os.getenv("EXTERNAL_SEP_API_URL"),
        EXTERNAL_SEP_API_KEY=os.getenv("EXTERNAL_SEP_API_KEY"),
        MUSIC_DETECTION_ENABLED=_to_bool(os.getenv("MUSIC_DETECTION_ENABLED"), False),
        SKIP_IF_MUSIC=_to_bool(os.getenv("SKIP_IF_MUSIC"), False),
        MUSIC_DETECTION_THRESHOLD=_to_float(os.getenv("MUSIC_DETECTION_THRESHOLD"), 0.3),
        AUDIO_SEPARATION_ENABLED=_to_bool(os.getenv("AUDIO_SEPARATION_ENABLED"), True),
        DEFAULT_TEST_URL=os.getenv("DEFAULT_TEST_URL"),
        YTDLP_FORCE_IPV4=_to_bool(os.getenv("YTDLP_FORCE_IPV4"), True),
        # PIP
        REACTIONS_DIR=os.getenv("REACTIONS_DIR", "reactions"),
        PIP_SCALE=float(os.getenv("PIP_SCALE", "0.7")),
        PIP_MARGIN=_to_int(os.getenv("PIP_MARGIN"), 6),
        PIP_POSITION=os.getenv("PIP_POSITION", "bottom_right"),
        # Background Removal
        BACKGROUND_REMOVAL_ENABLED=_to_bool(os.getenv("BACKGROUND_REMOVAL_ENABLED"), True),
        BACKGROUND_DIR=os.getenv("BACKGROUND_DIR", "background"),
        # Scheduler
        RUN_DAILY_AT=os.getenv("RUN_DAILY_AT"),
        RUN_ONLY_ON_WIFI=_to_bool(os.getenv("RUN_ONLY_ON_WIFI"), True),
        RUN_ONLY_WHILE_CHARGING=_to_bool(os.getenv("RUN_ONLY_WHILE_CHARGING"), True),
        # Performance
        ORT_THREADS=_to_int(os.getenv("ORT_THREADS"), 2),
        QUANTIZE=_to_bool(os.getenv("QUANTIZE"), True),
        # Telegram
        TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN"),
        LOCAL_BOT_API_URL=os.getenv("LOCAL_BOT_API_URL"),
        TELEGRAM_API_ID=_to_int(os.getenv("TELEGRAM_API_ID"), None),
        TELEGRAM_API_HASH=os.getenv("TELEGRAM_API_HASH"),
        TELEGRAM_ALLOWED_USER_IDS=_to_list(os.getenv("TELEGRAM_ALLOWED_USER_IDS")),
        TG_MODE=os.getenv("TG_MODE", "polling"),
        TELEGRAM_WEBHOOK_URL=os.getenv("TELEGRAM_WEBHOOK_URL"),
        TELEGRAM_WEBHOOK_SECRET_PATH=os.getenv("TELEGRAM_WEBHOOK_SECRET_PATH"),
        TELEGRAM_PERSISTENCE=os.getenv("TELEGRAM_PERSISTENCE", "sqlite"),
        TELEGRAM_DB_PATH=os.getenv("TELEGRAM_DB_PATH", ".data/tg_state.db"),
        # Testing Mode
        SINGLE_VIDEO_MODE=_to_bool(os.getenv("SINGLE_VIDEO_MODE"), False),
        SKIP_PROCESSED_VIDEOS=_to_bool(os.getenv("SKIP_PROCESSED_VIDEOS"), True),
        # Global Fonts
        GLOBAL_FONT_AR=os.getenv("GLOBAL_FONT_AR"),
        GLOBAL_FONT_EN=os.getenv("GLOBAL_FONT_EN"),
        APP_DOWNLOAD_URL=os.getenv("APP_DOWNLOAD_URL", "https://download-4ma.pages.dev/"),
        # Supabase
        SUPABASE_URL=os.getenv("SUPABASE_URL"),
        SUPABASE_KEY=os.getenv("SUPABASE_KEY"),
    )

    cfg.CHANNEL_LIST_PATH = resolve_project_path(cfg.CHANNEL_LIST_PATH, "spec/channels.txt") or os.path.join(get_project_root(), "spec", "channels.txt")
    cfg.OUTPUT_DIR = resolve_project_path(cfg.OUTPUT_DIR, "outputs") or os.path.join(get_project_root(), "outputs")
    cfg.TEMP_DIR = resolve_project_path(cfg.TEMP_DIR, ".temp") or os.path.join(get_project_root(), ".temp")
    cfg.REACTIONS_DIR = resolve_project_path(cfg.REACTIONS_DIR, "reactions") or os.path.join(get_project_root(), "reactions")
    cfg.BACKGROUND_DIR = resolve_project_path(cfg.BACKGROUND_DIR, "background") or os.path.join(get_project_root(), "background")
    cfg.TELEGRAM_DB_PATH = resolve_project_path(cfg.TELEGRAM_DB_PATH, ".data/tg_state.db") or os.path.join(get_project_root(), ".data", "tg_state.db")
    cfg.TELEGRAM_WEBHOOK_SECRET_PATH = resolve_project_path(cfg.TELEGRAM_WEBHOOK_SECRET_PATH)

    # Override from state file if exists
    state_path = os.path.join(os.path.dirname(cfg.TELEGRAM_DB_PATH) or ".data", "tg_state.json")
    if os.path.exists(state_path):
        try:
            import json
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
                global_fonts = state.get("global_fonts", {})
                if global_fonts.get("ar"):
                    cfg.GLOBAL_FONT_AR = os.path.normpath(global_fonts["ar"])
                if global_fonts.get("en"):
                    cfg.GLOBAL_FONT_EN = os.path.normpath(global_fonts["en"])
                
                # App Download URL override
                if "app_download_url" in state:
                    cfg.APP_DOWNLOAD_URL = state["app_download_url"]
        except Exception:
            pass

    _config_cache = cfg
    return cfg


def ensure_dirs(cfg: Config) -> None:
    for path in [cfg.OUTPUT_DIR, cfg.TEMP_DIR, cfg.REACTIONS_DIR, os.path.dirname(cfg.TELEGRAM_DB_PATH) or ".data"]:
        if path and not os.path.exists(path):
            os.makedirs(path, exist_ok=True)


def ensure_channels_file(cfg: Config) -> None:
    path = cfg.CHANNEL_LIST_PATH
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("")


def update_admin_id(user_id: int) -> bool:
    """
    تحديث معرف المسؤول في الذاكرة وملف .env تلقائياً.
    يُستدعى عند أول رسالة يرسلها المستخدم للبوت.
    """
    global _config_cache
    try:
        user_id = int(user_id)

        # تحديث الذاكرة
        cfg = load_config()
        if user_id not in cfg.TELEGRAM_ALLOWED_USER_IDS:
            cfg.TELEGRAM_ALLOWED_USER_IDS.append(user_id)
        
        # استخدم ملف .env المحلي داخل جذر المشروع فقط.
        env_file = resolve_project_path(".env") or os.path.join(get_project_root(), ".env")
        
        # قراءة المحتوى الحالي
        existing_content = ""
        if os.path.exists(env_file):
            with open(env_file, "r", encoding="utf-8") as f:
                existing_content = f.read()
        
        # تحديث أو إضافة TELEGRAM_ALLOWED_USER_IDS
        ids_str = ",".join(str(uid) for uid in cfg.TELEGRAM_ALLOWED_USER_IDS)
        new_line = f"TELEGRAM_ALLOWED_USER_IDS={ids_str}"
        
        if "TELEGRAM_ALLOWED_USER_IDS" in existing_content:
            # استبدال السطر الموجود
            import re
            existing_content = re.sub(
                r"TELEGRAM_ALLOWED_USER_IDS=.*",
                new_line,
                existing_content
            )
        else:
            # إضافة سطر جديد
            if existing_content and not existing_content.endswith("\n"):
                existing_content += "\n"
            existing_content += new_line + "\n"
        
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(existing_content)
        
        # تحديث متغير البيئة أيضاً
        os.environ["TELEGRAM_ALLOWED_USER_IDS"] = ids_str

        try:
            from ..bot.security import get_security_manager
            get_security_manager(cfg)
        except Exception:
            pass
        
        logging.getLogger(__name__).info(f"✅ تم حفظ معرف المسؤول: {user_id} → {env_file}")
        return True
        
    except Exception as e:
        logging.getLogger(__name__).error(f"❌ فشل حفظ معرف المسؤول: {e}")
        return False
