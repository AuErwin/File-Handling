"""Headless checks for GUI handlers and their real file-operation pipeline."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from file_organizer.config import DEFAULT_EXTENSION_MAP, Config
from file_organizer.executor import Executor
from file_organizer.gui import App, messagebox, simpledialog
from file_organizer.models import ACTION_MOVE, PlannedAction
from file_organizer.undo import UndoManager


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Tree:
    def __init__(self):
        self.rows = {}
        self.selected = ()

    def get_children(self):
        return tuple(self.rows)

    def delete(self, *items):
        for item in items:
            self.rows.pop(item)

    def insert(self, parent, position, iid=None, values=(), **kwargs):
        key = iid if iid is not None else str(len(self.rows))
        self.rows[key] = values

    def selection_set(self, items):
        self.selected = (items,) if isinstance(items, str) else tuple(items)

    def selection(self):
        return self.selected

    def item(self, iid, **kwargs):
        pass

    def set(self, iid, column, value):
        pass

    def exists(self, iid):
        return iid in self.rows

    def see(self, iid):
        pass


def test_gui_organize_preview_execute_and_undo_without_a_display(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "files"
    root.mkdir()
    source = root / "photo.jpg"
    source.write_bytes(b"image")
    app = SimpleNamespace()
    app.config = Config(tmp_path / "config.json")
    app.work_dir = tmp_path / "work"
    app.log_dir = tmp_path / "logs"
    app.dest_var = Value("")
    app.use_ai_var = Value(False)
    app.org_dry_var = Value(False)
    app.org_summary_var = Value("")
    app.org_tree = Tree()
    app.get_root = lambda: root
    app.extra_excludes = lambda: set()
    app.trash_dir_for = lambda folder: folder / ".file_organizer_trash"
    app.scan_tree = lambda folder, excludes, paths: App.scan_tree(app, folder, excludes, paths)
    app.set_status = lambda text: None
    app.log = lambda text: None
    app.update_idletasks = lambda: None
    app.run_task = lambda work, done, status: done(work())
    app.new_executor = lambda folder, dry_run=False: App.new_executor(app, folder, dry_run)
    app._org_done = lambda outcome: App._org_done(app, outcome)
    app._outcome_rows = App._outcome_rows
    app.show_outcome = lambda title, rows, actions: None

    App.do_org_plan(app)

    assert len(app.org_items) == 1
    assert app.org_items[0][0].destination == root / "Images" / "photo.jpg"
    assert app.org_tree.rows["0"][2] == "Images"
    assert "待移动 1" in app.org_summary_var.get()
    assert source.is_file()  # Preview has no filesystem effect.

    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: True)
    App.do_org_execute(app)

    destination = root / "Images" / "photo.jpg"
    assert destination.read_bytes() == b"image"
    assert not source.exists()
    assert app.org_items == []
    assert UndoManager(app.log_dir).restore().restored == 1
    assert source.read_bytes() == b"image"


def test_gui_delete_confirmation_requires_exact_token(tmp_path: Path, monkeypatch) -> None:
    app = SimpleNamespace(config=Config(tmp_path / "config.json"))
    monkeypatch.setattr(simpledialog, "askstring", lambda *args, **kwargs: "delete")
    assert not App.confirm_destructive(app, "remove")
    monkeypatch.setattr(simpledialog, "askstring", lambda *args, **kwargs: " DELETE ")
    assert App.confirm_destructive(app, "remove")

    app.config.set("safety.require_delete_confirmation", False)
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: False)
    assert not App.confirm_destructive(app, "remove")


def test_gui_size_search_orders_largest_and_smallest(tmp_path: Path) -> None:
    root = tmp_path / "files"
    root.mkdir()
    (root / "small.txt").write_bytes(b"a")
    (root / "large.txt").write_bytes(b"abcdef")
    app = SimpleNamespace(
        config=Config(tmp_path / "config.json"),
        size_top_var=Value(1),
        size_mode_var=Value("最大"),
        size_tree=Tree(),
        size_summary_var=Value(""),
    )
    app.get_root = lambda: root
    app.extra_excludes = lambda: set()
    app.scan_tree = lambda folder, excludes: App.scan_tree(app, folder, excludes)
    app.trash_dir_for = lambda folder: folder / ".file_organizer_trash"
    app.run_task = lambda work, done, status: done(work())

    App.do_size_find(app)
    assert [info.name for info in app.size_files] == ["large.txt"]
    assert app.size_tree.rows["0"][2] == "85.7%"

    app.size_mode_var.set("最小")
    App.do_size_find(app)
    assert [info.name for info in app.size_files] == ["small.txt"]

    app.log_dir = tmp_path / "logs"
    app.size_tree.selection_set("0")
    app._size_selected = lambda: App._size_selected(app)
    app.confirm_destructive = lambda summary: True
    app.new_executor = lambda folder, dry_run=False: App.new_executor(app, folder, dry_run)
    app.show_outcome = lambda title, rows, actions: None
    app._outcome_rows = App._outcome_rows
    app.do_size_find = lambda: None
    App.do_size_trash(app)
    assert not (root / "small.txt").exists()
    assert (root / "large.txt").exists()
    assert UndoManager(app.log_dir).restore().restored == 1


def test_gui_empty_folder_cleanup_can_be_undone(tmp_path: Path) -> None:
    root = tmp_path / "files"
    empty = root / "empty"
    empty.mkdir(parents=True)
    app = SimpleNamespace(
        config=Config(tmp_path / "config.json"),
        log_dir=tmp_path / "logs",
        clean_tree=Tree(),
        clean_summary_var=Value(""),
        clean_dry_var=Value(False),
    )
    app.get_root = lambda: root
    app.extra_excludes = lambda: set()
    app.trash_dir_for = lambda folder: folder / ".file_organizer_trash"
    app.new_executor = lambda folder, dry_run=False: App.new_executor(app, folder, dry_run)
    app.run_task = lambda work, done, status: done(work())
    app._clean_done = lambda outcome: App._clean_done(app, outcome)
    app._outcome_rows = App._outcome_rows
    app.show_outcome = lambda title, rows, actions: None
    app.confirm_destructive = lambda summary: True
    app.log = lambda text: None

    App.do_clean_find(app)
    assert app.clean_dirs == [empty]
    assert app.clean_tree.selection() == ("0",)
    App.do_clean_execute(app)
    assert not empty.exists()
    assert UndoManager(app.log_dir).restore().restored == 1
    assert empty.is_dir()


def test_gui_duplicate_cleanup_keeps_one_copy_and_can_undo(tmp_path: Path) -> None:
    root = tmp_path / "files"
    root.mkdir()
    first = root / "first.txt"
    second = root / "second.txt"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    app = SimpleNamespace(
        config=Config(tmp_path / "config.json"),
        log_dir=tmp_path / "logs",
        dup_tree=Tree(),
        dup_summary_var=Value(""),
        dup_empty_var=Value(False),
        dup_dry_var=Value(False),
    )
    app.get_root = lambda: root
    app.extra_excludes = lambda: set()
    app.trash_dir_for = lambda folder: folder / ".file_organizer_trash"
    app.scan_tree = lambda folder, excludes: App.scan_tree(app, folder, excludes)
    app.set_status = lambda text: None
    app.log = lambda text: None
    app.run_task = lambda work, done, status: done(work())
    app._dup_refresh_group = lambda index: App._dup_refresh_group(app, index)
    app._dup_update_summary = lambda: App._dup_update_summary(app)
    app._dup_done = lambda outcome: App._dup_done(app, outcome)
    app._outcome_rows = App._outcome_rows
    app.new_executor = lambda folder, dry_run=False: App.new_executor(app, folder, dry_run)
    app.show_outcome = lambda title, rows, actions: None
    app.confirm_destructive = lambda summary: True

    App.do_dup_find(app)
    assert len(app.dup_groups) == 1
    assert len(app.dup_groups[0].files) == 2
    App.do_dup_execute(app)
    assert first.exists() != second.exists()
    assert UndoManager(app.log_dir).restore().restored == 1
    assert first.exists() and second.exists()


def test_gui_settings_are_saved_and_normalized(tmp_path: Path, monkeypatch) -> None:
    app = SimpleNamespace(config=Config(tmp_path / "config.json"))
    app.ai_vars = {
        "enabled": Value(False),
        "model": Value(" custom-model "),
        "base_url": Value(""),
        "api_key": Value(""),
        "api_key_env": Value("OPENAI_API_KEY"),
        "batch_size": Value("bad"),
        "max_files": Value("-3"),
        "timeout": Value("0"),
    }
    app.excl_names_var = Value("cache， builds")
    app.follow_var = Value(True)
    app.confirm_var = Value(False)
    app.use_ai_var = Value(True)
    app.log = lambda text: None
    app.apply_ai_settings = lambda: App.apply_ai_settings(app)
    app.load_settings = lambda: App.load_settings(app)
    monkeypatch.setattr(messagebox, "showinfo", lambda *args, **kwargs: None)

    App.save_settings(app)

    loaded = Config(app.config.path)
    assert loaded.get("ai.enabled") is False
    assert loaded.get("ai.model") == "custom-model"
    assert loaded.get("ai.batch_size") == 20
    assert loaded.get("ai.max_files") == 0
    assert loaded.get("ai.timeout") == 1
    assert loaded.excluded_names == ["cache", "builds"]
    assert loaded.get("scan.follow_symlinks") is True
    assert loaded.get("safety.require_delete_confirmation") is False


def test_gui_custom_rule_can_be_saved(tmp_path: Path, monkeypatch) -> None:
    app = SimpleNamespace(config=Config(tmp_path / "config.json"))
    app._rules_map = dict(DEFAULT_EXTENSION_MAP)
    app.categories_var = Value("Images, Custom")
    app.rule_ext_var = Value(" foo ")
    app.rule_cat_var = Value("Custom")
    app.rules_tree = Tree()
    app.rule_cat_box = SimpleNamespace(configure=lambda **kwargs: None)
    app.log = lambda text: None
    app._rules_redraw = lambda: App._rules_redraw(app)
    app.refresh_rules = lambda: App.refresh_rules(app)
    monkeypatch.setattr(messagebox, "showinfo", lambda *args, **kwargs: None)

    App._rule_apply(app)
    assert app._rules_map[".foo"] == "Custom"
    App._rules_save(app)

    loaded = Config(app.config.path)
    assert loaded.category_for_extension(".foo") == "Custom"
    assert "Custom" in loaded.categories


def test_gui_undo_rejects_other_folder_then_restores_selected_log(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "files"
    root.mkdir()
    source = root / "a.txt"
    source.write_text("x", encoding="utf-8")
    destination = root / "Documents" / "a.txt"
    config = Config(tmp_path / "config.json")
    log_dir = tmp_path / "logs"
    report = Executor(
        trash_dir=root / config.trash_dir_name,
        log_dir=log_dir,
        config=config,
    ).execute([PlannedAction(ACTION_MOVE, source, destination)])
    assert report.log_path is not None

    app = SimpleNamespace(
        config=config,
        log_dir=log_dir,
        undo_tree=Tree(),
        undo_logs=[report.log_path],
    )
    app.undo_tree.selection_set("0")
    app.get_root = lambda silent=False: tmp_path / "other"
    app._undo_manager = lambda: UndoManager(log_dir)
    app.refresh_undo_logs = lambda: None
    app.log = lambda text: None
    app.run_task = lambda work, done, status: done(work())
    warnings = []
    monkeypatch.setattr(messagebox, "showwarning", lambda *args, **kwargs: warnings.append(args))
    monkeypatch.setattr(messagebox, "showinfo", lambda *args, **kwargs: None)
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: True)

    App.do_undo(app, dry_run=False)
    assert warnings and destination.exists() and not source.exists()

    app.get_root = lambda silent=False: root
    App.do_undo(app, dry_run=True)
    assert destination.exists() and not source.exists()
    App.do_undo(app, dry_run=False)
    assert source.exists() and not destination.exists()


def test_gui_file_browser_lists_selected_folder(tmp_path: Path) -> None:
    root = tmp_path / "files"
    root.mkdir()
    (root / "note.txt").write_text("hello", encoding="utf-8")
    app = SimpleNamespace(
        browser_dir=root,
        browser_tree=Tree(),
        browser_location=Value(""),
    )
    app.run_task = lambda work, done, status: done(work())
    app._browser_show_text = lambda content: None

    App.browse_refresh(app)

    assert app.browser_paths == [root / "note.txt"]
    assert app.browser_tree.rows["0"] == ("note.txt", "文件", "5 B")
    assert "1 项" in app.browser_location.get()
