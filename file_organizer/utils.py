"""Standard-library helpers shared by file organizer modules."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence, Union

PathLike = Union[str, Path]

#: Read buffer used by the SHA-256 hasher (1 MiB keeps memory flat for huge files).
DEFAULT_CHUNK_SIZE = 1024 * 1024

#: Separator used when a timestamp is appended to a conflicting file name.
NAME_STAMP_FORMAT = "%Y%m%d"


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def human_readable_size(num_bytes: Union[int, float]) -> str:
    """Return ``num_bytes`` formatted with a binary unit (KiB style labels)."""
    try:
        value = float(num_bytes)
    except (TypeError, ValueError):
        return "0 B"
    if value < 0:
        value = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0:
            if unit == "B":
                return "%d B" % int(value)
            return "%.2f %s" % (value, unit)
        value /= 1024.0
    return "%.2f PB" % value


def timestamp_to_string(epoch: Union[int, float, None]) -> str:
    """Convert an epoch timestamp to ``YYYY-MM-DD HH:MM:SS`` (local time)."""
    try:
        if not epoch:
            return "-"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(epoch)))
    except (OverflowError, OSError, ValueError, TypeError):
        return "-"


def timestamp_slug(epoch: Optional[float] = None) -> str:
    """Return a compact ``YYYYmmdd_HHMMSS`` slug, used for log file names."""
    moment = time.localtime(epoch) if epoch is not None else time.localtime()
    return time.strftime("%Y%m%d_%H%M%S", moment)


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #
def normalize_extension(path: PathLike) -> str:
    """Return the lower-cased extension of ``path`` including the leading dot.

    ``"ARCHIVE.TAR.GZ"`` -> ``".gz"`` (we only look at the last suffix, which is
    what users expect when they write rules in ``config.json``).
    """
    return Path(str(path)).suffix.lower()


def unique_path(path: Path, stamp: Optional[str] = None) -> Path:
    """Return a non-existing path derived from ``path``.

    ``report.pdf`` -> ``report_20231027.pdf`` -> ``report_20231027_1.pdf`` ...
    The timestamp comes first so that repeated runs stay human readable.
    """
    candidate = Path(path)
    if not candidate.exists():
        return candidate
    marker = stamp or time.strftime(NAME_STAMP_FORMAT, time.localtime())
    suffix = candidate.suffix
    stem = candidate.stem
    index = 0
    while True:
        if index == 0:
            name = "%s_%s%s" % (stem, marker, suffix)
        else:
            name = "%s_%s_%d%s" % (stem, marker, index, suffix)
        candidate = candidate.with_name(name)
        if not candidate.exists():
            return candidate
        index += 1
        if index > 100000:  # pathological safety valve
            return candidate.with_name("%s_%s_%s%s" % (stem, marker, id(candidate), suffix))


# --------------------------------------------------------------------------- #
# Hashing helpers
# --------------------------------------------------------------------------- #
def chunked_hash(
    path: PathLike,
    algorithm: str = "sha256",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Optional[str]:
    """Stream ``path`` and return its hex digest, or ``None`` on any I/O error.

    The file is read in ``chunk_size`` blocks so a 50 GB video never ends up in
    memory.
    """
    digest = hashlib.new(algorithm)
    try:
        with open(str(path), "rb") as handle:
            while True:
                block = handle.read(chunk_size)
                if not block:
                    break
                digest.update(block)
    except (OSError, ValueError, PermissionError):
        return None
    return digest.hexdigest()


def cache_key(text: str) -> str:
    """Normalize ``text`` into a stable cache key (sha1 hex, case-insensitive)."""
    normalized = " ".join(str(text).strip().lower().split())
    return hashlib.sha1(normalized.encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------------- #
# Misc helpers
# --------------------------------------------------------------------------- #
def chunked(sequence: Sequence[Any], size: int) -> Iterator[List[Any]]:
    """Yield consecutive slices of ``sequence`` containing at most ``size`` items."""
    if size <= 0:
        size = 1
    bucket: List[Any] = []
    for item in sequence:
        bucket.append(item)
        if len(bucket) >= size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket


def is_regular_file(path: PathLike) -> bool:
    """Return ``True`` when ``path`` is a regular file (not a device/socket)."""
    try:
        return stat.S_ISREG(os.stat(str(path)).st_mode)
    except (OSError, ValueError):
        return False


def read_json(path: PathLike, default: Any = None) -> Any:
    """Read a UTF-8 JSON file, returning ``default`` when anything goes wrong."""
    target = Path(str(path))
    if not target.is_file():
        return default
    try:
        with target.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return default


def write_json(path: PathLike, payload: Any, indent: int = 2) -> bool:
    """Atomically-ish write ``payload`` as pretty printed UTF-8 JSON."""
    target = Path(str(path))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=indent, sort_keys=False)
            handle.write("\n")
        if target.exists():
            target.unlink()
        temp.replace(target)
        return True
    except (OSError, ValueError, TypeError):
        return False
