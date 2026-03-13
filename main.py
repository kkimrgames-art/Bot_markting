#!/usr/bin/env python3
"""
Auto-Mod Bot — Standalone Entry Point (وكيل مستقل ذاتي التعافي)
Specialized for Render deployment (Web Service).

يتضمن:
- TaskSupervisor:  يراقب جميع المهام الخلفية ويعيد تشغيلها عند الفشل
- Global Exception Handler:  يمنع التوقف نهائياً عند أي خطأ غير متوقع
- Enhanced Health Endpoint:  تشخيص شامل لحالة النظام
- Graceful Shutdown:  حفظ الحالة قبل الإغلاق
"""
import os
import sys
import time
import signal
import logging
import asyncio
import json
import warnings
from typing import Dict, Optional, Callable, Coroutine, Any

from aiohttp import web
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Render Optimization: Force underlying libraries to use only 1 thread to avoid OOM
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
try:
    from telegram.warnings import PTBUserWarning
    warnings.filterwarnings("ignore", category=PTBUserWarning, message=r"If 'per_message=False'.*")
except Exception:
    pass

# ==================== Global State ====================
START_TIME = time.time()
_SINGLE_INSTANCE_LOCK_OWNED = False


def _get_single_instance_lock_path() -> str:
    """المسار المحلي لقفل منع تشغيل نسخ متعددة من نفس العملية."""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".temp")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "automodbot_instance.lock")


def _pid_exists(pid: int) -> bool:
    """التحقق من وجود PID حي بطريقة متوافقة عبر الأنظمة."""
    try:
        if pid <= 0:
            return False
        if os.name == "nt":
            import subprocess
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
            )
            return str(pid) in (res.stdout or "")
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _is_same_bot_process_running(pid: int) -> bool:
    if not _pid_exists(pid):
        return False

    current_script = os.path.normcase(os.path.abspath(__file__))
    current_name = os.path.normcase(os.path.basename(current_script))

    try:
        if os.name == "nt":
            import subprocess
            cmd = (
                "$p = Get-CimInstance Win32_Process -Filter "
                f"\"ProcessId = {pid}\"; if ($p) {{ $p.CommandLine }}"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True,
                text=True,
                timeout=5,
            )
            command_line = os.path.normcase((res.stdout or "").strip().replace('"', ""))
            if not command_line:
                return True
            is_python = "python" in command_line or "py.exe" in command_line
            return is_python and (current_script in command_line or current_name in command_line)

        proc_cmdline_path = f"/proc/{pid}/cmdline"
        if os.path.exists(proc_cmdline_path):
            with open(proc_cmdline_path, "rb") as f:
                raw = f.read().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
            command_line = os.path.normcase(raw)
            return current_script in command_line or current_name in command_line
    except Exception:
        return True

    return True


