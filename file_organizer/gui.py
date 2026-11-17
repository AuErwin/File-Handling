"""Tkinter 图形界面 —— 智能文件整理器。

启动方式::

    python main.py               # 放在 file_organizer 文件夹的上一级

界面使用 Tkinter，并直接复用业务模块：
Scanner / Classifier / DuplicateChecker / Cleaner / Executor / UndoManager。
所有耗时操作都在后台线程执行，界面不会卡死。
"""

from __future__ import annotations

import os
import codecs
import queue
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from file_organizer import ai_providers
from file_organizer.ai_providers import PROVIDER_INFO, PROVIDER_ORDER, ProviderError
from file_organizer.classifier import AIClassifier, Classifier
from file_organizer.cleaner import Cleaner
from file_organizer.config import DEFAULT_EXTENSION_MAP, Config
from file_organizer.duplicate_checker import DuplicateChecker
from file_organizer.executor import DELETE_TOKEN, Executor
from file_organizer.models import (
    ACTION_DELETE,
    ACTION_MOVE,
    CATEGORY_OTHERS,
    DuplicateGroup,
    FileInfo,
    PlannedAction,
    ScanResult,
)
from file_organizer.scanner import Scanner
from file_organizer.storage import CacheStore
from file_organizer.undo import UndoManager
from file_organizer.utils import chunked_hash, human_readable_size

APP_TITLE = "智能文件整理器"
CHECK_ON = "☑"
CHECK_OFF = "☐"
SOURCE_TEXT = {"rule": "规则", "ai": "AI", "cache": "缓存", "unknown": "未识别"}
PREVIEW_LIMIT = 64 * 1024
BG = "#ffffff"
SURFACE = "#ffffff"
FIELD = "#ffffff"
TEXT = "#202b3a"
MUTED = "#5f6b7a"
BORDER = "#cbd3dd"
ACCENT = "#176bc1"


def initial_window_geometry(screen_width: int, screen_height: int) -> str:
    """Open at a readable, centered size with room around the window."""
    width = min(max(1, screen_width - 40), max(960, int(screen_width * 0.68)))
    height = min(max(1, screen_height - 80), max(620, int(screen_height * 0.75)))
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    return f"{width}x{height}+{x}+{y}"


def make_menu(parent: tk.Misc) -> tk.Menu:
    return tk.Menu(
        parent, tearoff=False, background=SURFACE, foreground=TEXT,
        activebackground=ACCENT, activeforeground="#ffffff", selectcolor=TEXT,
        font=tkfont.nametofont("TkMenuFont"),
    )


def read_text_preview(path: Path) -> str:
    """Read a bounded UTF-8 preview without loading a large file into memory."""
    with path.open("rb") as handle:
        data = handle.read(PREVIEW_LIMIT + 1)
    if b"\x00" in data:
        return "这是二进制文件，无法在这里显示内容。请用系统应用打开。"
    try:
        decoder = codecs.getincrementaldecoder("utf-8-sig")()
        content = decoder.decode(data[:PREVIEW_LIMIT], final=len(data) <= PREVIEW_LIMIT)
    except UnicodeDecodeError:
        return "文件不是 UTF-8 文本，无法在这里显示内容。请用系统应用打开。"
    if len(data) > PREVIEW_LIMIT:
        content += "\n\n……仅显示前 64 KiB。"
    return content


def list_folder_entries(folder: Path) -> List[Tuple[Path, bool, str]]:
    """List a folder's immediate children, with directories first."""
    entries: List[Tuple[Path, bool, str]] = []
    with os.scandir(str(folder)) as iterator:
        for item in iterator:
            try:
                is_dir = item.is_dir(follow_symlinks=False)
                size = "" if is_dir else human_readable_size(item.stat(follow_symlinks=False).st_size)
                entries.append((Path(item.path), is_dir, size))
            except OSError:
                continue
    entries.sort(key=lambda row: (not row[1], row[0].name.casefold()))
    return entries


