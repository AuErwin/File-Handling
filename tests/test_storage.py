"""SQLite cache and its in-memory fallback."""
from __future__ import annotations

from pathlib import Path

from file_organizer.storage import CacheStore


def test_cache_persists_updates_and_clears(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "cache.db"
    with CacheStore(path) as store:
        assert store.mode == "sqlite"
        assert store.get("missing") is None
        store.set("a", "Images", "a.jpg")
        store.set("b", "Documents", "b.pdf")
        store.set("a", "Documents", "a.jpg")
        assert store.get("a") == "Documents"
        assert store.stats()["entries"] == 2
        assert store.stats()["by_category"] == {"Documents": 2}

    with CacheStore(path) as store:
        assert store.get("a") == "Documents"
        assert store.clear() == 2
        assert store.stats()["entries"] == 0
        assert store.get("a") is None


def test_memory_cache_validates_keys_and_can_be_cleared() -> None:
    store = CacheStore(None)
    assert store.mode == "memory"
    store.set("", "Images")
    store.set("a", "")
    assert store.stats()["entries"] == 0
    store.set("a", "Images")
    store.set("b", "Images")
    assert store.stats()["by_category"] == {"Images": 2}
    assert store.clear() == 2
    store.close()
    store.close()
    assert store.get("a") is None


def test_unopenable_database_uses_memory_without_losing_values(tmp_path: Path) -> None:
    path = tmp_path / "directory"
    path.mkdir()  # sqlite3 cannot open a directory as a database file.
    store = CacheStore(path)

    assert store.mode == "memory"
    store.set("a", "Images")
    assert store.get("a") == "Images"
    assert store.stats()["entries"] == 1


def test_database_error_switches_to_memory_fallback(tmp_path: Path) -> None:
    store = CacheStore(tmp_path / "cache.db")
    store.set("a", "Images")
    assert store._connection is not None
    store._connection.close()  # Simulate a database that becomes unavailable mid-session.

    assert store.get("a") == "Images"
    assert store.mode == "memory"
    store.set("b", "Documents")
    assert store.stats()["entries"] == 2
