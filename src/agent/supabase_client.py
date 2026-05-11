#!/usr/bin/env python3
"""
Supabase Client - طبقة الاتصال بقاعدة بيانات Supabase
مع دعم Fallback للتخزين المحلي وإعادة المزامنة التلقائية
"""
import os
import json
import time
import asyncio
import logging
import threading
from typing import Optional, Any, Dict, List, Callable
from pathlib import Path
from datetime import datetime

from .config import get_project_root

# Load environment variables early to avoid race conditions with top-level variable reads
import os
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(get_project_root(), ".env"), override=True)

logger = logging.getLogger(__name__)

class SupabaseInfrastructureError(Exception):
    """استثناء يرفع عند وجود خطأ في البنية التحتية لـ Supabase (مثل 502 Bad Gateway)"""
    def __init__(self, message, code=None, details=None):
        super().__init__(message)
        self.code = code
        self.details = details

# متغيرات البيئة
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_KEY") or "").strip()
_SUPABASE_ENABLED_BY_ENV = os.environ.get("USE_SUPABASE", "true").lower() == "true"
USE_SUPABASE = _SUPABASE_ENABLED_BY_ENV and bool(SUPABASE_URL) and bool(SUPABASE_KEY)

if _SUPABASE_ENABLED_BY_ENV and not SUPABASE_URL:
    logger.warning("⚠️ USE_SUPABASE is enabled but SUPABASE_URL is missing. Falling back to local-only mode.")
elif _SUPABASE_ENABLED_BY_ENV and not SUPABASE_KEY:
    logger.warning("⚠️ USE_SUPABASE is enabled but SUPABASE_KEY is missing. Falling back to local-only mode.")

# مسار التخزين المحلي
LOCAL_SYNC_QUEUE_PATH = Path(get_project_root()) / ".data" / "supabase_sync_queue.json"
LOCAL_SYNC_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)

# قفل للعمليات المتزامنة
_client_lock = threading.RLock()
_sync_lock = threading.RLock()


_DEFAULT_ON_CONFLICT_BY_TABLE: Dict[str, str] = {
    "openrouter_models": "model_name",
    "published_videos": "channel_id,video_id",
    "youtube_api_keys": "key_id",
}

_REQUIRED_FIELDS_BY_TABLE: Dict[str, List[str]] = {
    "auto_mod_sources": ["id", "instance_id", "channel_id", "source_url"],
    "auto_mod_schedule": ["id", "instance_id", "channel_id", "content_type"],
    "auto_mod_processed": ["id", "instance_id", "source_video_id", "channel_id"],
    "youtube_api_keys": ["key_id", "api_key"],
}

_REPAIR_LOOKUPS_BY_TABLE: Dict[str, List[List[str]]] = {
    "auto_mod_sources": [["id"]],
    "auto_mod_schedule": [["id"]],
    "auto_mod_processed": [["id"], ["source_video_id", "channel_id"]],
    "youtube_api_keys": [["key_id"]],
}


class _DropQueuedOperation(Exception):
    """Raised when an invalid queued operation cannot be repaired safely."""


def _sanitize_table_payload(table: str, data: Optional[Dict]) -> Dict:
    """Sanitize payloads for known table/schema mismatches before remote sync."""
    payload = dict(data or {})

    return payload


