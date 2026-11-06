"""Data models shared by the scanner, classifier, cleaner and executor.

Every model is a plain :func:`dataclasses.dataclass` so it remains cheap to
create (millions of files) and trivial to serialize into the undo log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .utils import human_readable_size, normalize_extension, timestamp_to_string

# --------------------------------------------------------------------------- #
# Action verbs understood by the executor / undo manager
# --------------------------------------------------------------------------- #
ACTION_MOVE = "move"
ACTION_DELETE = "delete"
ACTION_RMDIR = "rmdir"

DESTRUCTIVE_ACTIONS = (ACTION_DELETE, ACTION_RMDIR)

# How a file was categorized.
SOURCE_RULE = "rule"
SOURCE_AI = "ai"
SOURCE_CACHE = "cache"
SOURCE_UNKNOWN = "unknown"

#: Fallback category for anything the rules and the AI cannot place.
CATEGORY_OTHERS = "Others"


# --------------------------------------------------------------------------- #
# File metadata
# --------------------------------------------------------------------------- #
@dataclass
class FileInfo:
    """A single file discovered on disk.

    Attributes:
        path: Absolute (or at least normalized) location of the file.
        size: Size in bytes.
        mtime: Last modification time as an epoch timestamp.
        extension: Lower-cased extension, e.g. ``".pdf"``.
        category: Category assigned by :class:`~file_organizer.classifier.Classifier`.
        hash_value: SHA-256 digest, filled in by the duplicate checker only.
        classification_source: ``rule`` / ``ai`` / ``cache`` / ``unknown``.
        error: Human readable error captured while stat-ing the file.
    """

    path: Path
    size: int = 0
    mtime: float = 0.0
    extension: str = ""
    category: Optional[str] = None
    hash_value: Optional[str] = None
    classification_source: str = SOURCE_UNKNOWN
    error: Optional[str] = None

    # -- convenience ------------------------------------------------------- #
    @property
    def name(self) -> str:
        """File name including its extension."""
        return self.path.name

    @property
    def parent(self) -> Path:
        """Directory containing the file."""
        return self.path.parent

    @property
    def size_text(self) -> str:
        """Human readable size, e.g. ``"1.25 MB"``."""
        return human_readable_size(self.size)

    @property
    def mtime_text(self) -> str:
        """Human readable modification time."""
        return timestamp_to_string(self.mtime)

    @property
    def short_path(self) -> str:
        """Path rendered for terminal output (kept intact, no truncation)."""
        return str(self.path)

    # -- (de)serialization -------------------------------------------------- #
    @classmethod
    def from_path(cls, path: Path) -> Optional["FileInfo"]:
        """Build a :class:`FileInfo` from ``path`` using :func:`os.stat`.

        Returns ``None`` when the file vanished or cannot be read, which keeps
        the caller free of try/except noise.
        """
        import os

        try:
            stat_result = os.stat(str(path))
        except (OSError, ValueError):
            return None
        return cls(
            path=Path(str(path)),
            size=int(stat_result.st_size),
            mtime=float(stat_result.st_mtime),
            extension=normalize_extension(path),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation of this file."""
        return {
            "path": str(self.path),
            "name": self.name,
            "size": self.size,
            "mtime": self.mtime,
            "extension": self.extension,
            "category": self.category,
            "hash": self.hash_value,
            "source": self.classification_source,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileInfo":
        """Rebuild a :class:`FileInfo` from :meth:`to_dict` output."""
        return cls(
            path=Path(str(data.get("path", ""))),
            size=int(data.get("size", 0) or 0),
            mtime=float(data.get("mtime", 0.0) or 0.0),
            extension=str(data.get("extension", "") or ""),
            category=data.get("category"),
            hash_value=data.get("hash"),
            classification_source=str(data.get("source", SOURCE_UNKNOWN)),
            error=data.get("error"),
        )


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
@dataclass
class ScanResult:
    """Outcome of a directory walk."""

    root: Path
    files: List[FileInfo] = field(default_factory=list)
    skipped_dirs: List[Tuple[str, str]] = field(default_factory=list)
    skipped_files: List[Tuple[str, str]] = field(default_factory=list)
    excluded_dirs: List[str] = field(default_factory=list)
    scanned_dirs: int = 0
    elapsed: float = 0.0

    @property
    def total_size(self) -> int:
        """Sum of the size of every discovered file, in bytes."""
        return sum(item.size for item in self.files)

    @property
    def total_size_text(self) -> str:
        """Human readable :attr:`total_size`."""
        return human_readable_size(self.total_size)

    @property
    def error_count(self) -> int:
        """Number of directories / files that could not be accessed."""
        return len(self.skipped_dirs) + len(self.skipped_files)


# --------------------------------------------------------------------------- #
# Duplicates
# --------------------------------------------------------------------------- #
@dataclass
class DuplicateGroup:
    """A set of files sharing exactly the same content."""

    hash_value: str
    size: int
    files: List[FileInfo] = field(default_factory=list)

    @property
    def recoverable_bytes(self) -> int:
        """Bytes that would be freed by keeping a single copy."""
        if len(self.files) <= 1:
            return 0
        return self.size * (len(self.files) - 1)

    @property
    def recoverable_text(self) -> str:
        """Human readable :attr:`recoverable_bytes`."""
        return human_readable_size(self.recoverable_bytes)

    @property
    def short_hash(self) -> str:
        """First 16 hex characters of the digest, for display purposes."""
        return str(self.hash_value or "")[:16]


# --------------------------------------------------------------------------- #
# Execution plan
# --------------------------------------------------------------------------- #
@dataclass
class PlannedAction:
    """One reversible unit of work handled by :class:`Executor`.

    For ``delete`` the file is *moved* into the recycle bin, therefore the
    ``destination`` field carries the trash location and stays undoable.
    """

    action: str
    source: Path
    destination: Optional[Path] = None
    reason: str = ""
    status: str = "pending"  # pending | ok | skipped | failed
    message: str = ""

    @property
    def is_destructive(self) -> bool:
        """``True`` for delete / rmdir, which require the ``DELETE`` safeguard."""
        return self.action in DESTRUCTIVE_ACTIONS

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation of this action."""
        return {
            "action": self.action,
            "source": str(self.source),
            "destination": str(self.destination) if self.destination else "",
            "reason": self.reason,
            "status": self.status,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlannedAction":
        """Rebuild a :class:`PlannedAction` from :meth:`to_dict` output."""
        destination = str(data.get("destination", "") or "")
        return cls(
            action=str(data.get("action", ACTION_MOVE)),
            source=Path(str(data.get("source", ""))),
            destination=Path(destination) if destination else None,
            reason=str(data.get("reason", "") or ""),
            status=str(data.get("status", "pending")),
            message=str(data.get("message", "") or ""),
        )
