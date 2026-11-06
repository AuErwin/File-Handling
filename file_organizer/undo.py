"""Undo support: replay an ``undo_log_*.json`` and put everything back.

Restoration never overwrites an existing file: when the original location is
already occupied the file comes back as ``name_restored_1.ext``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import ACTION_DELETE, ACTION_MOVE, ACTION_RMDIR, PlannedAction
from .utils import PathLike, escape_markup, human_readable_size, read_json, truncate

#: Marker appended when the original path is already taken.
RESTORE_SUFFIX = "_restored"


class UndoReport(object):
    """Outcome counters produced by :meth:`UndoManager.restore`."""

    def __init__(self) -> None:
        """Create an empty report."""
        self.restored = 0
        self.failed = 0
        self.skipped = 0
        self.renamed = 0
        self.bytes_moved = 0
        self.messages: List[str] = []

    @property
    def total(self) -> int:
        """Number of processed log entries."""
        return self.restored + self.failed + self.skipped

    def as_rows(self) -> List[Tuple[str, str]]:
        """Return ``(label, value)`` pairs for terminal rendering."""
        return [
            ("Restored", str(self.restored)),
            ("Renamed on conflict", str(self.renamed)),
            ("Skipped", str(self.skipped)),
            ("Failed", str(self.failed)),
            ("Bytes moved back", human_readable_size(self.bytes_moved)),
        ]


class UndoManager(object):
    """Read undo logs and roll the file system back to its previous state."""

    def __init__(self, log_dir: PathLike, console: Optional[Any] = None) -> None:
        """Bind the manager to a directory containing ``undo_log_*.json`` files."""
        self.log_dir = Path(str(log_dir))
        self.console = console

    # ------------------------------------------------------------------ #
    # Log discovery
    # ------------------------------------------------------------------ #
    def list_logs(self) -> List[Path]:
        """Return available undo logs, newest first."""
        if not self.log_dir.is_dir():
            return []
        logs = [item for item in self.log_dir.glob("undo_log_*.json") if item.is_file()]
        logs.sort(key=lambda item: item.name, reverse=True)
        return logs

    def latest(self) -> Optional[Path]:
        """Return the most recent undo log, if any."""
        logs = self.list_logs()
        return logs[0] if logs else None

    def load(self, path: PathLike) -> Optional[Dict[str, Any]]:
        """Load and sanity-check an undo log document."""
        payload = read_json(path, default=None)
        if not isinstance(payload, dict):
            return None
        actions = payload.get("actions")
        if not isinstance(actions, list):
            payload["actions"] = []
        return payload

    def render_logs(self) -> None:
        """Print the available undo logs as a table."""
        if self.console is None:
            return
        from rich.table import Table

        logs = self.list_logs()
        table = Table(title="Undo logs in %s" % self.log_dir, header_style="bold cyan", expand=True)
        table.add_column("#", style="bold cyan", width=4, justify="right")
        table.add_column("Log file", style="white")
        table.add_column("Created", style="yellow", width=22)
        table.add_column("Actions", style="green", justify="right", width=10)
        if not logs:
            self.console.print("[yellow]No undo log found in %s[/yellow]" % escape_markup(str(self.log_dir)))
            return
        for index, log in enumerate(logs, start=1):
            payload = self.load(log) or {}
            created = str(payload.get("created_at_text", "-"))
            count = len(payload.get("actions", []) or [])
            table.add_row(str(index), escape_markup(log.name), created, str(count))
        self.console.print(table)

    # ------------------------------------------------------------------ #
    # Restoration
    # ------------------------------------------------------------------ #
    @staticmethod
    def _free_target(original: Path) -> Tuple[Path, bool]:
        """Return ``(path, renamed)`` -- ``path`` never points at an existing file."""
        if not original.exists():
            return original, False
        suffix = original.suffix
        stem = original.stem
        index = 1
        while True:
            if index == 1:
                candidate = original.with_name("%s%s%s" % (stem, RESTORE_SUFFIX, suffix))
            else:
                candidate = original.with_name(
                    "%s%s_%d%s" % (stem, RESTORE_SUFFIX, index, suffix)
                )
            if not candidate.exists():
                return candidate, True
            index += 1
            if index > 100000:  # safety valve
                return original.with_name("%s%s_%s%s" % (stem, RESTORE_SUFFIX, id(original), suffix)), True

    def _restore_one(self, item: PlannedAction, report: UndoReport, dry_run: bool) -> None:
        """Undo a single action."""
        original = Path(item.source)

        if item.action == ACTION_RMDIR:
            if original.exists():
                item.status = "skipped"
                report.skipped += 1
                return
            if dry_run:
                item.status = "ok"
                report.restored += 1
                return
            try:
                os.makedirs(str(original), exist_ok=True)
            except OSError as exc:
                item.status = "failed"
                item.message = str(exc)
                report.failed += 1
                return
            item.status = "ok"
            report.restored += 1
            return

        # move / delete both store the current location in `destination`.
        current = Path(item.destination) if item.destination else None
        if current is None or not current.exists():
            item.status = "skipped"
            item.message = "backup copy not found: %s" % (current or "-")
            report.skipped += 1
            report.messages.append(item.message)
            return

        target, renamed = self._free_target(original)
        if renamed:
            report.renamed += 1
            report.messages.append(
                "%s already exists -> restored as %s" % (original, target.name)
            )

        try:
            size = int(os.path.getsize(str(current)))
        except OSError:
            size = 0

        if dry_run:
            item.status = "ok"
            report.restored += 1
            return

        try:
            if target.parent and not target.parent.exists():
                os.makedirs(str(target.parent), exist_ok=True)
            shutil.move(str(current), str(target))
        except (OSError, shutil.Error) as exc:
            item.status = "failed"
            item.message = str(exc)
            report.failed += 1
            report.messages.append("%s -> %s failed: %s" % (current, target, exc))
            return

        item.status = "ok"
        item.message = "restored to %s" % target
        report.restored += 1
        report.bytes_moved += size

    def restore(
        self,
        log_path: Optional[PathLike] = None,
        dry_run: bool = False,
        limit: Optional[int] = None,
    ) -> UndoReport:
        """Roll back every successful action recorded in the log.

        Args:
            log_path: Undo log to replay (defaults to the most recent one).
            dry_run: Report only, do not touch the disk.
            limit: Only process the first ``limit`` actions (debug helper).
        """
        report = UndoReport()
        target_path = Path(str(log_path)) if log_path else self.latest()
        if target_path is None or not Path(target_path).is_file():
            report.messages.append("No undo log available at %s" % (log_path or self.log_dir))
            return report

        payload = self.load(target_path)
        if payload is None:
            report.messages.append("Undo log %s is unreadable or corrupted." % target_path)
            return report
        if payload.get("dry_run"):
            report.messages.append("This is a simulation log; no file operation can be restored.")
            return report

        raw_actions: Sequence[Dict[str, Any]] = payload.get("actions", []) or []
        if limit is not None and limit >= 0:
            raw_actions = raw_actions[:limit]

        actions: List[PlannedAction] = []
        for raw in raw_actions:
            if not isinstance(raw, dict):
                continue
            try:
                actions.append(PlannedAction.from_dict(raw))
            except (TypeError, ValueError):
                continue

        # Reverse: the last executed action must be undone first.  Within the
        # reversed list we keep rmdir in front of file moves so that folders are
        # recreated before their content is put back.
        ordered = list(reversed(actions))
        ordered.sort(key=lambda item: 0 if item.action == ACTION_RMDIR else 1)

        if self.console is not None:
            self.console.print(
                "Restoring [bold]%d[/bold] action(s) from [cyan]%s[/cyan]"
                % (len(ordered), escape_markup(str(target_path)))
            )

        for item in ordered:
            if item.status == "failed":
                report.skipped += 1
                continue
            try:
                self._restore_one(item, report, dry_run)
            except Exception as exc:  # defensive: one bad entry must not stop the roll back
                report.failed += 1
                report.messages.append("%s failed: %s" % (item.source, exc))
        return report

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #
    def render_report(self, report: UndoReport, limit: int = 15) -> None:
        """Print the restoration outcome, including the first few conflicts."""
        if self.console is None:
            return
        from rich.table import Table

        table = Table(title="Undo result", header_style="bold cyan")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="white", justify="right")
        for label, value in report.as_rows():
            table.add_row(label, value)
        self.console.print(table)

        if report.messages:
            self.console.print("\n[bold yellow]Notes[/bold yellow]")
            for message in report.messages[:limit]:
                self.console.print("  - %s" % escape_markup(truncate(message, 100)))
            if len(report.messages) > limit:
                self.console.print("  ... and %d more" % (len(report.messages) - limit))
