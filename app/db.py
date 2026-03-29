"""
Database layer — SQLite via stdlib only, no ORM needed at this scale.

Thread safety: each call opens a short-lived connection, executes, and closes.
This avoids the check_same_thread=False risk when Flask serves concurrent
requests from multiple threads (scanner background thread + UI requests).
WAL mode is enabled so readers never block writers.
"""

import sqlite3
import os
from contextlib import contextmanager
from typing import List, Optional


class Database:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self._migrate()

    @contextmanager
    def _conn(self):
        """Open a connection, yield it, then close. Thread-safe by design."""
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # readers don't block writers
        conn.execute("PRAGMA synchronous=NORMAL")  # safe + faster than FULL
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tracks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    path        TEXT UNIQUE NOT NULL,
                    artist      TEXT,
                    album       TEXT,
                    title       TEXT,
                    track_num   INTEGER,
                    duration    REAL,
                    bitrate     INTEGER,
                    format      TEXT,
                    filesize    INTEGER,
                    has_cover   INTEGER DEFAULT 0,
                    fingerprint TEXT,
                    scanned_at  TEXT DEFAULT (datetime('now')),
                    mtime       REAL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_fingerprint ON tracks(fingerprint);
                CREATE INDEX IF NOT EXISTS idx_artist_title ON tracks(artist, title);
                CREATE INDEX IF NOT EXISTS idx_path ON tracks(path);

                CREATE TABLE IF NOT EXISTS tag_fixes (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    path                 TEXT UNIQUE NOT NULL,
                    original_album_artist TEXT,
                    fixed_at             TEXT DEFAULT (datetime('now')),
                    restored             INTEGER DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_tag_fixes_path ON tag_fixes(path);

                CREATE TABLE IF NOT EXISTS album_moves (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path  TEXT NOT NULL,
                    dest_path    TEXT NOT NULL,
                    source_folder TEXT NOT NULL,
                    keeper_folder TEXT NOT NULL,
                    moved_at     TEXT DEFAULT (datetime('now')),
                    restored     INTEGER DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_album_moves_folder ON album_moves(source_folder);
            """)
        # Add mtime column if upgrading from older schema
        try:
            with self._conn() as conn:
                conn.execute("ALTER TABLE tracks ADD COLUMN mtime REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists

    def upsert_track(self, track: dict):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO tracks
                    (path, artist, album, title, track_num, duration, bitrate,
                     format, filesize, has_cover, fingerprint, mtime)
                VALUES
                    (:path, :artist, :album, :title, :track_num, :duration, :bitrate,
                     :format, :filesize, :has_cover, :fingerprint, :mtime)
                ON CONFLICT(path) DO UPDATE SET
                    artist=excluded.artist, album=excluded.album,
                    title=excluded.title, track_num=excluded.track_num,
                    duration=excluded.duration, bitrate=excluded.bitrate,
                    format=excluded.format, filesize=excluded.filesize,
                    has_cover=excluded.has_cover, fingerprint=excluded.fingerprint,
                    scanned_at=datetime('now'), mtime=excluded.mtime
            """, track)

    def get_track_mtime(self, path: str) -> Optional[float]:
        """Return the stored mtime for a path, or None if not in DB."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT mtime FROM tracks WHERE path=?", (path,)
            ).fetchone()
        return row["mtime"] if row else None

    def all_tracks(self) -> List[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM tracks ORDER BY path").fetchall()

    def tracks_missing_covers(self) -> List[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM tracks WHERE has_cover=0 ORDER BY artist, album"
            ).fetchall()

    def update_cover_status(self, path: str, has_cover: bool):
        with self._conn() as conn:
            conn.execute(
                "UPDATE tracks SET has_cover=? WHERE path=?",
                (1 if has_cover else 0, path)
            )

    def remove_track(self, path: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM tracks WHERE path=?", (path,))

    def cleanup_missing(self) -> int:
        """Remove DB entries for files that no longer exist on disk."""
        with self._conn() as conn:
            all_paths = [row["path"] for row in
                         conn.execute("SELECT path FROM tracks").fetchall()]
        missing = [p for p in all_paths if not os.path.exists(p)]
        if missing:
            with self._conn() as conn:
                conn.executemany(
                    "DELETE FROM tracks WHERE path=?", [(p,) for p in missing]
                )
        return len(missing)

    # ── Tag fix methods ───────────────────────────────────────────────────────

    def log_tag_fix(self, path: str, original_album_artist: str):
        """Record original Album Artist before overwriting."""
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO tag_fixes (path, original_album_artist, restored)
                VALUES (?, ?, 0)
                ON CONFLICT(path) DO UPDATE SET
                    original_album_artist=excluded.original_album_artist,
                    fixed_at=datetime('now'),
                    restored=0
            """, (path, original_album_artist))

    def remove_tag_fix(self, path: str):
        """Remove a tag fix log entry (used when write failed)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM tag_fixes WHERE path=?", (path,))

    def get_tag_fixes_for_folder(self, folder: str) -> List[sqlite3.Row]:
        """Return all tag fix records for files in a given folder."""
        with self._conn() as conn:
            return conn.execute("""
                SELECT * FROM tag_fixes
                WHERE path LIKE ? AND restored=0
            """, (folder + "/%",)).fetchall()

    def restore_tag_fix(self, path: str):
        """Mark a tag fix as restored."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE tag_fixes SET restored=1 WHERE path=?", (path,)
            )

    def all_tag_fixes(self) -> List[sqlite3.Row]:
        """Return all active (non-restored) tag fixes grouped by folder."""
        with self._conn() as conn:
            return conn.execute("""
                SELECT * FROM tag_fixes WHERE restored=0 ORDER BY path
            """).fetchall()

    # ── Album move methods ────────────────────────────────────────────────────

    def log_album_move(self, source_path: str, dest_path: str, keeper_folder: str):
        """Record a file moved as part of an album dedup operation."""
        source_folder = os.path.dirname(source_path)
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO album_moves (source_path, dest_path, source_folder, keeper_folder, restored)
                VALUES (?, ?, ?, ?, 0)
            """, (source_path, dest_path, source_folder, keeper_folder))

    def get_album_moves_for_folder(self, source_folder: str) -> List[sqlite3.Row]:
        """Return all active move records for a given source folder."""
        with self._conn() as conn:
            return conn.execute("""
                SELECT * FROM album_moves WHERE source_folder=? AND restored=0
            """, (source_folder,)).fetchall()

    def restore_album_move(self, move_id: int):
        """Mark an album move as restored."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE album_moves SET restored=1 WHERE id=?", (move_id,)
            )

    def all_album_moves(self) -> List[sqlite3.Row]:
        """Return all active (non-restored) album moves."""
        with self._conn() as conn:
            return conn.execute("""
                SELECT * FROM album_moves WHERE restored=0
                ORDER BY moved_at DESC
            """).fetchall()
