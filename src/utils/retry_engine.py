"""
RetryEngine — محرك إعادة المحاولة الموحد مع exponential backoff

يوفر واجهة واحدة لجميع العمليات التي قد تفشل:
تنزيل، رفع، استدعاء API، اتصال قاعدة بيانات، إلخ.
"""
import asyncio
import time
import random
import logging
from typing import (
    Any, Callable, Optional, Sequence, Tuple, Type, Union,
)

logger = logging.getLogger(__name__)


def classify_error(exc: Exception) -> str:
    """
    تصنيف الخطأ لتحديد الإجراء المناسب.
    Returns: "retryable", "fatal", "quota", "auth", "network", "timeout"
    """
    msg = str(exc).lower()
    exc_type = type(exc).__name__.lower()

    # Auth errors
    if any(k in msg for k in ["unauthorized", "401", "forbidden", "403", "invalid_grant", "token"]):
        if "quota" not in msg:
            return "auth"

    # Quota errors
    if any(k in msg for k in ["quota", "rate limit", "429", "too many requests"]):
        return "quota"

    # Timeout
    if any(k in msg for k in ["timeout", "timed out", "deadline"]):
        return "timeout"

    # Network errors
    if any(k in msg for k in [
        "connection", "network", "dns", "unreachable", "reset",
        "broken pipe", "eof", "ssl", "socket", "refused",
    ]):
        return "network"

    # Disk errors
    if any(k in msg for k in ["no space", "disk full", "enospc"]):
        return "fatal"

    # OOM
    if any(k in msg for k in ["memory", "oom", "killed"]):
        return "fatal"

    # Default: retryable
    return "retryable"


async def run_async(
    func: Callable,
    *args,
    max_retries: int = 3,
    base_delay: float = 5.0,
    max_delay: float = 300.0,
    jitter: bool = True,
    retryable_types: Sequence[str] = ("retryable", "network", "timeout"),
    on_retry: Optional[Callable] = None,
    operation_name: str = "operation",
    **kwargs,
) -> Any:
    """
    تشغيل دالة async مع retry ذكي.

    Args:
        func: الدالة المراد تشغيلها (async)
        max_retries: الحد الأقصى لإعادة المحاولة
        base_delay: التأخير الأساسي بالثواني
        max_delay: الحد الأقصى للتأخير
        jitter: إضافة عشوائية للتأخير
        retryable_types: أنواع الأخطاء القابلة لإعادة المحاولة
        on_retry: callback عند إعادة المحاولة (attempt, error, delay)
        operation_name: اسم العملية للـ logging
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_error = e
            error_type = classify_error(e)

            if attempt >= max_retries or error_type not in retryable_types:
                logger.error(
                    f"❌ {operation_name} failed permanently "
                    f"(attempt {attempt + 1}/{max_retries + 1}, type={error_type}): {e}"
                )
                raise

            delay = min(base_delay * (2 ** attempt), max_delay)
            if jitter:
                delay = delay * (0.5 + random.random())

            logger.warning(
                f"⚠️ {operation_name} failed (attempt {attempt + 1}/{max_retries + 1}, "
                f"type={error_type}), retrying in {delay:.1f}s: {e}"
            )

            if on_retry:
                try:
                    result = on_retry(attempt + 1, e, delay)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass

            await asyncio.sleep(delay)

    raise last_error


def run_sync(
    func: Callable,
    *args,
    max_retries: int = 3,
    base_delay: float = 5.0,
    max_delay: float = 300.0,
    jitter: bool = True,
    retryable_types: Sequence[str] = ("retryable", "network", "timeout"),
    on_retry: Optional[Callable] = None,
    operation_name: str = "operation",
    **kwargs,
) -> Any:
    """
    تشغيل دالة sync مع retry ذكي.
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_error = e
            error_type = classify_error(e)

            if attempt >= max_retries or error_type not in retryable_types:
                logger.error(
                    f"❌ {operation_name} failed permanently "
                    f"(attempt {attempt + 1}/{max_retries + 1}, type={error_type}): {e}"
                )
                raise

            delay = min(base_delay * (2 ** attempt), max_delay)
            if jitter:
                delay = delay * (0.5 + random.random())

            logger.warning(
                f"⚠️ {operation_name} failed (attempt {attempt + 1}/{max_retries + 1}, "
                f"type={error_type}), retrying in {delay:.1f}s: {e}"
            )

            if on_retry:
                try:
                    on_retry(attempt + 1, e, delay)
                except Exception:
                    pass

            time.sleep(delay)

    raise last_error
