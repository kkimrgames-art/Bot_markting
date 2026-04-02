import sqlite3
import json
import time
import logging
import os
from typing import Optional, Dict, Any, List
from pathlib import Path

from .config import get_project_root

logger = logging.getLogger(__name__)

DB_PATH = Path(
    os.environ.get(
        "JOB_QUEUE_DB_PATH",
        str(Path(get_project_root()) / ".data" / "job_queue.db"),
    )
)

class JobQueue:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(JobQueue, cls).__new__(cls)
            cls._instance._init_db()
        return cls._instance

    def _get_conn(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    error_msg TEXT
                )
            """)
            conn.commit()

    def add_job(self, agent_id: str, task_type: str, payload: Dict[str, Any]) -> int:
        """Add a job to the queue."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs (agent_id, task_type, payload, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, task_type, json.dumps(payload), 'pending', time.time(), time.time())
            )
            conn.commit()
            job_id = cursor.lastrowid
            logger.info(f"📥 Job added: {job_id} (Agent: {agent_id}, Type: {task_type})")
            return job_id

    def get_next_job(self) -> Optional[Dict[str, Any]]:
        """Get the next pending job and mark it as processing (atomic-ish)."""
        with self._get_conn() as conn:
            conn.execute("BEGIN EXCLUSIVE") # Lock the database
            try:
                cursor = conn.execute(
                    "SELECT id, agent_id, task_type, payload, created_at FROM jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
                )
                row = cursor.fetchone()
                if row:
                    job_id, agent_id, task_type, payload_json, created_at = row
                    conn.execute(
                        "UPDATE jobs SET status = 'processing', updated_at = ? WHERE id = ?",
                        (time.time(), job_id)
                    )
                    conn.commit()
                    return {
                        "id": job_id,
                        "agent_id": agent_id,
                        "task_type": task_type,
                        "payload": json.loads(payload_json),
                        "created_at": created_at
                    }
                else:
                    conn.rollback() # Release lock
                    return None
            except Exception as e:
                conn.rollback()
                logger.error(f"Error getting next job: {e}")
                return None

    def complete_job(self, job_id: int):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'completed', updated_at = ? WHERE id = ?",
                (time.time(), job_id)
            )
            conn.commit()
            logger.info(f"✅ Job completed: {job_id}")

    def fail_job(self, job_id: int, error_msg: str):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'failed', error_msg = ?, updated_at = ? WHERE id = ?",
                (str(error_msg), time.time(), job_id)
            )
            conn.commit()
            logger.error(f"❌ Job failed: {job_id} - {error_msg}")

    def reset_stuck_jobs(self, timeout_seconds: int = 3600):
        """Reset jobs that were left in 'processing' state for too long."""
        with self._get_conn() as conn:
            threshold = time.time() - timeout_seconds
            cursor = conn.execute(
                "UPDATE jobs SET status = 'pending', updated_at = ? WHERE status = 'processing' AND updated_at < ?",
                (time.time(), threshold)
            )
            conn.commit()
            if cursor.rowcount > 0:
                logger.warning(f"⚠️ Reset {cursor.rowcount} stuck jobs (timeout > {timeout_seconds}s) to pending.")

    def cleanup_completed_jobs(self, max_age_seconds: int = 86400):
        """Remove old completed/failed jobs to keep DB small."""
        with self._get_conn() as conn:
            threshold = time.time() - max_age_seconds
            conn.execute(
                "DELETE FROM jobs WHERE status IN ('completed', 'failed') AND updated_at < ?",
                (threshold,)
            )
            conn.commit()

    def get_pending_count(self) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'")
            return cursor.fetchone()[0]

    def is_agent_busy_or_queued(self, agent_id: str) -> bool:
        """Check if an agent already has a pending or processing job."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE agent_id = ? AND status IN ('pending', 'processing')",
                (agent_id,)
            )
            return cursor.fetchone()[0] > 0
