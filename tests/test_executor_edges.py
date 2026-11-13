"""Failure and no-op paths for destructive file operations."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from file_organizer.config import Config
from file_organizer.executor import Executor
from file_organizer.models import ACTION_DELETE, ACTION_MOVE, ACTION_RMDIR, PlannedAction
from file_organizer.undo import UndoManager


def _executor(tmp_path: Path) -> Executor:
    return Executor(
        trash_dir=tmp_path / "trash",
        log_dir=tmp_path / "logs",
        config=Config(tmp_path / "config.json"),
    )


def test_move_to_same_path_does_not_rename_source(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("keep", encoding="utf-8")

    report = _executor(tmp_path).execute([PlannedAction(ACTION_MOVE, source, source)])

    assert report.skipped == 1
    assert report.ok == 0
    assert source.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.glob("source_*.txt")) == []


def test_invalid_and_missing_actions_have_one_outcome_each(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("keep", encoding="utf-8")
    actions = [
        PlannedAction("unknown", source),
        PlannedAction(ACTION_MOVE, source),
        PlannedAction(ACTION_MOVE, tmp_path / "missing.txt", tmp_path / "out.txt"),
    ]

    report = _executor(tmp_path).execute(actions)

    assert (report.ok, report.failed, report.skipped) == (0, 2, 1)
    assert len(report.actions) == 3
    assert source.read_text(encoding="utf-8") == "keep"


def test_nonempty_directory_is_not_removed(tmp_path: Path) -> None:
    directory = tmp_path / "full"
    directory.mkdir()
    (directory / "file.txt").write_text("x", encoding="utf-8")

    report = _executor(tmp_path).execute([PlannedAction(ACTION_RMDIR, directory)])

    assert report.failed == 1
    assert directory.is_dir()
    assert (directory / "file.txt").is_file()


def test_undo_dry_run_and_invalid_logs_do_not_move_files(tmp_path: Path) -> None:
    source = tmp_path / "a.txt"
    destination = tmp_path / "out" / "a.txt"
    source.write_text("x", encoding="utf-8")
    report = _executor(tmp_path).execute([PlannedAction(ACTION_MOVE, source, destination)])
    manager = UndoManager(tmp_path / "logs")

    assert manager.latest() == report.log_path
    assert manager.restore(report.log_path, dry_run=True).restored == 1
    assert destination.exists() and not source.exists()
    assert manager.restore(tmp_path / "missing.json").restored == 0

    bad_log = tmp_path / "bad.json"
    bad_log.write_text("not json", encoding="utf-8")
    assert "corrupted" in manager.restore(bad_log).messages[0]

    simulated = tmp_path / "simulated.json"
    simulated.write_text(json.dumps({"dry_run": True, "actions": []}), encoding="utf-8")
    assert "simulation" in manager.restore(simulated).messages[0]
    assert destination.exists() and not source.exists()


def test_failed_move_does_not_stop_following_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from file_organizer import executor as executor_module

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    real_move = executor_module.shutil.move

    def move_with_one_failure(source: str, target: str) -> str:
        if source == str(first):
            raise OSError("unavailable")
        return real_move(source, target)

    monkeypatch.setattr(executor_module.shutil, "move", move_with_one_failure)
    report = _executor(tmp_path).execute([
        PlannedAction(ACTION_MOVE, first, tmp_path / "out" / "first.txt"),
        PlannedAction(ACTION_MOVE, second, tmp_path / "out" / "second.txt"),
    ])

    assert (report.ok, report.failed) == (1, 1)
    assert first.read_text(encoding="utf-8") == "first"
    assert (tmp_path / "out" / "second.txt").read_text(encoding="utf-8") == "second"
    assert report.log_path is not None and report.log_path.is_file()


def test_undo_skips_failed_and_missing_backup_actions(tmp_path: Path) -> None:
    log = tmp_path / "undo_log_20261114.json"
    payload = {"actions": [
        PlannedAction(ACTION_MOVE, tmp_path / "failed.txt", tmp_path / "missing.txt", status="failed").to_dict(),
        PlannedAction(ACTION_MOVE, tmp_path / "gone.txt", tmp_path / "missing.txt", status="ok").to_dict(),
    ]}
    log.write_text(json.dumps(payload), encoding="utf-8")

    manager = UndoManager(tmp_path)
    report = manager.restore(limit=1)
    assert report.skipped == 1 and report.failed == 0
    assert report.restored == 0

    report = manager.restore()
    assert report.skipped == 2 and report.failed == 0
    assert any("backup copy not found" in message for message in report.messages)


def test_dry_run_delete_and_remove_dir_leave_disk_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "a.txt"
    source.write_text("keep", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    executor = Executor(
        trash_dir=tmp_path / "trash",
        log_dir=tmp_path / "logs",
        config=Config(tmp_path / "config.json"),
        dry_run=True,
    )

    report = executor.execute([
        PlannedAction(ACTION_DELETE, source),
        PlannedAction(ACTION_RMDIR, empty),
    ])

    assert report.ok == 2 and report.failed == 0
    assert report.log_path is None
    assert source.read_text(encoding="utf-8") == "keep"
    assert empty.is_dir()
    assert not (tmp_path / "trash").exists()


def test_long_source_path_is_trashed_and_restored(tmp_path: Path) -> None:
    directory = tmp_path / ("a" * 60) / ("b" * 60) / ("c" * 60)
    directory.mkdir(parents=True)
    source = directory / "report.txt"
    source.write_text("recover me", encoding="utf-8")

    report = _executor(tmp_path).execute([PlannedAction(ACTION_DELETE, source)])

    assert report.ok == 1
    trashed = report.actions[0].destination
    assert trashed is not None and trashed.exists()
    assert trashed.name != source.name  # Long paths use the shortened hash name.
    assert UndoManager(tmp_path / "logs").restore(report.log_path).restored == 1
    assert source.read_text(encoding="utf-8") == "recover me"


def test_undo_move_error_keeps_backup_and_reports_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from file_organizer import undo as undo_module

    source = tmp_path / "a.txt"
    destination = tmp_path / "out" / "a.txt"
    source.write_text("recover me", encoding="utf-8")
    report = _executor(tmp_path).execute([PlannedAction(ACTION_MOVE, source, destination)])

    def fail_move(*args):
        raise OSError("disk full")

    monkeypatch.setattr(undo_module.shutil, "move", fail_move)

    undone = UndoManager(tmp_path / "logs").restore(report.log_path)

    assert undone.failed == 1 and undone.restored == 0
    assert "disk full" in undone.messages[0]
    assert destination.read_text(encoding="utf-8") == "recover me"
    assert not source.exists()