def _is_blank_required_value(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _missing_required_fields(table: str, data: Optional[Dict]) -> List[str]:
    payload = dict(data or {})
    required = _REQUIRED_FIELDS_BY_TABLE.get(table, [])
    return [field for field in required if _is_blank_required_value(payload.get(field))]


def _select_one_direct(client, table: str, filters: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not client or not filters:
        return None
    try:
        query = client.table(table).select("*")
        for key, value in filters.items():
            query = query.eq(key, value)
        response = query.limit(1).execute()
        data = getattr(response, "data", None)
        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.debug(f"تعذر تحميل سجل موجود من {table} لإصلاح الـ upsert: {e}")
    return None


def _load_local_youtube_api_key(key_id: str) -> Optional[Dict[str, Any]]:
    if not key_id:
        return None
    path = Path(get_project_root()) / ".data" / "youtube_api_keys.json"
    try:
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            rows = json.load(f)
        if not isinstance(rows, list):
            return None
        for row in rows:
            if isinstance(row, dict) and row.get("key_id") == key_id:
                return dict(row)
    except Exception as e:
        logger.debug(f"تعذر تحميل youtube_api_keys المحلي لإصلاح المفتاح {key_id}: {e}")
    return None


def _repair_upsert_payload(client, table: str, data: Optional[Dict], key_field: str = "id", *, drop_if_unrepairable: bool = False) -> Dict:
    payload = _sanitize_table_payload(table, data)
    if not payload:
        return payload

    existing = None
    for lookup_fields in _REPAIR_LOOKUPS_BY_TABLE.get(table, [[key_field]]):
        filters = {}
        for field in lookup_fields:
            value = payload.get(field)
            if _is_blank_required_value(value):
                filters = {}
                break
            filters[field] = value
        if filters:
            existing = _select_one_direct(client, table, filters)
            if existing:
                break

    repaired = dict(existing or {})
    repaired.update(payload)

    if table == "youtube_api_keys" and _is_blank_required_value(repaired.get("api_key")):
        local_key = _load_local_youtube_api_key(str(repaired.get("key_id") or ""))
        if local_key:
            merged = dict(local_key)
            merged.update(repaired)
            repaired = merged

    repaired = _sanitize_table_payload(table, repaired)
    missing = _missing_required_fields(table, repaired)
    if missing and drop_if_unrepairable:
        raise _DropQueuedOperation(
            f"missing required fields after repair: {', '.join(missing)}"
        )
    return repaired

# عميل Supabase المُخَزّن
_supabase_client = None
_last_connection_check = 0
_connection_check_interval = 30  # ثانية


def _healthcheck_interval_sec() -> float:
    raw = (os.environ.get("SUPABASE_HEALTHCHECK_INTERVAL_SEC") or "").strip()
    try:
        val = float(raw) if raw else 60.0
    except Exception:
        val = 60.0
    return max(5.0, val)
_is_online = True
_sync_paused = False
_consecutive_failures = 0
_CIRCUIT_BREAKER_THRESHOLD = 5   # فشل متتالي = فتح الدائرة
_CIRCUIT_BREAKER_COOLDOWN = 300  # 5 دقائق وضع offline
_circuit_open_until = 0.0
_background_sync_lock = threading.RLock()
_background_sync_task = None
_background_sync_thread = None
_background_sync_interval = None


def _get_supabase():
    """الحصول على عميل Supabase (lazy loading)"""
    global _supabase_client
    
    if not USE_SUPABASE:
        return None
    
    with _client_lock:
        if _supabase_client is None:
            try:
                from supabase import create_client, Client
                _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
                logger.info("✅ تم الاتصال بـ Supabase بنجاح")
            except Exception as e:
                logger.error(f"❌ فشل الاتصال بـ Supabase: {e}")
                return None
        return _supabase_client


def is_online() -> bool:
    """التحقق من اتصال الإنترنت وSupabase مع circuit breaker"""
    global _is_online, _last_connection_check, _consecutive_failures, _circuit_open_until

    if _sync_paused:
        return False
    
    now = time.time()
    
    # Circuit breaker: إذا كانت الدائرة مفتوحة، لا نحاول
    if now < _circuit_open_until:
        return False
    
    # تقليل تكرار الفحص
    if now - _last_connection_check < _healthcheck_interval_sec():
        return _is_online
    
    _last_connection_check = now
    
    try:
        client = _get_supabase()
        if client is None:
            _is_online = False
            _consecutive_failures += 1
            _check_circuit_breaker()
            return False
        
        # اختبار بسيط للاتصال
        client.table("bot_state").select("id").limit(1).execute()
        _is_online = True
        _consecutive_failures = 0  # نجاح = تصفير
        return True
    except Exception as e:
        logger.warning(f"⚠️ الاتصال بـ Supabase غير متاح: {e}")
        _is_online = False
        _consecutive_failures += 1
        _check_circuit_breaker()
        return False


def _check_circuit_breaker():
    """فحص وتفعيل circuit breaker عند الفشل المتكرر"""
    global _circuit_open_until, _consecutive_failures
    if _consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
        _circuit_open_until = time.time() + _CIRCUIT_BREAKER_COOLDOWN
        logger.warning(
            f"🔌 Circuit breaker OPEN: {_consecutive_failures} failures. "
            f"Offline for {_CIRCUIT_BREAKER_COOLDOWN}s"
        )
        _consecutive_failures = 0  # تصفير بعد فتح الدائرة


def _is_transient_supabase_error(exc: Exception) -> bool:
    msg = str(exc or "").lower()
    transient_markers = (
        "disconnect",
        "timeout",
        "timed out",
        "network",
        "closed",
        "10054",
        "temporarily unavailable",
        "resource temporarily unavailable",
        "errno 11",
        "[errno 11]",
        "eagain",
    )
    return any(marker in msg for marker in transient_markers)


def reset_connection():
    """إعادة تعيين الاتصال"""
    global _supabase_client, _is_online, _last_connection_check
    with _client_lock:
        _supabase_client = None
        _is_online = True
        _last_connection_check = 0


def pause_supabase_sync():
    """إيقاف المزامنة وفحوصات الاتصال مؤقتاً"""
    global _sync_paused
    _sync_paused = True


def resume_supabase_sync():
    """استئناف المزامنة وفحوصات الاتصال"""
    global _sync_paused
    _sync_paused = False


# ========== قائمة المزامنة المحلية ==========

def _load_sync_queue() -> List[Dict]:
    """تحميل قائمة العمليات المعلقة للمزامنة"""
    try:
        if LOCAL_SYNC_QUEUE_PATH.exists():
            with open(LOCAL_SYNC_QUEUE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"خطأ في تحميل قائمة المزامنة: {e}")
    return []


def _save_sync_queue(queue: List[Dict]):
    """حفظ قائمة العمليات المعلقة للمزامنة"""
    try:
        with open(LOCAL_SYNC_QUEUE_PATH, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"خطأ في حفظ قائمة المزامنة: {e}")


def queue_sync_operation(table: str, operation: str, data: Dict, key_field: str = "id", on_conflict: Optional[str] = None):
    """إضافة عملية للمزامنة لاحقاً"""
    with _sync_lock:
        queue = _load_sync_queue()
        sanitized_data = _sanitize_table_payload(table, data)
        queue.append({
            "table": table,
            "operation": operation,  # "upsert", "insert", "delete"
            "data": sanitized_data,
            "key_field": key_field,
            "on_conflict": on_conflict,
            "timestamp": time.time(),
            "retries": 0
        })
        _save_sync_queue(queue)
        logger.info(f"📝 تمت إضافة عملية {operation} على {table} لقائمة المزامنة")


async def sync_pending_operations():
    """مزامنة جميع العمليات المعلقة مع Supabase"""
    # إيقاف المزامنة تماماً أثناء معالجة الفيديو
    if _sync_paused:
        logger.debug("⏸️ المزامنة متوقفة مؤقتاً (معالجة فيديو)")
        return False
    
    if not is_online():
        logger.info("⏳ لا يوجد اتصال، المزامنة مؤجلة")
        return False
    
    with _sync_lock:
        queue = _load_sync_queue()
        if not queue:
            return True
        
        logger.info(f"🔄 مزامنة {len(queue)} عملية معلقة...")
        
        client = _get_supabase()
        if not client:
            return False
        
        failed = []
        success_count = 0
        dropped_count = 0
        
        for op in queue:
            try:
                table = op["table"]
                operation = op["operation"]
                data = _sanitize_table_payload(table, op.get("data"))
                op["data"] = data
                on_conflict = op.get("on_conflict") or _DEFAULT_ON_CONFLICT_BY_TABLE.get(table)
                
                if operation == "upsert":
                    try:
                        repaired = _repair_upsert_payload(
                            client,
                            table,
                            data,
                            op.get("key_field", "id"),
                            drop_if_unrepairable=True,
                        )
                        if repaired != data:
                            logger.info(f"🩹 تم ترميم payload قديم للطابور: {table}")
                            data = repaired
                            op["data"] = repaired
                    except _DropQueuedOperation as drop_e:
                        dropped_count += 1
                        logger.warning(
                            f"🧹 تم حذف عملية upsert غير صالحة من الطابور على {table}: {drop_e}"
                        )
                        continue
                    try:
                        if on_conflict:
                            client.table(table).upsert(data, on_conflict=on_conflict).execute()
                        else:
                            client.table(table).upsert(data).execute()
                    except Exception as inner_e:
                        # علاج خاص لأخطاء duplicate key التي تظهر كـ 409 عند غياب on_conflict
                        msg = str(inner_e)
                        if ("duplicate key value" in msg) or ("23505" in msg) or ("409" in msg and "Conflict" in msg):
                            fallback_conflict = _DEFAULT_ON_CONFLICT_BY_TABLE.get(table)
                            if fallback_conflict and not on_conflict:
                                client.table(table).upsert(data, on_conflict=fallback_conflict).execute()
                            else:
                                # إذا كانت البيانات موجودة أصلاً، نعتبر العملية ناجحة لتفريغ الـ queue
                                logger.warning(f"⚠️ duplicate key أثناء upsert على {table} - تم تجاهل العملية كمكررة")
                        else:
                            raise
                elif operation == "insert":
                    client.table(table).insert(data).execute()
                elif operation == "delete":
                    key_field = op.get("key_field", "id")
                    key_value = data.get(key_field)
                    if key_value:
                        client.table(table).delete().eq(key_field, key_value).execute()
                
                success_count += 1
                
            except Exception as e:
                logger.error(f"❌ فشل مزامنة {op['operation']} على {op['table']}: {e}")
                op["retries"] = op.get("retries", 0) + 1
                if op["retries"] < 5:  # أقصى 5 محاولات
                    failed.append(op)
        
        _save_sync_queue(failed)
        
        if success_count > 0:
            logger.info(f"✅ تمت مزامنة {success_count} عملية بنجاح")
        if dropped_count > 0:
            logger.warning(f"🧹 تم تنظيف {dropped_count} عملية تالفة من قائمة المزامنة")
        
        return len(failed) == 0


def start_background_sync(interval_seconds: int = 60):
    """بدء المزامنة الخلفية التلقائية"""
    if os.environ.get("SUPABASE_DISABLE_BG_SYNC") == "1":
        logger.info("⏸️ Background sync is disabled via SUPABASE_DISABLE_BG_SYNC. Skipping.")
        return
    global _background_sync_task, _background_sync_thread, _background_sync_interval

    async def _sync_loop():
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                if _sync_paused:
                    continue
                if is_online():
                    await sync_pending_operations()
            except Exception as e:
                logger.error(f"خطأ في المزامنة الخلفية: {e}")

    with _background_sync_lock:
        task_running = _background_sync_task is not None and not getattr(_background_sync_task, "done", lambda: False)()
        thread_running = _background_sync_thread is not None and getattr(_background_sync_thread, "is_alive", lambda: False)()
        if task_running or thread_running:
            logger.info(
                f"🔄 Background sync already running (interval={_background_sync_interval or interval_seconds}s); skipping duplicate start."
            )
            return

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                _background_sync_task = asyncio.create_task(_sync_loop())
                _background_sync_interval = interval_seconds
                logger.info(f"🔄 بدء المزامنة الخلفية كل {interval_seconds} ثانية")
                return
        except Exception:
            pass

        _background_sync_thread = threading.Thread(target=lambda: asyncio.run(_sync_loop()), daemon=True)
        _background_sync_thread.start()
        _background_sync_interval = interval_seconds

    logger.info(f"🔄 بدء المزامنة الخلفية كل {interval_seconds} ثانية")


# ========== عمليات CRUD الأساسية ==========

def supabase_upsert(table: str, data: Dict, key_field: str = "id", fallback_local: Callable = None, on_conflict: Optional[str] = None) -> bool:
    """إدراج أو تحديث سجل في Supabase مع fallback محلي"""
    conflict_target = on_conflict or _DEFAULT_ON_CONFLICT_BY_TABLE.get(table)
    payload = _sanitize_table_payload(table, data)

    if not USE_SUPABASE:
        if fallback_local:
            fallback_local(data)
        return True
    
    if is_online():
        for attempt in range(2):
            try:
                client = _get_supabase()
                if client:
                    payload = _repair_upsert_payload(client, table, payload, key_field)
                    query = client.table(table).upsert(payload, on_conflict=conflict_target) if conflict_target else client.table(table).upsert(payload)
                    query.execute()
                    return True
                break
            except Exception as e:
                msg = str(e).lower()
                # إذا كان الجدول غير موجود (مثل PGRST205)، لا نقوم بإضافته للطابور كي لا يحدث سبام مزامنة دائم.
                if ("pgrst205" in msg) or ("could not find the table" in msg) or ("in the schema cache" in msg):
                    logger.error(f"❌ Supabase table missing (upsert/{table}). Skipping queue for this table. [{e}]")
                    if fallback_local:
                        fallback_local(data)
                    return False
                if attempt == 0 and _is_transient_supabase_error(e):
                    logger.warning(f"⚠️ اتصال Supabase غير مستقر (upsert/{table})، إعادة محاولة... [{e}]")
                    reset_connection()
                    time.sleep(0.5)
                else:
                    logger.error(f"❌ فشل upsert في {table}: {e}")
                    break
    
    # Fallback: حفظ محلي + إضافة للمزامنة
    if fallback_local:
        fallback_local(data)
    queue_sync_operation(table, "upsert", payload, key_field, on_conflict=conflict_target)
    return True


def supabase_select(table: str, filters: Dict = None, fallback_local: Callable = None) -> Optional[List[Dict]]:
    """قراءة سجلات من Supabase مع fallback محلي"""
    if not USE_SUPABASE:
        if fallback_local:
            return fallback_local()
        return None
    
    if is_online():
        for attempt in range(2):
            try:
                client = _get_supabase()
                if client:
                    query = client.table(table).select("*")
                    if filters:
                        for key, value in filters.items():
                            query = query.eq(key, value)
                    response = query.execute()
                    return response.data
                break
            except Exception as e:
                msg = str(e).lower()
                
                # التحقق من أخطاء البنية التحتية (502, 503, 504)
                error_data = None
                if hasattr(e, 'message') and hasattr(e, 'code'):
                    error_data = {"message": e.message, "code": e.code}
                elif isinstance(e, dict) and "code" in e:
                    error_data = e
                
                if error_data and str(error_data.get("code")) in ("502", "503", "504"):
                    logger.critical(f"🚨 خطأ فادح في البنية التحتية لـ Supabase ({error_data.get('code')}): {error_data.get('message')}")
                    raise SupabaseInfrastructureError(
                        message=error_data.get("message", "Bad Gateway"),
                        code=error_data.get("code"),
                        details=error_data
                    )

                if attempt == 0 and _is_transient_supabase_error(e):
                    logger.warning(f"⚠️ اتصال Supabase غير مستقر (select/{table})، إعادة محاولة... [{e}]")
                    reset_connection()
                    time.sleep(0.5)
                else:
                    logger.error(f"❌ فشل select من {table}: {e}")
                    break
    
    # Fallback: قراءة محلية
    if fallback_local:
        return fallback_local()
    return None


def supabase_select_one(table: str, key_field: str, key_value: Any, fallback_local: Callable = None) -> Optional[Dict]:
    """قراءة سجل واحد من Supabase"""
    result = supabase_select(table, {key_field: key_value}, fallback_local)
    if result and len(result) > 0:
        return result[0]
    return None


def supabase_delete(table: str, key_field: str, key_value: Any, fallback_local: Callable = None) -> bool:
    """حذف سجل من Supabase"""
    if not USE_SUPABASE:
        if fallback_local:
            fallback_local(key_value)
        return True
    
    if is_online():
        for attempt in range(2):
            try:
                client = _get_supabase()
                if client:
                    client.table(table).delete().eq(key_field, key_value).execute()
                    return True
                break
            except Exception as e:
                if attempt == 0 and _is_transient_supabase_error(e):
                    logger.warning(f"⚠️ اتصال Supabase غير مستقر (delete/{table})، إعادة محاولة... [{e}]")
                    reset_connection()
                    time.sleep(0.5)
                else:
                    logger.error(f"❌ فشل delete من {table}: {e}")
                    break
    
    # Fallback
    if fallback_local:
        fallback_local(key_value)
    queue_sync_operation(table, "delete", {key_field: key_value}, key_field)
    return True


def supabase_insert_many(table: str, records: List[Dict]) -> bool:
    """إدراج عدة سجلات"""
    if not USE_SUPABASE or not records:
        return True
    
    if is_online():
        for attempt in range(2):
            try:
                client = _get_supabase()
                if client:
                    client.table(table).insert(records).execute()
                    return True
                break
            except Exception as e:
                if attempt == 0 and _is_transient_supabase_error(e):
                    logger.warning(f"⚠️ اتصال Supabase غير مستقر (insert_many/{table})، إعادة محاولة... [{e}]")
                    reset_connection()
                    time.sleep(0.5)
                else:
                    logger.error(f"❌ فشل insert_many في {table}: {e}")
                    break
    
    # Fallback: إضافة كل سجل للمزامنة
    for record in records:
        queue_sync_operation(table, "insert", record)
    return True


def supabase_storage_upload(bucket: str, object_path: str, file_path: str, *, content_type: Optional[str] = None, upsert: bool = True) -> Optional[str]:
    if not (bucket and object_path and file_path):
        return None
    if not USE_SUPABASE or not is_online():
        return None
    try:
        client = _get_supabase()
        if not client:
            return None
        with open(file_path, "rb") as f:
            data = f.read()
        # بعض إصدارات عميل التخزين تتطلب قيم Headers كنصوص
        file_options: Dict[str, Any] = {"upsert": "true" if bool(upsert) else "false"}
        if content_type:
            file_options["content-type"] = content_type
        client.storage.from_(bucket).upload(object_path, data, file_options)
        return object_path
    except Exception as e:
        logger.error(f"❌ فشل رفع الملف إلى Supabase Storage: {e}")
        return None


def supabase_storage_create_signed_url(bucket: str, object_path: str, *, expires_in: int = 3600) -> Optional[str]:
    if not (bucket and object_path):
        return None
    if not USE_SUPABASE or not is_online():
        return None
    try:
        client = _get_supabase()
        if not client:
            return None
        obj = str(object_path).strip().lstrip("/")
        resp = client.storage.from_(bucket).create_signed_url(obj, int(expires_in))
        if not isinstance(resp, dict):
            logger.warning(f"⚠️ Signed URL response not dict for bucket={bucket}, obj={obj[:80]}")
            return None
        url = resp.get("signedURL") or resp.get("signedUrl") or resp.get("signed_url")
        if not url:
            logger.warning(f"⚠️ Signed URL missing in response for bucket={bucket}, obj={obj[:80]} keys={list(resp.keys())[:10]}")
            return None
        if isinstance(url, str) and url.startswith("/"):
            return SUPABASE_URL.rstrip("/") + url
        return url
    except Exception as e:
        logger.error(f"❌ فشل إنشاء Signed URL: {e}")
        return None


def supabase_storage_download_to_file(bucket: str, object_path: str, dest_path: str, *, expires_in: int = 3600, timeout_sec: float = 1200.0) -> bool:
    if not (bucket and object_path and dest_path):
        return False
    obj = str(object_path).strip().lstrip("/")
    url = supabase_storage_create_signed_url(bucket, obj, expires_in=expires_in)
    try:
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        if url:
            import httpx
            with httpx.stream("GET", url, timeout=timeout_sec, follow_redirects=True) as r:
                r.raise_for_status()
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_bytes():
                        if chunk:
                            f.write(chunk)
            return True

        client = _get_supabase()
        if not client:
            logger.warning(f"⚠️ Storage download fallback: no client (bucket={bucket}, obj={obj[:80]})")
            return False
        try:
            data = client.storage.from_(bucket).download(obj)
            if data is None:
                return False
            if hasattr(data, "read"):
                content = data.read()
            else:
                content = data
            if isinstance(content, str):
                content = content.encode("utf-8", errors="ignore")
            if not isinstance(content, (bytes, bytearray)):
                logger.warning(f"⚠️ Storage download fallback returned non-bytes (type={type(content)})")
                return False
            with open(dest_path, "wb") as f:
                f.write(content)
            return True
        except Exception as inner:
            logger.error(f"❌ Storage download fallback failed: {inner}")
            return False
    except Exception as e:
        logger.error(f"❌ فشل تنزيل الملف من Supabase Storage: {e}")
        return False


def supabase_storage_delete(bucket: str, object_path: str) -> bool:
    """حذف ملف من Supabase Storage"""
    if not (bucket and object_path):
        return False
    if not USE_SUPABASE or not is_online():
        return False
    try:
        client = _get_supabase()
        if not client:
            return False
        obj = str(object_path).strip().lstrip("/")
        client.storage.from_(bucket).remove([obj])
        return True
    except Exception as e:
        logger.error(f"❌ فشل حذف الملف من Supabase Storage: {e}")
        return False


# ========== اختبار الاتصال ==========

def test_connection() -> Dict[str, Any]:
    """اختبار الاتصال بـ Supabase"""
    result = {
        "use_supabase": USE_SUPABASE,
        "url": SUPABASE_URL,
        "connected": False,
        "error": None,
        "tables": []
    }
    
    if not USE_SUPABASE:
        result["error"] = "Supabase disabled"
        return result
    
    try:
        client = _get_supabase()
        if client:
            # اختبار قراءة من جدول
            response = client.table("bot_state").select("id").limit(1).execute()
            result["connected"] = True
            result["tables"].append("bot_state")
            
            # اختبار جداول أخرى
            for table in ["publish_channels", "published_videos", "channel_configs"]:
                try:
                    client.table(table).select("*").limit(1).execute()
                    result["tables"].append(table)
                except:
                    pass
    except Exception as e:
        result["error"] = str(e)
    
    return result


if __name__ == "__main__":
    # اختبار الاتصال
    print("🔍 اختبار الاتصال بـ Supabase...")
    result = test_connection()
    print(f"النتيجة: {json.dumps(result, ensure_ascii=False, indent=2)}")
