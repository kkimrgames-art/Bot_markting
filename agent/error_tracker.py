"""
ErrorTracker — يتتبع أنماط الأخطاء ويتخذ إجراءات تلقائية

يحفظ سجل الأخطاء في الذاكرة ويكتشف الأنماط المتكررة
لتوجيه النظام نحو الإجراء الصحيح (retry, backoff, disable, update).
"""
import time
import logging
from typing import Dict, List, Optional, Tuple
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

# الاحتفاظ بأخطاء آخر 24 ساعة
MAX_AGE_SECONDS = 24 * 3600
MAX_ENTRIES = 500


class _ErrorEntry:
    __slots__ = ("timestamp", "component", "error_type", "message")

    def __init__(self, component: str, error_type: str, message: str):
        self.timestamp = time.time()
        self.component = component
        self.error_type = error_type
        self.message = message[:300]


class ErrorTracker:
    """Singleton لتتبع الأخطاء"""

    _instance: Optional["ErrorTracker"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._errors: deque = deque(maxlen=MAX_ENTRIES)
            cls._instance._component_counts: Dict[str, int] = defaultdict(int)
            cls._instance._consecutive_fails: Dict[str, int] = defaultdict(int)
            cls._instance._last_success: Dict[str, float] = {}
        return cls._instance

    # ---------- تسجيل ----------

    def record_error(self, component: str, error_type: str, message: str):
        """تسجيل خطأ"""
        entry = _ErrorEntry(component, error_type, message)
        self._errors.append(entry)
        self._component_counts[component] += 1
        self._consecutive_fails[component] = self._consecutive_fails.get(component, 0) + 1
        logger.debug(f"📊 Error tracked: {component}/{error_type} (consecutive: {self._consecutive_fails[component]})")

    def record_success(self, component: str):
        """تسجيل نجاح — يصفر عداد الأخطاء المتتالية"""
        self._consecutive_fails[component] = 0
        self._last_success[component] = time.time()

    # ---------- تحليل ----------

    def _prune_old(self):
        """حذف السجلات القديمة"""
        cutoff = time.time() - MAX_AGE_SECONDS
        while self._errors and self._errors[0].timestamp < cutoff:
            self._errors.popleft()

    def count_last_24h(self, component: str = None) -> int:
        """عدد الأخطاء في آخر 24 ساعة"""
        self._prune_old()
        if component:
            return sum(1 for e in self._errors if e.component == component)
        return len(self._errors)

    def count_last_hour(self, component: str = None) -> int:
        """عدد الأخطاء في آخر ساعة"""
        cutoff = time.time() - 3600
        if component:
            return sum(1 for e in self._errors if e.component == component and e.timestamp > cutoff)
        return sum(1 for e in self._errors if e.timestamp > cutoff)

    def consecutive_fails(self, component: str) -> int:
        """عدد الأخطاء المتتالية بدون نجاح"""
        return self._consecutive_fails.get(component, 0)

    def get_pattern(self, component: str) -> str:
        """
        تحليل نمط الأخطاء لمكوّن معين.
        Returns: "normal", "degraded", "failing", "dead"
        """
        consecutive = self.consecutive_fails(component)
        last_hour = self.count_last_hour(component)

        if consecutive >= 10 or last_hour >= 20:
            return "dead"
        if consecutive >= 5 or last_hour >= 10:
            return "failing"
        if consecutive >= 2 or last_hour >= 5:
            return "degraded"
        return "normal"

    def suggest_action(self, component: str) -> str:
        """
        اقتراح إجراء بناءً على نمط الأخطاء.
        Returns: "continue", "retry", "backoff", "disable", "update"
        """
        pattern = self.get_pattern(component)
        consecutive = self.consecutive_fails(component)

        # فحص آخر نوع خطأ
        last_error_type = None
        for e in reversed(self._errors):
            if e.component == component:
                last_error_type = e.error_type
                break

        if pattern == "dead":
            if last_error_type == "auth":
                return "disable"
            if last_error_type == "quota":
                return "backoff"
            if component == "download":
                return "update"  # yt-dlp update
            return "disable"

        if pattern == "failing":
            return "backoff"

        if pattern == "degraded":
            return "retry"

        return "continue"

    # ---------- ملخص ----------

    def status_dict(self) -> Dict:
        """ملخص شامل"""
        self._prune_old()
        components = set(e.component for e in self._errors)
        result = {
            "total_errors_24h": len(self._errors),
            "total_errors_1h": self.count_last_hour(),
            "components": {},
        }
        for comp in components:
            result["components"][comp] = {
                "errors_24h": self.count_last_24h(comp),
                "errors_1h": self.count_last_hour(comp),
                "consecutive_fails": self.consecutive_fails(comp),
                "pattern": self.get_pattern(comp),
                "suggested_action": self.suggest_action(comp),
            }
        return result

    def recent_errors(self, limit: int = 10) -> List[Dict]:
        """آخر الأخطاء"""
        self._prune_old()
        result = []
        for e in list(self._errors)[-limit:]:
            result.append({
                "component": e.component,
                "error_type": e.error_type,
                "message": e.message,
                "ago_seconds": round(time.time() - e.timestamp, 0),
            })
        return result


def get_error_tracker() -> ErrorTracker:
    return ErrorTracker()
