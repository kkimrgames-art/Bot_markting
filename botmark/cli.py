"""BotMark — واجهة الأوامر.

الأوامر:
    botmark                 تشغيل البوت (يطلب اختيار ملف .env عند أول تشغيل)
    botmark setup           إعادة اختيار ملف .env / مجلد المشروع
    botmark doctor          فحص جاهزية الجهاز قبل التشغيل
    botmark env             عرض ملخص ملف الإعدادات الحالي
    botmark logs            عرض سجل تشغيل البوت
    botmark status          حالة البوت في الخلفية
    botmark stop            إيقاف البوت الذي يعمل في الخلفية
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from botmark import __version__
from botmark.storage import (
    log_file,
    load_config,
    mask_value,
    parse_env_file,
    pid_file,
    resolve_project_root,
    save_config,
    validate_env,
)
from botmark.wizard import run_setup

BANNER = r"""
  ____        _   __    __           _
 |  _ \      | | / /   |  \/  | __ _ _ __ | | __
 | |_) | ___ | |/ / _  | |\/| |/ _` | '_ \| |/ /
 |  _ < / _ \| |\ \| |_| |  | | (_| | | | |   <
 |_| \_\\___/|_| \_\\__/|_|  |_|\__,_|_| |_|_|\_\
"""


def _utf8_stdio() -> None:
    """ضمان دعم العربية في الطرفية (خاصة Windows)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _print_banner() -> None:
    print(BANNER)
    print(f"   BotMark v{__version__} — مشغّل بوت الأتمتة المحلي")
    print("   -------------------------------------------------")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


# ==================== start ====================

def _spawn_bot(root: Path, env: dict, log_path: Path, background: bool) -> int:
    cmd = [sys.executable, "main.py"]
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if background:
        with open(log_path, "a", encoding="utf-8") as fh:
            proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=env,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        pid_file().write_text(str(proc.pid), encoding="utf-8")
        print(f"🟢 البوت يعمل في الخلفية (PID {proc.pid}).")
        print(f"   السجل:  botmark logs")
        print(f"   الحالة: botmark status")
        print(f"   إيقاف:  botmark stop")
        return 0

    # واجهة أمامية: نعرض السجل ونحفظه في نفس الوقت
    print("🚀 جارٍ تشغيل البوت... (اضغط Ctrl+C للإيقاف)")
    print(f"   📄 السجل يُحفظ أيضاً في: {log_path}\n")
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )
            assert proc.stdout is not None
            try:
                for raw in iter(proc.stdout.readline, b""):
                    try:
                        line = raw.decode("utf-8", errors="replace")
                    except Exception:
                        line = raw.decode(errors="replace")
                    line = line.rstrip("\n")
                    if line:
                        print(line, flush=True)
                        fh.write(line + "\n")
                        fh.flush()
            finally:
                proc.stdout.close()
        return proc.wait()
    except KeyboardInterrupt:
        print("\n🛑 جارٍ إيقاف البوت بأمان...")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass
        return 0


def cmd_start(args) -> int:
    cfg = load_config()

    if args.env_file:
        pth = Path(args.env_file).expanduser().resolve()
        if not pth.is_file():
            print(f"❌ ملف الإعدادات غير موجود: {pth}")
            return 1
        cfg["env_file"] = str(pth)
    if args.project_root:
        cfg["project_root"] = str(Path(args.project_root).expanduser().resolve())

    # أول تشغيل → معالج اختيار ملف .env
    if not cfg.get("env_file") or not Path(cfg["env_file"]).is_file():
        _print_banner()
        print("\n👋 يبدو أن هذه أول مرة تشغّل فيها البوت على هذا الجهاز.")
        print("   سنحتاج إلى ملف .env يحتوي بيانات الاتصال (توكن البوت + قاعدة البيانات).")
        saved = run_setup(
            env_file=args.env_file,
            project_root=args.project_root,
            allow_missing=args.allow_missing,
        )
        if not saved:
            print("❌ أُلغيت العملية — لم يتم تشغيل البوت.")
            return 1
        cfg = saved

    root = resolve_project_root(cfg)
    if root is None:
        print("❌ تعذر تحديد مجلد المشروع (الذي يحتوي main.py).")
        print("   حدّده يدوياً:  botmark setup --project-root <المسار>")
        return 1

    env_path = Path(cfg["env_file"]).expanduser().resolve()
    if not env_path.is_file():
        print(f"❌ ملف الإعدادات المحفوظ غير موجود: {env_path}")
        print("   اختر ملفاً جديداً:  botmark setup")
        return 1

    values = parse_env_file(env_path)
    missing_critical, missing_rec = validate_env(values)
    if missing_critical and not args.allow_missing:
        _print_banner()
        print("\n❌ ملف الإعدادات ناقص مفاتيح أساسية:")
        for k in missing_critical:
            print(f"   • {k}")
        print("\n   املأها في ملفك ثم أعد المحاولة، أو اختر ملفاً آخر:")
        print("   botmark setup")
        return 1

    _print_banner()
    print(f"   الملف:     {env_path}")
    print(f"   المشروع:   {root}")
    print(f"   التوكن:    {mask_value('TELEGRAM_BOT_TOKEN', str(values.get('TELEGRAM_BOT_TOKEN') or ''))}")
    if missing_rec:
        print(f"   ⚠️  مفاتيح موصى بها ناقصة: {', '.join(missing_rec)}")
    print()

    child_env = dict(os.environ)
    for k, v in values.items():
        if v is not None:
            child_env[k] = v
    child_env["BOTMARK_ENV_FILE"] = str(env_path)
    child_env["PYTHONPATH"] = str(root)
    child_env.setdefault("PYTHONUNBUFFERED", "1")

    return _spawn_bot(root, child_env, log_file(), args.background)


# ==================== setup ====================

def cmd_setup(args) -> int:
    saved = run_setup(
        env_file=args.env_file,
        project_root=args.project_root,
        allow_missing=args.allow_missing,
        quiet=args.quiet,
    )
    return 0 if saved else 1


# ==================== doctor ====================

def cmd_doctor(args) -> int:
    checks: list[tuple[str, str, bool]] = []

    ver = sys.version_info
    checks.append(("بايثون", f"{ver.major}.{ver.minor}.{ver.micro}", ver >= (3, 9)))

    ffmpeg = shutil.which("ffmpeg")
    checks.append(
        ("ffmpeg", str(ffmpeg) if ffmpeg else "غير مثبت (سيثبّته البوت تلقائياً عند الحاجة)", bool(ffmpeg))
    )

    deps_ok = True
    dep_names = []
    for mod in ("telegram", "yt_dlp", "dotenv", "supabase"):
        try:
            __import__(mod)
            dep_names.append(f"✓ {mod}")
        except Exception:
            dep_names.append(f"✗ {mod}")
            deps_ok = False
    checks.append(("المتطلبات", ", ".join(dep_names), deps_ok))

    cfg = load_config()
    root = resolve_project_root(cfg)
    main_ok = root is not None and (root / "main.py").is_file()
    checks.append(("مجلد المشروع", str(root) if root else "غير محدد — botmark setup --project-root <path>", main_ok))

    env_path = Path(cfg.get("env_file") or "").expanduser() if cfg.get("env_file") else None
    if env_path is not None and env_path.is_file():
        values = parse_env_file(env_path)
        mc, mr = validate_env(values)
        status = f"{env_path}"
        if mc:
            status += f" — ناقص أساسي: {', '.join(mc)}"
        elif mr:
            status += f" — ناقص موصى به: {', '.join(mr)}"
        else:
            status += " — مكتمل ✅"
        checks.append(("ملف .env", status, not mc))
    else:
        checks.append(("ملف .env", "لم يُحدد بعد — شغّل: botmark setup", False))

    print(f"🩺 BotMark Doctor v{__version__}")
    print("   " + "-" * 60)
    all_ok = True
    for name, detail, ok in checks:
        mark = "✅" if ok else ("⚠️" if "موصى" in detail or "غير مثبت" in detail else "❌")
        if not ok and not ("موصى" in detail or "غير مثبت" in detail):
            all_ok = False
        print(f"   {mark} {name}: {detail}")
    print("   " + "-" * 60)
    if all_ok:
        print("   🎉 الجهاز جاهز — شغّل:  botmark")
        return 0
    print("   عالج الملاحظات أعلاه ثم أعد الفحص:  botmark doctor")
    return 1


# ==================== env ====================

def cmd_env(args) -> int:
    cfg = load_config()
    env_path = Path(cfg.get("env_file") or "").expanduser() if cfg.get("env_file") else None
    if env_path is None or not env_path.is_file():
        print("❌ لا يوجد ملف إعدادات محفوظ بعد. شغّل:  botmark setup")
        return 1

    values = parse_env_file(env_path)
    mc, mr = validate_env(values)
    print(f"📋 ملف الإعدادات: {env_path}")
    print("   " + "-" * 60)
    keys = sorted(set(values) | {"TELEGRAM_BOT_TOKEN", "SUPABASE_URL", "SUPABASE_KEY"})
    for k in keys:
        mark = "✅" if str(values.get(k) or "").strip() else "❌"
        print(f"   {mark} {k} = {mask_value(k, str(values.get(k) or ''))}")
    print("   " + "-" * 60)
    if mc:
        print(f"   ⚠️  أساسية ناقصة: {', '.join(mc)}")
    if mr:
        print(f"   ℹ️  موصى بها ناقصة: {', '.join(mr)}")
    return 0 if not mc else 1


# ==================== logs / status / stop ====================

def cmd_logs(args) -> int:
    p = log_file()
    if not p.exists():
        print("لا يوجد سجل بعد — شغّل البوت أولاً.")
        return 0
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = max(1, args.tail)
    for line in lines[-tail:]:
        print(line)
    return 0


def cmd_status(args) -> int:
    pid = 0
    if pid_file().exists():
        try:
            pid = int(pid_file().read_text(encoding="utf-8").strip() or "0")
        except Exception:
            pid = 0
    if pid and _pid_alive(pid):
        print(f"🟢 البوت يعمل في الخلفية (PID {pid}).")
        print("   أوقفه بـ:  botmark stop")
        print("   شاهد السجل بـ:  botmark logs")
        return 0
    if pid_file().exists():
        try:
            pid_file().unlink()
        except OSError:
            pass
    print("⚪ البوت ليس قيد التشغيل حالياً.")
    print("   شغّله بـ:  botmark")
    return 0


def cmd_stop(args) -> int:
    pid = 0
    if pid_file().exists():
        try:
            pid = int(pid_file().read_text(encoding="utf-8").strip() or "0")
        except Exception:
            pid = 0
    if not pid or not _pid_alive(pid):
        try:
            pid_file().unlink(missing_ok=True)
        except OSError:
            pass
        print("⚪ لا يوجد بوت يعمل في الخلفية.")
        return 0

    print(f"🛑 جارٍ إيقاف البوت (PID {pid})...")
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, signal.SIGINT)
            deadline = time.time() + 15
            while time.time() < deadline and _pid_alive(pid):
                time.sleep(0.5)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
    except Exception as e:
        print(f"⚠️ تعذر إيقاف العملية: {e}")
    try:
        pid_file().unlink(missing_ok=True)
    except OSError:
        pass
    print("✅ تم إيقاف البوت.")
    return 0


# ==================== main ====================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="botmark",
        description="BotMark — شغّل بوت الأتمتة (نشر الفيديوهات) محلياً على جهازك.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="store_true", help="عرض الإصدار والخروج")

    sub = parser.add_subparsers(dest="command", metavar="أمر")

    p_start = sub.add_parser("start", help="تشغيل البوت (الأمر الافتراضي)")
    p_start.add_argument("--env-file", help="مسار ملف .env (بدون معالج تفاعلي)")
    p_start.add_argument("--project-root", help="مسار مجلد المشروع الذي يحتوي main.py")
    p_start.add_argument("--allow-missing", action="store_true", help="السماح بالتشغيل رغم نقص مفاتيح أساسية")
    p_start.add_argument("--background", "-b", action="store_true", help="تشغيل البوت في الخلفية")

    sub.add_parser("version", help="عرض رقم الإصدار")

    p_setup = sub.add_parser("setup", help="اختيار ملف .env ومجلد المشروع")
    p_setup.add_argument("--env-file", help="مسار ملف .env")
    p_setup.add_argument("--project-root", help="مسار مجلد المشروع")
    p_setup.add_argument("--allow-missing", action="store_true", help="تجاهل نقص المفاتيح الأساسية")
    p_setup.add_argument("--quiet", action="store_true", help="بدون أسئلة (للسكربتات)")

    sub.add_parser("doctor", help="فحص جاهزية الجهاز")
    sub.add_parser("env", help="عرض ملخص ملف الإعدادات")
    p_logs = sub.add_parser("logs", help="عرض سجل البوت")
    p_logs.add_argument("--tail", type=int, default=50, help="عدد الأسطر الأخيرة (الافتراضي 50)")
    sub.add_parser("status", help="حالة البوت في الخلفية")
    sub.add_parser("stop", help="إيقاف البوت الذي يعمل في الخلفية")

    return parser


def main(argv: list | None = None) -> int:
    _utf8_stdio()
    argv = list(argv) if argv is not None else sys.argv[1:]

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version or (args.command == "version"):
        print(f"botmark {__version__}")
        return 0

    command = args.command or "start"

    try:
        if command == "start":
            return cmd_start(args)
        if command == "setup":
            return cmd_setup(args)
        if command == "doctor":
            return cmd_doctor(args)
        if command == "env":
            return cmd_env(args)
        if command == "logs":
            return cmd_logs(args)
        if command == "status":
            return cmd_status(args)
        if command == "stop":
            return cmd_stop(args)
    except KeyboardInterrupt:
        print("\n👋 تم الإلغاء.")
        return 130

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
