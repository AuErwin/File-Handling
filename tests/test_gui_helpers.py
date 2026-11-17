"""GUI data helpers that can run without opening a desktop window."""
from __future__ import annotations

from pathlib import Path

from file_organizer.gui import (
    PREVIEW_LIMIT,
    initial_window_geometry,
    list_folder_entries,
    log_belongs_to_root,
    read_text_preview,
)


def test_initial_window_is_centered_with_room_around_it() -> None:
    assert initial_window_geometry(2000, 1300) == "1360x975+320+162"
    assert initial_window_geometry(1366, 768) == "960x620+203+74"
    assert initial_window_geometry(800, 600) == "760x520+20+40"


def test_text_preview_handles_utf8_bom_binary_and_invalid_text(tmp_path: Path) -> None:
    text = tmp_path / "text.txt"
    text.write_bytes(b"\xef\xbb\xbfhello")
    assert read_text_preview(text) == "hello"

    binary = tmp_path / "binary.dat"
    binary.write_bytes(b"a\x00b")
    assert "二进制" in read_text_preview(binary)

    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff")
    assert "UTF-8" in read_text_preview(invalid)


def test_text_preview_is_bounded_and_does_not_break_a_partial_character(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"a" * (PREVIEW_LIMIT - 1) + "中".encode("utf-8"))

    preview = read_text_preview(path)

    assert preview.startswith("a" * (PREVIEW_LIMIT - 1))
    assert "仅显示前 64 KiB" in preview


def test_folder_entries_are_immediate_and_directories_first(tmp_path: Path) -> None:
    (tmp_path / "Zoo").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "b.txt").write_bytes(b"12")
    (tmp_path / "A.txt").write_bytes(b"x")
    (tmp_path / "Zoo" / "nested.txt").write_bytes(b"hidden")

    rows = list_folder_entries(tmp_path)

    assert [row[0].name for row in rows] == ["alpha", "Zoo", "A.txt", "b.txt"]
    assert [row[1] for row in rows] == [True, True, False, False]
    assert [row[2] for row in rows] == ["", "", "1 B", "2 B"]


def test_undo_log_is_only_visible_for_its_exact_root(tmp_path: Path) -> None:
    root = tmp_path / "files"
    root.mkdir()
    sibling = tmp_path / "files-other"
    sibling.mkdir()
    payload = {
        "trash_dir": str(root / ".file_organizer_trash"),
        "actions": [{"source": str(root / "sub" / "a.txt")}],
    }

    assert log_belongs_to_root(payload, root, ".file_organizer_trash")
    assert not log_belongs_to_root(payload, sibling, ".file_organizer_trash")
    payload["actions"].append({"source": str(sibling / "outside.txt")})
    assert not log_belongs_to_root(payload, root, ".file_organizer_trash")
    assert not log_belongs_to_root({"actions": []}, root, ".file_organizer_trash")
