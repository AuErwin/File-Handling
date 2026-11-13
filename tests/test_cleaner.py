"""Empty-directory planning and safe removal."""
from __future__ import annotations

from pathlib import Path

from file_organizer.cleaner import Cleaner
from file_organizer.config import Config
from file_organizer.executor import Executor
from file_organizer.models import ACTION_RMDIR
from file_organizer.undo import UndoManager


def test_find_empty_dirs_is_deepest_first_and_never_returns_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "empty" / "child").mkdir(parents=True)
    (root / "full").mkdir()
    (root / "full" / "file.txt").write_text("x", encoding="utf-8")

    found = Cleaner().find_empty_dirs(root)

    assert found == [root / "empty" / "child", root / "empty"]
    assert root not in found


def test_find_empty_dirs_respects_protected_and_excluded_folders(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "protected" / "child").mkdir(parents=True)
    (root / "ignored" / "child").mkdir(parents=True)

    found = Cleaner().find_empty_dirs(
        root,
        protected={str(root / "protected")},
        exclude_names={"ignored"},
    )

    assert root / "protected" / "child" in found
    assert root / "ignored" / "child" not in found
    assert root / "protected" not in found
    assert root / "ignored" not in found
    assert Cleaner().find_empty_dirs(root / "missing") == []


def test_remove_empty_chain_and_restore_it(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "outer" / "inner").mkdir(parents=True)
    cleaner = Cleaner()
    plan = cleaner.plan_dir_removal(cleaner.find_empty_dirs(root))
    assert len(plan) == 2
    assert all(item.action == ACTION_RMDIR for item in plan)

    executor = Executor(
        trash_dir=tmp_path / "trash",
        log_dir=tmp_path / "logs",
        config=Config(tmp_path / "config.json"),
    )
    report = executor.execute(plan)
    assert report.ok == 2
    assert not (root / "outer").exists()

    undo = UndoManager(tmp_path / "logs").restore(report.log_path)
    assert undo.restored == 2
    assert (root / "outer" / "inner").is_dir()
