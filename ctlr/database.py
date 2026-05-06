"""
SQLite database for persistent session and segment tracking.
Enables duplicate detection, progress tracking, and restart recovery.
"""

import sqlite3
import threading
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime
from contextlib import contextmanager

from config import get_config
from logger import log_info, log_error


# Database path
DB_PATH = Path("/data/ctlr/sessions.db")


class Database:
    """Thread-safe SQLite database for session tracking."""

    _instance: Optional["Database"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._conn_lock = threading.Lock()
        self._init_db()
        self._initialized = True

    def _init_db(self):
        """Initialize database schema."""
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)

        with self._get_conn() as conn:
            conn.executescript("""
                -- Sessions table
                CREATE TABLE IF NOT EXISTS sessions (
                    uuid TEXT PRIMARY KEY,
                    started_at TIMESTAMP,
                    stopped_at TIMESTAMP,
                    status TEXT DEFAULT 'recording',
                    cameras_count INTEGER DEFAULT 0,
                    phone_synced INTEGER DEFAULT 0,
                    watch_synced INTEGER DEFAULT 0,
                    exported INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                -- Segments received from cameras
                CREATE TABLE IF NOT EXISTS segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid TEXT NOT NULL,
                    camera TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(uuid, camera, filename),
                    FOREIGN KEY (uuid) REFERENCES sessions(uuid)
                );

                -- Phone data sync
                CREATE TABLE IF NOT EXISTS phone_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(uuid, filename),
                    FOREIGN KEY (uuid) REFERENCES sessions(uuid)
                );

                -- Watch data sync
                CREATE TABLE IF NOT EXISTS watch_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(uuid, filename),
                    FOREIGN KEY (uuid) REFERENCES sessions(uuid)
                );

                -- Session camera info (expected segments per camera)
                CREATE TABLE IF NOT EXISTS session_cameras (
                    uuid TEXT NOT NULL,
                    camera TEXT NOT NULL,
                    expected_segments INTEGER NOT NULL,
                    reported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (uuid, camera),
                    FOREIGN KEY (uuid) REFERENCES sessions(uuid)
                );

                -- Indexes for common queries
                CREATE INDEX IF NOT EXISTS idx_segments_uuid ON segments(uuid);
                CREATE INDEX IF NOT EXISTS idx_segments_camera ON segments(camera);
                CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
            """)

        log_info("database", f"Initialized database at {DB_PATH}")

    @contextmanager
    def _get_conn(self):
        """Get a thread-safe database connection."""
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Session operations

    def create_session(self, uuid: str, cameras_count: int = 0) -> bool:
        """Create a new recording session."""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO sessions (uuid, started_at, cameras_count)
                       VALUES (?, ?, ?)""",
                    (uuid, datetime.now(), cameras_count)
                )
            log_info("database", f"Created session {uuid}")
            return True
        except Exception as e:
            log_error("database", f"Failed to create session: {e}")
            return False

    def stop_session(self, uuid: str) -> bool:
        """Mark session as stopped."""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """UPDATE sessions SET stopped_at = ?, status = 'stopped'
                       WHERE uuid = ?""",
                    (datetime.now(), uuid)
                )
            return True
        except Exception as e:
            log_error("database", f"Failed to stop session: {e}")
            return False

    def get_session(self, uuid: str) -> Optional[Dict]:
        """Get session details."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE uuid = ?", (uuid,)
                ).fetchone()
                return dict(row) if row else None
        except Exception as e:
            log_error("database", f"Failed to get session: {e}")
            return None

    def list_sessions(self, limit: int = 50) -> List[Dict]:
        """List recent sessions."""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    """SELECT * FROM sessions
                       ORDER BY created_at DESC LIMIT ?""",
                    (limit,)
                ).fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            log_error("database", f"Failed to list sessions: {e}")
            return []

    # Segment operations

    def segment_exists(self, uuid: str, camera: str, filename: str) -> bool:
        """Check if segment already received (duplicate detection)."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    """SELECT 1 FROM segments
                       WHERE uuid = ? AND camera = ? AND filename = ?""",
                    (uuid, camera, filename)
                ).fetchone()
                return row is not None
        except Exception as e:
            log_error("database", f"Failed to check segment: {e}")
            return False

    def insert_segment(self, uuid: str, camera: str, filename: str, size_bytes: int) -> bool:
        """Record a received segment."""
        try:
            with self._get_conn() as conn:
                # Ensure session exists
                conn.execute(
                    "INSERT OR IGNORE INTO sessions (uuid) VALUES (?)",
                    (uuid,)
                )
                # Insert segment
                conn.execute(
                    """INSERT OR IGNORE INTO segments (uuid, camera, filename, size_bytes)
                       VALUES (?, ?, ?, ?)""",
                    (uuid, camera, filename, size_bytes)
                )
            return True
        except Exception as e:
            log_error("database", f"Failed to insert segment: {e}")
            return False

    def get_session_segments(self, uuid: str) -> Dict[str, List[Dict]]:
        """Get all segments for a session, grouped by camera."""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    """SELECT camera, filename, size_bytes, received_at
                       FROM segments WHERE uuid = ?
                       ORDER BY camera, filename""",
                    (uuid,)
                ).fetchall()

                result = {}
                for row in rows:
                    camera = row["camera"]
                    if camera not in result:
                        result[camera] = []
                    result[camera].append({
                        "filename": row["filename"],
                        "size_bytes": row["size_bytes"],
                        "received_at": row["received_at"],
                    })
                return result
        except Exception as e:
            log_error("database", f"Failed to get segments: {e}")
            return {}

    def get_segment_counts(self, uuid: str) -> Dict[str, int]:
        """Get segment count per camera for a session."""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    """SELECT camera, COUNT(*) as count
                       FROM segments WHERE uuid = ?
                       GROUP BY camera""",
                    (uuid,)
                ).fetchall()
                return {row["camera"]: row["count"] for row in rows}
        except Exception as e:
            log_error("database", f"Failed to get segment counts: {e}")
            return {}

    def get_total_segments(self, uuid: str) -> int:
        """Get total segment count for a session."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) as count FROM segments WHERE uuid = ?",
                    (uuid,)
                ).fetchone()
                return row["count"] if row else 0
        except Exception as e:
            return 0

    # Expected segments (camera reports total on stop)

    def set_expected_segments(self, uuid: str, camera: str, expected: int) -> bool:
        """Set expected segment count for a camera (reported when recording stops)."""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO session_cameras (uuid, camera, expected_segments)
                       VALUES (?, ?, ?)""",
                    (uuid, camera, expected)
                )
            log_info("database", f"Set expected segments: {camera}={expected}", uuid=uuid)
            return True
        except Exception as e:
            log_error("database", f"Failed to set expected segments: {e}")
            return False

    def get_expected_segments(self, uuid: str) -> Dict[str, int]:
        """Get expected segment count per camera for a session."""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT camera, expected_segments FROM session_cameras WHERE uuid = ?",
                    (uuid,)
                ).fetchall()
                return {row["camera"]: row["expected_segments"] for row in rows}
        except Exception as e:
            log_error("database", f"Failed to get expected segments: {e}")
            return {}

    def get_total_expected_segments(self, uuid: str) -> int:
        """Get total expected segment count for a session (sum of all cameras)."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT SUM(expected_segments) as total FROM session_cameras WHERE uuid = ?",
                    (uuid,)
                ).fetchone()
                return row["total"] if row and row["total"] else 0
        except Exception as e:
            return 0

    # Phone/Watch data

    def insert_phone_data(self, uuid: str, filename: str, size_bytes: int) -> bool:
        """Record phone data file received."""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO phone_data (uuid, filename, size_bytes)
                       VALUES (?, ?, ?)""",
                    (uuid, filename, size_bytes)
                )
                conn.execute(
                    "UPDATE sessions SET phone_synced = 1 WHERE uuid = ?",
                    (uuid,)
                )
            return True
        except Exception as e:
            log_error("database", f"Failed to insert phone data: {e}")
            return False

    def insert_watch_data(self, uuid: str, filename: str, size_bytes: int) -> bool:
        """Record watch data file received."""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO watch_data (uuid, filename, size_bytes)
                       VALUES (?, ?, ?)""",
                    (uuid, filename, size_bytes)
                )
                conn.execute(
                    "UPDATE sessions SET watch_synced = 1 WHERE uuid = ?",
                    (uuid,)
                )
            return True
        except Exception as e:
            log_error("database", f"Failed to insert watch data: {e}")
            return False

    # Export tracking

    def mark_exported(self, uuid: str) -> bool:
        """Mark session as exported to Mac partition."""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE sessions SET exported = 1 WHERE uuid = ?",
                    (uuid,)
                )
            return True
        except Exception as e:
            log_error("database", f"Failed to mark exported: {e}")
            return False

    def get_unexported_sessions(self) -> List[Dict]:
        """Get sessions not yet exported."""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    """SELECT * FROM sessions
                       WHERE exported = 0 AND status = 'stopped'
                       ORDER BY created_at DESC"""
                ).fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            return []

    # Stats

    def get_session_stats(self, uuid: str) -> Dict:
        """Get comprehensive stats for a session."""
        try:
            with self._get_conn() as conn:
                # Session info
                session = conn.execute(
                    "SELECT * FROM sessions WHERE uuid = ?", (uuid,)
                ).fetchone()

                if not session:
                    return {"exists": False}

                # Segment stats
                seg_stats = conn.execute(
                    """SELECT
                         COUNT(*) as total_segments,
                         SUM(size_bytes) as total_bytes,
                         COUNT(DISTINCT camera) as cameras
                       FROM segments WHERE uuid = ?""",
                    (uuid,)
                ).fetchone()

                # Phone files
                phone_count = conn.execute(
                    "SELECT COUNT(*) as count FROM phone_data WHERE uuid = ?",
                    (uuid,)
                ).fetchone()["count"]

                # Watch files
                watch_count = conn.execute(
                    "SELECT COUNT(*) as count FROM watch_data WHERE uuid = ?",
                    (uuid,)
                ).fetchone()["count"]

                return {
                    "exists": True,
                    "uuid": uuid,
                    "status": session["status"],
                    "started_at": session["started_at"],
                    "stopped_at": session["stopped_at"],
                    "segments": {
                        "total": seg_stats["total_segments"],
                        "size_mb": round((seg_stats["total_bytes"] or 0) / (1024 * 1024), 2),
                        "cameras": seg_stats["cameras"],
                    },
                    "phone_files": phone_count,
                    "watch_files": watch_count,
                    "exported": bool(session["exported"]),
                }
        except Exception as e:
            log_error("database", f"Failed to get session stats: {e}")
            return {"exists": False, "error": str(e)}


# Singleton accessor
def get_db() -> Database:
    """Get database instance."""
    return Database()
