"""Safe execution engine: conflict resolution, soft delete, logging.

Guarantees
----------
* **Never overwrite.**  When the destination exists, a timestamp (then an index)
  is appended: ``report.pdf`` -> ``report_20231027.pdf`` -> ``report_20231027_1.pdf``.
* **Never delete directly.**  ``delete`` moves the file into a recycle bin.
* **Undoable on successful logging.**  Every run saves a unique undo log.
* The GUI confirms destructive batches before calling this engine.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

from .config import Config
from .models import ACTION_DELETE, ACTION_MOVE, ACTION_RMDIR, PlannedAction
from .utils import (
    PathLike,
    human_readable_size,
    timestamp_slug,
    unique_path,
    write_json,
)

#: Word the operator must type to authorize a destructive batch.
DELETE_TOKEN = "DELETE"

#: Maximum length of a generated trash path (Windows MAX_PATH safety margin).
_MAX_PATH_LENGTH = 240


class ExecutionReport(object):
    """Outcome counters produced by :meth:`Executor.execute`."""

    def __init__(self) -> None:
        """Create an empty report."""
        self.ok = 0
        self.failed = 0
        self.skipped = 0
        self.moved_bytes = 0
        self.deleted_bytes = 0
        self.log_path: Optional[Path] = None
        self.actions: List[PlannedAction] = []

    def as_rows(self) -> List[Tuple[str, str]]:
        """Return ``(label, value)`` pairs for the GUI summary."""
        return [
            ("Succeeded", str(self.ok)),
            ("Failed", str(self.failed)),
            ("Skipped", str(self.skipped)),
            ("Moved", human_readable_size(self.moved_bytes)),
            ("Trashed", human_readable_size(self.deleted_bytes)),
            ("Undo log", str(self.log_path or "-")),
        ]


class Executor(object):
    """Apply a list of :class:`PlannedAction` objects, safely and reversibly."""

    def __init__(
        self,
        trash_dir: Optional[PathLike] = None,
        log_dir: Optional[PathLike] = None,
        config: Optional[Config] = None,
        dry_run: bool = False,
    ) -> None:
        """Create the executor.

        Args:
            trash_dir: Recycle bin location (defaults to ``<cwd>/.file_organizer_trash``).
            log_dir: Where ``undo_log_*.json`` files are written.
            config: Provides ``safety.*`` settings.
            dry_run: Report what *would* happen without touching the disk.
        """
        self.config = config or Config()
        self.dry_run = bool(dry_run)
        self.trash_dir = Path(str(trash_dir)) if trash_dir else (
            Path.cwd() / self.config.trash_dir_name
        )
        base_log = Path(str(log_dir)) if log_dir else (Path.home() / ".file_organizer" / "undo_logs")
        self.log_dir: Path = base_log
        self.batch_id: str = "%s_%s" % (timestamp_slug(), uuid4().hex)

    # ------------------------------------------------------------------ #
    # Naming helpers
    # ------------------------------------------------------------------ #
    def resolve_conflict(self, destination: PathLike) -> Path:
        """Return a free path for ``destination``.

        ``report.pdf`` -> ``report_20231027.pdf`` -> ``report_20231027_1.pdf``.
        """
        return unique_path(Path(str(destination)))

    def _trash_target(self, source: Path) -> Path:
        """Compute the recycle-bin path that mirrors the original location."""
        try:
            resolved = Path(os.path.abspath(str(source)))
        except (OSError, ValueError):
            resolved = Path(str(source))

        drive = (resolved.drive or "").replace(":", "").replace("\\", "").strip()
        drive = drive or "ROOT"
        try:
            relative = resolved.relative_to(resolved.anchor)
        except (ValueError, IndexError):
            relative = Path(resolved.name or "file")

        target = self.trash_dir / self.batch_id / drive / relative
        if len(str(target)) > _MAX_PATH_LENGTH:
            # Keep the extension and shorten with a stable hash prefix.
            import hashlib

            digest = hashlib.sha1(str(resolved).encode("utf-8", "replace")).hexdigest()[:12]
            suffix = resolved.suffix
            target = self.trash_dir / self.batch_id / drive / ("%s%s" % (digest, suffix))
        return self.resolve_conflict(target)

    @staticmethod
    def _same_path(left: PathLike, right: PathLike) -> bool:
        """Case-insensitive path comparison (Windows semantics)."""
        try:
            return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
                os.path.abspath(str(right))
            )
        except (OSError, ValueError):
            return str(left) == str(right)

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def execute(self, actions: Sequence[PlannedAction]) -> ExecutionReport:
        """Apply every action, then write the undo log."""
        report = ExecutionReport()
        if not actions:
            return report

        self.batch_id = "%s_%s" % (timestamp_slug(), uuid4().hex)
        executed: List[PlannedAction] = []

        for item in actions:
            try:
                if item.action == ACTION_MOVE:
                    self._do_move(item, report)
                elif item.action == ACTION_DELETE:
                    self._do_delete(item, report)
                elif item.action == ACTION_RMDIR:
                    self._do_rmdir(item, report)
                else:
                    item.status = "failed"
                    item.message = "unknown action '%s'" % item.action
            except KeyboardInterrupt:
                item.status = "failed"
                item.message = "interrupted by user"
                executed.append(item)
                report.failed += 1
                break
            except Exception as exc:  # never let a single file kill the run
                item.status = "failed"
                item.message = "%s: %s" % (type(exc).__name__, exc)

            executed.append(item)
            if item.status == "ok":
                report.ok += 1
            elif item.status == "skipped":
                report.skipped += 1
            else:
                report.failed += 1

        report.actions = executed
        if not self.dry_run:
            report.log_path = self.write_undo_log(executed)
            if report.log_path is None:
                raise RuntimeError(
                    "已完成 %d 项操作，但撤销日志写入失败（%s）。请手动检查文件；无法通过撤销历史还原。"
                    % (report.ok, self.log_dir)
                )
        return report

    def _do_move(self, item: PlannedAction, report: ExecutionReport) -> None:
        """Move a file, renaming on conflict."""
        source = Path(item.source)
        if item.destination is None:
            item.status = "failed"
            item.message = "missing destination"
            return
        if not source.exists():
            item.status = "skipped"
            item.message = "source vanished"
            return

        destination = Path(item.destination)
        if destination.exists() and destination.is_dir():
            destination = destination / source.name
        if self._same_path(source, destination):
            item.status = "skipped"
            item.message = "source and destination are identical"
            return
        destination = self.resolve_conflict(destination)

        item.destination = destination
        if self.dry_run:
            item.status = "ok"
            item.message = "dry-run"
            return

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        item.status = "ok"
        item.message = "moved"
        try:
            report.moved_bytes += int(os.path.getsize(str(destination)))
        except OSError:
            pass

    def _do_delete(self, item: PlannedAction, report: ExecutionReport) -> None:
        """Move a file into the recycle bin (soft delete)."""
        source = Path(item.source)
        if not source.exists():
            item.status = "skipped"
            item.message = "source vanished"
            return
        target = self._trash_target(source)
        try:
            size = int(os.path.getsize(str(source)))
        except OSError:
            size = 0

        item.destination = target
        if self.dry_run:
            item.status = "ok"
            item.message = "dry-run (would move to recycle bin)"
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        item.status = "ok"
        item.message = "moved to recycle bin"
        report.deleted_bytes += size

    def _do_rmdir(self, item: PlannedAction, report: ExecutionReport) -> None:
        """Remove an empty directory (``os.rmdir`` refuses non-empty folders)."""
        target = Path(item.source)
        if not target.exists():
            item.status = "skipped"
            item.message = "already gone"
            return
        if self.dry_run:
            item.status = "ok"
            item.message = "dry-run"
            return
        os.rmdir(str(target))
        item.status = "ok"
        item.message = "directory removed"

    # ------------------------------------------------------------------ #
    # Undo log
    # ------------------------------------------------------------------ #
    def write_undo_log(self, actions: Sequence[PlannedAction]) -> Optional[Path]:
        """Persist this batch's undo log and return its path."""
        payload: Dict[str, Any] = {
            "version": 1,
            "batch_id": self.batch_id,
            "created_at": time.time(),
            "created_at_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "dry_run": self.dry_run,
            "trash_dir": str(self.trash_dir),
            "actions": [item.to_dict() for item in actions],
        }
        path = self.log_dir / ("undo_log_%s.json" % self.batch_id)
        if write_json(path, payload):
            return path
        return None
