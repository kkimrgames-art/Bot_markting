import json
import time
import logging
import os
import uuid
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _use_supabase() -> bool:
    try:
        from .supabase_client import USE_SUPABASE, is_online
        return bool(USE_SUPABASE and is_online())
    except Exception:
        return False


def _supabase_select(table: str, filters: dict) -> List[dict]:
    from .supabase_client import supabase_select
    return supabase_select(table, filters) or []


def _supabase_upsert(table: str, data: dict, key_field: str, on_conflict: str = None):
    from .supabase_client import supabase_upsert
    return supabase_upsert(table, data, key_field=key_field, on_conflict=on_conflict)


def _supabase_delete(table: str, key_field: str, key_value: str):
    from .supabase_client import supabase_delete
    return supabase_delete(table, key_field, key_value)


class JobQueue:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(JobQueue, cls).__new__(cls)
        return cls._instance

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _now_ts(self) -> float:
        return time.time()

    @staticmethod
    def _busy_ttl_seconds(default_seconds: int = 21600) -> int:
        try:
            raw = (os.environ.get("JOB_QUEUE_BUSY_TTL_SECONDS", str(default_seconds)) or str(default_seconds)).strip()
            value = int(float(raw))
        except Exception:
            value = default_seconds
        return max(300, min(7 * 24 * 3600, value))

    @staticmethod
    def _parse_job_timestamp(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except Exception:
                return None
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return float(raw)
        except Exception:
            pass
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return float(dt.timestamp())
        except Exception:
            return None

    @staticmethod
    def _local_jobs_column_types(conn) -> Dict[str, str]:
        try:
            rows = conn.execute("PRAGMA table_info(jobs)").fetchall()
            result: Dict[str, str] = {}
            for row in rows or []:
                # row format: (cid, name, type, notnull, dflt_value, pk)
                if len(row) >= 3:
                    result[str(row[1]).strip().lower()] = str(row[2] or "").strip().lower()
            return result
        except Exception:
            return {}

    @staticmethod
    def _coerce_time_for_column(iso_value: str, column_type: str):
        ctype = str(column_type or "").lower()
        if any(token in ctype for token in ("int", "real", "float", "double", "numeric", "decimal")):
            try:
                dt = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                return time.time()
        return iso_value

    @staticmethod
    def _supabase_mirror_enabled() -> bool:
        raw = (os.environ.get("JOB_QUEUE_SUPABASE_MIRROR") or "").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def add_job(self, agent_id: str, task_type: str, payload: Dict[str, Any]) -> str:
        job_id = str(uuid.uuid4())
        now = self._now_iso()
        record = {
            "id": job_id,
            "agent_id": agent_id,
            "task_type": task_type,
            "payload": json.dumps(payload),
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "error_msg": None,
        }

        # Local queue is the primary execution source for reliability.
        local_job_id = self._add_job_local(record)

        # Optional Supabase mirror (disabled by default).
        if self._supabase_mirror_enabled() and _use_supabase():
            try:
                mirror_record = dict(record)
                mirror_record["id"] = str(local_job_id or job_id)
                _supabase_upsert("job_queue", mirror_record, key_field="id")
                logger.info(f"📥 Job mirrored to Supabase: {mirror_record['id']} (Agent: {agent_id})")
            except Exception as e:
                logger.error(f"Failed to mirror job to Supabase: {e}")

        return str(local_job_id or job_id)

    def _add_job_local(self, record: dict) -> Optional[str]:
        try:
            import sqlite3
            from pathlib import Path
            from .config import get_project_root
            db_path = Path(get_project_root()) / ".data" / "job_queue.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(db_path), timeout=30.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        agent_id TEXT NOT NULL,
                        task_type TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        error_msg TEXT
                    )
                """)
                column_types = self._local_jobs_column_types(conn)
                created_val = self._coerce_time_for_column(record["created_at"], column_types.get("created_at", "text"))
                updated_val = self._coerce_time_for_column(record["updated_at"], column_types.get("updated_at", "text"))

                id_type = column_types.get("id", "text")
                if "int" in id_type:
                    cursor = conn.execute(
                        "INSERT INTO jobs (agent_id, task_type, payload, status, created_at, updated_at, error_msg) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            record["agent_id"],
                            record["task_type"],
                            record["payload"],
                            record["status"],
                            created_val,
                            updated_val,
                            record["error_msg"],
                        )
                    )
                    local_job_id = str(cursor.lastrowid)
                else:
                    conn.execute(
                        "INSERT INTO jobs (id, agent_id, task_type, payload, status, created_at, updated_at, error_msg) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record["id"],
                            record["agent_id"],
                            record["task_type"],
                            record["payload"],
                            record["status"],
                            created_val,
                            updated_val,
                            record["error_msg"],
                        )
                    )
                    local_job_id = str(record["id"])
                conn.commit()
                logger.info(f"📥 Job added locally: {local_job_id}")
                return local_job_id
        except Exception as e:
            logger.error(f"Failed to add job locally: {e}")
            return None

    def get_next_job(self) -> Optional[Dict[str, Any]]:
        """
        جلب المهمة التالية مع local-first strategy.

        المحلي هو المصدر الأساسي للتنفيذ؛ وSupabase مجرد mirror اختياري.
        """
        local_job = self._get_next_job_local()
        if local_job:
            return local_job

        if _use_supabase():
            job = self._get_next_job_supabase()
            if job:
                return job
        return None

    def _get_next_job_supabase(self) -> Optional[Dict[str, Any]]:
        try:
            from .supabase_client import _get_supabase
            client = _get_supabase()
            if not client:
                return None
            rows = client.table("job_queue").select("*").eq("status", "pending").order("created_at").limit(1).execute().data
            if not rows:
                return None
            job = rows[0]
            job_id = job["id"]
            client.table("job_queue").update({
                "status": "processing",
                "updated_at": self._now_iso(),
            }).eq("id", job_id).execute()
            job["payload"] = json.loads(job["payload"]) if isinstance(job.get("payload"), str) else job.get("payload", {})
            logger.info(f"📥 Got job from Supabase: {job_id}")
            return job
        except Exception as e:
            logger.error(f"Failed to get job from Supabase: {e}")
            return None

    def _get_next_job_local(self) -> Optional[Dict[str, Any]]:
        try:
            import sqlite3
            from pathlib import Path
            from .config import get_project_root
            db_path = Path(get_project_root()) / ".data" / "job_queue.db"
            if not db_path.exists():
                return None
            with sqlite3.connect(str(db_path), timeout=30.0) as conn:
                conn.execute("BEGIN EXCLUSIVE")
                try:
                    cursor = conn.execute(
                        "SELECT id, agent_id, task_type, payload, created_at FROM jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
                    )
                    row = cursor.fetchone()
                    if row:
                        job_id, agent_id, task_type, payload_json, created_at = row
                        conn.execute(
                            "UPDATE jobs SET status = 'processing', updated_at = ? WHERE id = ?",
                            (self._now_iso(), job_id)
                        )
                        conn.commit()
                        return {
                            "id": job_id,
                            "agent_id": agent_id,
                            "task_type": task_type,
                            "payload": json.loads(payload_json),
                            "created_at": created_at,
                        }
                    else:
                        conn.rollback()
                        return None
                except Exception:
                    conn.rollback()
                    return None
        except Exception as e:
            logger.error(f"Failed to get local job: {e}")
            return None

    def _get_pending_count_local(self) -> int:
        try:
            import sqlite3
            from pathlib import Path
            from .config import get_project_root

            db_path = Path(get_project_root()) / ".data" / "job_queue.db"
            if not db_path.exists():
                return 0

            with sqlite3.connect(str(db_path), timeout=30.0) as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'")
                row = cursor.fetchone()
                return int(row[0] if row else 0)
        except Exception:
            return 0

    def _is_agent_busy_or_queued_local(self, agent_id: str) -> bool:
        try:
            import sqlite3
            from pathlib import Path
            from .config import get_project_root

            db_path = Path(get_project_root()) / ".data" / "job_queue.db"
            if not db_path.exists():
                return False

            ttl_seconds = self._busy_ttl_seconds()
            now_ts = time.time()

            with sqlite3.connect(str(db_path), timeout=30.0) as conn:
                cursor = conn.execute(
                    "SELECT id, status, created_at, updated_at FROM jobs WHERE agent_id = ? AND status IN ('pending', 'processing')",
                    (agent_id,),
                )
                rows = cursor.fetchall() or []
                if not rows:
                    return False

                stale_ids: List[str] = []
                for row in rows:
                    job_id = row[0]
                    created_at = row[2]
                    updated_at = row[3]
                    ref_ts = self._parse_job_timestamp(updated_at)
                    if ref_ts is None:
                        ref_ts = self._parse_job_timestamp(created_at)
                    if ref_ts is None:
                        # إذا تعذر تحليل الوقت، نعتبره مشغولاً لتجنب التداخل
                        return True

                    age_seconds = max(0.0, now_ts - ref_ts)
                    if age_seconds <= ttl_seconds:
                        return True
                    stale_ids.append(str(job_id))

                # سجلات قديمة جداً: لا تمنع جدولة جديدة
                if stale_ids:
                    now_iso = self._now_iso()
                    for stale_id in stale_ids:
                        try:
                            conn.execute(
                                "UPDATE jobs SET status = 'failed', error_msg = ?, updated_at = ? WHERE id = ?",
                                (f"stale busy marker auto-cleared after >{ttl_seconds}s", now_iso, stale_id),
                            )
                        except Exception:
                            pass
                    conn.commit()
                    logger.warning(
                        "⚠️ Auto-cleared %s stale busy queue rows for agent '%s'.",
                        len(stale_ids),
                        agent_id,
                    )
                return False
        except Exception:
            return False

    def complete_job(self, job_id: str):
        now = self._now_iso()
        if _use_supabase():
            try:
                from .supabase_client import _get_supabase
                client = _get_supabase()
                if client:
                    client.table("job_queue").update({
                        "status": "completed",
                        "updated_at": now,
                    }).eq("id", job_id).execute()
                    logger.info(f"✅ Job completed (Supabase): {job_id}")
            except Exception as e:
                logger.error(f"Failed to complete job on Supabase: {e}")
        self._update_job_local(job_id, "completed", now)

    def fail_job(self, job_id: str, error_msg: str):
        now = self._now_iso()
        if _use_supabase():
            try:
                from .supabase_client import _get_supabase
                client = _get_supabase()
                if client:
                    client.table("job_queue").update({
                        "status": "failed",
                        "error_msg": str(error_msg)[:500],
                        "updated_at": now,
                    }).eq("id", job_id).execute()
                    logger.error(f"❌ Job failed (Supabase): {job_id} - {error_msg}")
            except Exception as e:
                logger.error(f"Failed to fail job on Supabase: {e}")
        self._update_job_local(job_id, "failed", now, error_msg)

    def _update_job_local(self, job_id: str, status: str, now: str, error_msg: str = None):
        try:
            import sqlite3
            from pathlib import Path
            from .config import get_project_root
            db_path = Path(get_project_root()) / ".data" / "job_queue.db"
            if not db_path.exists():
                return
            with sqlite3.connect(str(db_path), timeout=30.0) as conn:
                if error_msg:
                    conn.execute(
                        "UPDATE jobs SET status = ?, error_msg = ?, updated_at = ? WHERE id = ?",
                        (status, str(error_msg)[:500], now, job_id)
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                        (status, now, job_id)
                    )
                conn.commit()
                emoji = "✅" if status == "completed" else "❌"
                logger.info(f"{emoji} Job {status} (local): {job_id}")
        except Exception as e:
            logger.error(f"Failed to update local job: {e}")

    def reset_stuck_jobs(self, timeout_seconds: int = 3600):
        if _use_supabase():
            try:
                from .supabase_client import _get_supabase
                from datetime import timedelta
                client = _get_supabase()
                if client:
                    threshold = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
                    result = client.table("job_queue").update({
                        "status": "pending",
                        "updated_at": self._now_iso(),
                    }).eq("status", "processing").lt("updated_at", threshold).execute()
                    count = len(result.data) if result.data else 0
                    if count > 0:
                        logger.warning(f"⚠️ Reset {count} stuck jobs (Supabase)")
            except Exception as e:
                logger.error(f"Failed to reset stuck jobs on Supabase: {e}")
        
        try:
            import sqlite3
            from pathlib import Path
            from .config import get_project_root
            db_path = Path(get_project_root()) / ".data" / "job_queue.db"
            if not db_path.exists():
                return
            now_ts = time.time()
            stale_ids: List[str] = []
            with sqlite3.connect(str(db_path), timeout=30.0) as conn:
                cursor = conn.execute(
                    "SELECT id, created_at, updated_at FROM jobs WHERE status = 'processing'"
                )
                rows = cursor.fetchall() or []
                for row in rows:
                    job_id, created_at, updated_at = row
                    ref_ts = self._parse_job_timestamp(updated_at)
                    if ref_ts is None:
                        ref_ts = self._parse_job_timestamp(created_at)
                    if ref_ts is None:
                        continue
                    if (now_ts - ref_ts) >= max(30, int(timeout_seconds)):
                        stale_ids.append(str(job_id))

                if stale_ids:
                    now_iso = self._now_iso()
                    for stale_id in stale_ids:
                        conn.execute(
                            "UPDATE jobs SET status = 'pending', updated_at = ?, error_msg = ? WHERE id = ?",
                            (now_iso, f"auto-reset stale processing job after >{int(timeout_seconds)}s", stale_id),
                        )
                    conn.commit()
                    logger.warning(f"⚠️ Reset {len(stale_ids)} stuck jobs (local)")
        except Exception as e:
            logger.error(f"Failed to reset local stuck jobs: {e}")

    def cleanup_completed_jobs(self, max_age_seconds: int = 86400):
        if _use_supabase():
            try:
                from .supabase_client import _get_supabase
                from datetime import timedelta
                client = _get_supabase()
                if client:
                    threshold = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
                    client.table("job_queue").delete().in_("status", ["completed", "failed"]).lt("updated_at", threshold).execute()
            except Exception as e:
                logger.error(f"Failed to cleanup Supabase jobs: {e}")
        
        try:
            import sqlite3
            from pathlib import Path
            from .config import get_project_root
            db_path = Path(get_project_root()) / ".data" / "job_queue.db"
            if not db_path.exists():
                return
            now_ts = time.time()
            delete_ids: List[str] = []
            with sqlite3.connect(str(db_path), timeout=30.0) as conn:
                cursor = conn.execute(
                    "SELECT id, created_at, updated_at FROM jobs WHERE status IN ('completed', 'failed')"
                )
                rows = cursor.fetchall() or []
                for row in rows:
                    job_id, created_at, updated_at = row
                    ref_ts = self._parse_job_timestamp(updated_at)
                    if ref_ts is None:
                        ref_ts = self._parse_job_timestamp(created_at)
                    if ref_ts is None:
                        continue
                    if (now_ts - ref_ts) >= max(60, int(max_age_seconds)):
                        delete_ids.append(str(job_id))

                if delete_ids:
                    for job_id in delete_ids:
                        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
                    conn.commit()
        except Exception as e:
            logger.error(f"Failed to cleanup local jobs: {e}")

    def get_pending_count(self) -> int:
        supabase_count = 0
        if _use_supabase():
            try:
                from .supabase_client import _get_supabase
                client = _get_supabase()
                if client:
                    result = client.table("job_queue").select("id", count="exact").eq("status", "pending").execute()
                    supabase_count = result.count or 0
            except Exception:
                supabase_count = 0

        local_count = self._get_pending_count_local()
        return int(supabase_count) + int(local_count)

    def is_agent_busy_or_queued(self, agent_id: str) -> bool:
        if self._is_agent_busy_or_queued_local(agent_id):
            return True

        supabase_busy = False
        if _use_supabase():
            try:
                from .supabase_client import _get_supabase
                client = _get_supabase()
                if client:
                    ttl_seconds = self._busy_ttl_seconds()
                    now_ts = time.time()
                    result = (
                        client.table("job_queue")
                        .select("id,created_at,updated_at")
                        .eq("agent_id", agent_id)
                        .in_("status", ["pending", "processing"])
                        .order("updated_at", desc=True)
                        .limit(20)
                        .execute()
                    )
                    rows = result.data or []
                    for row in rows:
                        ref_ts = self._parse_job_timestamp((row or {}).get("updated_at"))
                        if ref_ts is None:
                            ref_ts = self._parse_job_timestamp((row or {}).get("created_at"))
                        if ref_ts is None:
                            supabase_busy = True
                            break
                        if (now_ts - ref_ts) <= ttl_seconds:
                            supabase_busy = True
                            break
            except Exception:
                supabase_busy = False

        if supabase_busy:
            return True
        return False
