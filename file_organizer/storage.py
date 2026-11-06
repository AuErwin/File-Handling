"""SQLite backed caches used by the organizer (AI classification cache).

The store degrades gracefully: if SQLite cannot be opened (read-only volume,
corrupted file, ancient Python build) it transparently switches to an
in-memory dictionary so the program keeps working.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .utils import PathLike

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_cache (
    key        TEXT PRIMARY KEY,
    file_name  TEXT NOT NULL,
    category   TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'ai',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_cache_created ON ai_cache(created_at);
"""


class CacheStore(object):
    """A tiny key/value cache persisted in SQLite.

    Args:
        db_path: Location of the ``.db`` file.  ``None`` disables persistence
            entirely (pure in-memory mode).
    """

    def __init__(self, db_path: Optional[PathLike] = None) -> None:
        """Open (or create) the SQLite database at ``db_path``."""
        self.db_path: Optional[Path] = Path(str(db_path)) if db_path else None
        self._lock = threading.RLock()
        self._fallback: Dict[str, Tuple[str, str]] = {}
        self._connection: Optional[sqlite3.Connection] = None
        self._enabled = False
        if self.db_path is not None:
            self._enabled = self._connect()

    # -- connection handling ------------------------------------------------ #
    def _connect(self) -> bool:
        """Open the database and create the schema.  ``False`` on any failure."""
        if self.db_path is None:
            return False
        try:
            if self.db_path.parent and not self.db_path.parent.exists():
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.db_path), timeout=10.0)
            connection.execute("PRAGMA journal_mode=WAL;")
            connection.execute("PRAGMA synchronous=NORMAL;")
            connection.executescript(_SCHEMA)
            connection.commit()
            self._connection = connection
            return True
        except (sqlite3.Error, OSError, ValueError):
            self._connection = None
            return False

    @property
    def enabled(self) -> bool:
        """``True`` when the SQLite backend is usable."""
        return bool(self._enabled and self._connection is not None)

    @property
    def mode(self) -> str:
        """``"sqlite"`` or ``"memory"`` -- useful for diagnostics output."""
        return "sqlite" if self.enabled else "memory"

    def close(self) -> None:
        """Close the underlying connection (safe to call twice)."""
        with self._lock:
            if self._connection is not None:
                try:
                    self._connection.close()
                except sqlite3.Error:
                    pass
                self._connection = None
            self._enabled = False

    # -- context manager ----------------------------------------------------- #
    def __enter__(self) -> "CacheStore":
        """Enter a ``with`` block."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """Leave a ``with`` block and close the connection."""
        self.close()

    # -- public API ---------------------------------------------------------- #
    def get(self, key: str) -> Optional[str]:
        """Return the cached category for ``key``, or ``None`` when absent."""
        key = str(key)
        with self._lock:
            if not self.enabled:
                entry = self._fallback.get(key)
                return entry[0] if entry else None
            try:
                cursor = self._connection.execute(
                    "SELECT category FROM ai_cache WHERE key = ? LIMIT 1;", (key,)
                )
                row = cursor.fetchone()
            except sqlite3.Error:
                self._enabled = False
                entry = self._fallback.get(key)
                return entry[0] if entry else None
        return str(row[0]) if row else None

    def get_many(self, keys: List[str]) -> Dict[str, str]:
        """Batch lookup; returns only the keys that were found."""
        found: Dict[str, str] = {}
        for key in keys:
            value = self.get(key)
            if value:
                found[str(key)] = value
        return found

    def set(self, key: str, category: str, file_name: str = "", source: str = "ai") -> None:
        """Store ``category`` under ``key``."""
        key, category = str(key), str(category)
        if not key or not category:
            return
        stamp = time.time()
        with self._lock:
            self._fallback[key] = (category, source)
            if not self.enabled:
                return
            try:
                self._connection.execute(
                    "INSERT INTO ai_cache (key, file_name, category, source, created_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET category=excluded.category, "
                    "source=excluded.source, created_at=excluded.created_at;",
                    (key, str(file_name)[:255], category, source, stamp),
                )
                self._connection.commit()
            except sqlite3.Error:
                self._enabled = False

    def set_many(self, mapping: Dict[str, Tuple[str, str]]) -> None:
        """Store several entries: ``{key: (category, file_name)}``."""
        for key, payload in mapping.items():
            if isinstance(payload, (tuple, list)) and len(payload) >= 2:
                self.set(key, payload[0], payload[1])
            else:
                self.set(key, str(payload))

    def stats(self) -> Dict[str, Any]:
        """Return diagnostic counters about the cache."""
        stats: Dict[str, Any] = {
            "mode": self.mode,
            "entries": 0,
            "by_category": {},
            "db_path": str(self.db_path) if self.db_path else "",
        }
        with self._lock:
            if not self.enabled:
                stats["entries"] = len(self._fallback)
                buckets: Dict[str, int] = {}
                for category, _source in self._fallback.values():
                    buckets[category] = buckets.get(category, 0) + 1
                stats["by_category"] = buckets
                return stats
            try:
                cursor = self._connection.execute("SELECT COUNT(*) FROM ai_cache;")
                row = cursor.fetchone()
                stats["entries"] = int(row[0]) if row else 0
                cursor = self._connection.execute(
                    "SELECT category, COUNT(*) FROM ai_cache GROUP BY category ORDER BY 2 DESC;"
                )
                stats["by_category"] = {str(name): int(count) for name, count in cursor.fetchall()}
            except sqlite3.Error:
                stats["entries"] = len(self._fallback)
        return stats

    def clear(self) -> int:
        """Drop every cached entry and return how many rows were removed."""
        with self._lock:
            removed = len(self._fallback)
            self._fallback.clear()
            if not self.enabled:
                return removed
            try:
                cursor = self._connection.execute("SELECT COUNT(*) FROM ai_cache;")
                row = cursor.fetchone()
                removed = int(row[0]) if row else 0
                self._connection.execute("DELETE FROM ai_cache;")
                self._connection.commit()
            except sqlite3.Error:
                self._enabled = False
        return removed