def log_belongs_to_root(payload: Dict[str, Any], root: Path, trash_name: str) -> bool:
    """Only expose undo logs produced for this exact source folder."""
    actions = payload.get("actions")
    trash = payload.get("trash_dir")
    if not isinstance(actions, list) or not actions or not isinstance(trash, str):
        return False
    try:
        base = root.resolve()
        if Path(trash).resolve() != (base / trash_name).resolve():
            return False
        for action in actions:
            if not isinstance(action, dict) or not action.get("source"):
                return False
            Path(str(action["source"])).resolve().relative_to(base)
    except (OSError, ValueError, RuntimeError):
        return False
    return True


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def open_in_os(path: Path, select: bool = False) -> None:
    """用系统文件管理器打开目录，或定位到某个文件。"""
    try:
        if sys.platform.startswith("win"):
            if select and path.exists():
                subprocess.Popen(["explorer", "/select," + str(path)])
            else:
                os.startfile(str(path if path.is_dir() else path.parent))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)] if select else ["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
    except Exception as exc:  # pragma: no cover - 取决于平台
        messagebox.showerror("无法打开", "%s" % exc)


class _TableTree(ttk.Treeview):
    """Keep full cell text while showing an ellipsis in narrow columns."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._full: Dict[str, Dict[str, str]] = {}
        self._font = tkfont.nametofont("TkDefaultFont")
        self._tip: Optional[tk.Toplevel] = None
        self._hover: Tuple[str, str] = ("", "")
        self.bind("<Motion>", self._show_full_cell, add="+")
        self.bind("<Motion>", self._block_column_resize, add="+")
        self.bind("<Leave>", self._hide_tip, add="+")
        self.bind("<ButtonPress>", self._hide_tip, add="+")
        self.bind("<ButtonPress-1>", self._block_column_resize, add="+")
        self.bind("<Double-1>", self._block_column_resize, add="+")

    def _shorten(self, value: str, column: str) -> str:
        width = self.column(column, "width") - 20
        if self._font.measure(value) <= width:
            return value
        lo, hi = 0, len(value)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._font.measure(value[:mid] + "…") <= width:
                lo = mid
            else:
                hi = mid - 1
        return value[:lo] + "…"

    def insert(self, parent: str, index: Any, iid: Optional[str] = None, **kwargs: Any) -> str:
        values = kwargs.get("values", ())
        columns = self["columns"]
        full = {col: str(value) for col, value in zip(columns, values)}
        if "text" in kwargs:
            full["#0"] = str(kwargs["text"])
            kwargs["text"] = self._shorten(full["#0"], "#0")
        kwargs["values"] = tuple(self._shorten(full[col], col) for col in columns if col in full)
        new_iid = super().insert(parent, index, iid=iid, **kwargs)
        self._full[new_iid] = full
        return new_iid

    def set(self, item: str, column: str, value: Any = None) -> Any:
        if value is None:
            stored = self._full.get(item, {}).get(column)
            return stored if stored is not None else super().set(item, column)
        full = str(value)
        self._full.setdefault(item, {})[column] = full
        return super().set(item, column, self._shorten(full, column))

    def item(self, item: str, **kwargs: Any) -> Any:
        if "text" in kwargs:
            full = str(kwargs["text"])
            self._full.setdefault(item, {})["#0"] = full
            kwargs["text"] = self._shorten(full, "#0")
        return super().item(item, **kwargs)

    def delete(self, *items: str) -> None:
        for item in items:
            self._full.pop(item, None)
            for child in self.get_children(item):
                self._full.pop(child, None)
        super().delete(*items)

    def _block_column_resize(self, event: Any) -> Optional[str]:
        if self.identify("region", event.x, event.y) == "separator":
            self.configure(cursor="arrow")
            return "break"
        return None

    def refresh_text(self) -> None:
        for iid, values in self._full.items():
            if not self.exists(iid):
                continue
            if "#0" in values:
                ttk.Treeview.item(self, iid, text=self._shorten(values["#0"], "#0"))
            for col, value in values.items():
                if col != "#0":
                    ttk.Treeview.set(self, iid, col, self._shorten(value, col))

    def _hide_tip(self, _event: Any = None) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None
        self._hover = ("", "")

    def _show_full_cell(self, event: Any) -> None:
        iid = self.identify_row(event.y)
        column = self.identify_column(event.x)
        columns = self["columns"]
        index = int(column[1:]) - 1 if column and column != "#0" else -1
        col_id = "#0" if column == "#0" else columns[index] if 0 <= index < len(columns) else ""
        target = (iid, col_id)
        if target == self._hover:
            return
        self._hide_tip()
        self._hover = target
        full = self._full.get(iid, {}).get(col_id, "")
        if not full or self._shorten(full, col_id) == full:
            return
        self._tip = tk.Toplevel(self)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry("+%d+%d" % (event.x_root + 12, event.y_root + 14))
        ttk.Label(self._tip, text=full, padding=5, wraplength=700).pack()


def make_tree(
    parent: tk.Misc,
    columns: Sequence[Tuple[str, str, int, str]],
    height: int = 10,
    selectmode: str = "extended",
    show: str = "headings",
    tree_heading: str = "",
    tree_width: int = 200,
    equal_columns: bool = False,
    expand_column: str = "",
) -> Tuple[ttk.Frame, _TableTree]:
    """创建列宽随容器调整的 Treeview。columns = (id, 标题, 宽度, 对齐)。

    所有列位于同一张表中；可选等宽或指定伸缩列，否则最后一列保持原宽度。
    """
    frame = ttk.Frame(parent)
    # The inner frame lets wide tables shrink to the available window width.
    container = ttk.Frame(frame, width=200)
    container.pack_propagate(False)
    main = _TableTree(container, columns=[c[0] for c in columns], show=show,
                      selectmode=selectmode, height=height)
    if "tree" in show:
        main.heading("#0", text=tree_heading.strip(), anchor="center")
        main.column("#0", width=tree_width, minwidth=1, stretch=False)
    for col_id, title, width, anchor in columns:
        main.heading(col_id, text=title.strip(), anchor="center")
        main.column(col_id, width=width, minwidth=1, anchor=anchor, stretch=False)

    vsb = ttk.Scrollbar(frame, orient="vertical", command=main.yview)
    main.configure(yscrollcommand=vsb.set)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    main.pack(fill="both", expand=True)
    container.grid(row=0, column=0, sticky="nsew")
    vsb.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")
    vsb.lift()

    layout = ([("#0", tree_heading, tree_width, "w")] if "tree" in show else []) + list(columns)
    fixed_width = columns[-1][2] if len(columns) > 1 and not equal_columns and not expand_column else 0
    flexible = layout[:-1] if fixed_width else layout
    grow_index = next(
        (i for i, (col, _title, width, anchor) in enumerate(flexible)
         if col != "#0" and anchor == "w" and width >= 120), 0,
    )
    heading_font = tkfont.nametofont("TkHeadingFont")
    minimums = [max(32, heading_font.measure(title.strip()) + 24)
                for _col, title, _width, _anchor in flexible]
    desired = [max(width, minimum) for (_col, _title, width, _anchor), minimum in zip(flexible, minimums)]
    minimum_total = sum(minimums)
    desired_total = sum(desired)

    def _fit_columns(event: Any) -> None:
        # The scrollbar covers the Treeview's right border; 2 px is its heading inset.
        available = max(1, event.width - vsb.winfo_reqwidth() + 2 - fixed_width)
        if expand_column:
            remaining = max(1, available - sum(
                width for col, _title, width, _anchor in layout if col != expand_column
            ))
            for col, _title, width, _anchor in layout:
                main.column(col, width=remaining if col == expand_column else width)
            main.refresh_text()
            return
        if equal_columns:
            widths = [available // len(flexible)] * len(flexible)
            widths[-1] += available % len(flexible)
        elif available >= desired_total:
            widths = desired.copy()
        elif available >= minimum_total:
            room = available - minimum_total
            widths = [minimum + room * (width - minimum) // (desired_total - minimum_total)
                      for width, minimum in zip(desired, minimums)]
        else:
            widths = [max(1, available * minimum // minimum_total) for minimum in minimums]
        widths[grow_index] += max(0, available - sum(widths))
        for (col_id, _title, _width, _anchor), width in zip(flexible, widths):
            main.column(col_id, width=width)
        if fixed_width:
            main.column(columns[-1][0], width=fixed_width)
        main.refresh_text()

    main.bind("<Configure>", _fit_columns, add="+")
    return frame, main


def clear_tree(tree: Any) -> None:
    tree.delete(*tree.get_children())


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    """整个图形界面。"""

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(initial_window_geometry(self.winfo_screenwidth(), self.winfo_screenheight()))
        self.minsize(min(960, self.winfo_screenwidth() - 40), min(620, self.winfo_screenheight() - 80))

        # ---- 后端对象 ----
        self.work_dir = Path.home() / ".file_organizer"
        self.config = Config(self.work_dir / "config.json")
        self.log_dir = self.work_dir / "undo_logs"

        # ---- 线程通信 ----
        self.events: "queue.Queue[Tuple[Any, ...]]" = queue.Queue()
        self.busy = False

        # ---- 各页状态 ----
        self.org_items: List[Tuple[PlannedAction, FileInfo]] = []
        self.org_checked: Dict[str, bool] = {}
        self.org_root: Optional[Path] = None
        self.dup_groups: List[DuplicateGroup] = []
        self.dup_checked: Set[Tuple[int, int]] = set()
        self.dup_root: Optional[Path] = None
        self.clean_dirs: List[Path] = []
        self.clean_root: Optional[Path] = None
        self.size_files: List[FileInfo] = []
        self.size_root: Optional[Path] = None
        self.browser_root: Optional[Path] = None
        self.browser_dir: Optional[Path] = None
        self.browser_paths: List[Path] = []

        self._setup_style()
        self._build_top_bar()
        self._build_bottom()  # 先占住底部空间，避免窗口较矮时日志区被挤掉
        self._build_notebook()
        self.root_var.trace_add("write", lambda *_args: self._clear_undo_view())

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(80, self._poll_events)
        self.log("就绪。工作目录：%s" % self.work_dir)

    # ------------------------------------------------------------------ #
    # 样式 / 菜单 / 公共区域
    # ------------------------------------------------------------------ #
    def _setup_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        self.configure(background=BG)
        families = set(tkfont.families())
        for candidate in ("Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", "WenQuanYi Micro Hei"):
            if candidate in families:
                for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                    tkfont.nametofont(name).configure(family=candidate)
                break
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(size=11)
        default_font = tkfont.nametofont("TkDefaultFont")
        tkfont.nametofont("TkHeadingFont").configure(weight="bold")
        style.configure(".", background=BG, foreground=TEXT, font=default_font)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Title.TLabel", font=(default_font.actual("family"), 12, "bold"))
        style.configure("Hint.TLabel", foreground=MUTED)
        style.configure("Success.TLabel", foreground="#18794e")
        style.configure("Danger.TLabel", foreground="#b42318")
        style.configure("TLabelframe", background=BG, bordercolor=BORDER)
        style.configure("TLabelframe.Label", background=BG, foreground=TEXT)
        style.configure("TButton", background="#e9eef4", foreground=TEXT, bordercolor=BORDER,
                        padding=(10, 5), relief="flat")
        style.map("TButton", background=[("disabled", "#edf0f3"), ("pressed", ACCENT),
                                         ("active", "#dce8f7")],
                  foreground=[("disabled", "#89939e"), ("pressed", "#ffffff")],
                  relief=[("pressed", "flat"), ("active", "flat")])
        style.layout("TButton", [
            ("Button.border", {"sticky": "nswe", "border": 1, "children": [
                ("Button.padding", {"sticky": "nswe", "children": [
                    ("Button.label", {"sticky": "nswe"}),
                ]}),
            ]}),
        ])
        style.configure("Danger.TButton", foreground="#b42318")
        style.configure("TMenubutton", background="#e9eef4", foreground=TEXT, bordercolor=BORDER,
                        padding=(10, 5), relief="flat")
        style.map("TMenubutton", background=[("pressed", ACCENT), ("active", "#dce8f7")],
                  foreground=[("pressed", "#ffffff")],
                  relief=[("pressed", "flat"), ("active", "flat")])
        style.configure("TopBar.TButton", padding=(4, 2))
        style.configure("TopBar.TMenubutton", padding=(4, 2), anchor="center")
        style.layout("TMenubutton", [
            ("Menubutton.border", {"sticky": "nswe", "children": [
                ("Menubutton.indicator", {"side": "right"}),
                ("Menubutton.padding", {"sticky": "nswe", "children": [
                    ("Menubutton.label", {"sticky": "nswe"}),
                ]}),
            ]}),
        ])
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, bordercolor=BORDER,
                        insertcolor=TEXT, selectbackground=ACCENT, selectforeground="#ffffff", padding=4)
        style.map("TEntry", fieldbackground=[("disabled", SURFACE)], foreground=[("disabled", MUTED)])
        style.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                        arrowcolor=TEXT, bordercolor=BORDER, selectbackground=ACCENT,
                        selectforeground="#ffffff", padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD), ("disabled", SURFACE)],
                  foreground=[("disabled", MUTED)])
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)

        def check_image(selected: bool, disabled: bool) -> tk.PhotoImage:
            image = tk.PhotoImage(master=self, width=16, height=16)
            border = "#aeb8c4" if disabled else (ACCENT if selected else BORDER)
            fill = "#d4d9df" if disabled and selected else (ACCENT if selected else FIELD)
            image.put(border, to=(0, 0, 16, 16))
            image.put(fill, to=(1, 1, 15, 15))
            if selected:
                for x, y in ((3, 8), (4, 9), (5, 10), (6, 9), (7, 8),
                             (8, 7), (9, 6), (10, 5), (11, 4)):
                    image.put("#ffffff", to=(x, y, x + 2, y + 2))
            return image

        unchecked = check_image(False, False)
        checked = check_image(True, False)
        unchecked_disabled = check_image(False, True)
        checked_disabled = check_image(True, True)
        self._checkbox_images = (unchecked, checked, unchecked_disabled, checked_disabled)
        style.configure("TCheckbutton", background=BG, foreground=TEXT, space=4)
        style.map("TCheckbutton", foreground=[("disabled", MUTED)])
        style.layout("TCheckbutton", [
            ("Checkbutton.padding", {"sticky": "nswe", "children": [
                ("Checkbutton.label", {"sticky": "w"}),
            ]}),
        ])
        style.configure("TNotebook", background=BG, bordercolor=BORDER)
        style.configure("TNotebook.Tab", background="#e9eef4", foreground=TEXT,
                        bordercolor=BORDER, padding=(12, 6), relief="flat")
        style.map("TNotebook.Tab", background=[("selected", ACCENT), ("active", "#dce8f7")],
                  foreground=[("selected", "#ffffff")],
                  bordercolor=[("selected", ACCENT)],
                  padding=[("selected", (12, 6)), ("active", (12, 6))],
                  relief=[("selected", "flat"), ("active", "flat")])
        style.layout("TNotebook.Tab", [
            ("Notebook.tab", {"sticky": "nswe", "children": [
                ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                    ("Notebook.label", {"side": "top"}),
                ]}),
            ]}),
        ])
        style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT,
                        borderwidth=0, rowheight=max(26, default_font.metrics("linespace") + 8))
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=SURFACE, foreground=TEXT, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, borderwidth=1, relief="solid")
        style.map("Treeview.Heading", background=[("active", "#dce8f7")])
        style.configure("TScrollbar", background="#c3ccd7", troughcolor=BG, arrowcolor=TEXT,
                        bordercolor=BORDER)
        style.configure("TProgressbar", background=ACCENT, troughcolor=SURFACE)

    def _checkbutton(self, parent: tk.Misc, **kwargs: Any) -> ttk.Checkbutton:
        unchecked, checked, unchecked_disabled, checked_disabled = self._checkbox_images
        return ttk.Checkbutton(
            parent, image=(str(unchecked), "disabled selected", str(checked_disabled),
                           "disabled", str(unchecked_disabled), "selected", str(checked)),
            compound="left", **kwargs,
        )

    def _build_menu(self) -> None:
        tools = make_menu(self.tools_button)
        tools.add_command(label="打开工作目录", command=lambda: open_in_os(self.work_dir))
        tools.add_command(label="打开撤销日志目录", command=lambda: self._open_dir_safe(self.log_dir))
        tools.add_command(label="打开当前目录的回收站", command=self._open_trash)
        tools.add_separator()
        tools.add_command(label="退出", command=self.destroy)
        self.tools_button["menu"] = tools

    def _open_dir_safe(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        open_in_os(path)

    def _open_trash(self) -> None:
        root = self.get_root(silent=True)
        if root is None:
            messagebox.showinfo("提示", "请先选择有效的目标文件夹。")
            return
        trash = self.trash_dir_for(root)
        if not trash.exists():
            messagebox.showinfo("提示", "该目录下还没有回收站：\n%s" % trash)
            return
        open_in_os(trash)

    def _build_top_bar(self) -> None:
        bar = ttk.Frame(self, padding=(10, 8, 10, 2))
        bar.pack(fill="x")
        ttk.Label(bar, text="目标文件夹：", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        downloads = Path.home() / "Downloads"
        self.root_var = tk.StringVar(value=str(downloads if downloads.is_dir() else Path.home()))
        ttk.Entry(bar, textvariable=self.root_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(bar, text="浏览…", width=6, style="TopBar.TButton",
                   command=lambda: self._browse(self.root_var)).grid(row=0, column=2)
        self.tools_button = ttk.Menubutton(bar, text="工具", width=6, style="TopBar.TMenubutton")
        self.tools_button.grid(row=0, column=3, padx=(6, 0))
        self._build_menu()
        ttk.Label(bar, text="额外排除的目录名（逗号分隔）：", style="Hint.TLabel").grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )
        self.exclude_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.exclude_var).grid(row=1, column=1, sticky="ew", padx=6, pady=(4, 0))
        bar.columnconfigure(1, weight=1)

    def _build_bottom(self) -> None:
        bottom = ttk.Frame(self, padding=(10, 0, 10, 8))
        bottom.pack(fill="x", side="bottom")
        status_row = ttk.Frame(bottom)
        status_row.pack(fill="x", pady=(4, 2))
        self.status_var = tk.StringVar(value="空闲")
        ttk.Label(status_row, textvariable=self.status_var).pack(side="left")
        # 只在真的有后台任务（扫描 / 生成预览 / 测试连接…）运行时才显示，
        # 空闲时收起来，避免在界面上留下一条看起来没用的占位横线。
        self.progress = ttk.Progressbar(status_row, mode="indeterminate", length=180)
        log_frame = ttk.Frame(bottom)
        log_frame.pack(fill="x")
        self.log_text = tk.Text(
            log_frame, height=2, wrap="word", state="disabled", relief="flat", borderwidth=0,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
            background=SURFACE, foreground=TEXT, insertbackground=TEXT, selectbackground=ACCENT,
            selectforeground="#ffffff", font=tkfont.nametofont("TkTextFont"),
        )
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="x", expand=True)
        log_scroll.pack(side="right", fill="y")

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=6)
        builders = [
            ("扫描概览", self._build_overview_tab),
            ("文件浏览", self._build_browser_tab),
            ("整理归类", self._build_organize_tab),
            ("重复文件", self._build_dedupe_tab),
            ("空文件夹", self._build_clean_tab),
            ("大 / 小文件", self._build_size_tab),
            ("撤销历史", self._build_undo_tab),
            ("分类规则", self._build_rules_tab),
            ("AI 缓存", self._build_cache_tab),
            ("设置", self._build_settings_tab),
        ]
        for title, builder in builders:
            frame = ttk.Frame(self.notebook, padding=8)
            self.notebook.add(frame, text=" %s " % title)
            builder(frame)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, _event: Any) -> None:
        text = self.notebook.tab(self.notebook.select(), "text").strip()
        if text == "撤销历史":
            self.refresh_undo_logs()
        elif text == "AI 缓存":
            self.refresh_cache_stats()

    # ------------------------------------------------------------------ #
    # 线程 / 日志 / 状态
    # ------------------------------------------------------------------ #
    def log(self, message: str) -> None:
        """线程安全地写日志（主线程直接写，子线程走队列）。"""
        if threading.current_thread() is threading.main_thread():
            self._append_log(message)
        else:
            self.events.put(("log", message))

    def _append_log(self, message: str) -> None:
        stamp = __import__("time").strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "[%s] %s\n" % (stamp, message))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_status(self, text: str) -> None:
        if threading.current_thread() is threading.main_thread():
            self.status_var.set(text)
        else:
            self.events.put(("status", text))

    def run_task(self, work: Callable[[], Any], done: Callable[[Any], None], text: str = "处理中…") -> bool:
        """在后台线程执行 ``work``，完成后在主线程调用 ``done(result)``。"""
        if self.busy:
            messagebox.showinfo("请稍候", "当前有任务正在运行，请等待完成后再操作。", parent=self)
            return False
        self.busy = True
        self.status_var.set(text)
        self.progress.pack(side="right")
        self.progress.start(12)
        self.log(text)

        def target() -> None:
            try:
                result = work()
                self.events.put(("done", done, result, None))
            except BaseException as exc:  # noqa: BLE001 - 需要把任何异常带回主线程
                self.events.put(("done", done, None, (exc, traceback.format_exc())))

        threading.Thread(target=target, daemon=True).start()
        return True

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._append_log(event[1])
                elif kind == "status":
                    self.status_var.set(event[1])
                elif kind == "done":
                    self._finish_task(event[1], event[2], event[3])
        except queue.Empty:
            pass
        self.after(80, self._poll_events)

    def _finish_task(self, done: Callable[[Any], None], result: Any, error: Any) -> None:
        self.busy = False
        self.progress.stop()
        self.progress.pack_forget()
        if error is not None:
            exc, trace = error
            self.status_var.set("失败")
            self._append_log("出错：%s: %s" % (type(exc).__name__, exc))
            self._append_log(trace.strip().splitlines()[-1] if trace else "")
            messagebox.showerror("操作失败", "%s: %s" % (type(exc).__name__, exc), parent=self)
            return
        self.status_var.set("完成")
        try:
            done(result)
        except Exception as exc:  # 回调中的错误也要可见
            self._append_log("界面更新出错：%s: %s" % (type(exc).__name__, exc))
            messagebox.showerror("界面更新失败", "%s: %s" % (type(exc).__name__, exc), parent=self)

    # ------------------------------------------------------------------ #
    # 公共辅助
    # ------------------------------------------------------------------ #
    def _browse(self, variable: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=variable.get() or str(Path.home()), parent=self)
        if chosen:
            variable.set(os.path.normpath(chosen))

    def get_root(self, silent: bool = False) -> Optional[Path]:
        text = self.root_var.get().strip()
        path = Path(text).expanduser() if text else None
        if path is None or not path.is_dir():
            if not silent:
                messagebox.showwarning("路径无效", "请选择一个存在的文件夹作为目标文件夹。", parent=self)
            return None
        return path

    def extra_excludes(self) -> Set[str]:
        return {item.strip() for item in self.exclude_var.get().replace("，", ",").split(",") if item.strip()}

    def trash_dir_for(self, root: Path) -> Path:
        return root / self.config.trash_dir_name

    def scan_tree(self, root: Path, excludes: Set[str], extra_paths: Optional[Set[Path]] = None) -> ScanResult:
        """在后台线程中调用：按统一规则扫描目录。"""
        exclude_paths: Set[str] = {str(self.trash_dir_for(root))}
        for item in extra_paths or set():
            exclude_paths.add(str(item))
        scanner = Scanner(
            root=root,
            config=self.config,
            exclude_names=excludes,
            exclude_paths=exclude_paths,
        )
        return scanner.scan()

    def new_executor(self, root: Path, dry_run: bool = False) -> Executor:
        return Executor(
            trash_dir=self.trash_dir_for(root),
            log_dir=self.log_dir,
            config=self.config,
            dry_run=dry_run,
        )

    def confirm_destructive(self, summary: str) -> bool:
        """删除类操作的二次确认；遵循 config 里 safety.require_delete_confirmation。"""
        if bool(self.config.get("safety.require_delete_confirmation", True)):
            answer = simpledialog.askstring(
                "危险操作确认",
                "%s\n\n文件只会被移入回收站，可在“撤销历史”中还原。\n请输入大写 %s 以继续："
                % (summary, DELETE_TOKEN),
                parent=self,
            )
            return answer is not None and answer.strip() == DELETE_TOKEN
        return messagebox.askyesno("确认", summary, parent=self)

    def show_outcome(self, title: str, rows: Sequence[Tuple[str, str]], actions: Sequence[PlannedAction] = ()) -> None:
        lines = ["%s：%s" % (label, value) for label, value in rows]
        failed = [item for item in actions if item.status == "failed"]
        if failed:
            lines.append("")
            lines.append("失败项（最多显示 8 条）：")
            for item in failed[:8]:
                lines.append("  %s\n    → %s" % (item.source, item.message))
        messagebox.showinfo(title, "\n".join(lines), parent=self)
        self.log("%s：%s" % (title, "，".join("%s %s" % (a, b) for a, b in rows if a in ("成功", "失败", "跳过"))))

    # ------------------------------------------------------------------ #
    # 文件浏览与文本预览
    # ------------------------------------------------------------------ #
    def _build_browser_tab(self, tab: ttk.Frame) -> None:
        controls = ttk.Frame(tab)
        controls.pack(fill="x")
        ttk.Button(controls, text="打开所选文件夹", command=self.browse_root).pack(side="left")
        ttk.Button(controls, text="上一级", command=self.browse_up).pack(side="left", padx=6)
        ttk.Button(controls, text="刷新", command=self.browse_refresh).pack(side="left")
        ttk.Button(controls, text="用系统应用打开", command=self.browse_open).pack(side="right")
        self.browser_location = tk.StringVar(value="选择上方的文件夹，点击“打开所选文件夹”。")
        ttk.Label(tab, textvariable=self.browser_location, style="Hint.TLabel").pack(fill="x", pady=6)

        panes = ttk.PanedWindow(tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        frame, self.browser_tree = make_tree(
            panes, [("name", "名称", 300, "w"), ("type", "类型", 90, "w"),
                    ("size", "大小", 100, "e")], height=16, selectmode="browse"
        )
        panes.add(frame, weight=3)
        self.browser_tree.bind("<<TreeviewSelect>>", self.browse_preview)
        self.browser_tree.bind("<Double-1>", self.browse_enter)
        preview_frame = ttk.Frame(panes)
        self.browser_text = tk.Text(
            preview_frame, wrap="word", state="disabled", relief="flat", borderwidth=0,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
            background=SURFACE, foreground=TEXT,
            insertbackground=TEXT, selectbackground=ACCENT, selectforeground="#ffffff",
            font=tkfont.nametofont("TkTextFont"),
        )
        scrollbar = ttk.Scrollbar(preview_frame, command=self.browser_text.yview)
        self.browser_text.configure(yscrollcommand=scrollbar.set)
        self.browser_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        panes.add(preview_frame, weight=2)
        panes.bind("<Configure>", lambda event: panes.sashpos(0, round(event.width * 0.6)))

    def _browser_show_text(self, content: str) -> None:
        self.browser_text.configure(state="normal")
        self.browser_text.delete("1.0", "end")
        self.browser_text.insert("1.0", content)
        self.browser_text.configure(state="disabled")

    def browse_root(self) -> None:
        root = self.get_root()
        if root is not None:
            self.browser_root = root
            self.browser_dir = root
            self.browse_refresh()

    def browse_up(self) -> None:
        if self.browser_dir is not None and self.browser_root is not None and self.browser_dir != self.browser_root:
            self.browser_dir = self.browser_dir.parent
            self.browse_refresh()

    def browse_refresh(self) -> None:
        folder = self.browser_dir
        if folder is None:
            self.browse_root()
            return

        def done(entries: List[Tuple[Path, bool, str]]) -> None:
            self.browser_paths = [path for path, _is_dir, _size in entries]
            clear_tree(self.browser_tree)
            for index, (path, is_dir, size) in enumerate(entries):
                self.browser_tree.insert("", "end", iid=str(index),
                                         values=(path.name, "文件夹" if is_dir else "文件", size))
            self.browser_location.set("%s（%d 项）" % (folder, len(entries)))
            self._browser_show_text("选中文件可预览 UTF-8 文本；双击文件夹可进入。")

        self.run_task(lambda: list_folder_entries(folder), done, "正在读取文件夹…")

    def _browser_selected(self) -> Optional[Path]:
        selected = self.browser_tree.selection()
        if not selected:
            return None
        index = int(selected[0])
        return self.browser_paths[index] if index < len(self.browser_paths) else None

    def browse_preview(self, _event: Any = None) -> None:
        path = self._browser_selected()
        if path is None:
            return
        if path.is_dir():
            self._browser_show_text("文件夹：%s\n\n双击进入。" % path)
            return
        try:
            self._browser_show_text("%s\n大小：%s\n\n%s" % (
                path, human_readable_size(path.stat().st_size), read_text_preview(path)
            ))
        except OSError as exc:
            self._browser_show_text("无法读取文件：%s" % exc)

    def browse_enter(self, _event: Any = None) -> None:
        path = self._browser_selected()
        if path is not None and path.is_dir():
            self.browser_dir = path
            self.browse_refresh()

    def browse_open(self) -> None:
        path = self._browser_selected()
        if path is not None:
            open_in_os(path)

    # 页签 1：扫描概览
    # ------------------------------------------------------------------ #
    def _build_overview_tab(self, tab: ttk.Frame) -> None:
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Button(row, text="开始扫描", command=self.do_scan).pack(side="left")
        ttk.Label(row, text="统计文件数量、总大小以及各扩展名分布，不会改动任何文件。", style="Hint.TLabel").pack(
            side="left", padx=10
        )
        panes = ttk.Frame(tab)
        panes.pack(fill="both", expand=True, pady=(8, 0))
        left_frame, self.scan_summary = make_tree(
            panes, [("metric", "指标", 160, "w"), ("value", "数值", 220, "w")], height=10, selectmode="none"
        )
        right_frame, self.scan_ext = make_tree(
            panes, [("ext", "扩展名", 140, "w"), ("count", "文件数", 90, "e"), ("size", "总大小", 120, "e")],
            height=10, selectmode="none",
        )
        left_frame.pack(side="left", fill="both", expand=True, padx=(0, 6))
        right_frame.pack(side="left", fill="both", expand=True)

    def do_scan(self) -> None:
        root = self.get_root()
        if root is None:
            return
        excludes = self.extra_excludes()
        self.run_task(lambda: self.scan_tree(root, excludes), self._scan_done, "正在扫描 %s …" % root)

    def _scan_done(self, result: ScanResult) -> None:
        clear_tree(self.scan_summary)
        clear_tree(self.scan_ext)
        rows = [
            ("根目录", str(result.root)),
            ("文件数", "{:,}".format(len(result.files))),
            ("访问目录数", "{:,}".format(result.scanned_dirs)),
            ("总大小", result.total_size_text),
            ("耗时", "%.2f 秒" % result.elapsed),
            ("无法访问的目录", len(result.skipped_dirs)),
            ("无法访问的文件", len(result.skipped_files)),
            ("被排除的目录", len(result.excluded_dirs)),
        ]
        for label, value in rows:
            self.scan_summary.insert("", "end", values=(label, value))
        buckets: Dict[str, List[int]] = {}
        for info in result.files:
            key = info.extension or "(无扩展名)"
            entry = buckets.setdefault(key, [0, 0])
            entry[0] += 1
            entry[1] += info.size
        for ext, (count, size) in sorted(buckets.items(), key=lambda kv: (-kv[1][0], kv[0]))[:200]:
            self.scan_ext.insert("", "end", values=(ext, "{:,}".format(count), human_readable_size(size)))
        self.log("扫描完成：%d 个文件，%s" % (len(result.files), result.total_size_text))
        for path, reason in result.skipped_dirs[:5]:
            self.log("  跳过目录 %s（%s）" % (path, reason))

    # ------------------------------------------------------------------ #
    # 页签 2：整理归类
    # ------------------------------------------------------------------ #
    def _build_organize_tab(self, tab: ttk.Frame) -> None:
        opts = ttk.Frame(tab)
        opts.pack(fill="x")
        ttk.Label(opts, text="整理到（留空＝原目录）：").grid(row=0, column=0, sticky="w")
        self.dest_var = tk.StringVar()
        ttk.Entry(opts, textvariable=self.dest_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(opts, text="浏览…", command=lambda: self._browse(self.dest_var)).grid(row=0, column=2)
        opts.columnconfigure(1, weight=1)

        flags = ttk.Frame(tab)
        flags.pack(fill="x", pady=(6, 0))
        self.use_ai_var = tk.BooleanVar(value=bool(self.config.get("ai.enabled", True)))
        self._checkbutton(flags, text="启用 AI 补充分类（只发送文件名）", variable=self.use_ai_var).pack(side="left")
        self.org_dry_var = tk.BooleanVar(value=False)
        self._checkbutton(flags, text="仅模拟（不真正移动）", variable=self.org_dry_var).pack(side="left", padx=14)
        ttk.Button(flags, text="生成预览", command=self.do_org_plan).pack(side="left", padx=(14, 0))
        ttk.Button(flags, text="运行选项", command=self.do_org_execute).pack(side="left", padx=6)

        self.org_summary_var = tk.StringVar(value="点击“生成预览”查看将要移动哪些文件。点击最左列的方框可勾选 / 取消。")
        ttk.Label(tab, textvariable=self.org_summary_var, style="Hint.TLabel", wraplength=1050, justify="left").pack(
            fill="x", pady=6
        )
        frame, self.org_tree = make_tree(
            tab,
            [
                ("chk", "选", 40, "center"),
                ("name", "文件名", 230, "w"),
                ("category", "分类", 100, "w"),
                ("source", "来源", 60, "center"),
                ("size", "大小", 90, "e"),
                ("src", "原位置", 280, "w"),
                ("dst", "目标位置", 320, "w"),
            ],
            height=14,
        )
        frame.pack(fill="both", expand=True)
        self.org_tree.bind("<Button-1>", self._org_click, add="+")

    def do_org_plan(self) -> None:
        root = self.get_root()
        if root is None:
            return
        dest_text = self.dest_var.get().strip()
        dest = Path(dest_text).expanduser() if dest_text else root
        use_ai = bool(self.use_ai_var.get())
        excludes = self.extra_excludes()

        def work() -> Tuple[ScanResult, Any, List[Tuple[PlannedAction, FileInfo]]]:
            exclude_paths: Set[Path] = {self.trash_dir_for(root)}
            if dest != root:
                exclude_paths.add(dest)
            for category in self.config.categories:
                exclude_paths.add(dest / category)
            result = self.scan_tree(root, excludes, exclude_paths)
            self.set_status("正在分类 %d 个文件…" % len(result.files))
            with CacheStore(self.work_dir / "ai_cache.db") as cache:  # 在工作线程中创建 SQLite 连接
                report = Classifier(config=self.config, cache=cache).classify_all(
                    result.files, use_ai=use_ai
                )
            items: List[Tuple[PlannedAction, FileInfo]] = []
            for info in result.files:
                category = info.category or CATEGORY_OTHERS
                target_dir = dest / category
                target_file = target_dir / info.name
                try:
                    same_folder = os.path.normcase(os.path.abspath(str(info.parent))) == os.path.normcase(
                        os.path.abspath(str(target_dir))
                    )
                except (OSError, ValueError):
                    same_folder = False
                if same_folder and os.path.normcase(str(info.path)) == os.path.normcase(str(target_file)):
                    continue
                action = PlannedAction(
                    action=ACTION_MOVE,
                    source=info.path,
                    destination=target_file,
                    reason="category: %s (%s)" % (category, info.classification_source),
                )
                items.append((action, info))
            return result, report, items

        def done(payload: Tuple[ScanResult, Any, List[Tuple[PlannedAction, FileInfo]]]) -> None:
            result, report, items = payload
            self.org_root = root
            self.org_items = items
            clear_tree(self.org_tree)
            self.org_checked = {}
            counts: Dict[str, int] = {}
            for index, (action, info) in enumerate(items):
                iid = str(index)
                self.org_checked[iid] = True
                category = info.category or CATEGORY_OTHERS
                counts[category] = counts.get(category, 0) + 1
                self.org_tree.insert(
                    "", "end", iid=iid,
                    values=(
                        CHECK_ON, info.name, category, SOURCE_TEXT.get(info.classification_source, "?"),
                        info.size_text, str(info.parent), str(action.destination),
                    ),
                )
                if index and index % 3000 == 0:
                    self.update_idletasks()
            if not result.files:
                self.org_summary_var.set("目录下没有找到文件。")
            elif not items:
                self.org_summary_var.set("共 %d 个文件，全部已经在正确的分类文件夹里，无需整理。" % len(result.files))
            else:
                dist = "　".join("%s %d" % (k, v) for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
                self.org_summary_var.set(
                    "扫描 %d 个文件｜规则 %d｜AI %d｜缓存 %d｜未识别(Others) %d｜待移动 %d\n分布：%s"
                    % (len(result.files), report.rule, report.ai, report.cache, report.unresolved, len(items), dist)
                )
            self.log("预览已生成：%d 项待移动" % len(items))

        self.run_task(work, done, "正在扫描并分类…")

    def _org_click(self, event: Any) -> Optional[str]:
        if self.org_tree.identify("region", event.x, event.y) != "cell":
            return None
        if self.org_tree.identify_column(event.x) != "#1":
            return None
        iid = self.org_tree.identify_row(event.y)
        if not iid:
            return None
        self._org_set(iid, not self.org_checked.get(iid, False))
        return "break"

    def _org_set(self, iid: str, value: bool) -> None:
        self.org_checked[iid] = value
        self.org_tree.set(iid, "chk", CHECK_ON if value else CHECK_OFF)

    def do_org_execute(self) -> None:
        if not self.org_items or self.org_root is None:
            messagebox.showinfo("提示", "请先点击“生成预览”。", parent=self)
            return
        selected = [action for index, (action, _info) in enumerate(self.org_items) if self.org_checked.get(str(index))]
        if not selected:
            messagebox.showinfo("提示", "没有勾选任何文件。", parent=self)
            return
        dry = bool(self.org_dry_var.get())
        if not messagebox.askyesno(
            "确认整理",
            "将%s %d 个文件。\n同名文件不会被覆盖（自动加时间戳后缀），且可在“撤销历史”中还原。\n\n继续吗？"
            % ("模拟移动" if dry else "移动", len(selected)),
            parent=self,
        ):
            return
        executor = self.new_executor(self.org_root, dry_run=dry)
        self.run_task(lambda: executor.execute(selected), self._org_done, "正在移动文件…")

    def _org_done(self, outcome: Any) -> None:
        self.show_outcome("整理结果", self._outcome_rows(outcome), outcome.actions)
        self.org_items = []
        self.org_checked = {}
        clear_tree(self.org_tree)
        self.org_summary_var.set("已执行。需要的话可到“撤销历史”还原；也可以重新生成预览。")

    @staticmethod
    def _outcome_rows(outcome: Any) -> List[Tuple[str, str]]:
        labels = {"Succeeded": "成功", "Failed": "失败", "Skipped": "跳过", "Moved": "移动数据量",
                  "Trashed": "移入回收站", "Undo log": "撤销日志"}
        return [(labels.get(a, a), b) for a, b in outcome.as_rows()]

    # ------------------------------------------------------------------ #
    # 页签 3：重复文件
    # ------------------------------------------------------------------ #
    def _build_dedupe_tab(self, tab: ttk.Frame) -> None:
        top = ttk.Frame(tab)
        top.pack(fill="x")
        self.dup_empty_var = tk.BooleanVar(value=False)
        self.dup_dry_var = tk.BooleanVar(value=False)
        ttk.Button(top, text="扫描重复文件", command=self.do_dup_find).pack(side="left")
        choose = ttk.Menubutton(top, text="选择方式")
        choose_menu = make_menu(choose)
        choose_menu.add_command(label="每组保留最新，选其余副本", command=self._dup_auto_select)
        choose_menu.add_command(label="全选文件", command=self._dup_select_all)
        choose_menu.add_command(label="取消选择", command=self._dup_clear_selection)
        choose["menu"] = choose_menu
        choose.pack(side="left", padx=6)
        more = ttk.Menubutton(top, text="更多")
        more_menu = make_menu(more)
        more_menu.add_checkbutton(label="包含 0 字节文件", variable=self.dup_empty_var)
        more_menu.add_checkbutton(label="仅模拟处理", variable=self.dup_dry_var)
        more_menu.add_command(label="定位所选文件", command=self._dup_reveal)
        more_menu.configure(postcommand=lambda: more_menu.entryconfigure(
            "定位所选文件", state="normal" if self.dup_tree.selection() or self.dup_checked else "disabled"
        ))
        more["menu"] = more_menu
        more.pack(side="left")
        self.dup_execute_button = ttk.Button(
            top, text="处理所选文件", style="Danger.TButton", command=self.do_dup_execute, state="disabled"
        )
        self.dup_execute_button.pack(side="right")
        self.dup_summary_var = tk.StringVar(value="点击“查找重复文件”（先按大小分组，再比对 SHA-256）。")
        ttk.Label(tab, textvariable=self.dup_summary_var, style="Hint.TLabel").pack(fill="x", pady=6)

        frame, self.dup_tree = make_tree(
            tab,
            [
                ("state", "选择", 70, "center"),
                ("path", "路径", 390, "w"),
                ("size", "大小", 85, "e"),
                ("mtime", "修改时间", 155, "center"),
            ],
            height=14, show="tree headings", tree_heading="分组", tree_width=210,
        )
        frame.pack(fill="both", expand=True)
        self.dup_tree.tag_configure("trash", foreground="#b42318")
        self.dup_tree.bind("<Button-1>", self._dup_click, add="+")

    def do_dup_find(self) -> None:
        root = self.get_root()
        if root is None:
            return
        excludes = self.extra_excludes()
        include_empty = bool(self.dup_empty_var.get())

        def work() -> Tuple[ScanResult, List[DuplicateGroup]]:
            result = self.scan_tree(root, excludes)
            self.set_status("正在比对 %d 个文件的内容…" % len(result.files))
            groups = DuplicateChecker(result.files, include_empty=include_empty).find()
            return result, groups

        def done(payload: Tuple[ScanResult, List[DuplicateGroup]]) -> None:
            result, groups = payload
            self.dup_root = root
            self.dup_groups = groups
            self.dup_checked: Set[Tuple[int, int]] = set()
            clear_tree(self.dup_tree)
            for index, group in enumerate(groups):
                gid = "g%d" % index
                self.dup_tree.insert("", "end", iid=gid, text="", open=len(groups) <= 150,
                                     values=("", "", "", ""))
                for pos, info in enumerate(group.files):
                    self.dup_tree.insert(
                        gid, "end", iid="g%d_%d" % (index, pos), text="",
                        values=("", str(info.path), info.size_text, info.mtime_text),
                    )
                self._dup_refresh_group(index)
            self.log("重复文件查找完成：%d 组" % len(groups))
            self._dup_update_summary()

        self.run_task(work, done, "正在查找重复文件…")

    def _dup_refresh_group(self, index: int) -> None:
        group = self.dup_groups[index]
        gid = "g%d" % index
        label = "第 %d 组 · %d 份 · %s" % (index + 1, len(group.files), human_readable_size(group.size))
        self.dup_tree.item(gid, text=label)
        for pos in range(len(group.files)):
            cid = "g%d_%d" % (index, pos)
            checked = (index, pos) in self.dup_checked
            self.dup_tree.set(cid, "state", CHECK_ON if checked else CHECK_OFF)
            self.dup_tree.item(cid, tags=("trash",) if checked else ())

    def _dup_update_summary(self) -> None:
        selected = len(self.dup_checked)
        button = getattr(self, "dup_execute_button", None)
        if button is not None:
            button.configure(state="normal" if selected else "disabled")
        if not self.dup_groups:
            self.dup_summary_var.set("没有发现重复文件。")
            return
        stats = DuplicateChecker.summarize(self.dup_groups)
        bytes_selected = sum(self.dup_groups[g].files[p].size for g, p in self.dup_checked)
        unsafe = any(all((g, p) in self.dup_checked for p in range(len(group.files)))
                     for g, group in enumerate(self.dup_groups))
        self.dup_summary_var.set(
            "共 %d 组重复｜已选 %d 个文件｜清空回收站后可释放 %s。点击文件前的方框可调整。%s"
            % (stats["groups"], selected, human_readable_size(bytes_selected),
               "每组至少须留一份，请取消部分选择。" if unsafe else "处理时默认移入程序回收站。")
        )

    def _dup_parse(self, iid: str) -> Tuple[int, Optional[int]]:
        body = iid[1:]
        if "_" in body:
            g, p = body.split("_", 1)
            return int(g), int(p)
        return int(body), None

    def _dup_click(self, event: Any) -> Optional[str]:
        if (self.dup_tree.identify("region", event.x, event.y) != "cell"
                or self.dup_tree.identify_column(event.x) != "#1"):
            return None
        iid = self.dup_tree.identify_row(event.y)
        if not iid or "_" not in iid:
            return None
        g, p = self._dup_parse(iid)
        key = (g, p)
        if key in self.dup_checked:
            self.dup_checked.remove(key)
        else:
            self.dup_checked.add(key)
        self._dup_refresh_group(g)
        self._dup_update_summary()
        return "break"

    def _dup_auto_select(self) -> None:
        self.dup_checked = {(g, p) for g, group in enumerate(self.dup_groups)
                            for p in range(1, len(group.files))}
        for index in range(len(self.dup_groups)):
            self._dup_refresh_group(index)
        self._dup_update_summary()

    def _dup_select_all(self) -> None:
        self.dup_checked = {(g, p) for g, group in enumerate(self.dup_groups)
                            for p in range(len(group.files))}
        for index in range(len(self.dup_groups)):
            self._dup_refresh_group(index)
        self._dup_update_summary()

    def _dup_clear_selection(self) -> None:
        self.dup_checked.clear()
        for index in range(len(self.dup_groups)):
            self._dup_refresh_group(index)
        self._dup_update_summary()

    def _dup_reveal(self) -> None:
        picked = self.dup_tree.selection() or tuple("g%d_%d" % key for key in sorted(self.dup_checked))
        for iid in picked:
            g, p = self._dup_parse(iid)
            if p is not None:
                open_in_os(Path(self.dup_groups[g].files[p].path), select=True)
                return

    def do_dup_execute(self) -> None:
        if not self.dup_groups or self.dup_root is None:
            messagebox.showinfo("提示", "请先查找重复文件。", parent=self)
            return
        if not self.dup_checked:
            messagebox.showinfo("提示", "请先勾选要处理的文件。", parent=self)
            return
        actions: List[PlannedAction] = []
        checks: List[Tuple[DuplicateGroup, FileInfo, List[FileInfo]]] = []
        selected_bytes = 0
        for index, group in enumerate(self.dup_groups):
            selected = [p for p in range(len(group.files)) if (index, p) in self.dup_checked]
            if not selected:
                continue
            remaining = [info for p, info in enumerate(group.files) if p not in selected]
            if not remaining:
                messagebox.showinfo("提示", "第 %d 组全部勾选了，请至少保留一份。" % (index + 1), parent=self)
                return
            keeper = remaining[0]
            selected_files = [group.files[pos] for pos in selected]
            checks.append((group, keeper, selected_files))
            selected_bytes += sum(info.size for info in selected_files)
            for pos in selected:
                info = group.files[pos]
                actions.append(
                    PlannedAction(
                        action=ACTION_DELETE,
                        source=Path(info.path),
                        reason="duplicate of %s (sha256:%s)" % (keeper.path, group.short_hash),
                    )
                )
        dry = bool(self.dup_dry_var.get())
        if not dry and not self.confirm_destructive(
            "将有 %d 个重复文件（%s）被移入回收站，每组至少保留一份。"
            % (len(actions), human_readable_size(selected_bytes))
        ):
            return
        executor = self.new_executor(self.dup_root, dry_run=dry)

        def work() -> Any:
            for group, keeper, selected_files in checks:
                if chunked_hash(keeper.path) != group.hash_value or any(
                    chunked_hash(info.path) != group.hash_value for info in selected_files
                ):
                    raise RuntimeError("文件内容已变化，请重新扫描重复文件后再处理。")
            return executor.execute(actions)

        self.run_task(work, self._dup_done, "正在处理重复文件…")

    def _dup_done(self, outcome: Any) -> None:
        self.show_outcome("重复文件处理结果", self._outcome_rows(outcome), outcome.actions)
        self.dup_groups = []
        self.dup_checked = set()
        button = getattr(self, "dup_execute_button", None)
        if button is not None:
            button.configure(state="disabled")
        clear_tree(self.dup_tree)
        self.dup_summary_var.set("已处理。可重新查找确认结果，或到“撤销历史”还原。")

    # ------------------------------------------------------------------ #
    # 页签 4：空文件夹
    # ------------------------------------------------------------------ #
    def _build_clean_tab(self, tab: ttk.Frame) -> None:
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Button(top, text="查找空文件夹", command=self.do_clean_find).pack(side="left")
        self.clean_dry_var = tk.BooleanVar(value=False)
        self._checkbutton(top, text="仅模拟", variable=self.clean_dry_var).pack(side="left", padx=12)
        ttk.Button(top, text="全选", command=lambda: self.clean_tree.selection_set(self.clean_tree.get_children())).pack(
            side="left", padx=(12, 0)
        )
        ttk.Button(top, text="删除所选空文件夹", style="Danger.TButton", command=self.do_clean_execute).pack(side="right")
        self.clean_summary_var = tk.StringVar(value="空文件夹＝其中没有任何文件（嵌套的空文件夹链也会一并列出）。")
        ttk.Label(tab, textvariable=self.clean_summary_var, style="Hint.TLabel").pack(fill="x", pady=6)
        frame, self.clean_tree = make_tree(tab, [("path", "路径", 900, "w")], height=14)
        frame.pack(fill="both", expand=True)

    def do_clean_find(self) -> None:
        root = self.get_root()
        if root is None:
            return
        excludes = self.extra_excludes() | set(self.config.excluded_names)
        protected = {str(root), str(self.trash_dir_for(root))}

        def work() -> List[Path]:
            return Cleaner().find_empty_dirs(root, protected=protected, exclude_names=excludes)

        def done(dirs: List[Path]) -> None:
            self.clean_root = root
            self.clean_dirs = dirs
            clear_tree(self.clean_tree)
            for index, path in enumerate(dirs):
                self.clean_tree.insert("", "end", iid=str(index), values=(str(path),))
            self.clean_tree.selection_set(self.clean_tree.get_children())
            self.clean_summary_var.set(
                "找到 %d 个空文件夹（已默认全选）。" % len(dirs) if dirs else "没有发现空文件夹。"
            )
            self.log("空文件夹查找完成：%d 个" % len(dirs))

        self.run_task(work, done, "正在查找空文件夹…")

    def do_clean_execute(self) -> None:
        if not self.clean_dirs or self.clean_root is None:
            messagebox.showinfo("提示", "请先查找空文件夹。", parent=self)
            return
        picked = sorted(int(i) for i in self.clean_tree.selection())  # 保持“最深的在前”的原始顺序
        if not picked:
            messagebox.showinfo("提示", "没有选中任何文件夹。", parent=self)
            return
        dry = bool(self.clean_dry_var.get())
        if not dry and not self.confirm_destructive("将删除 %d 个空文件夹。" % len(picked)):
            return
        actions = Cleaner().plan_dir_removal([self.clean_dirs[i] for i in picked])
        executor = self.new_executor(self.clean_root, dry_run=dry)
        self.run_task(lambda: executor.execute(actions), self._clean_done, "正在删除空文件夹…")

    def _clean_done(self, outcome: Any) -> None:
        self.show_outcome("清理结果", self._outcome_rows(outcome), outcome.actions)
        self.clean_dirs = []
        clear_tree(self.clean_tree)
        self.clean_summary_var.set("已处理。可重新查找确认。")

    # ------------------------------------------------------------------ #
    # 页签 5：大 / 小文件
    # ------------------------------------------------------------------ #
    def _build_size_tab(self, tab: ttk.Frame) -> None:
        top = ttk.Frame(tab)
        top.pack(fill="x")
        self.size_mode_var = tk.StringVar(value="最大")
        self._checkbutton(top, text="最大的文件", variable=self.size_mode_var,
                          onvalue="最大", offvalue="最大").pack(side="left")
        self._checkbutton(top, text="最小的文件", variable=self.size_mode_var,
                          onvalue="最小", offvalue="最小").pack(side="left", padx=8)
        ttk.Label(top, text="数量：").pack(side="left", padx=(12, 0))
        self.size_top_var = tk.IntVar(value=int(self.config.get("clean.top_large_files", 10) or 10))
        ttk.Spinbox(top, from_=1, to=5000, width=7, textvariable=self.size_top_var).pack(side="left")
        ttk.Button(top, text="查找", command=self.do_size_find).pack(side="left", padx=12)
        ttk.Button(top, text="把所选移入回收站", style="Danger.TButton", command=self.do_size_trash).pack(side="right")
        ttk.Button(top, text="定位所选", command=self._size_reveal).pack(side="right", padx=6)
        self.size_summary_var = tk.StringVar(value="双击一行可在文件管理器中定位该文件。")
        ttk.Label(tab, textvariable=self.size_summary_var, style="Hint.TLabel").pack(fill="x", pady=6)
        frame, self.size_tree = make_tree(
            tab,
            [
                ("rank", "#", 50, "e"),
                ("size", "大小", 100, "e"),
                ("share", "目录占比", 100, "e"),
                ("mtime", "修改时间", 150, "center"),
                ("path", "路径", 560, "w"),
            ],
            height=14, expand_column="path",
        )
        frame.pack(fill="both", expand=True)
        self.size_tree.bind("<Double-1>", lambda _e: self._size_reveal())

    def do_size_find(self) -> None:
        root = self.get_root()
        if root is None:
            return
        try:
            top = max(1, int(self.size_top_var.get()))
        except (tk.TclError, ValueError):
            top = 10
        largest = self.size_mode_var.get() == "最大"
        excludes = self.extra_excludes()

        def work() -> Tuple[List[FileInfo], int, int]:
            result = self.scan_tree(root, excludes)
            if largest:
                picked = sorted(result.files, key=lambda i: (-i.size, str(i.path)))[:top]
            else:
                picked = sorted(result.files, key=lambda i: (i.size, str(i.path)))[:top]
            return picked, len(result.files), result.total_size

        def done(payload: Tuple[List[FileInfo], int, int]) -> None:
            files, total, total_size = payload
            self.size_root = root
            self.size_files = files
            clear_tree(self.size_tree)
            for index, info in enumerate(files):
                share = "%.1f%%" % (100.0 * info.size / total_size) if total_size else "0.0%"
                if info.size and share == "0.0%":
                    share = "<0.1%"
                self.size_tree.insert(
                    "", "end", iid=str(index),
                    values=(index + 1, info.size_text, share, info.mtime_text, str(info.path)),
                )
            self.size_summary_var.set(
                "共扫描 %d 个文件、总大小 %s，列出%s的 %d 个。占比按目录总大小计算。" %
                (total, human_readable_size(total_size), "最大" if largest else "最小", len(files))
            )

        self.run_task(work, done, "正在扫描…")

    def _size_selected(self) -> List[FileInfo]:
        return [self.size_files[int(i)] for i in self.size_tree.selection()]

    def _size_reveal(self) -> None:
        picked = self._size_selected()
        if picked:
            open_in_os(Path(picked[0].path), select=True)

    def do_size_trash(self) -> None:
        picked = self._size_selected()
        if not picked or self.size_root is None:
            messagebox.showinfo("提示", "请先在列表中选中要处理的文件。", parent=self)
            return
        total = sum(i.size for i in picked)
        if not self.confirm_destructive("将有 %d 个文件（共 %s）被移入回收站。" % (len(picked), human_readable_size(total))):
            return
        actions = [PlannedAction(action=ACTION_DELETE, source=Path(i.path), reason="manual cleanup") for i in picked]
        executor = self.new_executor(self.size_root)

        def done(outcome: Any) -> None:
            self.show_outcome("处理结果", self._outcome_rows(outcome), outcome.actions)
            self.do_size_find()

        self.run_task(lambda: executor.execute(actions), done, "正在移入回收站…")

    # ------------------------------------------------------------------ #
    # 页签 6：撤销历史
    # ------------------------------------------------------------------ #
    def _build_undo_tab(self, tab: ttk.Frame) -> None:
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Button(top, text="刷新", command=self.refresh_undo_logs).pack(side="left")
        ttk.Button(top, text="模拟还原", command=lambda: self.do_undo(True)).pack(side="left", padx=8)
        ttk.Button(top, text="还原所选记录", command=lambda: self.do_undo(False)).pack(side="left")
        ttk.Label(top, text="只显示当前目标文件夹的记录；还原不覆盖现有文件。", style="Hint.TLabel").pack(
            side="left", padx=12
        )
        panes = ttk.PanedWindow(tab, orient="vertical")
        panes.pack(fill="both", expand=True, pady=(8, 0))
        list_frame, self.undo_tree = make_tree(
            panes,
            [("name", "日志文件", 300, "w"), ("created", "创建时间", 170, "center"), ("count", "动作数", 80, "e")],
            height=6, selectmode="browse",
        )
        detail_frame, self.undo_detail = make_tree(
            panes,
            [("action", "动作", 70, "center"), ("status", "状态", 70, "center"),
             ("source", "原路径", 420, "w"), ("dest", "当前位置", 420, "w")],
            height=8, selectmode="none",
        )
        panes.add(list_frame, weight=1)
        panes.add(detail_frame, weight=2)
        self.undo_logs: List[Path] = []
        self.undo_tree.bind("<<TreeviewSelect>>", lambda _e: self._undo_show_detail())

    def _undo_manager(self) -> UndoManager:
        return UndoManager(log_dir=self.log_dir)

    def _clear_undo_view(self) -> None:
        self.undo_logs = []
        clear_tree(self.undo_tree)
        clear_tree(self.undo_detail)

    def refresh_undo_logs(self) -> None:
        self._clear_undo_view()
        root = self.get_root(silent=True)
        if root is None:
            return
        manager = self._undo_manager()
        for path in manager.list_logs():
            payload = manager.load(path) or {}
            if log_belongs_to_root(payload, root, self.config.trash_dir_name):
                self.undo_logs.append(path)
        for index, path in enumerate(self.undo_logs):
            payload = manager.load(path) or {}
            tag = "（模拟）" if payload.get("dry_run") else ""
            self.undo_tree.insert(
                "", "end", iid=str(index),
                values=(path.name + tag, payload.get("created_at_text", "-"), len(payload.get("actions", []) or [])),
            )
        if self.undo_logs:
            self.undo_tree.selection_set("0")

    def _undo_show_detail(self) -> None:
        clear_tree(self.undo_detail)
        sel = self.undo_tree.selection()
        if not sel:
            return
        payload = self._undo_manager().load(self.undo_logs[int(sel[0])]) or {}
        for index, raw in enumerate(payload.get("actions", [])[:1500]):
            self.undo_detail.insert(
                "", "end",
                values=(
                    raw.get("action", ""),
                    raw.get("status", ""),
                    raw.get("source", ""),
                    raw.get("destination", ""),
                ),
            )

    def do_undo(self, dry_run: bool) -> None:
        sel = self.undo_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一条日志。", parent=self)
            return
        log_path = self.undo_logs[int(sel[0])]
        root = self.get_root(silent=True)
        payload = self._undo_manager().load(log_path) or {}
        if root is None or not log_belongs_to_root(payload, root, self.config.trash_dir_name):
            self.refresh_undo_logs()
            messagebox.showwarning("无法还原", "该记录不属于当前目标文件夹，请重新选择文件夹和记录。", parent=self)
            return
        if payload.get("dry_run"):
            messagebox.showinfo("提示", "这是一条模拟记录，没有实际移动文件，无法还原。", parent=self)
            return
        if not dry_run and not messagebox.askyesno(
            "确认还原", "将根据日志还原：\n%s\n\n继续吗？" % log_path.name, parent=self
        ):
            return
        manager = self._undo_manager()

        def done(report: Any) -> None:
            labels = {"Restored": "已还原", "Renamed on conflict": "冲突改名", "Skipped": "跳过",
                      "Failed": "失败", "Bytes moved back": "移回数据量"}
            lines = ["%s：%s" % (labels.get(a, a), b) for a, b in report.as_rows()]
            if report.messages:
                lines.append("")
                lines.extend("· " + m for m in report.messages[:8])
            messagebox.showinfo("模拟还原结果" if dry_run else "还原结果", "\n".join(lines), parent=self)
            self.log("还原完成：%s" % log_path.name)
            self.refresh_undo_logs()

        self.run_task(lambda: manager.restore(log_path, dry_run=dry_run), done, "正在还原…")

    # ------------------------------------------------------------------ #
    # 页签 7：分类规则
    # ------------------------------------------------------------------ #
    def _build_rules_tab(self, tab: ttk.Frame) -> None:
        cat_row = ttk.Frame(tab)
        cat_row.pack(fill="x")
        ttk.Label(cat_row, text="分类文件夹（逗号分隔）：").pack(side="left")
        self.categories_var = tk.StringVar()
        ttk.Entry(cat_row, textvariable=self.categories_var).pack(side="left", fill="x", expand=True, padx=6)

        edit = ttk.Frame(tab)
        edit.pack(fill="x", pady=8)
        ttk.Label(edit, text="扩展名：").pack(side="left")
        self.rule_ext_var = tk.StringVar()
        ttk.Entry(edit, textvariable=self.rule_ext_var, width=8).pack(side="left", padx=4)
        ttk.Label(edit, text="归入：").pack(side="left", padx=(10, 0))
        self.rule_cat_var = tk.StringVar()
        self.rule_cat_box = ttk.Combobox(edit, textvariable=self.rule_cat_var, width=13,
                                         values=self.config.categories)
        self.rule_cat_box.pack(side="left", padx=4)
        ttk.Button(edit, text="添加 / 更新", command=self._rule_apply).pack(side="left", padx=8)
        ttk.Button(edit, text="删除所选自定义规则", command=self._rule_delete).pack(side="left")
        ttk.Button(edit, text="重新载入", command=self.refresh_rules).pack(side="left", padx=(6, 0))
        ttk.Button(edit, text="保存到配置文件", command=self._rules_save).pack(side="left", padx=(6, 0))

        ttk.Label(
            tab,
            text="提示：内置的扩展名规则不能被删除，只能改归入的分类；带“*”的是新增或改动的规则。修改后记得点“保存”。",
            style="Hint.TLabel",
        ).pack(fill="x")
        frame, self.rules_tree = make_tree(
            tab, [("ext", "扩展名", 140, "w"), ("cat", "分类", 180, "w")],
            height=14, selectmode="browse", equal_columns=True,
        )
        frame.pack(fill="both", expand=True, pady=(6, 0))
        self.rules_tree.bind("<<TreeviewSelect>>", self._rule_pick)
        self._rules_map: Dict[str, str] = {}
        self.refresh_rules()

    def refresh_rules(self) -> None:
        self._rules_map = dict(self.config.extension_map)
        self.categories_var.set(", ".join(self.config.categories))
        self.rule_cat_box.configure(values=self.config.categories)
        self._rules_redraw()

    def _rules_redraw(self) -> None:
        clear_tree(self.rules_tree)
        for ext in sorted(self._rules_map):
            custom = DEFAULT_EXTENSION_MAP.get(ext) != self._rules_map[ext]
            self.rules_tree.insert("", "end", iid=ext, values=(ext + (" *" if custom else ""), self._rules_map[ext]))

    def _rule_pick(self, _event: Any) -> None:
        sel = self.rules_tree.selection()
        if sel:
            self.rule_ext_var.set(sel[0])
            self.rule_cat_var.set(self._rules_map.get(sel[0], ""))

    def _rule_apply(self) -> None:
        ext = self.rule_ext_var.get().strip().lower()
        cat = self.rule_cat_var.get().strip()
        if not ext or not cat:
            messagebox.showinfo("提示", "请填写扩展名和分类。", parent=self)
            return
        if not ext.startswith("."):
            ext = "." + ext
        self._rules_map[ext] = cat
        self._rules_redraw()
        if self.rules_tree.exists(ext):
            self.rules_tree.selection_set(ext)
            self.rules_tree.see(ext)

    def _rule_delete(self) -> None:
        sel = self.rules_tree.selection()
        if not sel:
            return
        ext = sel[0]
        if ext in DEFAULT_EXTENSION_MAP:
            messagebox.showinfo(
                "不能删除", "%s 是内置规则，无法删除（重新载入配置时会恢复）。\n可以把它改归到别的分类，例如 Others。" % ext,
                parent=self,
            )
            return
        self._rules_map.pop(ext, None)
        self._rules_redraw()

    def _rules_save(self) -> None:
        cats = [c.strip() for c in self.categories_var.get().replace("，", ",").split(",") if c.strip()]
        if CATEGORY_OTHERS not in cats:
            cats.append(CATEGORY_OTHERS)
        self.config.set("categories", cats)
        self.config.set("extension_map", dict(self._rules_map))
        if self.config.save():
            self.config.load()
            self.refresh_rules()
            self.log("分类规则已保存。")
            messagebox.showinfo("已保存", "分类规则已写入：\n%s" % self.config.path, parent=self)
        else:
            messagebox.showerror("保存失败", "无法写入 %s" % self.config.path, parent=self)

    # ------------------------------------------------------------------ #
    # 页签 8：AI 缓存
    # ------------------------------------------------------------------ #
    def _build_cache_tab(self, tab: ttk.Frame) -> None:
        top = ttk.Frame(tab)
        top.pack(fill="x")
        self.cache_var = tk.StringVar(value="本程序缓存记录：0 条")
        ttk.Label(top, textvariable=self.cache_var).pack(side="left")
        ttk.Button(top, text="清空缓存", command=self.clear_cache).pack(side="right")
        ttk.Button(top, text="刷新", command=self.refresh_cache_stats).pack(side="right", padx=6)
        ttk.Label(tab, text="仅显示本程序保存的 AI 分类结果；清空后再次分类可能重新调用 AI。",
                  style="Hint.TLabel").pack(anchor="w", pady=(6, 8))
        frame, self.cache_tree = make_tree(
            tab,
            [("name", "文件名", 420, "w"), ("category", "分类", 190, "w"),
             ("source", "来源", 100, "center"), ("created", "缓存时间", 180, "center")],
            height=15, selectmode="browse",
        )
        frame.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ #
    # 页签 9：设置（AI / 安全）
    # ------------------------------------------------------------------ #
    def _build_settings_tab(self, tab: ttk.Frame) -> None:
        ai = ttk.LabelFrame(tab, text="AI 分类（只发送文件名，不发送文件内容）", padding=4)
        ai.pack(fill="x")

        # -- 共享设置（启用开关 + 批量 / 超时参数，三个服务商通用） -------- #
        self.ai_vars: Dict[str, tk.Variable] = {
            "enabled": tk.BooleanVar(),
            "model": tk.StringVar(),
            "base_url": tk.StringVar(),
            "api_key": tk.StringVar(),
            "api_key_env": tk.StringVar(),
            "batch_size": tk.StringVar(),
            "max_files": tk.StringVar(),
            "timeout": tk.StringVar(),
        }
        self._checkbutton(ai, text="启用 AI 补充分类", variable=self.ai_vars["enabled"]).grid(
            row=0, column=0, sticky="w"
        )

        # -- 服务商选择：切换后下面的模型 / 连接信息整体跟着切换 --------- #
        provider_row = ttk.Frame(ai)
        provider_row.grid(row=1, column=0, sticky="ew", pady=1)
        provider_row.columnconfigure(0, weight=1, uniform="setting")
        provider_row.columnconfigure(1, weight=1, uniform="setting")
        provider_field = ttk.Frame(provider_row)
        provider_field.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(provider_field, text="AI 服务商：").pack(side="left")
        self._provider_labels = [PROVIDER_INFO[pid]["label"] for pid in PROVIDER_ORDER]
        self._label_to_provider = {PROVIDER_INFO[pid]["label"]: pid for pid in PROVIDER_ORDER}
        self.ai_provider_label_var = tk.StringVar()
        provider_combo = ttk.Combobox(
            provider_field, textvariable=self.ai_provider_label_var, values=self._provider_labels,
            state="readonly", width=20,
        )
        provider_combo.pack(side="left", fill="x", expand=True, padx=(4, 0))
        provider_combo.bind("<<ComboboxSelected>>", self._on_ai_provider_changed)
        self._current_ai_provider = PROVIDER_ORDER[0]
        # 每个服务商各自的 model / base_url / api_key / api_key_env，内存中保留，
        # 切换服务商时不会互相覆盖（保存时一并写入配置文件）。
        self._ai_provider_cache: Dict[str, Dict[str, str]] = {}

        # -- 模型：下拉可编辑（允许手填模型 ID），旁边一个“获取模型列表” --- #
        model_controls = ttk.Frame(provider_row)
        model_controls.grid(row=0, column=1, sticky="ew")
        ttk.Label(model_controls, text="模型：").pack(side="left")
        self.ai_model_combo = ttk.Combobox(model_controls, textvariable=self.ai_vars["model"], width=18)
        self.ai_model_combo.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.ai_fetch_models_btn = ttk.Button(model_controls, text="获取模型列表", command=self.fetch_ai_models)
        self.ai_fetch_models_btn.pack(side="left", padx=(4, 0))

        fields = [
            ("base_url", "Base URL（留空＝官方地址）"), ("api_key", "API Key"),
            ("api_key_env", "API Key 环境变量名"), ("batch_size", "每批文件数（≤20）"),
            ("max_files", "最多送 AI 的文件数"), ("timeout", "超时（秒）"),
        ]
        for row, (key, label) in enumerate(fields[:2], start=2):
            line = ttk.Frame(ai)
            line.grid(row=row, column=0, sticky="ew", pady=1)
            ttk.Label(line, text=label + "：").pack(side="left")
            ttk.Entry(line, textvariable=self.ai_vars[key], show="•" if key == "api_key" else "").pack(
                side="left", fill="x", expand=True, padx=(4, 0)
            )
        for row in (4, 5):
            line = ttk.Frame(ai)
            line.grid(row=row, column=0, sticky="ew", pady=1)
            line.columnconfigure(0, weight=1, uniform="setting")
            line.columnconfigure(1, weight=1, uniform="setting")
            for column, (key, label) in enumerate(fields[2 + (row - 4) * 2:4 + (row - 4) * 2]):
                field = ttk.Frame(line)
                field.grid(row=0, column=column, sticky="ew", padx=(0, 8) if column == 0 else 0)
                ttk.Label(field, text=label + "：").pack(side="left")
                ttk.Entry(field, textvariable=self.ai_vars[key]).pack(
                    side="left", fill="x", expand=True, padx=(4, 0)
                )
        ai.columnconfigure(0, weight=1)

        hint_row = 6
        hint_text = (
            "安全提示：API Key 在配置文件中明文保存，建议使用环境变量；保存时会保留各服务商设置。"
        )
        ttk.Label(ai, text=hint_text, style="Hint.TLabel", wraplength=900, justify="left").grid(
            row=hint_row, column=0, sticky="w", pady=(3, 0)
        )

        # -- 测试连接：后台线程执行，避免卡界面 / 重复点击 ----------------- #
        test_row = ttk.Frame(ai)
        test_row.grid(row=hint_row + 1, column=0, sticky="ew", pady=(3, 0))
        self.ai_test_btn = ttk.Button(test_row, text="测试连接", command=self.test_ai_connection)
        self.ai_test_btn.pack(side="left")
        ttk.Label(test_row, text="（测试会发送一次最小请求，消耗少量 API 额度）", style="Hint.TLabel").pack(
            side="left", padx=8
        )
        self.ai_test_result_var = tk.StringVar(value="")
        self.ai_test_result_label = ttk.Label(
            ai, textvariable=self.ai_test_result_var, justify="left", wraplength=900,
        )
        self.ai_test_result_label.grid(row=hint_row + 2, column=0, sticky="w", pady=(2, 0))

        scan = ttk.LabelFrame(tab, text="扫描与安全", padding=4)
        scan.pack(fill="x", pady=(4, 4))
        self.excl_names_var = tk.StringVar()
        self.follow_var = tk.BooleanVar()
        self.confirm_var = tk.BooleanVar()
        ttk.Label(scan, text="始终排除的目录名（逗号分隔）：").grid(row=0, column=0, sticky="w")
        ttk.Entry(scan, textvariable=self.excl_names_var).grid(row=0, column=1, sticky="ew", padx=6)
        self._checkbutton(scan, text="扫描时跟随符号链接", variable=self.follow_var).grid(
            row=1, column=0, sticky="w", pady=1
        )
        self._checkbutton(scan, text="删除类操作需要输入 DELETE 确认", variable=self.confirm_var).grid(
            row=1, column=1, sticky="w"
        )
        scan.columnconfigure(1, weight=1)

        btns = ttk.Frame(tab)
        btns.pack(fill="x")
        ttk.Button(btns, text="保存设置", command=self.save_settings).pack(side="left")
        ttk.Button(btns, text="打开配置文件所在目录", command=lambda: open_in_os(self.work_dir)).pack(side="left", padx=8)

        self.load_settings()

    # -- 服务商切换 / 读写辅助 ------------------------------------------- #
    def _read_provider_fields_from_vars(self) -> Dict[str, str]:
        """当前界面上 model / base_url / api_key / api_key_env 的即时取值。"""
        return {
            "model": str(self.ai_vars["model"].get()).strip(),
            "base_url": str(self.ai_vars["base_url"].get()).strip(),
            "api_key": str(self.ai_vars["api_key"].get()).strip(),
            "api_key_env": str(self.ai_vars["api_key_env"].get()).strip(),
        }

    def _write_provider_fields_to_vars(self, provider_id: str) -> None:
        """把内存缓存中某个服务商的设置，显示到界面的输入框里（含模型下拉候选）。"""
        info = PROVIDER_INFO[provider_id]
        values = self._ai_provider_cache.get(provider_id, {})
        self.ai_vars["model"].set(str(values.get("model") or info["default_model"]))
        self.ai_vars["base_url"].set(str(values.get("base_url") or ""))
        self.ai_vars["api_key"].set(str(values.get("api_key") or ""))
        self.ai_vars["api_key_env"].set(str(values.get("api_key_env") or info["default_api_key_env"]))
        self.ai_model_combo.configure(values=ai_providers.fallback_models(provider_id))

    def _on_ai_provider_changed(self, _event: Any = None) -> None:
        # 先把刚才那个服务商填的内容存进内存缓存，再切到新的服务商。
        self._ai_provider_cache[self._current_ai_provider] = self._read_provider_fields_from_vars()
        new_provider = self._label_to_provider.get(self.ai_provider_label_var.get(), PROVIDER_ORDER[0])
        self._current_ai_provider = new_provider
        self._write_provider_fields_to_vars(new_provider)
        self.ai_test_result_var.set("")
        self.ai_test_result_label.configure(style="TLabel")

    def load_settings(self) -> None:
        ai = self.config.get("ai", {})
        if not isinstance(ai, dict):
            ai = {}
        self.ai_vars["enabled"].set(bool(ai.get("enabled", True)))
        for key, default in (("batch_size", 20), ("max_files", 400), ("timeout", 30)):
            self.ai_vars[key].set(str(ai.get(key, default)))
        self._ai_provider_cache = {
            provider_id: dict(self.config.ai_provider_settings(provider_id)) for provider_id in PROVIDER_ORDER
        }
        self._current_ai_provider = self.config.ai_provider
        self.ai_provider_label_var.set(PROVIDER_INFO[self._current_ai_provider]["label"])
        self._write_provider_fields_to_vars(self._current_ai_provider)
        self.ai_test_result_var.set("")
        self.excl_names_var.set(", ".join(self.config.excluded_names))
        self.follow_var.set(bool(self.config.get("scan.follow_symlinks", False)))
        self.confirm_var.set(bool(self.config.get("safety.require_delete_confirmation", True)))

    def save_settings(self) -> None:
        self.apply_ai_settings()
        for key, default in (("batch_size", 20), ("max_files", 400), ("timeout", 30)):
            try:
                value = int(str(self.ai_vars[key].get()).strip())
            except ValueError:
                value = default
            self.config.set("ai." + key, value)
        names = [n.strip() for n in self.excl_names_var.get().replace("，", ",").split(",") if n.strip()]
        self.config.set("scan.exclude_names", names)
        self.config.set("scan.follow_symlinks", bool(self.follow_var.get()))
        self.config.set("safety.require_delete_confirmation", bool(self.confirm_var.get()))
        if self.config.save():
            self.config.load()
            self.load_settings()
            self.use_ai_var.set(bool(self.config.get("ai.enabled", True)))
            self.log("设置已保存。当前 AI 服务商：%s" % PROVIDER_INFO[self.config.ai_provider]["label"])
            messagebox.showinfo("已保存", "设置已写入：\n%s" % self.config.path, parent=self)
        else:
            messagebox.showerror("保存失败", "无法写入 %s" % self.config.path, parent=self)

    def apply_ai_settings(self) -> None:
        """把界面上的 AI 设置（含全部服务商、不止当前显示的这个）应用到内存配置（不写文件）。"""
        self.config.set("ai.enabled", bool(self.ai_vars["enabled"].get()))
        self._ai_provider_cache[self._current_ai_provider] = self._read_provider_fields_from_vars()
        self.config.set("ai.provider", self._current_ai_provider)
        for provider_id, values in self._ai_provider_cache.items():
            self.config.set_ai_provider_settings(provider_id, values)

    def _set_ai_controls_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.ai_test_btn.configure(state=state)
        self.ai_fetch_models_btn.configure(state=state)

    def test_ai_connection(self) -> None:
        """后台发送一次最小请求，验证 API Key / Base URL / 模型是否可用。"""
        self.apply_ai_settings()
        provider_id = self._current_ai_provider
        provider_label = PROVIDER_INFO[provider_id]["label"]
        classifier = AIClassifier(self.config, CacheStore(None))
        self.ai_test_result_var.set("")
        self.ai_test_result_label.configure(style="TLabel")

        started = self.run_task(
            classifier.test_connection,
            lambda result: self._on_ai_test_done(provider_label, result),
            text="正在测试 %s 连接…" % provider_label,
        )
        if started:
            self._set_ai_controls_busy(True)

    def _on_ai_test_done(self, provider_label: str, result: Any) -> None:
        self._set_ai_controls_busy(False)
        if result.ok:
            self.ai_test_result_label.configure(style="Success.TLabel")
            self.ai_test_result_var.set(
                "✅ 连接成功｜服务商：%s｜模型：%s｜耗时：%.0f ms" % (provider_label, result.model, result.elapsed_ms)
            )
            self.log("AI 连接测试成功（%s / %s，%.0f ms）。" % (provider_label, result.model, result.elapsed_ms))
        else:
            self.ai_test_result_label.configure(style="Danger.TLabel")
            self.ai_test_result_var.set("❌ 连接失败｜服务商：%s｜原因：%s" % (provider_label, result.message))
            self.log("AI 连接测试失败（%s）：%s" % (provider_label, result.message))

    def fetch_ai_models(self) -> None:
        """后台调用服务商的模型列表接口，成功则刷新下拉框候选项。

        接口不支持 / 调用失败时不当作致命错误：保留可维护的静态模型列表，
        并在日志里说明原因，用户仍可在模型框里手动填写模型 ID。
        """
        self.apply_ai_settings()
        provider_id = self._current_ai_provider
        provider_label = PROVIDER_INFO[provider_id]["label"]
        classifier = AIClassifier(self.config, CacheStore(None))

        def work() -> Tuple[List[str], Optional[str]]:
            try:
                return classifier.list_models(), None
            except ProviderError as exc:
                return [], str(exc)
            except Exception as exc:  # noqa: BLE001 - never let a bad response kill the UI thread
                return [], str(exc)

        started = self.run_task(
            work,
            lambda outcome: self._on_ai_models_fetched(provider_id, provider_label, outcome),
            text="正在获取 %s 的模型列表…" % provider_label,
        )
        if started:
            self._set_ai_controls_busy(True)

    def _on_ai_models_fetched(
        self, provider_id: str, provider_label: str, outcome: Tuple[List[str], Optional[str]]
    ) -> None:
        self._set_ai_controls_busy(False)
        models, error = outcome
        if error:
            self.log("获取 %s 模型列表失败（%s），已保留内置列表，可手动填写模型 ID。" % (provider_label, error))
            return
        merged = sorted(set(models) | set(ai_providers.fallback_models(provider_id)))
        if self._current_ai_provider == provider_id:
            self.ai_model_combo.configure(values=merged)
        self.log("已获取 %s 模型列表：%d 个。" % (provider_label, len(models)))

    def refresh_cache_stats(self) -> None:
        with CacheStore(self.work_dir / "ai_cache.db") as store:
            entries = store.list_entries()
        clear_tree(self.cache_tree)
        for index, (name, category, source, stamp) in enumerate(entries):
            self.cache_tree.insert(
                "", "end", iid=str(index),
                values=(name, category, SOURCE_TEXT.get(source, source),
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))),
            )
        self.cache_var.set("本程序缓存记录：%d 条" % len(entries))

    def clear_cache(self) -> None:
        if not messagebox.askyesno("确认", "清空本程序的全部 AI 分类缓存？之后相同文件名会重新调用 AI。", parent=self):
            return
        with CacheStore(self.work_dir / "ai_cache.db") as store:
            removed = store.clear()
        self.log("已清除 %d 条缓存。" % removed)
        self.refresh_cache_stats()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    """启动图形界面。"""
    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:
            pass
    app = App()
    app.mainloop()
    return 0
