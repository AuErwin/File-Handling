"""Tests for :mod:`file_organizer.executor` and :mod:`file_organizer.undo`.

These exercise a full move -> undo round trip, plus the no-overwrite and
soft-delete guarantees the executor promises.
"""
from __future__ import annotations

from pathlib import Path

from file_organizer.config import Config
from file_organizer.executor import Executor
from file_organizer.models import ACTION_DELETE, ACTION_MOVE, PlannedAction
from file_organizer.undo import UndoManager


def _config(tmp_path: Path) -> Config:
    return Config(path=tmp_path / "config.json")


def _executor(tmp_path: Path) -> Executor:
    return Executor(
        trash_dir=tmp_path / "trash",
        log_dir=tmp_path / "undo_logs",
        config=_config(tmp_path),
        dry_run=False,
    )


def test_move_then_undo_round_trip(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()
    source_file = source_dir / "report.pdf"
    source_file.write_text("contents", encoding="utf-8")

    executor = _executor(tmp_path)
    action = PlannedAction(action=ACTION_MOVE, source=source_file, destination=dest_dir / "report.pdf")

    report = executor.execute([action])

    assert report.ok == 1
    assert report.failed == 0
    moved_path = dest_dir / "report.pdf"
    assert moved_path.exists()
    assert not source_file.exists()
    assert report.log_path is not None and report.log_path.exists()

    undo_manager = UndoManager(log_dir=tmp_path / "undo_logs")
    undo_report = undo_manager.restore(log_path=report.log_path)

    assert undo_report.restored == 1
    assert undo_report.failed == 0
    assert source_file.exists()
    assert not moved_path.exists()
    assert source_file.read_text(encoding="utf-8") == "contents"


def test_move_never_overwrites_existing_destination(tmp_path: Path) -> None:
    source_file = tmp_path / "a.txt"
    source_file.write_text("new", encoding="utf-8")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    existing = dest_dir / "a.txt"
    existing.write_text("old", encoding="utf-8")

    executor = _executor(tmp_path)
    action = PlannedAction(action=ACTION_MOVE, source=source_file, destination=existing)

    report = executor.execute([action])

    assert report.ok == 1
    assert existing.read_text(encoding="utf-8") == "old"
    renamed_candidates = list(dest_dir.glob("a_*.txt"))
    assert len(renamed_candidates) == 1
    assert renamed_candidates[0].read_text(encoding="utf-8") == "new"


def test_delete_action_is_a_soft_delete_and_is_undoable(tmp_path: Path) -> None:
    target = tmp_path / "junk.tmp"
    target.write_text("throwaway", encoding="utf-8")

    executor = _executor(tmp_path)
    action = PlannedAction(action=ACTION_DELETE, source=target, reason="duplicate")

    report = executor.execute([action])

    assert report.ok == 1
    assert not target.exists()
    assert report.deleted_bytes > 0
    # The file was moved into the recycle bin, not removed from disk. Long
    # paths are shortened to a hash-based name, so check by content instead.
    trashed = [p for p in (tmp_path / "trash").rglob("*.tmp") if p.is_file()]
    assert len(trashed) == 1
    assert Path(action.destination) == trashed[0]
    assert trashed[0].read_text(encoding="utf-8") == "throwaway"

    undo_manager = UndoManager(log_dir=tmp_path / "undo_logs")
    undo_report = undo_manager.restore(log_path=report.log_path)

    assert undo_report.restored == 1
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "throwaway"


def test_undo_restores_as_renamed_copy_when_original_path_is_occupied(tmp_path: Path) -> None:
    source_file = tmp_path / "note.txt"
    source_file.write_text("first version", encoding="utf-8")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    executor = _executor(tmp_path)
    action = PlannedAction(action=ACTION_MOVE, source=source_file, destination=dest_dir / "note.txt")
    report = executor.execute([action])
    assert report.ok == 1

    # Something else now occupies the original path.
    source_file.write_text("second version", encoding="utf-8")

    undo_manager = UndoManager(log_dir=tmp_path / "undo_logs")
    undo_report = undo_manager.restore(log_path=report.log_path)

    assert undo_report.restored == 1
    assert undo_report.renamed == 1
    assert source_file.read_text(encoding="utf-8") == "second version"
    restored = tmp_path / "note_restored.txt"
    assert restored.exists()
    assert restored.read_text(encoding="utf-8") == "first version"


def test_dry_run_does_not_touch_disk_or_write_undo_log(tmp_path: Path) -> None:
    source_file = tmp_path / "a.txt"
    source_file.write_text("x", encoding="utf-8")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    executor = Executor(
        trash_dir=tmp_path / "trash",
        log_dir=tmp_path / "undo_logs",
        config=_config(tmp_path),
        dry_run=True,
    )
    action = PlannedAction(action=ACTION_MOVE, source=source_file, destination=dest_dir / "a.txt")

    report = executor.execute([action])

    assert report.ok == 1
    assert report.log_path is None
    assert source_file.exists()
    assert not (dest_dir / "a.txt").exists()
