"""
HireWire — SQLite Memory Layer
Tracks processed projects to prevent duplicate Telegram notifications.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from config import DB_NAME, logger


# ---------------------------------------------------------------------------
# Connection Manager
# ---------------------------------------------------------------------------
@contextmanager
def _get_connection():
    """Context manager for safe DB connections — auto-commits and closes."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------
def init_db() -> None:
    """Create the processed_projects table if it doesn't exist."""
    with _get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT UNIQUE NOT NULL,
                title       TEXT NOT NULL,
                hiring_rate INTEGER DEFAULT 0,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
    logger.info("✅ Database initialized: %s", DB_NAME)


# ---------------------------------------------------------------------------
# Core Operations
# ---------------------------------------------------------------------------
def is_processed(url: str) -> bool:
    """Check if a project URL has already been processed."""
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_projects WHERE url = ?", (url,)
        ).fetchone()
    return row is not None


def mark_as_processed(url: str, title: str, hiring_rate: int = 0) -> None:
    """Save a project URL to prevent future duplicates."""
    with _get_connection() as conn:
        try:
            conn.execute(
                "INSERT INTO processed_projects (url, title, hiring_rate) VALUES (?, ?, ?)",
                (url, title, hiring_rate),
            )
        except sqlite3.IntegrityError:
            pass  # Already exists — safe to ignore


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------
def cleanup_old_entries(days: int = 30) -> int:
    """Delete entries older than N days. Returns count of deleted rows."""
    cutoff = datetime.now() - timedelta(days=days)
    with _get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM processed_projects WHERE created_at < ?",
            (cutoff.isoformat(),),
        )
        deleted = cursor.rowcount
    if deleted > 0:
        logger.info("🧹 Cleaned up %d old entries (older than %d days)", deleted, days)
    return deleted


def get_stats() -> dict:
    """Return database statistics for diagnostics."""
    with _get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM processed_projects").fetchone()[0]
        recent = conn.execute(
            "SELECT COUNT(*) FROM processed_projects WHERE created_at > datetime('now', '-7 days')"
        ).fetchone()[0]
    return {"total_entries": total, "last_7_days": recent}
