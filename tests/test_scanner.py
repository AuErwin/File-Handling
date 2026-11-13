"""Tests for file_organizer.scanner -- scanning and statistics."""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

import pytest

from file_organizer.config import Config
from file_organizer.models import FileInfo
from file_organizer.scanner import Scanner


def _scan_root(tmp_path: Path) -> Tuple[Path, Config]:
    """Return (root, config) with the config file kept outside root.

    The config file must never live inside the directory being scanned, or
    it would show up as just another file in the results.
    """
    root = tmp_path / "data"
    root.mkdir()
    config = Config(path=tmp_path / "config.json")
    return root, config


def test_scan_counts_files_and_total_size(tmp_path: Path) -> None:
    root, config = _scan_root(tmp_path)
    (root / "a.txt").write_text("hello", encoding="utf-8")
    (root / "b.txt").write_text("world!", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("12345", encoding="utf-8")

    scanner = Scanner(root, config=config)
    result = scanner.scan()

    assert len(result.files) == 3
    assert result.total_size == 5 + 6 + 5
    assert not result.skipped_dirs and not result.skipped_files


def test_scan_respects_excluded_names(tmp_path: Path) -> None:
    root, config = _scan_root(tmp_path)
    (root / "keep.txt").write_text("x", encoding="utf-8")
    excluded_dir = root / "__pycache__"
    excluded_dir.mkdir()
    (excluded_dir / "ignored.pyc").write_text("y", encoding="utf-8")

    scanner = Scanner(root, config=config)
    result = scanner.scan()

    names = {info.name for info in result.files}
    assert names == {"keep.txt"}
    assert str(excluded_dir) in result.excluded_dirs


def test_scan_extension_distribution(tmp_path: Path) -> None:
    root, config = _scan_root(tmp_path)
    (root / "a.PDF").write_text("x", encoding="utf-8")
    (root / "b.pdf").write_text("y", encoding="utf-8")
    (root / "c.jpg").write_text("z", encoding="utf-8")

    scanner = Scanner(root, config=config)
    result = scanner.scan()

    extensions = [info.extension for info in result.files]
    assert extensions.count(".pdf") == 2
    assert extensions.count(".jpg") == 1


def test_scan_excludes_explicit_paths(tmp_path: Path) -> None:
    root, config = _scan_root(tmp_path)
    blocked = root / "blocked"
    blocked.mkdir()
    (blocked / "secret.txt").write_text("skip", encoding="utf-8")
    (root / "keep.txt").write_text("keep", encoding="utf-8")

    result = Scanner(root, config=config, exclude_paths={str(blocked)}).scan()

    assert [item.name for item in result.files] == ["keep.txt"]
    assert str(blocked) in result.excluded_dirs


def test_scan_records_file_that_vanishes_before_stat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, config = _scan_root(tmp_path)
    path = root / "vanishing.txt"
    path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(FileInfo, "from_path", classmethod(lambda cls, file_path: None))

    result = Scanner(root, config=config).scan()

    assert result.files == []
    assert result.skipped_files == [(str(path), "stat failed")]
