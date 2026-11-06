"""Two-stage duplicate detection with an interactive keep/delete prompt.

Stage 1 groups files by **size** (cheap, pure metadata).
Stage 2 only hashes the files that share a size with at least one sibling.

Nothing is ever deleted automatically: the interactive step turns the user's
choice into ``delete`` actions handled by :class:`~file_organizer.executor.Executor`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import ACTION_DELETE, DuplicateGroup, FileInfo, PlannedAction
from .utils import chunked_hash, escape_markup, human_readable_size, truncate

DEFAULT_CHUNK_SIZE = 1024 * 1024


class DuplicateChecker(object):
    """Find identical files and let the operator decide what to keep."""

    def __init__(
        self,
        files: Sequence[FileInfo],
        console: Optional[Any] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        include_empty: bool = False,
    ) -> None:
        """Create the checker over an already scanned file list."""
        self.files: List[FileInfo] = list(files)
        self.console = console
        self.chunk_size = max(64 * 1024, int(chunk_size))
        self.include_empty = bool(include_empty)

    # -- stage 1 + 2 ---------------------------------------------------------- #
    def _group_by_size(self) -> Dict[int, List[FileInfo]]:
        """Return ``{size: [files...]}`` for sizes shared by at least two files."""
        buckets: Dict[int, List[FileInfo]] = {}
        for info in self.files:
            if not self.include_empty and info.size <= 0:
                continue
            buckets.setdefault(info.size, []).append(info)
        return {size: items for size, items in buckets.items() if len(items) > 1}

    def _hash_candidates(self, buckets: Dict[int, List[FileInfo]]) -> Dict[str, DuplicateGroup]:
        """Hash every candidate file and group them by digest."""
        candidates: List[FileInfo] = []
        for size in sorted(buckets.keys()):
            candidates.extend(buckets[size])
        if not candidates:
            return {}

        groups: Dict[str, DuplicateGroup] = {}
        iterator = candidates
        use_tqdm = False
        progress: Any = None
        try:
            from tqdm import tqdm  # type: ignore

            progress = tqdm(
                total=len(candidates),
                desc="Hashing candidates",
                unit="file",
                leave=False,
            )
            use_tqdm = True
        except Exception:  # pragma: no cover - tqdm is optional
            progress = None

        try:
            for info in iterator:
                digest = chunked_hash(info.path, "sha256", self.chunk_size)
                if progress is not None:
                    try:
                        progress.update(1)
                    except Exception:  # pragma: no cover
                        pass
                if not digest:
                    continue
                info.hash_value = digest
                group = groups.get(digest)
                if group is None:
                    group = DuplicateGroup(hash_value=digest, size=info.size, files=[])
                    groups[digest] = group
                group.files.append(info)
        finally:
            if progress is not None and use_tqdm:
                try:
                    progress.close()
                except Exception:  # pragma: no cover
                    pass

        return {key: group for key, group in groups.items() if len(group.files) > 1}

    def find(self) -> List[DuplicateGroup]:
        """Run both stages and return duplicate groups ordered by wasted space."""
        buckets = self._group_by_size()
        if not buckets:
            return []
        groups = self._hash_candidates(buckets)
        result = list(groups.values())
        # Newest first inside a group makes "keep the first" a sane default.
        for group in result:
            group.files.sort(key=lambda item: (item.mtime, str(item.path)), reverse=True)
        result.sort(key=lambda item: (item.recoverable_bytes, item.size), reverse=True)
        return result

    # -- rendering ------------------------------------------------------------- #
    def render_group(self, group: DuplicateGroup, index: int, total: int) -> None:
        """Print a single duplicate group as a coloured table."""
        if self.console is None:
            return
        from rich.table import Table

        table = Table(
            title="Group %d/%d  |  %s x %s  |  sha256:%s  |  reclaimable %s"
            % (
                index,
                total,
                len(group.files),
                human_readable_size(group.size),
                group.short_hash,
                group.recoverable_text,
            ),
            header_style="bold magenta",
            expand=True,
        )
        table.add_column("#", style="bold cyan", justify="right", width=4)
        table.add_column("Path", style="white", overflow="fold")
        table.add_column("Size", justify="right", style="green", width=12)
        table.add_column("Modified", justify="center", style="yellow", width=20)
        for position, info in enumerate(group.files, start=1):
            table.add_row(
                str(position),
                escape_markup(str(info.path)),
                info.size_text,
                info.mtime_text,
            )
        self.console.print(table)

    # -- interaction ------------------------------------------------------------ #
    def _ask_choice(self, group: DuplicateGroup, index: int, total: int) -> Optional[int]:
        """Ask which copy to keep.  Returns the 1-based index, or ``None`` to skip."""
        if self.console is None:
            return 1
        try:
            from rich.prompt import Prompt  # type: ignore

            answer = Prompt.ask(
                "Keep which copy? [1-%d]  ([bold]s[/bold]kip group, "
                "[bold]a[/bold]bort remaining)" % len(group.files),
                default="1",
                console=self.console,
                show_choices=False,
            )
        except (EOFError, KeyboardInterrupt):
            self.console.print("\n[yellow]Input interrupted, skipping remaining groups.[/yellow]")
            raise
        except Exception:  # pragma: no cover - no tty at all
            return 1

        answer = str(answer).strip().lower()
        if answer in ("s", "skip", ""):
            return None
        if answer in ("a", "abort", "q", "quit"):
            raise KeyboardInterrupt("aborted by user")
        try:
            choice = int(answer)
        except ValueError:
            self.console.print("  [yellow]'%s' is not a number - skipping this group.[/yellow]"
                               % escape_markup(answer))
            return None
        if 1 <= choice <= len(group.files):
            return choice
        self.console.print("  [yellow]Out of range - skipping this group.[/yellow]")
        return None

    def interactive_select(
        self,
        groups: Sequence[DuplicateGroup],
        auto_keep_newest: bool = False,
    ) -> Tuple[List[PlannedAction], List[DuplicateGroup]]:
        """Walk the operator through every group and collect delete actions.

        Returns:
            ``(actions, skipped_groups)``.  Every action moves a redundant copy
            into the recycle bin; the keeper is never touched.
        """
        actions: List[PlannedAction] = []
        skipped: List[DuplicateGroup] = []
        total = len(groups)
        for position, group in enumerate(groups, start=1):
            self.render_group(group, position, total)
            if auto_keep_newest:
                keep = 1
            else:
                try:
                    keep = self._ask_choice(group, position, total)
                except (EOFError, KeyboardInterrupt):
                    skipped.extend(groups[position - 1:])
                    break
                if keep is None:
                    if self.console is not None:
                        self.console.print("  [dim]skipped[/dim]")
                    skipped.append(group)
                    continue
            keeper = group.files[keep - 1]
            for info in group.files:
                if info is keeper:
                    continue
                actions.append(
                    PlannedAction(
                        action=ACTION_DELETE,
                        source=Path(info.path),
                        destination=None,
                        reason="duplicate of %s (sha256:%s)"
                        % (truncate(str(keeper.path), 60), group.short_hash),
                    )
                )
            if self.console is not None:
                self.console.print(
                    "  [green]keep[/green] %s  [red]trash[/red] %d file(s)\n"
                    % (escape_markup(str(keeper.path)), len(group.files) - 1)
                )
        return actions, skipped

    # -- helpers ---------------------------------------------------------------- #
    @staticmethod
    def summarize(groups: Sequence[DuplicateGroup]) -> Dict[str, Any]:
        """Aggregate statistics about the detected duplicates."""
        total_files = sum(len(group.files) for group in groups)
        wasted = sum(group.recoverable_bytes for group in groups)
        return {
            "groups": len(groups),
            "files": total_files,
            "redundant": total_files - len(groups),
            "wasted_bytes": wasted,
            "wasted_text": human_readable_size(wasted),
        }
