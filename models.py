"""
models.py — SQLite database layer for Custom Parts Bureau.

Single `jobs` table tracking the full lifecycle:
uploaded → analyzing → quoted → paying → paid → printing → completed | rejected
"""

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "cpb.db"


def _get_conn():
    """Get a SQLite connection with row_factory for dict-like access."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create the jobs table if it doesn't exist."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id                      TEXT PRIMARY KEY,
            filename                TEXT NOT NULL,
            email                   TEXT NOT NULL,
            status                  TEXT NOT NULL DEFAULT 'uploaded',
            analysis_stage          TEXT NOT NULL DEFAULT 'uploaded',
            decision                TEXT,
            confidence              REAL,
            volume_cm3              REAL,
            surface_area_cm2        REAL,
            triangle_count          INTEGER,
            bounding_box            TEXT,
            overhang_pct            REAL,
            min_wall_mm             REAL,
            material_usd            REAL,
            machine_usd             REAL,
            support_usd             REAL,
            margin_usd              REAL,
            margin_pct              REAL,
            total_usd               REAL,
            reasoning_text          TEXT,
            nemotron_explanation    TEXT,
            line_items_json         TEXT,
            stripe_session_id       TEXT,
            stripe_payment_status   TEXT,
            stl_path                TEXT,
            agent_decision          TEXT,
            agent_reasoning         TEXT,
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL
        )
    """)
    
    # Simple migration for existing DBs — add columns if missing
    for col, default in [
        ("analysis_stage", "'uploaded'"),
        ("agent_decision", "NULL"),
        ("agent_reasoning", "NULL"),
    ]:
        try:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    conn.commit()
    conn.close()


def _row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return None
    d = dict(row)
    # Parse line_items_json back to list
    if d.get("line_items_json"):
        try:
            d["line_items"] = json.loads(d["line_items_json"])
        except (json.JSONDecodeError, TypeError):
            d["line_items"] = []
    else:
        d["line_items"] = []
    return d


def _now():
    """ISO timestamp in UTC."""
    return datetime.now(timezone.utc).isoformat()


def create_job(job_id, filename, email, stl_path):
    """Create a new job record. Returns the job as a dict."""
    now = _now()
    conn = _get_conn()
    conn.execute(
        """INSERT INTO jobs (id, filename, email, stl_path, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'uploaded', ?, ?)""",
        (job_id, filename, email, str(stl_path), now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def update_job(job_id, **fields):
    """
    Update a job record with arbitrary fields.
    Returns the updated job as a dict, or None if not found.
    """
    if not fields:
        return get_job(job_id)

    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in fields.keys())
    values = list(fields.values()) + [job_id]

    conn = _get_conn()
    conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", values)
    conn.commit()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def update_job_stage(job_id, stage):
    """Fast update for analysis_stage without returning the full dict."""
    conn = _get_conn()
    conn.execute("UPDATE jobs SET analysis_stage = ?, updated_at = ? WHERE id = ?", 
                 (stage, _now(), job_id))
    conn.commit()
    conn.close()


def get_job(job_id):
    """Get a single job by ID. Returns dict or None."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_all_jobs():
    """Get all jobs, newest first."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_jobs_by_email(email):
    """Get all jobs for a given email, newest first."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE email = ? ORDER BY created_at DESC",
        (email,),
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_jobs_awaiting_decision():
    """Find jobs where the agent has written a decision to the shared DB.
    
    These are jobs in 'analyzing' state with analysis_stage='reasoning'
    where the agent has populated agent_decision via the synced DB.
    """
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE status = 'analyzing'
             AND analysis_stage = 'reasoning'
             AND agent_decision IS NOT NULL"""
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_stale_jobs(timeout_minutes=5):
    """Find jobs stuck in 'analyzing' with no agent decision past the timeout.
    
    Returns jobs that have completed the pipeline (stage='reasoning')
    but where the agent has not yet written a decision, and the
    updated_at timestamp is older than timeout_minutes ago.
    """
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE status = 'analyzing'
             AND analysis_stage = 'reasoning'
             AND agent_decision IS NULL
             AND updated_at < datetime('now', ? || ' minutes')""",
        (f"-{timeout_minutes}",)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_midpipeline_jobs():
    """Find jobs whose pipeline was interrupted (container restart).
    
    These are jobs in 'analyzing' state where analysis_stage is NOT
    'reasoning' — meaning the pipeline thread died before completing.
    """
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE status = 'analyzing'
             AND analysis_stage != 'reasoning'"""
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def delete_job(job_id):
    """Delete a job by ID. Returns True if deleted, False if not found."""
    conn = _get_conn()
    cursor = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
