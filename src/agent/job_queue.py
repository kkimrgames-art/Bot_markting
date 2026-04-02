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
        if _use_supabase():
            try:
                _supabase_upsert("job_queue", record, key_field="id")
                logger.info(f"📥 Job added to Supabase: {job_id} (Agent: {agent_id})")
                return job_id
            except Exception as e:
                logger.error(f"Failed to add job to Supabase: {e}")
        
        self._add_job_local(record)
        return job_id

    def _add_job_local(self, record: dict):
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
                conn.execute(
                    "INSERT INTO jobs (id, agent_id, task_type, payload, status, created_at, updated_at, error_msg) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (record["id"], record["agent_id"], record["task_type"], record["payload"],
                     record["status"], record["created_at"], record["updated_at"], record["error_msg"])
                )
                conn.commit()
                logger.info(f"📥 Job added locally: {record['id']}")
        except Exception as e:
            logger.error(f"Failed to add job locally: {e}")

    def get_next_job(self) -> Optional[Dict[str, Any]]:
        if _use_supabase():
            return self._get_next_job_supabase()
        return self._get_next_job_local()

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
                    return
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
                    return
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
                    return
            except Exception as e:
                logger.error(f"Failed to reset stuck jobs on Supabase: {e}")
        
        try:
            import sqlite3
            from pathlib import Path
            from .config import get_project_root
            db_path = Path(get_project_root()) / ".data" / "job_queue.db"
            if not db_path.exists():
                return
            with sqlite3.connect(str(db_path), timeout=30.0) as conn:
                threshold = self._now_iso()
                cursor = conn.execute(
                    "UPDATE jobs SET status = 'pending', updated_at = ? WHERE status = 'processing'",
                    (threshold,)
                )
                conn.commit()
                if cursor.rowcount > 0:
                    logger.warning(f"⚠️ Reset {cursor.rowcount} stuck jobs (local)")
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
                    return
            except Exception as e:
                logger.error(f"Failed to cleanup Supabase jobs: {e}")
        
        try:
            import sqlite3
            from pathlib import Path
            from .config import get_project_root
            db_path = Path(get_project_root()) / ".data" / "job_queue.db"
            if not db_path.exists():
                return
            with sqlite3.connect(str(db_path), timeout=30.0) as conn:
                conn.execute(
                    "DELETE FROM jobs WHERE status IN ('completed', 'failed')",
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to cleanup local jobs: {e}")

    def get_pending_count(self) -> int:
        if _use_supabase():
            try:
                from .supabase_client import _get_supabase
                client = _get_supabase()
                if client:
                    result = client.table("job_queue").select("id", count="exact").eq("status", "pending").execute()
                    return result.count or 0
            except Exception:
                pass
        return 0

    def is_agent_busy_or_queued(self, agent_id: str) -> bool:
        if _use_supabase():
            try:
                from .supabase_client import _get_supabase
                client = _get_supabase()
                if client:
                    result = client.table("job_queue").select("id").eq("agent_id", agent_id).in_("status", ["pending", "processing"]).limit(1).execute()
                    return bool(result.data)
            except Exception:
                pass
        return False
