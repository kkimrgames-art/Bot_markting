"""
AlertSystem — نظام إشعارات ذكي للمسؤول عبر Telegram

يدعم: throttling (عدم تكرار نفس الإشعار)، أولويات، تقارير يومية.
"""
import os
import time
import asyncio
import logging
from typing import Dict, Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Throttle: لا ترسل نفس الإشعار أكثر من مرة كل N دقيقة
THROTTLE_MINUTES = int(os.getenv("ALERT_THROTTLE_MINUTES", "30"))


class AlertSystem:
    """Singleton لإرسال إشعارات ذكية"""

    _instance: Optional["AlertSystem"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._last_alert: Dict[str, float] = {}  # key → timestamp
            cls._instance._bot_app = None
            cls._instance._admin_chat_id = None
            cls._instance._alert_count = 0
        return cls._instance

    def configure(self, bot_app=None, admin_chat_id: int = None):
        """ضبط البوت و chat_id للمسؤول"""
        if bot_app is not None:
            self._bot_app = bot_app
        if admin_chat_id is not None:
            self._admin_chat_id = int(admin_chat_id)

    def _resolve_admin_chat_id(self) -> Optional[int]:
        """استنتاج chat_id للمسؤول من الإعدادات الحالية عند الحاجة."""
        if self._admin_chat_id:
            return self._admin_chat_id

        try:
            from ..agent.config import load_config

            cfg = load_config()
            admin_ids = getattr(cfg, "TELEGRAM_ALLOWED_USER_IDS", None) or []
            if admin_ids:
                self._admin_chat_id = int(admin_ids[0])
                return self._admin_chat_id

            # If config cache was initialized before admin auto-detection,
            # retry once with a forced reload to pick up updated .env values.
            cfg = load_config(force_reload=True)
            admin_ids = getattr(cfg, "TELEGRAM_ALLOWED_USER_IDS", None) or []
            if admin_ids:
                self._admin_chat_id = int(admin_ids[0])
                return self._admin_chat_id
        except Exception as e:
            logger.debug(f"AlertSystem failed to resolve admin from config: {e}")

        raw_admin_id = (os.getenv("ADMIN_CHAT_ID") or "").strip()
        if raw_admin_id:
            try:
                self._admin_chat_id = int(raw_admin_id)
                return self._admin_chat_id
            except ValueError:
                logger.warning("ADMIN_CHAT_ID is set but is not a valid integer")

        return None

    def get_bot_app(self):
        return self._bot_app

    def get_admin_chat_id(self) -> Optional[int]:
        return self._resolve_admin_chat_id()

    async def alert(self, level: str, title: str, details: str = ""):
        """
        إرسال إشعار للمسؤول.

        Args:
            level: "info", "warning", "critical"
            title: عنوان قصير
            details: تفاصيل إضافية
        """
        # Throttle check
        throttle_key = f"{level}:{title}"
        now = time.time()
        last = self._last_alert.get(throttle_key, 0)
        if now - last < THROTTLE_MINUTES * 60:
            logger.debug(f"🔕 Alert throttled: {throttle_key}")
            return False

        self._last_alert[throttle_key] = now
        self._alert_count += 1

        # بناء الرسالة
        icons = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
        icon = icons.get(level, "📢")
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        message = (
            f"{icon} *{title}*\n"
            f"🕐 `{timestamp}`\n"
        )
        if details:
            message += f"\n{details}"

        # إرسال عبر Telegram
        sent = await self._send_telegram(message)
        if not sent:
            # Fallback: log فقط
            logger.warning(f"ALERT [{level}] {title}: {details}")
        return sent

    async def _send_telegram(self, message: str) -> bool:
        """إرسال رسالة Telegram"""
        admin_chat_id = self._resolve_admin_chat_id()
        if not admin_chat_id:
            logger.info("AlertSystem skipped Telegram send because no admin chat ID is configured yet")
            return False

        if self._bot_app is not None:
            try:
                await self._bot_app.bot.send_message(
                    chat_id=admin_chat_id,
                    text=message,
                    parse_mode="Markdown",
                )
                return True
            except Exception as e:
                logger.warning(f"Telegram alert via bot app failed: {e}")

        # محاولة الإرسال عبر Bot API مباشرة (HTTP)
        try:
            token = os.getenv("TELEGRAM_BOT_TOKEN")
            if token:
                import aiohttp
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = {
                    "chat_id": admin_chat_id,
                    "text": message,
                    "parse_mode": "Markdown",
                }
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            return True
                        else:
                            body = await resp.text()
                            logger.warning(f"Telegram alert failed ({resp.status}): {body[:200]}")
        except Exception as e:
            logger.warning(f"Telegram alert error: {e}")

        return False

    async def daily_report(self):
        """تقرير يومي تلقائي"""
        try:
            from .heartbeat import get_heartbeat_monitor
            from .error_tracker import get_error_tracker
            from .disk_guard import status_dict as disk_status
            from .memory_guard import status_dict as mem_status

            hb = get_heartbeat_monitor()
            et = get_error_tracker()

            hb_status = hb.status_dict()
            err_status = et.status_dict()
            disk = disk_status()
            mem = mem_status()

            report = (
                "📊 *التقرير اليومي*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"💾 القرص: `{disk.get('free_mb', '?')}MB` حر ({disk.get('level', '?')})\n"
                f"🧠 الذاكرة: `{mem.get('rss_mb', '?')}MB` RSS ({mem.get('level', '?')})\n"
                f"❌ أخطاء 24 ساعة: `{err_status.get('total_errors_24h', 0)}`\n"
                f"❌ أخطاء ساعة: `{err_status.get('total_errors_1h', 0)}`\n"
            )

            # حالة المهام
            for name, info in hb_status.items():
                status_emoji = "✅" if info["healthy"] else "❌"
                report += f"{status_emoji} {name}: آخر نبضة `{info['last_beat_ago_seconds']:.0f}s`\n"

            # مكونات مع مشاكل
            problem_components = [
                (comp, info) for comp, info in err_status.get("components", {}).items()
                if info.get("pattern") != "normal"
            ]
            if problem_components:
                report += "\n⚠️ *مكونات مع مشاكل:*\n"
                for comp, info in problem_components:
                    report += f"  • `{comp}`: {info['pattern']} → {info['suggested_action']}\n"

            await self.alert("info", "التقرير اليومي", report)
        except Exception as e:
            logger.error(f"Daily report failed: {e}")

    def status_dict(self) -> Dict:
        """ملخص حالة AlertSystem"""
        return {
            "admin_chat_id": self._admin_chat_id,
            "total_alerts": self._alert_count,
            "throttled_keys": len(self._last_alert),
        }


def get_alert_system() -> AlertSystem:
    return AlertSystem()
