"""
HeartbeatMonitor — يراقب نبض المهام الحيوية ويكتشف التوقف الصامت

كل مهمة حيوية (fetch loop, keep-alive, etc.) تسجل "نبضة" دورياً.
إذا توقفت النبضات لمدة أطول من الـ threshold → إرسال تنبيه.
"""
import time
import logging
from typing import Dict, List, Tuple, Optional

logger = logging.getLogger(__name__)


class HeartbeatMonitor:
    """Singleton يراقب نبض المهام الحيوية"""

    _instance: Optional["HeartbeatMonitor"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._beats: Dict[str, float] = {}
            cls._instance._thresholds: Dict[str, int] = {}
            cls._instance._alert_sent: Dict[str, bool] = {}
        return cls._instance

    # ---------- تسجيل / فحص ----------

    def register(self, name: str, max_silence_seconds: int = 600):
        """تسجيل مهمة للمراقبة مع حد أقصى للصمت"""
        self._thresholds[name] = max_silence_seconds
        self._beats[name] = time.time()
        self._alert_sent[name] = False
        logger.debug(f"💓 Heartbeat registered: {name} (threshold={max_silence_seconds}s)")

    def beat(self, name: str):
        """تسجيل نبضة — يجب استدعاؤها دورياً من المهمة"""
        self._beats[name] = time.time()
        if self._alert_sent.get(name, False):
            logger.info(f"💚 Heartbeat RECOVERED: {name}")
            self._alert_sent[name] = False

    def last_beat(self, name: str) -> Optional[float]:
        """آخر نبضة مسجلة (timestamp)"""
        return self._beats.get(name)

    def seconds_since_beat(self, name: str) -> Optional[float]:
        """الثواني منذ آخر نبضة"""
        last = self._beats.get(name)
        if last is None:
            return None
        return time.time() - last

    # ---------- فحص الصحة ----------

    def check_all(self) -> List[Tuple[str, float, int]]:
        """
        فحص جميع المهام المسجلة.
        Returns: قائمة (name, seconds_since_beat, threshold) للمهام المتوقفة
        """
        stale = []
        now = time.time()
        for name, threshold in self._thresholds.items():
            last = self._beats.get(name, 0)
            silence = now - last
            if silence > threshold:
                stale.append((name, silence, threshold))
        return stale

    def is_healthy(self, name: str) -> bool:
        """هل المهمة حية؟"""
        threshold = self._thresholds.get(name)
        if threshold is None:
            return True  # غير مسجلة = لا مراقبة
        last = self._beats.get(name, 0)
        return (time.time() - last) <= threshold

    def all_healthy(self) -> bool:
        """هل جميع المهام حية؟"""
        return len(self.check_all()) == 0

    # ---------- ملخص ----------

    def status_dict(self) -> Dict[str, dict]:
        """ملخص كامل لحالة جميع المهام"""
        now = time.time()
        result = {}
        for name, threshold in self._thresholds.items():
            last = self._beats.get(name, 0)
            silence = now - last
            result[name] = {
                "last_beat_ago_seconds": round(silence, 1),
                "threshold_seconds": threshold,
                "healthy": silence <= threshold,
                "alert_sent": self._alert_sent.get(name, False),
            }
        return result

    def mark_alert_sent(self, name: str):
        """تسجيل أنه تم إرسال تنبيه (لتجنب التكرار)"""
        self._alert_sent[name] = True


# Singleton shortcut
_monitor = HeartbeatMonitor()


def get_heartbeat_monitor() -> HeartbeatMonitor:
    return _monitor
