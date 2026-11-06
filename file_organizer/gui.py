"""Tkinter 图形界面 —— 智能文件整理器。

启动方式（任选其一）::

    python -m file_organizer.gui
    python run_gui.py            # 放在 file_organizer 文件夹的上一级

界面只使用标准库（tkinter），并直接复用命令行版本的全部业务模块：
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
import traceback
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

if __package__ in (None, ""):  # 允许 `python file_organizer/gui.py` 直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
from file_organizer.utils import human_readable_size, timestamp_to_string

APP_TITLE = "智能文件整理器"
CHECK_ON = "☑"
CHECK_OFF = "☐"
SOURCE_TEXT = {"rule": "规则", "ai": "AI", "cache": "缓存", "unknown": "未识别"}
PREVIEW_LIMIT = 64 * 1024


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


def make_tree(
    parent: tk.Misc,
    columns: Sequence[Tuple[str, str, int, str]],
    height: int = 10,
    selectmode: str = "extended",
    show: str = "headings",
    tree_heading: str = "",
    tree_width: int = 200,
) -> Tuple[ttk.Frame, ttk.Treeview]:
    """创建带滚动条的 Treeview。columns = (id, 标题, 宽度, 对齐)。"""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(
        frame,
        columns=[c[0] for c in columns],
        show=show,
        selectmode=selectmode,
        height=height,
    )
    if "tree" in show:
        tree.heading("#0", text=tree_heading)
        tree.column("#0", width=tree_width, stretch=False)
    for col_id, title, width, anchor in columns:
        tree.heading(col_id, text=title)
        tree.column(col_id, width=width, anchor=anchor, stretch=False)
    tree.bind(
        "<Button-1>",
        lambda event: "break" if tree.identify_region(event.x, event.y) == "separator" else None,
        add="+",
    )
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame, tree


def clear_tree(tree: ttk.Treeview) -> None:
    tree.delete(*tree.get_children())


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    """整个图形界面。"""

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x760")
        self.minsize(960, 620)

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
        self.dup_keep: Dict[int, Optional[int]] = {}
        self.dup_root: Optional[Path] = None
        self.clean_dirs: List[Path] = []
        self.clean_root: Optional[Path] = None
        self.size_files: List[FileInfo] = []
        self.size_root: Optional[Path] = None
        self.browser_root: Optional[Path] = None
        self.browser_dir: Optional[Path] = None
        self.browser_paths: List[Path] = []

        self._setup_style()
        self._build_menu()
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
        theme = {"win32": "vista", "darwin": "aqua"}.get(sys.platform, "clam")
        if theme in style.theme_names():
            style.theme_use(theme)
        families = set(tkfont.families())
        for candidate in ("Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", "WenQuanYi Micro Hei"):
            if candidate in families:
                for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                    tkfont.nametofont(name).configure(family=candidate)
                break
        style.configure("Treeview", rowheight=24)
        style.configure("Title.TLabel", font=(tkfont.nametofont("TkDefaultFont").actual("family"), 12, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Danger.TButton", foreground="#b00020")

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        tools = tk.Menu(menubar, tearoff=False)
        tools.add_command(label="打开工作目录", command=lambda: open_in_os(self.work_dir))
        tools.add_command(label="打开撤销日志目录", command=lambda: self._open_dir_safe(self.log_dir))
        tools.add_command(label="打开当前目录的回收站", command=self._open_trash)
        tools.add_separator()
        tools.add_command(label="退出", command=self.destroy)
        menubar.add_cascade(label="工具", menu=tools)
        self.config_menu = menubar
        self.configure(menu=menubar)

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
        ttk.Button(bar, text="浏览…", command=lambda: self._browse(self.root_var)).grid(row=0, column=2)
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
        self.progress = ttk.Progressbar(status_row, mode="indeterminate", length=180)
        self.progress.pack(side="right")
        log_frame = ttk.Frame(bottom)
        log_frame.pack(fill="x")
        self.log_text = tk.Text(log_frame, height=5, wrap="word", state="disabled", relief="solid", borderwidth=1)
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
        elif text == "设置":
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
            console=None,
            exclude_names=excludes,
            exclude_paths=exclude_paths,
        )
        return scanner.scan(show_progress=False)

    def new_executor(self, root: Path, dry_run: bool = False) -> Executor:
        return Executor(
            console=None,
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
        panes.add(frame, weight=2)
        self.browser_tree.bind("<<TreeviewSelect>>", self.browse_preview)
        self.browser_tree.bind("<Double-1>", self.browse_enter)
        preview_frame = ttk.Frame(panes)
        self.browser_text = tk.Text(preview_frame, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(preview_frame, command=self.browser_text.yview)
        self.browser_text.configure(yscrollcommand=scrollbar.set)
        self.browser_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        panes.add(preview_frame, weight=3)

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
            panes, [("metric", "指标", 160, "w"), ("value", "数值", 220, "e")], height=10, selectmode="none"
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
        ttk.Checkbutton(flags, text="启用 AI 补充分类（只发送文件名）", variable=self.use_ai_var).pack(side="left")
        self.org_dry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(flags, text="仅模拟（不真正移动）", variable=self.org_dry_var).pack(side="left", padx=14)
        ttk.Button(flags, text="生成预览", command=self.do_org_plan).pack(side="left", padx=(14, 0))
        ttk.Button(flags, text="执行勾选项", command=self.do_org_execute).pack(side="left", padx=6)
        ttk.Button(flags, text="全不选", command=lambda: self._org_set_all(False)).pack(side="right")
        ttk.Button(flags, text="全选", command=lambda: self._org_set_all(True)).pack(side="right", padx=4)

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
            cache = CacheStore(self.work_dir / "ai_cache.db")  # SQLite 连接必须在使用它的线程里创建
            try:
                report = Classifier(config=self.config, cache=cache, console=None).classify_all(
                    result.files, use_ai=use_ai
                )
            finally:
                cache.close()
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
            for message in report.errors:
                self.log("AI：%s" % message)
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

    def _org_set_all(self, value: bool) -> None:
        for iid in self.org_tree.get_children():
            self._org_set(iid, value)

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
        ttk.Checkbutton(top, text="包含 0 字节文件", variable=self.dup_empty_var).pack(side="left")
        ttk.Button(top, text="查找重复文件", command=self.do_dup_find).pack(side="left", padx=10)
        self.dup_dry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="仅模拟", variable=self.dup_dry_var).pack(side="left")
        ttk.Button(top, text="把未保留的移入回收站", style="Danger.TButton", command=self.do_dup_execute).pack(
            side="right"
        )

        tools = ttk.Frame(tab)
        tools.pack(fill="x", pady=(6, 0))
        ttk.Button(tools, text="保留所选这一份", command=self._dup_keep_selected).pack(side="left")
        ttk.Button(tools, text="跳过所选的组", command=self._dup_skip_selected).pack(side="left", padx=6)
        ttk.Button(tools, text="全部重置为保留最新", command=self._dup_reset).pack(side="left")
        ttk.Button(tools, text="定位文件", command=self._dup_reveal).pack(side="left", padx=6)
        self.dup_summary_var = tk.StringVar(value="点击“查找重复文件”（先按大小分组，再比对 SHA-256）。")
        ttk.Label(tab, textvariable=self.dup_summary_var, style="Hint.TLabel").pack(fill="x", pady=6)

        frame, self.dup_tree = make_tree(
            tab,
            [
                ("state", "处理", 90, "center"),
                ("path", "路径", 520, "w"),
                ("size", "大小", 90, "e"),
                ("mtime", "修改时间", 150, "center"),
            ],
            height=14, show="tree headings", tree_heading="分组", tree_width=340,
        )
        frame.pack(fill="both", expand=True)
        self.dup_tree.tag_configure("keep", foreground="#1b7f3b")
        self.dup_tree.tag_configure("trash", foreground="#b00020")
        self.dup_tree.tag_configure("skip", foreground="#888888")
        self.dup_tree.bind("<Double-1>", lambda _e: self._dup_keep_selected())

    def do_dup_find(self) -> None:
        root = self.get_root()
        if root is None:
            return
        excludes = self.extra_excludes()
        include_empty = bool(self.dup_empty_var.get())

        def work() -> Tuple[ScanResult, List[DuplicateGroup]]:
            result = self.scan_tree(root, excludes)
            self.set_status("正在比对 %d 个文件的内容…" % len(result.files))
            groups = DuplicateChecker(result.files, console=None, include_empty=include_empty).find()
            return result, groups

        def done(payload: Tuple[ScanResult, List[DuplicateGroup]]) -> None:
            result, groups = payload
            self.dup_root = root
            self.dup_groups = groups
            self.dup_keep = {i: 0 for i in range(len(groups))}  # 组内已按修改时间降序，0 即最新
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
            if not groups:
                self.dup_summary_var.set("扫描了 %d 个文件，没有发现重复文件。" % len(result.files))
            self.log("重复文件查找完成：%d 组" % len(groups))
            self._dup_update_summary()

        self.run_task(work, done, "正在查找重复文件…")

    def _dup_refresh_group(self, index: int) -> None:
        group = self.dup_groups[index]
        keep = self.dup_keep.get(index)
        gid = "g%d" % index
        label = "第 %d 组 · %d 份 · %s · 可回收 %s" % (
            index + 1, len(group.files), human_readable_size(group.size), group.recoverable_text
        )
        self.dup_tree.item(gid, text=label)
        self.dup_tree.set(gid, "state", "已跳过" if keep is None else "")
        for pos in range(len(group.files)):
            cid = "g%d_%d" % (index, pos)
            if keep is None:
                self.dup_tree.set(cid, "state", "— 跳过")
                self.dup_tree.item(cid, tags=("skip",))
            elif pos == keep:
                self.dup_tree.set(cid, "state", "✔ 保留")
                self.dup_tree.item(cid, tags=("keep",))
            else:
                self.dup_tree.set(cid, "state", "🗑 回收站")
                self.dup_tree.item(cid, tags=("trash",))

    def _dup_update_summary(self) -> None:
        if not self.dup_groups:
            return
        stats = DuplicateChecker.summarize(self.dup_groups)
        pending = sum(len(g.files) - 1 for i, g in enumerate(self.dup_groups) if self.dup_keep.get(i) is not None)
        self.dup_summary_var.set(
            "共 %d 组重复，%d 份冗余，最多可回收 %s｜当前将移入回收站：%d 个文件。单击/双击某份文件可改为保留它。"
            % (stats["groups"], stats["redundant"], stats["wasted_text"], pending)
        )

    def _dup_parse(self, iid: str) -> Tuple[int, Optional[int]]:
        body = iid[1:]
        if "_" in body:
            g, p = body.split("_", 1)
            return int(g), int(p)
        return int(body), None

    def _dup_keep_selected(self) -> None:
        touched: Set[int] = set()
        for iid in self.dup_tree.selection():
            g, p = self._dup_parse(iid)
            if p is not None:
                self.dup_keep[g] = p
                touched.add(g)
        for g in touched:
            self._dup_refresh_group(g)
        self._dup_update_summary()

    def _dup_skip_selected(self) -> None:
        touched: Set[int] = set()
        for iid in self.dup_tree.selection():
            g, _p = self._dup_parse(iid)
            self.dup_keep[g] = None
            touched.add(g)
        for g in touched:
            self._dup_refresh_group(g)
        self._dup_update_summary()

    def _dup_reset(self) -> None:
        for index in range(len(self.dup_groups)):
            self.dup_keep[index] = 0
            self._dup_refresh_group(index)
        self._dup_update_summary()

    def _dup_reveal(self) -> None:
        for iid in self.dup_tree.selection():
            g, p = self._dup_parse(iid)
            if p is not None:
                open_in_os(Path(self.dup_groups[g].files[p].path), select=True)
                return

    def do_dup_execute(self) -> None:
        if not self.dup_groups or self.dup_root is None:
            messagebox.showinfo("提示", "请先查找重复文件。", parent=self)
            return
        actions: List[PlannedAction] = []
        for index, group in enumerate(self.dup_groups):
            keep = self.dup_keep.get(index)
            if keep is None:
                continue
            keeper = group.files[keep]
            for info in group.files:
                if info is keeper:
                    continue
                actions.append(
                    PlannedAction(
                        action=ACTION_DELETE,
                        source=Path(info.path),
                        reason="duplicate of %s (sha256:%s)" % (keeper.path, group.short_hash),
                    )
                )
        if not actions:
            messagebox.showinfo("提示", "没有需要处理的文件。", parent=self)
            return
        dry = bool(self.dup_dry_var.get())
        if not dry and not self.confirm_destructive("将有 %d 个重复文件被移入回收站。" % len(actions)):
            return
        executor = self.new_executor(self.dup_root, dry_run=dry)
        self.run_task(lambda: executor.execute(actions), self._dup_done, "正在处理重复文件…")

    def _dup_done(self, outcome: Any) -> None:
        self.show_outcome("重复文件处理结果", self._outcome_rows(outcome), outcome.actions)
        self.dup_groups = []
        self.dup_keep = {}
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
        ttk.Checkbutton(top, text="仅模拟", variable=self.clean_dry_var).pack(side="left", padx=12)
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
        excludes = self.extra_excludes()
        protected = {str(root), str(self.trash_dir_for(root))}

        def work() -> List[Path]:
            return Cleaner(console=None).find_empty_dirs(root, protected=protected, exclude_names=excludes)

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
        actions = Cleaner(console=None).plan_dir_removal([self.clean_dirs[i] for i in picked])
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
        ttk.Radiobutton(top, text="最大的文件", value="最大", variable=self.size_mode_var).pack(side="left")
        ttk.Radiobutton(top, text="最小的文件", value="最小", variable=self.size_mode_var).pack(side="left", padx=8)
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
            height=14,
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
                picked = Cleaner.find_large_files(result.files, top)
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
        return UndoManager(log_dir=self.log_dir, console=None)

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
                values=(raw.get("action", ""), raw.get("status", ""), raw.get("source", ""), raw.get("destination", "")),
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
        ttk.Entry(edit, textvariable=self.rule_ext_var, width=12).pack(side="left", padx=4)
        ttk.Label(edit, text="归入：").pack(side="left", padx=(10, 0))
        self.rule_cat_var = tk.StringVar()
        self.rule_cat_box = ttk.Combobox(edit, textvariable=self.rule_cat_var, width=16, values=self.config.categories)
        self.rule_cat_box.pack(side="left", padx=4)
        ttk.Button(edit, text="添加 / 更新", command=self._rule_apply).pack(side="left", padx=8)
        ttk.Button(edit, text="删除所选自定义规则", command=self._rule_delete).pack(side="left")
        ttk.Button(edit, text="保存到配置文件", command=self._rules_save).pack(side="right")
        ttk.Button(edit, text="重新载入", command=self.refresh_rules).pack(side="right", padx=6)

        ttk.Label(
            tab,
            text="提示：内置的扩展名规则不能被删除，只能改归入的分类；以“*”标记的是你的自定义规则。修改后记得点“保存”。",
            style="Hint.TLabel",
        ).pack(fill="x")
        frame, self.rules_tree = make_tree(
            tab, [("ext", "扩展名", 140, "w"), ("cat", "分类", 180, "w"), ("note", "", 80, "center")],
            height=14, selectmode="browse",
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
            self.rules_tree.insert("", "end", iid=ext, values=(ext, self._rules_map[ext], "*" if custom else ""))

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
    # 页签 8：设置（AI / 安全 / 缓存）
    # ------------------------------------------------------------------ #
    def _build_settings_tab(self, tab: ttk.Frame) -> None:
        ai = ttk.LabelFrame(tab, text="AI 分类（OpenAI 兼容接口，只发送文件名）", padding=10)
        ai.pack(fill="x")
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
        ttk.Checkbutton(ai, text="启用 AI 补充分类", variable=self.ai_vars["enabled"]).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        fields = [
            ("model", "模型"), ("base_url", "Base URL（可留空）"), ("api_key", "API Key"),
            ("api_key_env", "API Key 环境变量名"), ("batch_size", "每批文件数（≤20）"),
            ("max_files", "最多送 AI 的文件数"), ("timeout", "超时（秒）"),
        ]
        for row, (key, label) in enumerate(fields, start=1):
            ttk.Label(ai, text=label + "：").grid(row=row, column=0, sticky="w", pady=2)
            entry = ttk.Entry(ai, textvariable=self.ai_vars[key], show="•" if key == "api_key" else "")
            entry.grid(row=row, column=1, sticky="ew", padx=6, pady=2)
        ai.columnconfigure(1, weight=1)
        ttk.Label(
            ai, text="安全提示：API Key 写进配置文件是明文保存，推荐只设置环境变量（环境变量优先于这里的值）。",
            style="Hint.TLabel",
        ).grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        scan = ttk.LabelFrame(tab, text="扫描与安全", padding=10)
        scan.pack(fill="x", pady=8)
        self.excl_names_var = tk.StringVar()
        self.follow_var = tk.BooleanVar()
        self.confirm_var = tk.BooleanVar()
        ttk.Label(scan, text="始终排除的目录名（逗号分隔）：").grid(row=0, column=0, sticky="w")
        ttk.Entry(scan, textvariable=self.excl_names_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Checkbutton(scan, text="扫描时跟随符号链接", variable=self.follow_var).grid(row=1, column=0, sticky="w", pady=2)
        ttk.Checkbutton(scan, text="删除类操作需要输入 DELETE 确认", variable=self.confirm_var).grid(
            row=1, column=1, sticky="w"
        )
        scan.columnconfigure(1, weight=1)

        btns = ttk.Frame(tab)
        btns.pack(fill="x")
        ttk.Button(btns, text="保存设置", command=self.save_settings).pack(side="left")
        ttk.Button(btns, text="检测 AI 是否可用", command=self.check_ai).pack(side="left", padx=8)
        ttk.Button(btns, text="打开配置文件所在目录", command=lambda: open_in_os(self.work_dir)).pack(side="left")

        cache = ttk.LabelFrame(tab, text="AI 分类缓存", padding=10)
        cache.pack(fill="x", pady=8)
        self.cache_var = tk.StringVar(value="")
        ttk.Label(cache, textvariable=self.cache_var, justify="left").pack(side="left")
        ttk.Button(cache, text="清空缓存", command=self.clear_cache).pack(side="right")
        ttk.Button(cache, text="刷新", command=self.refresh_cache_stats).pack(side="right", padx=6)
        self.load_settings()

    def load_settings(self) -> None:
        ai = self.config.ai_settings
        self.ai_vars["enabled"].set(bool(ai.get("enabled", True)))
        for key in ("model", "base_url", "api_key", "api_key_env", "batch_size", "max_files", "timeout"):
            self.ai_vars[key].set(str(ai.get(key, "") if ai.get(key) is not None else ""))
        self.excl_names_var.set(", ".join(self.config.excluded_names))
        self.follow_var.set(bool(self.config.get("scan.follow_symlinks", False)))
        self.confirm_var.set(bool(self.config.get("safety.require_delete_confirmation", True)))

    def save_settings(self) -> None:
        self.config.set("ai.enabled", bool(self.ai_vars["enabled"].get()))
        for key in ("model", "base_url", "api_key", "api_key_env"):
            self.config.set("ai." + key, str(self.ai_vars[key].get()).strip())
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
            self.log("设置已保存。")
            messagebox.showinfo("已保存", "设置已写入：\n%s" % self.config.path, parent=self)
        else:
            messagebox.showerror("保存失败", "无法写入 %s" % self.config.path, parent=self)

    def check_ai(self) -> None:
        self.save_settings_quiet()
        classifier = AIClassifier(self.config, CacheStore(None), None)
        if classifier.available:
            messagebox.showinfo("AI 可用", "已找到 API Key 和 openai 库。\n模型：%s" % classifier.model, parent=self)
        else:
            messagebox.showwarning("AI 不可用", "原因：%s" % (classifier.last_error or "未知"), parent=self)

    def save_settings_quiet(self) -> None:
        """检测前先把界面上的值应用到内存配置（不写文件）。"""
        self.config.set("ai.enabled", bool(self.ai_vars["enabled"].get()))
        for key in ("model", "base_url", "api_key", "api_key_env"):
            self.config.set("ai." + key, str(self.ai_vars[key].get()).strip())

    def refresh_cache_stats(self) -> None:
        store = CacheStore(self.work_dir / "ai_cache.db")
        try:
            stats = store.stats()
        finally:
            store.close()
        top = sorted((stats.get("by_category") or {}).items(), key=lambda kv: -kv[1])[:6]
        extra = "　".join("%s %d" % kv for kv in top)
        self.cache_var.set("后端：%s｜条目：%s%s" % (stats["mode"], stats["entries"], ("\n分类：" + extra) if extra else ""))

    def clear_cache(self) -> None:
        if not messagebox.askyesno("确认", "清空全部 AI 分类缓存？之后相同文件名会重新调用 AI。", parent=self):
            return
        store = CacheStore(self.work_dir / "ai_cache.db")
        try:
            removed = store.clear()
        finally:
            store.close()
        self.log("已清除 %d 条缓存。" % removed)
        self.refresh_cache_stats()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    """启动图形界面。"""
    # pythonw / 双击运行时 stdout、stderr 可能为 None，tqdm 之类的库会因此报错
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:
            pass
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
