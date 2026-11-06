"""Command line entry point of the intelligent file organizer.

Sub commands
------------
``scan``      inventory a directory tree
``organize``  classify files and move them into category folders
``dedupe``    detect duplicates and let the operator pick the copy to keep
``clean``     remove empty directories
``largest``   show the biggest files
``smallest``  show the smallest files
``undo``      roll back a previous run using its undo log
``cache``     inspect or clear the AI classification cache
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .classifier import Classifier
from .cleaner import Cleaner
from .config import Config
from .duplicate_checker import DuplicateChecker
from .executor import Executor
from .models import ACTION_MOVE, CATEGORY_OTHERS, PlannedAction, ScanResult
from .scanner import Scanner
from .storage import CacheStore
from .undo import UndoManager
from .utils import (
    PathLike,
    ensure_utf8_stdio,
    escape_markup,
    human_readable_size,
    truncate,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ABORTED = 130


# --------------------------------------------------------------------------- #
# Shared context
# --------------------------------------------------------------------------- #
class Context(object):
    """Everything a sub command needs: console, config, cache and directories."""

    def __init__(self, args: argparse.Namespace) -> None:
        """Build the runtime context from parsed CLI arguments."""
        self.args = args
        self.console = _build_console(args)
        self.work_dir = Path(str(getattr(args, "work_dir") or (Path.home() / ".file_organizer"))).expanduser()
        self.config = Config(getattr(args, "config", None) or (self.work_dir / "config.json"))
        self.cache = CacheStore(self.work_dir / "ai_cache.db")
        self.log_dir = Path(
            str(getattr(args, "log_dir") or (self.work_dir / "undo_logs"))
        )
        self._default_trash: Optional[Path] = None

    # -- helpers ------------------------------------------------------------ #
    def trash_dir_for(self, root: PathLike) -> Path:
        """Recycle bin used for ``root`` (overridable with ``--trash-dir``)."""
        override = getattr(self.args, "trash_dir", None)
        if override:
            return Path(str(override))
        return Path(str(root)) / self.config.trash_dir_name

    def extra_excludes(self) -> Set[str]:
        """Extra directory names the user asked to skip."""
        values = getattr(self.args, "exclude", None) or []
        return {str(item) for item in values}

    def print(self, message: Any = "") -> None:
        """Print through the rich console when available."""
        if self.console is not None:
            self.console.print(message)
        else:  # pragma: no cover - console creation rarely fails
            print(message)

    def close(self) -> None:
        """Release resources."""
        self.cache.close()


def _build_console(args: argparse.Namespace) -> Any:
    """Create a ``rich`` console, or return ``None`` when rich is unavailable."""
    try:
        from rich.console import Console  # type: ignore
    except Exception:  # pragma: no cover
        return None
    return Console(highlight=False, soft_wrap=False, markup=True)


def _summary_table(title: str, rows: Sequence[Tuple[str, Any]], console: Any, color: str = "cyan") -> None:
    """Render a two column key/value table."""
    if console is None:
        for label, value in rows:
            print("%-28s %s" % (label, value))
        return
    from rich.table import Table

    table = Table(title=title, header_style="bold " + color, show_lines=False, expand=False)
    table.add_column("Metric", style="cyan", justify="left")
    table.add_column("Value", style="white", justify="right")
    for label, value in rows:
        table.add_row(escape_markup(str(label)), escape_markup(str(value)))
    console.print(table)


def _scan_tree(ctx: Context, root: PathLike, extra_paths: Optional[Set[PathLike]] = None) -> ScanResult:
    """Run a scan with the standard exclusion rules applied."""
    root_path = Path(str(root)).expanduser()
    if not root_path.exists():
        raise SystemExit("Path does not exist: %s" % root_path)
    if not root_path.is_dir():
        raise SystemExit("Not a directory: %s" % root_path)

    exclude_paths: Set[str] = {str(ctx.trash_dir_for(root_path))}
    for item in (extra_paths or set()):
        exclude_paths.add(str(item))

    scanner = Scanner(
        root=root_path,
        config=ctx.config,
        console=ctx.console,
        exclude_names=ctx.extra_excludes(),
        exclude_paths=exclude_paths,
    )
    quiet = bool(getattr(ctx.args, "quiet", False))
    result = scanner.scan(show_progress=not quiet, description="Scanning %s" % truncate(str(root_path), 32))
    if result.error_count and ctx.console is not None:
        ctx.console.print(
            "[yellow]%d location(s) were inaccessible and skipped.[/yellow]" % result.error_count
        )
    return result


def _report_scan(ctx: Context, result: ScanResult) -> None:
    """Print the outcome of a scan."""
    rows: List[Tuple[str, Any]] = [
        ("Root", str(result.root)),
        ("Files found", "{:,}".format(len(result.files))),
        ("Directories visited", "{:,}".format(result.scanned_dirs)),
        ("Total size", result.total_size_text),
        ("Elapsed", "%.2f s" % result.elapsed),
        ("Inaccessible dirs", len(result.skipped_dirs)),
        ("Inaccessible files", len(result.skipped_files)),
        ("Excluded dirs", len(result.excluded_dirs)),
    ]
    _summary_table("Scan summary", rows, ctx.console)

    if result.skipped_dirs and ctx.console is not None:
        ctx.console.print("\n[bold yellow]Skipped directories[/bold yellow]")
        for path, reason in result.skipped_dirs[:10]:
            ctx.console.print("  [dim]%s[/dim]  <- %s" % (escape_markup(truncate(path, 70)), escape_markup(reason)))
        if len(result.skipped_dirs) > 10:
            ctx.console.print("  [dim]... and %d more[/dim]" % (len(result.skipped_dirs) - 10))

    if result.files and ctx.console is not None:
        from rich.table import Table

        buckets: Dict[str, int] = {}
        for info in result.files:
            buckets[info.extension or "(none)"] = buckets.get(info.extension or "(none)", 0) + 1
        ranked = sorted(buckets.items(), key=lambda pair: (-pair[1], pair[0]))[:15]
        table = Table(title="Top extensions", header_style="bold magenta", expand=False)
        table.add_column("Extension", style="cyan")
        table.add_column("Files", style="white", justify="right")
        for extension, count in ranked:
            table.add_row(escape_markup(extension), "{:,}".format(count))
        ctx.console.print(table)


# --------------------------------------------------------------------------- #
# Sub commands
# --------------------------------------------------------------------------- #
def cmd_scan(ctx: Context) -> int:
    """Inventory a directory and print statistics."""
    result = _scan_tree(ctx, ctx.args.path)
    _report_scan(ctx, result)
    return EXIT_OK


def cmd_report(ctx: Context) -> int:
    """Summarize suggestions and preview the classification move plan."""
    root = Path(str(ctx.args.path)).expanduser()
    dest = Path(str(ctx.args.dest)).expanduser() if ctx.args.dest else root
    result, classification, actions = _organization_plan(ctx, root, dest)
    categories: Dict[str, int] = {}
    for info in result.files:
        category = info.category or CATEGORY_OTHERS
        categories[category] = categories.get(category, 0) + 1
    duplicates = DuplicateChecker(result.files, console=None).find()
    empty_dirs = Cleaner().find_empty_dirs(
        root, protected={str(root), str(dest), str(ctx.trash_dir_for(root))},
        exclude_names=ctx.extra_excludes(),
    )
    reclaimable = sum(group.recoverable_bytes for group in duplicates)
    _summary_table("Organization suggestions", [
        ("Files scanned", len(result.files)),
        ("Classified by rules", classification.rule),
        ("Classified by AI or cache", classification.ai + classification.cache),
        ("Needs review (Others)", classification.unresolved),
        ("Duplicate groups", len(duplicates)),
        ("Duplicate copies", sum(len(group.files) - 1 for group in duplicates)),
        ("Potential space after trash is emptied", human_readable_size(reclaimable)),
        ("Empty directories", len(empty_dirs)),
    ], ctx.console)
    if categories:
        _summary_table("Suggested categories", sorted(categories.items()), ctx.console)
    ctx.print("\nClassification move plan: %d file(s) -> %s" % (len(actions), escape_markup(str(dest))))
    if ctx.console is not None:
        Executor(console=ctx.console, config=ctx.config).preview(
            actions, limit=int(getattr(ctx.args, "preview", 25) or 25)
        )
    else:
        for action in actions[:int(getattr(ctx.args, "preview", 25) or 25)]:
            ctx.print("  %s -> %s" % (action.source, action.destination))
        if len(actions) > int(getattr(ctx.args, "preview", 25) or 25):
            ctx.print("  ... and %d more" % (len(actions) - int(getattr(ctx.args, "preview", 25) or 25)))
    for group in duplicates:
        ctx.print("\nDuplicate group (%s potentially reclaimable):" % group.recoverable_text)
        for info in group.files:
            ctx.print("  %s" % escape_markup(str(info.path)))
    if empty_dirs:
        ctx.print("\nEmpty directories:")
        for path in empty_dirs:
            ctx.print("  %s" % escape_markup(str(path)))
    ctx.print("\nThis report only previews file operations; no source files were changed.")
    return EXIT_OK


def _organization_plan(ctx: Context, root: Path, dest: Path) -> Tuple[ScanResult, Any, List[PlannedAction]]:
    """Build the same classification move plan for report and organize."""
    use_ai = not bool(ctx.args.no_ai)

    exclude_paths: Set[PathLike] = {ctx.trash_dir_for(root)}
    if dest != root:
        exclude_paths.add(dest)
    for category in ctx.config.categories:
        exclude_paths.add(dest / category)

    result = _scan_tree(ctx, root, exclude_paths)
    classifier = Classifier(config=ctx.config, cache=ctx.cache, console=ctx.console)
    report = classifier.classify_all(result.files, use_ai=use_ai)
    if report.errors and ctx.console is not None:
        for message in report.errors:
            ctx.console.print("[red]%s[/red]" % escape_markup(message))

    actions: List[Any] = []
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
        actions.append(
            _make_move_action(info.path, target_file, "category: %s (%s)" % (category, info.classification_source))
        )
    return result, report, actions


def cmd_organize(ctx: Context) -> int:
    """Classify files and move them into one folder per category."""
    root = Path(str(ctx.args.path)).expanduser()
    dest = Path(str(ctx.args.dest)).expanduser() if ctx.args.dest else root
    result, report, actions = _organization_plan(ctx, root, dest)
    if not result.files:
        ctx.print("[yellow]No file found under %s[/yellow]" % escape_markup(str(root)))
        return EXIT_OK
    _summary_table("Classification", report.as_rows(), ctx.console, color="magenta")
    Cleaner(console=ctx.console).render_category_chart(result.files)

    if not actions:
        ctx.print("[green]Everything is already organized.[/green]")
        return EXIT_OK

    executor = Executor(
        console=ctx.console,
        trash_dir=ctx.trash_dir_for(root),
        log_dir=ctx.log_dir,
        config=ctx.config,
    )
    executor.preview(actions, limit=int(getattr(ctx.args, "preview", 25) or 25))

    ctx.print("\n%d file(s) will be moved into %s" % (len(actions), escape_markup(str(dest))))
    if not executor.confirm(actions, auto_yes=bool(ctx.args.yes)):
        ctx.print("[yellow]Aborted - nothing was changed.[/yellow]")
        return EXIT_ABORTED

    outcome = executor.execute(actions)
    _summary_table("Execution", outcome.as_rows(), ctx.console, color="green")
    return EXIT_OK if outcome.failed == 0 else EXIT_ERROR


def _make_move_action(source: Path, destination: Path, reason: str) -> PlannedAction:
    """Build a ``move`` action for the organizer."""
    return PlannedAction(action=ACTION_MOVE, source=source, destination=destination, reason=reason)


def cmd_dedupe(ctx: Context) -> int:
    """Detect duplicates and let the operator choose what to keep."""
    root = Path(str(ctx.args.path)).expanduser()
    result = _scan_tree(ctx, root)
    if not result.files:
        ctx.print("[yellow]No file found under %s[/yellow]" % escape_markup(str(root)))
        return EXIT_OK

    checker = DuplicateChecker(
        files=result.files,
        console=ctx.console,
        include_empty=bool(getattr(ctx.args, "include_empty", False)),
    )
    groups = checker.find()
    if not groups:
        ctx.print("[green]No duplicate file detected.[/green]")
        return EXIT_OK

    stats = DuplicateChecker.summarize(groups)
    _summary_table(
        "Duplicates",
        [
            ("Duplicate groups", stats["groups"]),
            ("Files involved", stats["files"]),
            ("Redundant copies", stats["redundant"]),
            ("Reclaimable space", stats["wasted_text"]),
        ],
        ctx.console,
        color="magenta",
    )

    actions, skipped = checker.interactive_select(
        groups, auto_keep_newest=bool(ctx.args.keep_newest)
    )
    if skipped and ctx.console is not None:
        ctx.console.print("[dim]%d group(s) skipped.[/dim]" % len(skipped))
    if not actions:
        ctx.print("[yellow]Nothing to do.[/yellow]")
        return EXIT_OK

    dry_run = bool(ctx.args.dry_run)
    executor = Executor(
        console=ctx.console,
        trash_dir=ctx.trash_dir_for(root),
        log_dir=ctx.log_dir,
        config=ctx.config,
        dry_run=dry_run,
    )
    executor.preview(actions, limit=int(getattr(ctx.args, "preview", 25) or 25))
    ctx.print(
        "\n[bold red]%d file(s) will be moved into the recycle bin:[/bold red] %s"
        % (len(actions), escape_markup(str(ctx.trash_dir_for(root))))
    )
    if not executor.confirm(actions, auto_yes=bool(ctx.args.yes)):
        ctx.print("[yellow]Aborted - nothing was changed.[/yellow]")
        return EXIT_ABORTED

    outcome = executor.execute(actions)
    _summary_table("Execution", outcome.as_rows(), ctx.console, color="green")
    return EXIT_OK if outcome.failed == 0 else EXIT_ERROR


def cmd_clean(ctx: Context) -> int:
    """Remove empty directories."""
    root = Path(str(ctx.args.path)).expanduser()
    _scan_tree(ctx, root)
    cleaner = Cleaner(console=ctx.console)

    protected: Set[str] = {str(root), str(ctx.trash_dir_for(root))}
    empty_dirs = cleaner.find_empty_dirs(
        root,
        protected=protected,
        exclude_names=ctx.extra_excludes(),
    )

    if not empty_dirs:
        ctx.print("\n[green]No empty directory found.[/green]")
        return EXIT_OK

    ctx.print("\n[bold]%d empty directory(ies) found:[/bold]" % len(empty_dirs))
    for item in empty_dirs[:20]:
        ctx.print("  [dim]%s[/dim]" % escape_markup(str(item)))
    if len(empty_dirs) > 20:
        ctx.print("  [dim]... and %d more[/dim]" % (len(empty_dirs) - 20))

    actions = cleaner.plan_dir_removal(empty_dirs)
    dry_run = bool(ctx.args.dry_run)
    executor = Executor(
        console=ctx.console,
        trash_dir=ctx.trash_dir_for(root),
        log_dir=ctx.log_dir,
        config=ctx.config,
        dry_run=dry_run,
    )
    if not executor.confirm(actions, auto_yes=bool(ctx.args.yes)):
        ctx.print("[yellow]Aborted - nothing was changed.[/yellow]")
        return EXIT_ABORTED

    outcome = executor.execute(actions)
    _summary_table("Cleaning", outcome.as_rows(), ctx.console, color="green")
    return EXIT_OK if outcome.failed == 0 else EXIT_ERROR


def cmd_largest(ctx: Context) -> int:
    """Show the largest files without changing the directory."""
    root = Path(str(ctx.args.path)).expanduser()
    result = _scan_tree(ctx, root)
    largest = Cleaner.find_large_files(result.files, ctx.args.top)
    if not largest:
        ctx.print("[yellow]No file found under %s[/yellow]" % escape_markup(str(root)))
        return EXIT_OK
    if ctx.console is None:
        for info in largest:
            ctx.print("%s  %s" % (info.size_text, info.path))
    else:
        Cleaner(console=ctx.console).render_size_chart(
            largest, width=40, title="Top %d largest files" % len(largest)
        )
    return EXIT_OK


def cmd_smallest(ctx: Context) -> int:
    """Show the smallest files without changing the directory."""
    root = Path(str(ctx.args.path)).expanduser()
    result = _scan_tree(ctx, root)
    smallest = sorted(result.files, key=lambda info: (info.size, str(info.path)))[:ctx.args.top]
    if not smallest:
        ctx.print("[yellow]No file found under %s[/yellow]" % escape_markup(str(root)))
        return EXIT_OK
    for info in smallest:
        ctx.print("%s  %s" % (info.size_text, info.path))
    return EXIT_OK


def cmd_undo(ctx: Context) -> int:
    """Roll back a previous run."""
    manager = UndoManager(log_dir=ctx.log_dir, console=ctx.console)
    if ctx.args.list:
        manager.render_logs()
        return EXIT_OK

    log_path = ctx.args.log
    if not log_path:
        latest = manager.latest()
        if latest is None:
            ctx.print("[yellow]No undo log found in %s[/yellow]" % escape_markup(str(ctx.log_dir)))
            return EXIT_ERROR
        log_path = str(latest)

    payload = manager.load(log_path)
    if payload is None:
        ctx.print("[red]Cannot read undo log: %s[/red]" % escape_markup(str(log_path)))
        return EXIT_ERROR

    ctx.print(
        "Undo log [cyan]%s[/cyan] created at %s (%d action(s))"
        % (
            escape_markup(str(log_path)),
            escape_markup(str(payload.get("created_at_text", "-"))),
            len(payload.get("actions", []) or []),
        )
    )
    if not ctx.args.yes and ctx.console is not None:
        try:
            from rich.prompt import Prompt  # type: ignore

            answer = Prompt.ask("Roll back these actions? [y/N]", choices=["y", "n"], default="n",
                                console=ctx.console, show_choices=False)
        except (EOFError, KeyboardInterrupt):
            ctx.print("\n[yellow]Aborted.[/yellow]")
            return EXIT_ABORTED
        if str(answer).strip().lower() != "y":
            ctx.print("[yellow]Aborted - nothing was changed.[/yellow]")
            return EXIT_ABORTED

    report = manager.restore(log_path, dry_run=bool(ctx.args.dry_run))
    manager.render_report(report)
    return EXIT_OK if report.failed == 0 else EXIT_ERROR


def cmd_cache(ctx: Context) -> int:
    """Inspect or clear the AI classification cache."""
    stats = ctx.cache.stats()
    if ctx.args.clear:
        removed = ctx.cache.clear()
        ctx.print("[green]Removed %d cached entr(ies).[/green]" % removed)
        return EXIT_OK

    rows: List[Tuple[str, Any]] = [
        ("Backend", stats["mode"]),
        ("Database", stats["db_path"] or "(in-memory)"),
        ("Entries", stats["entries"]),
    ]
    _summary_table("AI cache", rows, ctx.console, color="blue")
    by_category = stats.get("by_category") or {}
    if by_category and ctx.console is not None:
        from rich.table import Table

        table = Table(title="Cached categories", header_style="bold blue", expand=False)
        table.add_column("Category", style="cyan")
        table.add_column("Entries", style="white", justify="right")
        for name, count in sorted(by_category.items(), key=lambda pair: -pair[1]):
            table.add_row(escape_markup(str(name)), str(count))
        ctx.console.print(table)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Argument parser
# --------------------------------------------------------------------------- #
class ListHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Display commands and arguments as simple terminal tables."""

    @staticmethod
    def _cell(value: str) -> str:
        """Pad using visible width, ignoring terminal colour sequences."""
        visible = re.sub(r"\x1b\[[0-9;]*m", "", value)
        return value + " " * max(0, 24 - len(visible))

    def add_arguments(self, actions: Sequence[argparse.Action]) -> None:
        if actions:
            self.add_text("| %-24s | %s\n%s" % ("Command / option", "Description", "-" * 72))
        super().add_arguments(actions)

    def _format_action(self, action: argparse.Action) -> str:
        if isinstance(action, argparse._SubParsersAction):
            return "".join(
                "  | %s | %s\n" % (self._cell(choice.dest), choice.help or "")
                for choice in action._choices_actions
            )
        name = self._format_action_invocation(action)
        description = self._expand_help(action) if action.help else ""
        return "  | %s | %s\n" % (self._cell(name), description)


