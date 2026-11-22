"""Tests for :mod:`file_organizer.duplicate_checker`."""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import pytest

from file_organizer import duplicate_checker
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


def test_find_ignores_file_removed_after_scan(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("a.txt", "b.txt", "c.txt")]
    for path in paths:
        path.write_bytes(b"same")
    files = [_info(path) for path in paths]
    paths[2].unlink()

    groups = DuplicateChecker(files).find()

    assert len(groups) == 1
    assert {item.path for item in groups[0].files} == set(paths[:2])


def test_find_places_newest_duplicate_first(tmp_path: Path) -> None:
    old = tmp_path / "old.txt"
    new = tmp_path / "new.txt"
    for path in (old, new):
        path.write_bytes(b"same")
    os.utime(old, (1700000000, 1700000000))
    os.utime(new, (1700000100, 1700000100))

    groups = DuplicateChecker([_info(old), _info(new)]).find()

    assert [item.path for item in groups[0].files] == [new, old]


def test_find_hashes_only_files_with_matching_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matching = [tmp_path / "a.txt", tmp_path / "b.txt"]
    unique = tmp_path / "unique.txt"
    for path in matching:
        path.write_bytes(b"same")
    unique.write_bytes(b"different length")
    hashed: List[Path] = []
    original_hash = duplicate_checker.chunked_hash

    def record_hash(path: Path, algorithm: str, chunk_size: int) -> Optional[str]:
        hashed.append(path)
        return original_hash(path, algorithm, chunk_size)

    monkeypatch.setattr(duplicate_checker, "chunked_hash", record_hash)

    groups = DuplicateChecker([_info(path) for path in matching + [unique]]).find()

    assert len(groups) == 1
    assert set(hashed) == set(matching)
