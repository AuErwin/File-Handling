"""Safe execution engine: conflict resolution, confirmation, soft delete, logging.

Guarantees
----------
* **Never overwrite.**  When the destination exists, a timestamp (then an index)
  is appended: ``report.pdf`` -> ``report_20231027.pdf`` -> ``report_20231027_1.pdf``.
* **Never delete directly.**  ``delete`` moves the file into a recycle bin.
* **Always reversible.**  Every run writes ``undo_log_<timestamp>.json``.
* **Always confirmed.**  Destructive batches require typing ``DELETE`` in caps.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .models import ACTION_DELETE, ACTION_MOVE, ACTION_RMDIR, PlannedAction
from .utils import (
    PathLike,
    escape_markup,
    human_readable_size,
    timestamp_slug,
    truncate,
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

    @property
    def total(self) -> int:
        """Number of processed actions."""
        return self.ok + self.failed + self.skipped

    def as_rows(self) -> List[Tuple[str, str]]:
        """Return ``(label, value)`` pairs for terminal rendering."""
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
        console: Optional[Any] = None,
        trash_dir: Optional[PathLike] = None,
        log_dir: Optional[PathLike] = None,
        config: Optional[Config] = None,
        dry_run: bool = False,
    ) -> None:
        """Create the executor.

        Args:
            console: Optional ``rich`` console.
            trash_dir: Recycle bin location (defaults to ``<cwd>/.file_organizer_trash``).
            log_dir: Where ``undo_log_*.json`` files are written.
            config: Provides ``safety.*`` settings.
            dry_run: Print what *would* happen without touching the disk.
        """
        self.console = console
        self.config = config or Config()
        self.dry_run = bool(dry_run)
        self.trash_dir = Path(str(trash_dir)) if trash_dir else (
            Path.cwd() / self.config.trash_dir_name
        )
        base_log = Path(str(log_dir)) if log_dir else (Path.home() / ".file_organizer" / "undo_logs")
        self.log_dir: Path = base_log
        self.batch_id: str = timestamp_slug()

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
    # Confirmation
    # ------------------------------------------------------------------ #
    def has_destructive(self, actions: Sequence[PlannedAction]) -> bool:
        """``True`` when the batch contains delete / rmdir actions."""
        return any(item.is_destructive for item in actions)

    def confirm(
        self,
        actions: Sequence[PlannedAction],
        auto_yes: bool = False,
        require_delete_token: Optional[bool] = None,
    ) -> bool:
        """Ask the operator for permission before touching anything.

        Destructive batches always require typing ``DELETE`` (unless the config
        explicitly disables the safeguard).  Non destructive batches only need a
        ``y`` (or ``--yes``).
        """
        if not actions:
            return False
        if require_delete_token is None:
            require_delete_token = bool(self.config.get("safety.require_delete_confirmation", True))

        destructive = self.has_destructive(actions)
        if self.console is None:
            return True

        if destructive and require_delete_token:
            deletion_count = sum(1 for item in actions if item.action == ACTION_DELETE)
            dir_count = sum(1 for item in actions if item.action == ACTION_RMDIR)
            self.console.print(
                "\n[bold red]DANGER ZONE[/bold red] - this batch is destructive:\n"
                "  files to move into the recycle bin : [bold]%d[/bold]\n"
                "  empty directories to remove        : [bold]%d[/bold]\n"
                "  total actions                      : [bold]%d[/bold]"
                % (deletion_count, dir_count, len(actions))
            )
            try:
                from rich.prompt import Prompt  # type: ignore

                answer = Prompt.ask(
                    'Type [bold]%s[/bold] (upper case) to proceed, anything else to abort'
                    % DELETE_TOKEN,
                    console=self.console,
                    default="",
                    show_choices=False,
                )
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[yellow]Aborted by user.[/yellow]")
                return False
            if str(answer).strip() != DELETE_TOKEN:
                self.console.print("[yellow]Confirmation failed - nothing was changed.[/yellow]")
                return False
            return True

        if auto_yes:
            return True
        try:
            from rich.prompt import Prompt  # type: ignore

            answer = Prompt.ask(
                "Apply [bold]%d[/bold] action(s)? [y/N]" % len(actions),
                choices=["y", "n", "Y", "N"],
                default="n",
                console=self.console,
                show_choices=False,
            )
        except (EOFError, KeyboardInterrupt):
            self.console.print("\n[yellow]Aborted by user.[/yellow]")
            return False
        return str(answer).strip().lower() == "y"

    # ------------------------------------------------------------------ #
    # Preview
    # ------------------------------------------------------------------ #
    def preview(self, actions: Sequence[PlannedAction], limit: int = 25) -> None:
        """Print the planned actions as a table."""
        if self.console is None:
            return
        from rich.table import Table

        table = Table(title="Planned actions (%d)" % len(actions), header_style="bold cyan", expand=True)
        table.add_column("Action", style="bold", width=8)
        table.add_column("Source", style="white", overflow="fold")
        table.add_column("Destination", style="green", overflow="fold")
        table.add_column("Reason", style="dim", overflow="fold")
        for item in actions[:limit]:
            color = "red" if item.is_destructive else "green"
            table.add_row(
                "[%s]%s[/%s]" % (color, item.action, color),
                escape_markup(truncate(str(item.source), 60)),
                escape_markup(truncate(str(item.destination), 60)) if item.destination else "-",
                escape_markup(truncate(item.reason, 40)),
            )
        self.console.print(table)
        if len(actions) > limit:
            self.console.print("[dim]... and %d more action(s)[/dim]" % (len(actions) - limit))

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def execute(self, actions: Sequence[PlannedAction]) -> ExecutionReport:
        """Apply every action, then write the undo log."""
        report = ExecutionReport()
        if not actions:
            return report

        self.batch_id = timestamp_slug()
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
                    report.failed += 1
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
        destination = self.resolve_conflict(destination)

        if self._same_path(source, destination):
            item.status = "skipped"
            item.message = "source and destination are identical"
            return

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
        """Persist ``undo_log_<timestamp>.json`` and return its path."""
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
