"""Empty directory detection and removal planning."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from .models import ACTION_RMDIR, PlannedAction
from .utils import PathLike


class Cleaner(object):
    """Find empty folders and plan their removal."""

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
