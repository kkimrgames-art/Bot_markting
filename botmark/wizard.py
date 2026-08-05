"""BotMark — معالج الإعداد الأول: اختيار ملف .env والتحقق منه وحفظه."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from botmark.storage import (
    CRITICAL_KEYS,
    find_env_candidates,
    load_config,
    mask_value,
    parse_env_file,
    resolve_project_root,
    save_config,
    validate_env,
)

TEMPLATE_ENV = """# ============================================
# BotMark — ملف إعدادات بوت الأتمتة (.env)
# املأ القيم واحفظ الملف ثم أعد تشغيل: botmark
# ============================================

# ---- تيليجرام (مطلوب) ----
# توكن البوت من @BotFather
TELEGRAM_BOT_TOKEN=
# معرّفات المستخدمين المسموح لهم (اختياري — يُضاف تلقائياً عند أول رسالة)
TELEGRAM_ALLOWED_USER_IDS=

# ---- قاعدة البيانات (Supabase) — بيانات الاتصال بقاعدة البيانات ----
SUPABASE_URL=
SUPABASE_KEY=

# ---- يوتيوب (Google OAuth) — لرفع الفيديوهات ----
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=http://localhost:8080/

# ---- كوكيز يوتيوب (اختياري لكنه يرفع نسبة نجاح التنزيل) ----
YTDLP_COOKIES_B64=
YTDLP_COOKIES_PATH=

# ---- أخرى (اختيارية) ----
INSTANCE_ID=local_pc
"""


def _safe_input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return "q"


def _pick_env_file_interactive(project_root: Path | None) -> Path | None:
    """قائمة تفاعلية لاختيار ملف .env."""
    candidates = find_env_candidates(project_root)

    print()
    if candidates:
        print("📂 وجدنا ملفات إعدادات جاهزة — اختر واحداً:")
        for i, p in enumerate(candidates, 1):
            print(f"   {i}) {p}")
        print("   p) لصق المسار الكامل لملف .env آخر")
        print("   c) إنشاء ملف .env جديد من القالب")
        print("   q) إلغاء")
    else:
        print("ℹ️  لم نجد أي ملف .env في هذا المجلد.")
        print("   p) لصق المسار الكامل لملف .env (من جهازك)")
        print("   c) إنشاء ملف .env جديد من القالب")
        print("   q) إلغاء")

    while True:
        choice = _safe_input("اختيارك > ").lower()

        if choice == "q":
            return None
        if choice == "c":
            return _create_env_from_template(project_root)
        if choice == "p":
            p = _safe_input("المسار الكامل لملف .env: ").strip().strip('"').strip("'")
            if not p:
                print("❌ لم تدخل أي مسار.")
                continue
            pth = Path(p).expanduser()
            if not pth.is_file():
                print(f"❌ الملف غير موجود: {pth}")
                continue
            return pth.resolve()

        try:
            idx = int(choice)
            if 1 <= idx <= len(candidates):
                return candidates[idx - 1].resolve()
        except ValueError:
            pass
        print("❌ اختيار غير صحيح، حاول مجدداً.")


def _create_env_from_template(project_root: Path | None) -> Path | None:
    base = project_root or Path.cwd()
    default = base / ".env"
    p = _safe_input(f"حفظ الملف في [{default}]: ") or str(default)
    pth = Path(p).expanduser()
    if pth.exists():
        overwrite = _safe_input(f"⚠️ الملف {pth} موجود. الكتابة فوقه؟ (y/n): ").lower()
        if overwrite != "y":
            print("❌ تم الإلغاء.")
            return None
    try:
        pth.parent.mkdir(parents=True, exist_ok=True)
        pth.write_text(TEMPLATE_ENV, encoding="utf-8")
        print(f"✅ تم إنشاء ملف الإعدادات: {pth}")
        print("   افتحه بأي محرر نصوص، املأ القيم، ثم أعد تشغيل: botmark")
        return pth.resolve()
    except OSError as e:
        print(f"❌ تعذر إنشاء الملف: {e}")
        return None


def _show_summary(path: Path, values: dict, missing_critical: list, missing_rec: list) -> None:
    print()
    print("📋 ملخص ملف الإعدادات:")
    print(f"   الملف: {path}")
    for key in CRITICAL_KEYS:
        status = "✅" if str(values.get(key) or "").strip() else "❌"
        print(f"   {status} {key} = {mask_value(key, str(values.get(key) or ''))}")
    if missing_rec:
        print(f"   ⚠️  مفاتيح موصى بها غير موجودة: {', '.join(missing_rec)}")
    if not missing_rec:
        print("   ✅ جميع المفاتيح الموصى بها موجودة (أو غير مطلوبة).")
    print()


def run_setup(
    env_file: str | None = None,
    project_root: str | None = None,
    allow_missing: bool = False,
    quiet: bool = False,
) -> dict | None:
    """تنفيذ معالج الإعداد وإرجاع الإعدادات المحفوظة (أو None عند الإلغاء)."""
    cfg = load_config()

    # 1) مجلد المشروع
    if project_root:
        root = Path(project_root).expanduser().resolve()
    else:
        root = resolve_project_root(cfg)

    if root is None:
        root = None
        if not quiet:
            p = _safe_input("لم نستطع تحديد مجلد المشروع تلقائياً.\nالمسار الكامل لمجلد المشروع (يحتوي main.py): ")
            if not p or p.lower() == "q":
                return None
            cand = Path(p).expanduser()
            if not (cand / "main.py").is_file():
                print(f"❌ لا يوجد main.py في: {cand}")
                return None
            root = cand.resolve()
    if root is None and quiet:
        return None

    # 2) الوضع الصامت مع تحديد المشروع فقط (يستخدمه المثبّت): نحفظ الموقع فقط
    #    وسيُطلب ملف .env عند أول تشغيل فعلي للأمر botmark
    if quiet and not env_file:
        saved = dict(cfg)
        saved["project_root"] = str(root)
        save_config(saved)
        return saved

    # 3) ملف الإعدادات .env
    chosen: Path | None = None
    if env_file:
        pth = Path(env_file).expanduser()
        if not pth.is_file():
            print(f"❌ ملف الإعدادات غير موجود: {pth}")
            return None
        chosen = pth.resolve()
    elif not quiet:
        chosen = _pick_env_file_interactive(root)
        if chosen is None:
            return None
    else:
        return None

    # 4) التحقق
    values = parse_env_file(chosen)
    missing_critical, missing_rec = validate_env(values)
    if not quiet:
        _show_summary(chosen, values, missing_critical, missing_rec)

    if missing_critical and not allow_missing:
        print("❌ ملف الإعدادات ناقص مفاتيح أساسية لا يمكن للبوت العمل بدونها:")
        for k in missing_critical:
            print(f"   • {k}")
        if quiet:
            return None
        force = _safe_input("هل تريد المتابعة رغم ذلك؟ (y/n): ").lower()
        if force != "y":
            return None

    # 5) الحفظ
    saved = dict(cfg)
    saved["env_file"] = str(chosen)
    saved["project_root"] = str(root)
    saved["installed_at"] = saved.get("installed_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_config(saved)

    if not quiet:
        print("🎉 تم حفظ الإعداد بنجاح في: " + str(Path.home() / ".botmark" / "config.json"))
        print("   شغّل البوت الآن بالأمر:  botmark")
    return saved
