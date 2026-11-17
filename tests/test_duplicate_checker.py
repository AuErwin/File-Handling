"""Tests for :mod:`file_organizer.duplicate_checker`."""
from __future__ import annotations

from pathlib import Path
from typing import List

from file_organizer.duplicate_checker import DuplicateChecker
from file_organizer.models import FileInfo


def _info(path: Path) -> FileInfo:
    info = FileInfo.from_path(path)
    assert info is not None
    return info


def test_find_groups_identical_content(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("same content", encoding="utf-8")
    (tmp_path / "b.txt").write_text("same content", encoding="utf-8")
    (tmp_path / "c.txt").write_text("different", encoding="utf-8")

    files: List[FileInfo] = [
        _info(tmp_path / "a.txt"),
        _info(tmp_path / "b.txt"),
        _info(tmp_path / "c.txt"),
    ]

    checker = DuplicateChecker(files)
    groups = checker.find()

    assert len(groups) == 1
    group = groups[0]
    assert len(group.files) == 2
    assert {item.name for item in group.files} == {"a.txt", "b.txt"}
    assert group.recoverable_bytes == group.size


def test_find_ignores_files_with_different_sizes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("short", encoding="utf-8")
    (tmp_path / "b.txt").write_text("a much longer piece of text", encoding="utf-8")

    files = [_info(tmp_path / "a.txt"), _info(tmp_path / "b.txt")]
    checker = DuplicateChecker(files)

    assert checker.find() == []


def test_find_rejects_same_size_with_different_content(tmp_path: Path) -> None:
    (tmp_path / "copy.txt").write_bytes(b"ABCD")
    (tmp_path / "other.txt").write_bytes(b"WXYZ")

    files = [_info(tmp_path / "copy.txt"), _info(tmp_path / "other.txt")]
    assert DuplicateChecker(files).find() == []


def test_find_excludes_empty_files_by_default(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("", encoding="utf-8")
    (tmp_path / "b.txt").write_text("", encoding="utf-8")

    files = [_info(tmp_path / "a.txt"), _info(tmp_path / "b.txt")]
    checker = DuplicateChecker(files)

    assert checker.find() == []

    checker_incl = DuplicateChecker(files, include_empty=True)
    assert len(checker_incl.find()) == 1


def test_summarize_reports_wasted_bytes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("xxxx", encoding="utf-8")
    (tmp_path / "b.txt").write_text("xxxx", encoding="utf-8")

    files = [_info(tmp_path / "a.txt"), _info(tmp_path / "b.txt")]
    checker = DuplicateChecker(files)
    groups = checker.find()

    summary = DuplicateChecker.summarize(groups)

    assert summary["groups"] == 1
    assert summary["files"] == 2
    assert summary["redundant"] == 1
    assert summary["wasted_bytes"] == 4