def build_parser() -> argparse.ArgumentParser:
    """Build the ``argparse`` parser for every sub command."""
    parser = argparse.ArgumentParser(
        prog="file_organizer",
        description="Intelligent CLI file organizer: scan, report, classify, dedupe, clean, largest, smallest, undo.",
        formatter_class=ListHelpFormatter,
        epilog=(
            "Examples:\n"
            "  • python -m file_organizer scan ~/Downloads\n"
            "  • python -m file_organizer report ~/Downloads --dest ~/Sorted\n"
            "  • python -m file_organizer organize ~/Downloads --dest ~/Sorted\n"
            "  • python -m file_organizer dedupe ~/Downloads\n"
            "  • python -m file_organizer clean ~/Downloads\n"
            "  • python -m file_organizer largest ~/Downloads --top 10\n"
            "  • python -m file_organizer smallest ~/Downloads --top 10\n"
            "  • python -m file_organizer undo --list\n"
        ),
    )
    parser.add_argument("--version", action="version", version="file_organizer 1.0.0")
    parser.add_argument("--config", help="Override config.json path within --work-dir")
    parser.add_argument("--work-dir", help="Directory for config.json, ai_cache.db and undo_logs")
    parser.add_argument("--log-dir", help="Directory holding undo_log_*.json files")
    parser.add_argument("--trash-dir", help="Recycle bin location (default: <root>/.file_organizer_trash)")
    parser.add_argument("--exclude", action="append", default=[],
                        help="Extra directory name to skip (repeatable)")
    parser.add_argument("--preview", type=int, default=25, help="Rows shown in the plan preview")
    parser.add_argument("--quiet", action="store_true", help="Disable the progress bar")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>",
                                       parser_class=lambda **kwargs: argparse.ArgumentParser(
                                           formatter_class=ListHelpFormatter, **kwargs))
    subparsers.required = True

    # ---- scan ------------------------------------------------------------ #
    scan_parser = subparsers.add_parser("scan", help="Inventory a directory tree")
    scan_parser.add_argument("path", help="Directory to scan")
    scan_parser.set_defaults(func=cmd_scan)

    report_parser = subparsers.add_parser("report", help="Preview moves, duplicates and empty directories")
    report_parser.add_argument("path", help="Directory to inspect")
    report_parser.add_argument("--dest", help="Classification destination (default: the inspected directory)")
    report_parser.add_argument("--no-ai", action="store_true", help="Use local classification rules only")
    report_parser.set_defaults(func=cmd_report)

    # ---- organize --------------------------------------------------------- #
    organize_parser = subparsers.add_parser("organize", help="Classify and move files into category folders")
    organize_parser.add_argument("path", help="Directory to organize")
    organize_parser.add_argument("--dest", help="Destination root (default: the scanned directory)")
    organize_parser.add_argument("--no-ai", action="store_true", help="Disable the AI fallback layer")
    organize_parser.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt")
    organize_parser.set_defaults(func=cmd_organize)

    # ---- dedupe ------------------------------------------------------------ #
    dedupe_parser = subparsers.add_parser("dedupe", help="Find duplicates and choose what to keep")
    dedupe_parser.add_argument("path", help="Directory to inspect")
    dedupe_parser.add_argument("--keep-newest", action="store_true",
                               help="Automatically keep the newest copy of every group")
    dedupe_parser.add_argument("--include-empty", action="store_true",
                               help="Also consider zero byte files as duplicates")
    dedupe_parser.add_argument("--dry-run", action="store_true", help="Show the plan without applying it")
    dedupe_parser.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt")
    dedupe_parser.set_defaults(func=cmd_dedupe)

    # ---- clean -------------------------------------------------------------- #
    clean_parser = subparsers.add_parser("clean", help="Remove empty directories")
    clean_parser.add_argument("path", help="Directory to clean")
    clean_parser.add_argument("--dry-run", action="store_true", help="Show the plan without applying it")
    clean_parser.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt")
    clean_parser.set_defaults(func=cmd_clean)

    largest_parser = subparsers.add_parser("largest", help="Show the largest files without changing them")
    largest_parser.add_argument("path", help="Directory to inspect")
    largest_parser.add_argument("--top", type=int, default=10, help="How many large files to show")
    largest_parser.set_defaults(func=cmd_largest)

    smallest_parser = subparsers.add_parser("smallest", help="Show the smallest files without changing them")
    smallest_parser.add_argument("path", help="Directory to inspect")
    smallest_parser.add_argument("--top", type=int, default=10, help="How many small files to show")
    smallest_parser.set_defaults(func=cmd_smallest)

    # ---- undo ---------------------------------------------------------------- #
    undo_parser = subparsers.add_parser("undo", help="Roll back a previous run")
    undo_parser.add_argument("--log", help="Undo log to replay (default: the most recent one)")
    undo_parser.add_argument("--list", action="store_true", help="List available undo logs")
    undo_parser.add_argument("--dry-run", action="store_true", help="Report only, do not touch files")
    undo_parser.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt")
    undo_parser.set_defaults(func=cmd_undo)

    # ---- cache --------------------------------------------------------------- #
    cache_parser = subparsers.add_parser("cache", help="Inspect or clear the AI cache")
    cache_parser.add_argument("--clear", action="store_true", help="Delete every cached entry")
    cache_parser.set_defaults(func=cmd_cache)

    return parser


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Program entry point.  Returns a process exit code."""
    ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    ctx = Context(args)
    try:
        handler = getattr(args, "func", None)
        if handler is None:  # pragma: no cover - argparse guarantees it
            parser.print_help()
            return EXIT_ERROR
        return int(handler(ctx) or EXIT_OK)
    except KeyboardInterrupt:
        ctx.print("\n[yellow]Interrupted by user.[/yellow]")
        return EXIT_ABORTED
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, str):
            ctx.print("[red]%s[/red]" % escape_markup(code))
            return EXIT_ERROR
        return int(code) if code else EXIT_OK
    except Exception as exc:  # pragma: no cover - last resort safety net
        ctx.print("[bold red]Unexpected error:[/bold red] %s: %s" % (type(exc).__name__, exc))
        if bool(getattr(args, "debug", False)):
            import traceback

            ctx.print(traceback.format_exc())
        return EXIT_ERROR
    finally:
        ctx.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
