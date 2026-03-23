#!/usr/bin/env python3
"""
SQLite Storage - تخزين محلي دائم عند انقطاع الإنترنت
"""
import json
import os
import sqlite3
import logging
import threading
from typing import Any, Dict, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(os.environ.get("LOCAL_DB_PATH", ".data/bot_state.db"))
_DB_LOCK = threading.RLock()


def _init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def save_state(state: Dict[str, Any], db_path: Optional[Path] = None) -> bool:
    """حفظ الحالة في SQLite."""
    path = db_path or _DEFAULT_DB_PATH
    try:
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        with _DB_LOCK:
            _init_db(path)
            conn = sqlite3.connect(str(path))
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO bot_state (id, state_json, updated_at) VALUES (?, ?, strftime('%s','now'))",
                    ("main", payload),
                )
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception as e:
        logger.warning(f"⚠️ فشل حفظ الحالة في SQLite: {e}")
        return False


def load_state(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """تحميل الحالة من SQLite."""
    path = db_path or _DEFAULT_DB_PATH
    if not path.exists():
        return {}
    try:
        with _DB_LOCK:
            _init_db(path)
            conn = sqlite3.connect(str(path))
            try:
                cur = conn.execute("SELECT state_json FROM bot_state WHERE id = ?", ("main",))
                row = cur.fetchone()
            finally:
                conn.close()
        if not row or not row[0]:
            return {}
        return json.loads(row[0])
    except Exception as e:
        logger.warning(f"⚠️ فشل تحميل الحالة من SQLite: {e}")
        return {}
