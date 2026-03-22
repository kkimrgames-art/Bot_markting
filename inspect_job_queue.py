
import sqlite3
import os
from pathlib import Path

DB_PATH = Path(".data/job_queue.db")

def inspect_jobs():
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 20")
    rows = cursor.fetchall()
    
    if not rows:
        print("No jobs found in the queue.")
    else:
        print(f"Found {len(rows)} recent jobs:")
        print("-" * 100)
        print(f"{'ID':<5} | {'Agent ID':<15} | {'Type':<20} | {'Status':<12} | {'Created At'}")
        print("-" * 100)
        for row in rows:
            print(f"{row['id']:<5} | {row['agent_id']:<15} | {row['task_type']:<20} | {row['status']:<12} | {row['created_at']}")
    
    conn.close()

if __name__ == "__main__":
    inspect_jobs()
