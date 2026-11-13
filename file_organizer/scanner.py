"""Directory scanner with exclusion rules and file access error reporting."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterator, List, Optional, Set, Tuple

from .config import Config
from .models import FileInfo, ScanResult
from .utils import PathLike, is_regular_file


class Scanner(object):
    """Walk a directory tree and yield :class:`~file_organizer.models.FileInfo`."""

    def __init__(
        self,
        root: PathLike,
        config: Optional[Config] = None,
        exclude_names: Optional[Set[str]] = None,
        exclude_paths: Optional[Set[str]] = None,
    ) -> None:
        """Prepare a scan.

        Args:
            root: Directory to walk.
            config: Provides ``scan.exclude_names`` / ``scan.follow_symlinks``.
            exclude_names: Extra directory base-names to prune.
            exclude_paths: Extra absolute directories to prune (compared in a
                case-insensitive way so Windows drive letter casing never matters).
        """
        self.root = Path(str(root)).expanduser()
        self.config = config or Config()
        self.exclude_names: Set[str] = set(self.config.excluded_names)
        if exclude_names:
            self.exclude_names.update({str(item) for item in exclude_names})
        self.exclude_paths: Set[str] = set()
        for item in (exclude_paths or set()):
            self.exclude_paths.add(os.path.normcase(os.path.abspath(str(item))))
        self.follow_symlinks: bool = bool(self.config.get("scan.follow_symlinks", False))
        self.skipped_dirs: List[Tuple[str, str]] = []
        self.skipped_files: List[Tuple[str, str]] = []
        self.excluded_dirs: List[str] = []

    # -- internal helpers ---------------------------------------------------- #
    def _log_skip_dir(self, path: PathLike, reason: Any) -> None:
        """Record a directory that could not be traversed."""
        self.skipped_dirs.append((str(path), str(reason)))

    def _log_skip_file(self, path: PathLike, reason: Any) -> None:
        """Record a file that could not be inspected."""
        self.skipped_files.append((str(path), str(reason)))

    def _on_walk_error(self, error: OSError) -> None:
        """``os.walk(onerror=...)`` callback -- swallows and records failures."""
        self._log_skip_dir(getattr(error, "filename", "<unknown>"), error)

    def _is_excluded_dir(self, dirpath: Path, dirname: str) -> bool:
        """Return ``True`` when ``dirpath/dirname`` must be pruned."""
        if dirname in self.exclude_names:
            return True
        if self.exclude_paths:
            candidate = os.path.normcase(os.path.abspath(str(dirpath / dirname)))
            if candidate in self.exclude_paths:
                return True
        return False

    def _walk(self) -> Iterator[Tuple[Path, List[str], List[str]]]:
        """Generator wrapper around :func:`os.walk` that never raises.

        ``next()`` is called manually because errors raised while the generator
        advances (not while ``scandir`` runs) are *not* routed to ``onerror``.
        """
        walker = os.walk(
            str(self.root),
            topdown=True,
            onerror=self._on_walk_error,
            followlinks=self.follow_symlinks,
        )
        while True:
            try:
                dirpath, dirnames, filenames = next(walker)
            except StopIteration:
                return
            except PermissionError as exc:  # pragma: no cover - defensive
                self._log_skip_dir(getattr(exc, "filename", self.root), exc)
                continue
            except OSError as exc:  # pragma: no cover - defensive
                self._log_skip_dir(getattr(exc, "filename", self.root), exc)
                continue

            current = Path(dirpath)
            # Prune in place so os.walk will not descend into excluded folders.
            kept: List[str] = []
            for dirname in dirnames:
                if self._is_excluded_dir(current, dirname):
                    self.excluded_dirs.append(str(current / dirname))
                else:
                    kept.append(dirname)
            dirnames[:] = kept
            yield current, dirnames, filenames

    # -- public API ----------------------------------------------------------- #
    def scan(self) -> ScanResult:
        """Walk the tree and return a :class:`ScanResult`."""
        started = time.time()
        self.skipped_dirs = []
        self.skipped_files = []
        self.excluded_dirs = []

        result = ScanResult(root=self.root)
        directory_count = 0
        for dirpath, _dirnames, filenames in self._walk():
            directory_count += 1
            for filename in filenames:
                full_path = dirpath / filename
                if not is_regular_file(full_path):
                    continue
                info = FileInfo.from_path(full_path)
                if info is None:
                    self._log_skip_file(full_path, "stat failed")
                    continue
                result.files.append(info)

        result.scanned_dirs = directory_count
        result.skipped_dirs = list(self.skipped_dirs)
        result.skipped_files = list(self.skipped_files)
        result.excluded_dirs = list(self.excluded_dirs)
        result.elapsed = time.time() - started
        return result
