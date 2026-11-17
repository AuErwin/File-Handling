"""Headless checks for GUI handlers and their real file-operation pipeline."""
from __future__ import annotations

import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

from file_organizer.config import DEFAULT_EXTENSION_MAP, Config
from file_organizer.executor import Executor
from file_organizer.gui import App, messagebox, simpledialog
from file_organizer.models import ACTION_MOVE, PlannedAction
from file_organizer.storage import CacheStore
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
        self.cells = {}
        self.hit_row = ""

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
        self.cells[(iid, column)] = value

    def identify(self, region, x, y):
        return "cell"

    def identify_column(self, x):
        return "#1"

    def identify_row(self, y):
        return self.hit_row

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
    (root / ".git" / "cache").mkdir(parents=True)
    (root / "ignored" / "cache").mkdir(parents=True)
    app = SimpleNamespace(
        config=Config(tmp_path / "config.json"),
        log_dir=tmp_path / "logs",
        clean_tree=Tree(),
        clean_summary_var=Value(""),
        clean_dry_var=Value(False),
    )
    app.get_root = lambda: root
    app.extra_excludes = lambda: {"ignored"}
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
    button = SimpleNamespace(state="disabled")
    button.configure = lambda **kwargs: setattr(button, "state", kwargs["state"])
    app = SimpleNamespace(
        config=Config(tmp_path / "config.json"),
        log_dir=tmp_path / "logs",
        dup_tree=Tree(),
        dup_summary_var=Value(""),
        dup_empty_var=Value(False),
        dup_dry_var=Value(False),
        dup_execute_button=button,
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
    app._dup_parse = lambda iid: App._dup_parse(app, iid)
    app._dup_done = lambda outcome: App._dup_done(app, outcome)
    app._outcome_rows = App._outcome_rows
    app.new_executor = lambda folder, dry_run=False: App.new_executor(app, folder, dry_run)
    app.show_outcome = lambda title, rows, actions: None
    app.confirm_destructive = lambda summary: True

    App.do_dup_find(app)
    assert len(app.dup_groups) == 1
    assert len(app.dup_groups[0].files) == 2
    assert not app.dup_checked
    assert button.state == "disabled"
    App._dup_auto_select(app)
    assert app.dup_checked == {(0, 1)}
    assert button.state == "normal"
    assert "已选 1 个文件" in app.dup_summary_var.get()
    App._dup_clear_selection(app)
    assert not app.dup_checked
    assert button.state == "disabled"
    app.dup_tree.hit_row = "g0_1"
    assert App._dup_click(app, SimpleNamespace(x=0, y=0)) == "break"
    assert app.dup_checked == {(0, 1)}
    assert app.dup_tree.cells[("g0_1", "state")] == "☑"
    App._dup_select_all(app)
    assert len(app.dup_checked) == 2
    with pytest.MonkeyPatch.context() as patch:
        messages = []
        patch.setattr(messagebox, "showinfo", lambda *args, **kwargs: messages.append(args[1]))
        App.do_dup_execute(app)
    assert "至少保留一份" in messages[0]
    assert first.exists() and second.exists()
    App._dup_auto_select(app)
    App.do_dup_execute(app)
    assert first.exists() != second.exists()
    assert UndoManager(app.log_dir).restore().restored == 1
    assert first.exists() and second.exists()


def test_duplicate_cleanup_refuses_changed_content(tmp_path: Path) -> None:
    root = tmp_path / "files"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    app = SimpleNamespace(
        config=Config(tmp_path / "config.json"), log_dir=tmp_path / "logs",
        dup_tree=Tree(), dup_summary_var=Value(""), dup_empty_var=Value(False), dup_dry_var=Value(False),
    )
    app.get_root = lambda: root
    app.extra_excludes = lambda: set()
    app.trash_dir_for = lambda folder: folder / ".file_organizer_trash"
    app.scan_tree = lambda folder, excludes: App.scan_tree(app, folder, excludes)
    app.set_status = app.log = lambda text: None
    app.run_task = lambda work, done, status: done(work())
    app._dup_refresh_group = lambda index: App._dup_refresh_group(app, index)
    app._dup_update_summary = lambda: App._dup_update_summary(app)
    app._dup_done = lambda outcome: None
    app.new_executor = lambda folder, dry_run=False: App.new_executor(app, folder, dry_run)
    app.confirm_destructive = lambda summary: True
    App.do_dup_find(app)
    App._dup_auto_select(app)
    changed = app.dup_groups[0].files[1].path
    changed.write_bytes(b"DIFFERENT")

    with pytest.raises(RuntimeError, match="文件内容已变化"):
        App.do_dup_execute(app)
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
    # Only the openai provider's fields are visible in the test fixture (the
    # other two providers simply have nothing cached for them yet, exactly as
    # a first-time user who never touched them would leave it).
    app._current_ai_provider = "openai"
    app._ai_provider_cache = {}
    app.ai_provider_label_var = Value("")
    app.ai_model_combo = SimpleNamespace(configure=lambda **kwargs: None)
    app.ai_test_result_var = Value("")
    app.excl_names_var = Value("cache， builds")
    app.follow_var = Value(True)
    app.confirm_var = Value(False)
    app.use_ai_var = Value(True)
    app.log = lambda text: None
    app.apply_ai_settings = lambda: App.apply_ai_settings(app)
    app._read_provider_fields_from_vars = lambda: App._read_provider_fields_from_vars(app)
    app._write_provider_fields_to_vars = lambda pid: App._write_provider_fields_to_vars(app, pid)
    app.load_settings = lambda: App.load_settings(app)
    monkeypatch.setattr(messagebox, "showinfo", lambda *args, **kwargs: None)

    App.save_settings(app)

    loaded = Config(app.config.path)
    assert loaded.get("ai.enabled") is False
    assert loaded.get("ai.providers.openai.model") == "custom-model"
    assert loaded.get("ai.batch_size") == 20
    assert loaded.get("ai.max_files") == 0
    assert loaded.get("ai.timeout") == 1
    assert loaded.excluded_names == ["cache", "builds"]
    assert loaded.get("scan.follow_symlinks") is True


def test_gui_provider_switch_keeps_each_providers_fields(tmp_path: Path) -> None:
    """Switching the provider combobox must not discard the other provider's values."""
    app = SimpleNamespace(config=Config(tmp_path / "config.json"))
    app.ai_vars = {
        "model": Value(""), "base_url": Value(""), "api_key": Value(""), "api_key_env": Value(""),
    }
    app.ai_model_combo = SimpleNamespace(configure=lambda **kwargs: None)
    app.ai_test_result_var = Value("")
    app.ai_test_result_label = SimpleNamespace(configure=lambda **kwargs: None)
    app._label_to_provider = {"Claude（Anthropic）": "claude", "ChatGPT（OpenAI）": "openai", "DeepSeek": "deepseek"}
    app.ai_provider_label_var = Value("ChatGPT（OpenAI）")
    app._current_ai_provider = "openai"
    app._ai_provider_cache = {"openai": {}, "deepseek": {}, "claude": {}}
    app._read_provider_fields_from_vars = lambda: App._read_provider_fields_from_vars(app)
    app._write_provider_fields_to_vars = lambda pid: App._write_provider_fields_to_vars(app, pid)

    app.ai_vars["model"].set("gpt-4o")
    app.ai_vars["api_key"].set("sk-openai")
    app.ai_provider_label_var.set("Claude（Anthropic）")
    App._on_ai_provider_changed(app)

    assert app._current_ai_provider == "claude"
    assert app._ai_provider_cache["openai"]["model"] == "gpt-4o"
    assert app._ai_provider_cache["openai"]["api_key"] == "sk-openai"
    # Switching shows Claude's own (still empty / default) fields, not OpenAI's.
    assert app.ai_vars["api_key"].get() == ""


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


def test_gui_browser_navigates_and_previews_without_moving_files(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "files"
    folder = root / "docs"
    folder.mkdir(parents=True)
    file = folder / "note.txt"
    file.write_text("hello", encoding="utf-8")
    app = SimpleNamespace(
        browser_root=None,
        browser_dir=None,
        browser_tree=Tree(),
        browser_location=Value(""),
    )
    shown = Value("")
    app.get_root = lambda: root
    app.run_task = lambda work, done, status: done(work())
    app._browser_show_text = shown.set
    app._browser_selected = lambda: App._browser_selected(app)
    app.browse_refresh = lambda: App.browse_refresh(app)
    opened = []
    monkeypatch.setattr("file_organizer.gui.open_in_os", lambda path: opened.append(path))

    App.browse_root(app)
    assert app.browser_paths == [folder]
    app.browser_tree.selection_set("0")
    App.browse_preview(app)
    assert "文件夹" in shown.get()
    App.browse_enter(app)
    assert app.browser_dir == folder
    app.browser_tree.selection_set("0")
    App.browse_preview(app)
    assert "hello" in shown.get()
    App.browse_open(app)
    assert opened == [file]
    App.browse_up(app)
    assert app.browser_dir == root
    assert file.read_text(encoding="utf-8") == "hello"


def test_gui_task_queue_delivers_results_and_errors(monkeypatch) -> None:
    from file_organizer import gui

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(gui.threading, "Thread", ImmediateThread)
    errors = []
    monkeypatch.setattr(messagebox, "showerror", lambda *args, **kwargs: errors.append(args))
    app = SimpleNamespace(
        busy=False,
        events=queue.Queue(),
        status_var=Value(""),
        progress=SimpleNamespace(
            start=lambda delay: None, stop=lambda: None,
            pack=lambda **kwargs: None, pack_forget=lambda: None,
        ),
    )
    logs = []
    results = []
    app.log = logs.append
    app._append_log = logs.append
    app._finish_task = lambda done, result, error: App._finish_task(app, done, result, error)
    app._poll_events = lambda: App._poll_events(app)
    app.after = lambda delay, callback: None

    assert App.run_task(app, lambda: 42, results.append, "计算中")
    assert app.busy
    App._poll_events(app)
    assert results == [42]
    assert not app.busy and app.status_var.get() == "完成"

    def fail():
        raise ValueError("broken")

    assert App.run_task(app, fail, results.append, "再次计算")
    App._poll_events(app)
    assert app.status_var.get() == "失败"
    assert len(results) == 1
    assert errors and "broken" in errors[0][1]


def test_gui_undo_history_only_lists_current_folder(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config = Config(tmp_path / "config.json")
    log_dir = tmp_path / "logs"
    for root in (first, second):
        source = root / "note.txt"
        source.write_text("x", encoding="utf-8")
        report = Executor(
            trash_dir=root / config.trash_dir_name,
            log_dir=log_dir,
            config=config,
        ).execute([PlannedAction(ACTION_MOVE, source, root / "Documents" / "note.txt")])
        assert report.log_path is not None
    assert len(list(log_dir.glob("undo_log_*.json"))) == 2

    app = SimpleNamespace(
        config=config,
        log_dir=log_dir,
        undo_tree=Tree(),
        undo_detail=Tree(),
        undo_logs=[],
    )
    app.get_root = lambda silent=False: first
    app._undo_manager = lambda: UndoManager(log_dir)
    app._clear_undo_view = lambda: App._clear_undo_view(app)

    App.refresh_undo_logs(app)
    assert len(app.undo_logs) == 1
    assert app.undo_tree.selection() == ("0",)
    App._undo_show_detail(app)
    assert len(app.undo_detail.rows) == 1
    assert app.undo_detail.rows["0"][2] == str(first / "note.txt")


def test_gui_cache_clear_needs_confirmation(tmp_path: Path, monkeypatch) -> None:
    work_dir = tmp_path / "work"
    with CacheStore(work_dir / "ai_cache.db") as store:
        store.set("a", "Images")
    app = SimpleNamespace(work_dir=work_dir, cache_var=Value(""), cache_tree=Tree())
    messages = []
    app.log = messages.append
    app.refresh_cache_stats = lambda: App.refresh_cache_stats(app)
    App.refresh_cache_stats(app)
    assert "1 条" in app.cache_var.get()
    assert app.cache_tree.rows["0"][:2] == ("", "Images")

    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: False)
    App.clear_cache(app)
    with CacheStore(work_dir / "ai_cache.db") as store:
        assert store.get("a") == "Images"

    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: True)
    App.clear_cache(app)
    assert "0 条" in app.cache_var.get()
    assert not app.cache_tree.rows
    assert "已清除 1 条" in messages[0]
