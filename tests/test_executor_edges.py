"""Failure and no-op paths for destructive file operations."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from file_organizer.config import Config
from file_organizer.executor import Executor
from file_organizer.models import ACTION_MOVE, ACTION_RMDIR, PlannedAction
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
