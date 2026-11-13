"""Shared helpers and data-model contracts used by file operations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from file_organizer.models import DuplicateGroup, FileInfo, PlannedAction, ScanResult
from file_organizer.utils import (
    cache_key,
    chunked,
    chunked_hash,
    human_readable_size,
    read_json,
    unique_path,
    write_json,
)


def test_hash_and_cache_key_are_stable_and_missing_file_is_safe(tmp_path: Path) -> None:
    path = tmp_path / "text.txt"
    path.write_bytes(b"abc")

    assert chunked_hash(path, chunk_size=1) == hashlib.sha256(b"abc").hexdigest()
    assert chunked_hash(tmp_path / "missing") is None
    assert cache_key("  My   FILE.PDF ") == cache_key("my file.pdf")


def test_unique_path_preserves_existing_files(tmp_path: Path) -> None:
    original = tmp_path / "report.pdf"
    original.write_text("original", encoding="utf-8")
    first = tmp_path / "report_20261114.pdf"
    first.write_text("first", encoding="utf-8")

    assert unique_path(original, stamp="20261114") == tmp_path / "report_20261114_1.pdf"
    assert unique_path(tmp_path / "free.pdf") == tmp_path / "free.pdf"
    assert original.read_text(encoding="utf-8") == "original"


def test_json_round_trip_and_corrupt_input(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "data.json"
    payload = {"label": "中文", "items": [1, 2]}

    assert write_json(path, payload)
    assert read_json(path) == payload
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    path.write_text("{broken", encoding="utf-8")
    assert read_json(path, default={"fallback": True}) == {"fallback": True}
    assert read_json(tmp_path / "missing", default=[]) == []
    assert not write_json(path, {"invalid": object()})


def test_chunking_and_model_serialization(tmp_path: Path) -> None:
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunked([], 2)) == []
    assert list(chunked([1, 2], 0)) == [[1], [2]]

    path = tmp_path / "a.txt"
    path.write_text("abc", encoding="utf-8")
    info = FileInfo.from_path(path)
    assert info is not None
    assert info.name == "a.txt" and info.size_text == "3 B"
    assert FileInfo.from_path(tmp_path / "missing") is None
    assert ScanResult(root=tmp_path, files=[info]).total_size == 3
    assert DuplicateGroup(hash_value="abcdef" * 8, size=3, files=[info, info]).recoverable_bytes == 3

    action = PlannedAction("move", path, tmp_path / "out" / "a.txt", reason="test")
    assert PlannedAction.from_dict(action.to_dict()) == action
    assert human_readable_size(1024) == "1.00 KB"
    assert human_readable_size("not a number") == "0 B"
