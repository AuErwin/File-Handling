"""Space cleaning helpers: empty directory detection and large file charts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .models import ACTION_RMDIR, FileInfo, PlannedAction
from .utils import PathLike, escape_markup, human_readable_size, truncate

#: Characters used to draw the bar chart (full block / light shade).
BLOCK_FULL = "\u2588"
BLOCK_EMPTY = "\u2591"

#: Rank -> colour used by the bar chart.
BAR_COLORS = ["bright_red", "red", "yellow", "cyan", "green", "blue", "magenta", "white"]


class Cleaner(object):
    """Locate reclaimable space: empty folders and oversized files."""

    def __init__(self, console: Optional[Any] = None) -> None:
        """Create the cleaner bound to an optional ``rich`` console."""
        self.console = console

    # ------------------------------------------------------------------ #
    # Empty directories
    # ------------------------------------------------------------------ #
    def find_empty_dirs(
        self,
        root: PathLike,
        protected: Optional[Set[str]] = None,
        exclude_names: Optional[Set[str]] = None,
    ) -> List[Path]:
        """Return directories that contain no file at all (recursively).

        A folder whose children are *all* empty is considered empty too, so
        nested chains such as ``a/b/c`` are reported in one pass.

        Args:
            root: Directory to inspect.  The root itself is never reported.
            protected: Absolute paths that must never be removed.
            exclude_names: Directory base-names to skip.
        """
        base = Path(str(root)).expanduser()
        if not base.is_dir():
            return []

        guard: Set[str] = set()
        for item in (protected or set()):
            guard.add(os.path.normcase(os.path.abspath(str(item))))
        guard.add(os.path.normcase(os.path.abspath(str(base))))

        skip_names = set(exclude_names or set())
        removable: Dict[str, bool] = {}
        # topdown=False guarantees children are visited before their parent.
        for dirpath, dirnames, filenames in os.walk(str(base), topdown=False, onerror=lambda e: None):
            key = os.path.normcase(os.path.abspath(dirpath))
            if key in guard:
                removable[key] = False
                continue
            if os.path.basename(os.path.normpath(dirpath)) in skip_names:
                removable[key] = False
                continue
            children_all_removable = True
            for child in dirnames:
                child_key = os.path.normcase(os.path.abspath(os.path.join(dirpath, child)))
                if not removable.get(child_key, False):
                    children_all_removable = False
                    break
            removable[key] = (len(filenames) == 0) and children_all_removable

        empty: List[Path] = []
        for dirpath, _dirnames, _filenames in os.walk(str(base), topdown=False, onerror=lambda e: None):
            key = os.path.normcase(os.path.abspath(dirpath))
            if removable.get(key, False):
                empty.append(Path(dirpath))
        # Deepest first, so the executor never tries to remove a parent before
        # its children.
        empty.sort(key=lambda item: (len(str(item)), str(item)), reverse=True)
        return empty

    def plan_dir_removal(self, dirs: Sequence[Path], reason: str = "empty directory") -> List[PlannedAction]:
        """Turn a list of directories into ``rmdir`` actions."""
        return [
            PlannedAction(action=ACTION_RMDIR, source=Path(str(item)), destination=None, reason=reason)
            for item in dirs
        ]

    # ------------------------------------------------------------------ #
    # Large files
    # ------------------------------------------------------------------ #
    @staticmethod
    def find_large_files(files: Sequence[FileInfo], top_n: int = 10) -> List[FileInfo]:
        """Return the ``top_n`` biggest files, descending by size."""
        ordered = sorted(files, key=lambda item: (item.size, str(item.path)), reverse=True)
        if top_n <= 0:
            return ordered
        return ordered[:top_n]

    def render_size_chart(
        self,
        files: Sequence[FileInfo],
        width: int = 40,
        title: str = "Largest files",
    ) -> None:
        """Draw a coloured ASCII bar chart of the given files."""
        if self.console is None or not files:
            return
        biggest = max(item.size for item in files)
        if biggest <= 0:
            biggest = 1
        self.console.print("\n[bold]%s[/bold]" % escape_markup(title))
        for rank, info in enumerate(files):
            ratio = float(info.size) / float(biggest)
            filled = int(round(width * ratio))
            filled = max(1, min(width, filled))
            bar = BLOCK_FULL * filled + BLOCK_EMPTY * (width - filled)
            color = BAR_COLORS[rank % len(BAR_COLORS)]
            label = truncate(str(info.path), 56)
            self.console.print(
                "[%s]%s[/%s] [bold]%9s[/bold]  %s"
                % (color, bar, color, human_readable_size(info.size), escape_markup(label))
            )

    def render_category_chart(self, files: Sequence[FileInfo], width: int = 32) -> None:
        """Draw a bar chart of total size per category."""
        if self.console is None or not files:
            return
        buckets: Dict[str, int] = {}
        for info in files:
            key = info.category or "Others"
            buckets[key] = buckets.get(key, 0) + info.size
        if not buckets:
            return
        rows = sorted(buckets.items(), key=lambda pair: pair[1], reverse=True)
        biggest = max(value for _key, value in rows) or 1
        self.console.print("\n[bold]Size by category[/bold]")
        for index, (name, total) in enumerate(rows):
            filled = max(1, min(width, int(round(width * float(total) / float(biggest)))))
            bar = BLOCK_FULL * filled + BLOCK_EMPTY * (width - filled)
            color = BAR_COLORS[index % len(BAR_COLORS)]
            self.console.print(
                "[%s]%s[/%s] [bold]%9s[/bold]  %s"
                % (color, bar, color, human_readable_size(total), escape_markup(name))
            )

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #
    def summarize_empty_dirs(self, dirs: Sequence[Path]) -> Dict[str, Any]:
        """Return a small dictionary describing the empty directory plan."""
        return {
            "count": len(dirs),
            "preview": [str(item) for item in dirs[:10]],
        }
