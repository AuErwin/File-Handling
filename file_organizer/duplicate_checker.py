"""Two-stage duplicate detection.

Stage 1 groups files by **size** (cheap, pure metadata).
Stage 2 only hashes the files that share a size with at least one sibling.

Results are returned to the GUI, which chooses what to keep.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from .models import DuplicateGroup, FileInfo
from .utils import chunked_hash, human_readable_size

DEFAULT_CHUNK_SIZE = 1024 * 1024


class DuplicateChecker(object):
    """Find identical files by size and content hash."""

    def __init__(
        self,
        files: Sequence[FileInfo],
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        include_empty: bool = False,
    ) -> None:
        """Create the checker over an already scanned file list."""
        self.files: List[FileInfo] = list(files)
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
        for info in candidates:
            digest = chunked_hash(info.path, "sha256", self.chunk_size)
            if not digest:
                continue
            group = groups.get(digest)
            if group is None:
                group = DuplicateGroup(hash_value=digest, size=info.size, files=[])
                groups[digest] = group
            group.files.append(info)

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
