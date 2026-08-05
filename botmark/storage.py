"""BotMark — تخزين الإعدادات وقراءة ملفات .env والتحقق منها."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# المفاتيح التي لا يمكن للبوت العمل بدونها
CRITICAL_KEYS = [
    "TELEGRAM_BOT_TOKEN",  # توكن بوت تيليجرام (من @BotFather)
    "SUPABASE_URL",        # رابط قاعدة البيانات
    "SUPABASE_KEY",        # مفتاح قاعدة البيانات
]

# مفاتيح مهمة لكن اختيارية (يُنصح بها)
RECOMMENDED_KEYS = [
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REDIRECT_URI",
    "YTDLP_COOKIES_B64",
    "YTDLP_COOKIES_PATH",
    "TELEGRAM_ALLOWED_USER_IDS",
    "INSTANCE_ID",
    "DOWNLOADER_WORKER_URL",
    "DOWNLOADER_WORKER_TOKEN",
]

# مفاتيح لا ينبغي إظهارها كاملة في المخرجات
SECRET_KEYS = {
    "TELEGRAM_BOT_TOKEN",
    "SUPABASE_KEY",
    "GOOGLE_CLIENT_SECRET",
    "YTDLP_COOKIES_B64",
    "DOWNLOADER_WORKER_TOKEN",
    "YTDLP_COOKIES_PATH",
}


def config_dir() -> Path:
    """مجلد إعدادات BotMark في منزل المستخدم (~/.botmark)."""
    d = Path.home() / ".botmark"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


def logs_dir() -> Path:
    d = config_dir() / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def log_file() -> Path:
    return logs_dir() / "bot.log"


def pid_file() -> Path:
    return config_dir() / "bot.pid"


def load_config() -> dict:
    p = config_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_config(cfg: dict) -> None:
    cfg = dict(cfg)
    cfg.setdefault("updated_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    config_path().write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_env_file(path: Path) -> dict:
    """قراءة ملف .env وإرجاع قاموس القيم (يتجاهل التعليقات والفراغات).

    يفضّل python-dotenv إن كان مثبتاً، ويعمل بنسخة بسيطة بدونه.
    """
    path = Path(path)
    if not path.is_file():
        return {}

    try:
        from dotenv import dotenv_values

        raw = dotenv_values(str(path))
        return {k: v for k, v in raw.items() if v is not None}
    except Exception:
        pass

    values: dict = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                val = val[1:-1]
            if key:
                values[key] = val
    except Exception:
        pass
    return values


def validate_env(values: dict) -> tuple[list, list]:
    """إرجاع (المفاتيح الحرجة الناقصة، المفاتيح الموصى بها الناقصة)."""
    missing_critical = [k for k in CRITICAL_KEYS if not str(values.get(k) or "").strip()]
    missing_rec = [k for k in RECOMMENDED_KEYS if not str(values.get(k) or "").strip()]
    return missing_critical, missing_rec


def mask_value(key: str, value: str) -> str:
    """إخفاء القيم الحساسة عند العرض."""
    if not value:
        return "(فارغ)"
    if key in SECRET_KEYS:
        return value[:6] + "••••••" + (value[-4:] if len(value) > 12 else "")
    if len(value) > 48:
        return value[:48] + "…"
    return value


def resolve_project_root(cfg: dict) -> Path | None:
    """إيجاد مجلد المشروع (الذي يحتوي main.py).

    الترتيب: متغير البيئة > الإعدادات المحفوظة > موقع حزمة botmark نفسها.
    """
    raw = os.environ.get("BOTMARK_PROJECT_ROOT") or (cfg or {}).get("project_root") or ""
    if raw:
        p = Path(raw).expanduser()
        if p.is_dir() and (p / "main.py").is_file():
            return p.resolve()

    pkg_parent = Path(__file__).resolve().parent.parent
    if (pkg_parent / "main.py").is_file():
        return pkg_parent.resolve()

    if raw:
        p = Path(raw).expanduser()
        if p.is_dir():
            return p.resolve()
    return None


def find_env_candidates(project_root: Path | None) -> list:
    """البحث عن ملفات .env جاهزة في المجلد الحالي ومجلد المشروع."""
    candidates: list = []
    bases = [Path.cwd()]
    if project_root and project_root.resolve() not in [b.resolve() for b in bases]:
        bases.append(project_root)
    for base in bases:
        try:
            for p in sorted(base.iterdir()):
                if p.is_file() and p.name.endswith(".env"):
                    if p not in candidates:
                        candidates.append(p)
            dot = base / ".env"
            if dot.is_file() and dot not in candidates:
                candidates.append(dot)
        except OSError:
            continue
    return candidates