def _multi_instance_allowed() -> bool:
    raw = (os.environ.get("AUTOMODBOT_ALLOW_MULTI_INSTANCE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _acquire_single_instance_lock() -> bool:
    """منع تشغيل نسختين من AutoModBot على نفس مساحة العمل إلا عند التصريح بذلك."""
    global _SINGLE_INSTANCE_LOCK_OWNED
    _SINGLE_INSTANCE_LOCK_OWNED = False

    if _multi_instance_allowed():
        logger.warning("⚠️ Single-instance lock skipped (AUTOMODBOT_ALLOW_MULTI_INSTANCE=true).")
        return True

    try:
        lock_path = _get_single_instance_lock_path()
        pid = os.getpid()

        if os.path.exists(lock_path):
            try:
                with open(lock_path, "r", encoding="utf-8") as f:
                    raw = (f.read() or "").strip()
                old_pid = int(raw) if raw.isdigit() else 0
            except Exception:
                old_pid = 0

            if old_pid and old_pid != pid and _is_same_bot_process_running(old_pid):
                logger.error(
                    f"❌ Another AutoModBot instance is already running (PID={old_pid}). "
                    "Stop it first or set AUTOMODBOT_ALLOW_MULTI_INSTANCE=true intentionally."
                )
                return False

            try:
                os.remove(lock_path)
            except Exception:
                pass

        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(str(pid))

        _SINGLE_INSTANCE_LOCK_OWNED = True
        return True
    except Exception as e:
        logger.error(f"⚠️ Failed to acquire single-instance lock: {e}")
        return False


def _release_single_instance_lock() -> None:
    """تحرير القفل فقط إذا كانت هذه العملية هي المالكة له فعلاً."""
    global _SINGLE_INSTANCE_LOCK_OWNED
    if not _SINGLE_INSTANCE_LOCK_OWNED:
        return

    try:
        lock_path = _get_single_instance_lock_path()
        if not os.path.exists(lock_path):
            return

        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                raw = (f.read() or "").strip()
            if raw and raw != str(os.getpid()):
                return
        except Exception:
            pass

        try:
            os.remove(lock_path)
        except Exception:
            pass
    finally:
        _SINGLE_INSTANCE_LOCK_OWNED = False


# ==================== Task Supervisor ====================
class TaskSupervisor:
    """
    يراقب المهام الحيوية ويعيد تشغيلها عند الفشل.
    كل مهمة مسجلة يتم مراقبتها — إذا انتهت بخطأ يتم إعادة تشغيلها
    مع exponential backoff.
    """

    def __init__(self):
        self._tasks: Dict[str, Dict] = {}

    async def register(
        self,
        name: str,
        coro_factory: Callable[[], Coroutine],
        max_restarts: int = 100,
        base_restart_delay: float = 10.0,
        max_restart_delay: float = 600.0,
    ):
        """
        تسجيل مهمة حيوية مع إعادة تشغيل تلقائية.

        Args:
            name: اسم المهمة
            coro_factory: دالة تُعيد coroutine (لإعادة التشغيل)
            max_restarts: الحد الأقصى لإعادة التشغيل
            base_restart_delay: تأخير أساسي بالثواني
            max_restart_delay: الحد الأقصى للتأخير
        """
        entry = {
            "factory": coro_factory,
            "task": None,
            "restarts": 0,
            "max_restarts": max_restarts,
            "base_delay": base_restart_delay,
            "max_delay": max_restart_delay,
            "last_start": time.time(),
            "status": "starting",
        }
        self._tasks[name] = entry

        # تشغيل المراقب
        asyncio.create_task(self._monitor(name))
        logger.info(f"🔧 Supervised task registered: {name}")

    async def _monitor(self, name: str):
        """مراقبة مهمة وإعادة تشغيلها عند الفشل"""
        entry = self._tasks[name]

        while entry["restarts"] <= entry["max_restarts"]:
            try:
                entry["status"] = "running"
                entry["last_start"] = time.time()
                entry["task"] = asyncio.create_task(entry["factory"]())
                await entry["task"]
                # إذا انتهت بشكل طبيعي
                entry["status"] = "completed"
                logger.info(f"✅ Task '{name}' completed normally.")
                return
            except asyncio.CancelledError:
                entry["status"] = "cancelled"
                logger.info(f"🛑 Task '{name}' was cancelled.")
                return
            except Exception as e:
                entry["restarts"] += 1
                delay = min(
                    entry["base_delay"] * (2 ** min(entry["restarts"] - 1, 6)),
                    entry["max_delay"],
                )
                entry["status"] = f"restarting (#{entry['restarts']})"

                logger.error(
                    f"💥 Task '{name}' crashed (restart {entry['restarts']}/{entry['max_restarts']}): {e}",
                    exc_info=True,
                )

                # إشعار المسؤول
                try:
                    from src.agent.alert_system import get_alert_system
                    await get_alert_system().alert(
                        "warning",
                        f"مهمة '{name}' تعطلت",
                        f"إعادة تشغيل #{entry['restarts']} بعد {delay:.0f}s\n"
                        f"الخطأ: `{str(e)[:200]}`"
                    )
                except Exception:
                    pass

                await asyncio.sleep(delay)

                # إذا نجحت المهمة لأكثر من 5 دقائق قبل التعطل، نصفر العداد
                if time.time() - entry["last_start"] > 300:
                    entry["restarts"] = max(0, entry["restarts"] - 2)

        entry["status"] = "dead"
        logger.critical(f"☠️ Task '{name}' exceeded max restarts ({entry['max_restarts']}). Giving up.")

        try:
            from src.agent.alert_system import get_alert_system
            await get_alert_system().alert(
                "critical",
                f"مهمة '{name}' ماتت نهائياً!",
                f"تجاوزت الحد الأقصى لإعادة التشغيل ({entry['max_restarts']}).\n"
                "يتطلب تدخل يدوي."
            )
        except Exception:
            pass

    def status(self) -> Dict:
        """ملخص حالة جميع المهام"""
        result = {}
        for name, entry in self._tasks.items():
            result[name] = {
                "status": entry["status"],
                "restarts": entry["restarts"],
                "max_restarts": entry["max_restarts"],
                "running_since_seconds": round(time.time() - entry["last_start"], 1),
            }
        return result

    async def cancel_all(self):
        """إلغاء جميع المهام (للإغلاق النظيف)"""
        pending = []
        for name, entry in self._tasks.items():
            task = entry.get("task")
            if task and not task.done():
                task.cancel()
                pending.append((name, task))
                logger.info(f"🛑 Cancelling task: {name}")

        if not pending:
            return

        try:
            _, still_running = await asyncio.wait(
                [task for _, task in pending],
                timeout=15,
            )
        except Exception as e:
            logger.warning(f"⚠️ Failed while awaiting task cancellation: {e}")
            return

        if still_running:
            for name, task in pending:
                if task in still_running:
                    logger.warning(f"⚠️ Task did not finish cancellation in time: {name}")


supervisor = TaskSupervisor()


# ==================== Global Exception Handler ====================
def _handle_sync_exception(exc_type, exc_value, exc_tb):
    """معالج استثناءات غير معالجة (متزامن)"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.critical(f"💥 Unhandled sync exception: {exc_value}", exc_info=(exc_type, exc_value, exc_tb))


def _handle_async_exception(loop, context):
    """معالج استثناءات غير معالجة (غير متزامن)"""
    exception = context.get("exception")
    message = context.get("message", "No message")

    if exception and isinstance(exception, asyncio.CancelledError):
        return  # تجاهل CancelledError

    logger.error(f"💥 Unhandled async exception: {message}", exc_info=exception)


def setup_global_exception_handlers():
    """تثبيت معالجات الاستثناءات العالمية"""
    sys.excepthook = _handle_sync_exception
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(_handle_async_exception)
    logger.info("🛡️ Global exception handlers installed.")


# ==================== Keep-Alive ====================
async def keep_alive_pinger(url):
    """Pings the web service periodically to prevent Render sleep"""
    import aiohttp
    from src.agent.heartbeat import get_heartbeat_monitor

    hb = get_heartbeat_monitor()
    hb.register("keep_alive", max_silence_seconds=900)  # 15 min

    logger.info(f"⏰ Keep-alive pinger started for: {url}")
    consecutive_fails = 0

    while True:
        await asyncio.sleep(300)  # Ping every 5 minutes (was 10)
        hb.beat("keep_alive")

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{url}/health") as resp:
                    if resp.status == 200:
                        consecutive_fails = 0
                        logger.info(f"💓 Keep-alive OK (status={resp.status})")
                    else:
                        consecutive_fails += 1
                        logger.warning(f"💓 Keep-alive got status {resp.status}")
        except Exception as e:
            consecutive_fails += 1
            logger.warning(f"💓 Keep-alive failed ({consecutive_fails}): {e}")

        if consecutive_fails >= 5:
            logger.error("💓 Keep-alive failing repeatedly! Service may be dying.")
            try:
                from src.agent.alert_system import get_alert_system
                await get_alert_system().alert(
                    "critical",
                    "Keep-Alive فشل متكرر",
                    f"{consecutive_fails} فشل متتالي — الخدمة قد تكون في خطر!"
                )
            except Exception:
                pass
            consecutive_fails = 0  # Reset to avoid spamming


# ==================== Periodic Maintenance ====================
async def periodic_maintenance():
    """صيانة دورية: تنظيف القرص، GC، فحص الصحة"""
    from src.agent.heartbeat import get_heartbeat_monitor
    from src.agent.disk_guard import cleanup_old_files, should_allow_download
    from src.agent.memory_guard import periodic_maintenance as mem_maintenance
    from src.agent.error_tracker import get_error_tracker
    from src.agent.alert_system import get_alert_system

    hb = get_heartbeat_monitor()
    hb.register("maintenance", max_silence_seconds=7200)  # 2 hours

    alert = get_alert_system()
    last_daily_report = 0

    logger.info("🔧 Periodic maintenance loop started.")

    while True:
        await asyncio.sleep(600)  # كل 10 دقائق
        hb.beat("maintenance")

        try:
            # 1. تنظيف القرص
            cleanup_old_files()

            # 2. صيانة الذاكرة
            mem_maintenance()

            # 3. فحص heartbeats
            stale = hb.check_all()
            for name, silence, threshold in stale:
                if not hb._alert_sent.get(name, False):
                    await alert.alert(
                        "critical",
                        f"مهمة '{name}' متوقفة!",
                        f"آخر نبضة منذ `{silence:.0f}` ثانية (الحد: {threshold}s)"
                    )
                    hb.mark_alert_sent(name)

            # 4. تقرير يومي (كل 24 ساعة)
            now = time.time()
            if now - last_daily_report > 86400:
                await alert.daily_report()
                last_daily_report = now

        except Exception as e:
            logger.error(f"Maintenance error: {e}")


# ==================== Web Server ====================
from src.bot.shared_state import oauth_callback_results


async def health_check(request):
    """Health endpoint شامل"""
    try:
        from src.agent.heartbeat import get_heartbeat_monitor
        from src.agent.error_tracker import get_error_tracker
        from src.agent.disk_guard import status_dict as disk_status
        from src.agent.memory_guard import status_dict as mem_status

        hb = get_heartbeat_monitor()
        et = get_error_tracker()

        health = {
            "status": "ok" if hb.all_healthy() else "degraded",
            "uptime_seconds": round(time.time() - START_TIME, 0),
            "memory": mem_status(),
            "disk": disk_status(),
            "tasks": supervisor.status(),
            "heartbeats": hb.status_dict(),
            "errors": {
                "last_24h": et.count_last_24h(),
                "last_1h": et.count_last_hour(),
            },
        }
        return web.json_response(health)
    except Exception as e:
        return web.json_response({"status": "ok", "uptime": round(time.time() - START_TIME, 0), "error": str(e)})


async def oauth_callback_handler(request):
    """Handles Google OAuth callbacks"""
    full_url = str(request.url)
    logger.info(f"📥 Received OAuth callback: {full_url}")
    oauth_callback_results['latest'] = full_url
    return web.Response(
        text="<div style='text-align:center;font-family:sans-serif;'>"
             "<h1>✅ Authentication Successful!</h1>"
             "<p>The bot has received your code. You can return to Telegram now.</p>"
             "</div>",
        content_type="text/html"
    )


async def start_web_server():
    """Start web server with health and OAuth endpoints"""
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    app.router.add_get("/oauth2/callback", oauth_callback_handler)

    telegram_app = globals().get("_TELEGRAM_APPLICATION")
    if telegram_app is not None:
        from telegram import Update

        webhook_path = (os.environ.get("TELEGRAM_WEBHOOK_PATH") or "/telegram").strip() or "/telegram"
        if not webhook_path.startswith("/"):
            webhook_path = f"/{webhook_path}"
        secret_token = (os.environ.get("TELEGRAM_WEBHOOK_SECRET_TOKEN") or "").strip()

        update_queue = globals().get("_TELEGRAM_UPDATE_QUEUE")
        if update_queue is None:
            update_queue = asyncio.Queue(maxsize=500)
            globals()["_TELEGRAM_UPDATE_QUEUE"] = update_queue

        update_worker = globals().get("_TELEGRAM_UPDATE_WORKER")
        if update_worker is None:
            async def _update_worker():
                while True:
                    upd = await update_queue.get()
                    try:
                        await telegram_app.process_update(upd)
                    except Exception as e:
                        logger.error(f"Telegram update processing failed: {e}", exc_info=True)
                    finally:
                        update_queue.task_done()

            update_worker = asyncio.create_task(_update_worker())
            globals()["_TELEGRAM_UPDATE_WORKER"] = update_worker

        async def telegram_webhook_handler(request: web.Request):
            if secret_token:
                incoming = (request.headers.get("X-Telegram-Bot-Api-Secret-Token") or "").strip()
                if incoming != secret_token:
                    raise web.HTTPUnauthorized()

            payload = await request.json()
            update = Update.de_json(payload, telegram_app.bot)
            try:
                update_queue.put_nowait(update)
            except asyncio.QueueFull:
                logger.warning("Telegram update queue is full; dropping update.")
            return web.Response(text="OK")

        app.router.add_post(webhook_path, telegram_webhook_handler)
        logger.info(f"🔗 Telegram webhook route enabled: {webhook_path}")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌍 Web server started on port {port} with /health and /oauth2/callback")
    return site


# ==================== Graceful Shutdown ====================
_shutdown_event = asyncio.Event()


async def graceful_shutdown():
    """حفظ الحالة وإلغاء المهام قبل الإغلاق"""
    logger.info("🛑 Graceful shutdown initiated...")

    # 1. إلغاء جميع المهام المراقبة
    await supervisor.cancel_all()

    # 2. تنظيف الملفات المؤقتة
    try:
        from src.agent.disk_guard import cleanup_old_files
        cleanup_old_files(max_age_hours=0)  # حذف كل شيء
    except Exception as e:
        logger.warning(f"⚠️ Cleanup during shutdown failed: {e}")

    # 3. مزامنة Supabase
    try:
        from src.agent.supabase_client import sync_pending_operations
        await sync_pending_operations()
    except Exception as e:
        logger.warning(f"⚠️ Supabase sync during shutdown failed: {e}")

    logger.info("✅ Graceful shutdown complete.")


def run_best_effort_shutdown():
    """تشغيل shutdown أخير في loop مستقل عند الخروج من أعلى مستوى."""
    loop = None
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(graceful_shutdown())
    except Exception:
        pass
    finally:
        if loop is not None:
            try:
                loop.close()
            except Exception:
                pass


# ==================== Main ====================
async def main():
    """Start the Auto-Mod Bot with full self-recovery"""
    from src.bot.telegram_bot import build_application, run_polling_forever, run_webhook_forever
    from src.agent.config import load_config
    from src.agent.alert_system import get_alert_system
    from src.agent.supabase_storage import sync_supabase_to_local
    from src.agent.heartbeat import get_heartbeat_monitor

    logger.info("🚀 Starting Auto-Mod Bot (Autonomous Agent Mode)...")

    # -1. التحقق من وجود FFmpeg وتثبيته إذا لزم الأمر (Self-Healing)
    try:
        from install_ffmpeg import install
        install()
    except Exception as e:
        logger.error(f"Failed to run auto-FFmpeg installer: {e}")


    # 0. تثبيت معالجات الأخطاء العالمية
    setup_global_exception_handlers()

    # 1. استعادة البيانات من Supabase
    logger.info("📥 Restoring data from Supabase...")
    try:
        await sync_supabase_to_local()
    except Exception as e:
        logger.warning(f"⚠️ Supabase sync failed (continuing with local data): {e}")

    # 2. تشغيل Web Server
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        logger.error("❌ TELEGRAM_BOT_TOKEN is missing!")
        return

    telegram_app = build_application(token)
    globals()["_TELEGRAM_APPLICATION"] = telegram_app

    cfg = load_config()
    admin_ids = cfg.TELEGRAM_ALLOWED_USER_IDS or []
    get_alert_system().configure(
        bot_app=telegram_app,
        admin_chat_id=admin_ids[0] if admin_ids else None,
    )

    external_url = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    webhook_path = (os.environ.get("TELEGRAM_WEBHOOK_PATH") or "/telegram").strip() or "/telegram"
    if not webhook_path.startswith("/"):
        webhook_path = f"/{webhook_path}"

    force_polling = (os.environ.get("TELEGRAM_FORCE_POLLING") or "").strip().lower() in {"1", "true", "yes", "on"}
    use_webhook = bool(external_url) and not force_polling

    await start_web_server()

    # 3. تسجيل المهام المراقبة

    # 3a. Keep-Alive
    if external_url:
        await supervisor.register(
            "keep_alive",
            lambda: keep_alive_pinger(external_url),
            max_restarts=100,
            base_restart_delay=10,
        )

    # 3b. Auto-Fetch Loop
    from src.agent.auto_mod_fetcher import start_auto_fetch_loop
    await supervisor.register(
        "auto_fetch",
        lambda: start_auto_fetch_loop(interval_seconds=60),
        max_restarts=100,
        base_restart_delay=15,
    )

    # 3c. Periodic Maintenance
    await supervisor.register(
        "maintenance",
        periodic_maintenance,
        max_restarts=50,
        base_restart_delay=30,
    )

    # 4. تشغيل البوت (المهمة الرئيسية — تبقى في الـ foreground)
    logger.info("🤖 Starting Telegram bot...")
    if use_webhook:
        webhook_url = f"{external_url}{webhook_path}"
        secret_token = (os.environ.get("TELEGRAM_WEBHOOK_SECRET_TOKEN") or "").strip() or None
        await run_webhook_forever(telegram_app, webhook_url, secret_token=secret_token)
    else:
        await run_polling_forever(telegram_app)


if __name__ == "__main__":
    lock_acquired = False
    try:
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        lock_acquired = _acquire_single_instance_lock()
        if not lock_acquired:
            sys.exit(1)

        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user.")
        run_best_effort_shutdown()
    except Exception as e:
        logger.critical(f"💥 Fatal error: {e}", exc_info=True)
        # محاولة أخيرة لحفظ الحالة
        run_best_effort_shutdown()
    finally:
        if lock_acquired:
            _release_single_instance_lock()
