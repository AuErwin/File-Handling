"""Configuration handling for the file organizer.

The configuration is a single UTF-8 ``config.json``.  It is created with sane
defaults on first use, and merged with those defaults on every load so that an
old / partial file never breaks the program.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import CATEGORY_OTHERS
from .utils import PathLike, read_json, write_json

#: Canonical category names offered to the AI and used as folder names.
DEFAULT_CATEGORIES: List[str] = [
    "Images",
    "Videos",
    "Audio",
    "Documents",
    "Spreadsheets",
    "Presentations",
    "Archives",
    "Code",
    "Data",
    "Executables",
    "Fonts",
    "Design",
    CATEGORY_OTHERS,
]

#: extension (lower-cased, with dot) -> category
DEFAULT_EXTENSION_MAP: Dict[str, str] = {
    # ---- Images -------------------------------------------------------- #
    ".jpg": "Images", ".jpeg": "Images", ".png": "Images", ".gif": "Images",
    ".bmp": "Images", ".tif": "Images", ".tiff": "Images", ".webp": "Images",
    ".heic": "Images", ".svg": "Images", ".ico": "Images", ".raw": "Images",
    ".psd": "Design", ".ai": "Design", ".xd": "Design", ".sketch": "Design",
    ".fig": "Design", ".eps": "Design",
    # ---- Videos -------------------------------------------------------- #
    ".mp4": "Videos", ".mkv": "Videos", ".avi": "Videos", ".mov": "Videos",
    ".wmv": "Videos", ".flv": "Videos", ".webm": "Videos", ".m4v": "Videos",
    ".mpg": "Videos", ".mpeg": "Videos", ".rmvb": "Videos", ".ts": "Videos",
    # ---- Audio --------------------------------------------------------- #
    ".mp3": "Audio", ".wav": "Audio", ".flac": "Audio", ".aac": "Audio",
    ".ogg": "Audio", ".m4a": "Audio", ".wma": "Audio", ".opus": "Audio",
    ".aiff": "Audio", ".mid": "Audio", ".midi": "Audio",
    # ---- Documents ------------------------------------------------------ #
    ".pdf": "Documents", ".doc": "Documents", ".docx": "Documents",
    ".txt": "Documents", ".md": "Documents", ".rtf": "Documents",
    ".odt": "Documents", ".tex": "Documents", ".log": "Documents",
    ".epub": "Documents", ".mobi": "Documents", ".chm": "Documents",
    ".xls": "Spreadsheets", ".xlsx": "Spreadsheets", ".csv": "Spreadsheets",
    ".ods": "Spreadsheets", ".tsv": "Spreadsheets",
    ".ppt": "Presentations", ".pptx": "Presentations", ".odp": "Presentations",
    ".key": "Presentations",
    # ---- Archives ------------------------------------------------------- #
    ".zip": "Archives", ".rar": "Archives", ".7z": "Archives", ".tar": "Archives",
    ".gz": "Archives", ".bz2": "Archives", ".xz": "Archives", ".iso": "Archives",
    ".cab": "Archives", ".tgz": "Archives",
    # ---- Code ----------------------------------------------------------- #
    ".py": "Code", ".pyw": "Code", ".ipynb": "Code", ".js": "Code",
    ".ts": "Code", ".jsx": "Code", ".tsx": "Code", ".java": "Code",
    ".c": "Code", ".h": "Code", ".cpp": "Code", ".hpp": "Code", ".cc": "Code",
    ".cs": "Code", ".go": "Code", ".rs": "Code", ".rb": "Code", ".php": "Code",
    ".swift": "Code", ".kt": "Code", ".scala": "Code", ".sh": "Code",
    ".bash": "Code", ".ps1": "Code", ".bat": "Code", ".cmd": "Code",
    ".sql": "Code", ".html": "Code", ".htm": "Code", ".css": "Code",
    ".scss": "Code", ".less": "Code", ".vue": "Code", ".json": "Code",
    ".xml": "Code", ".yaml": "Code", ".yml": "Code", ".toml": "Code",
    ".ini": "Code", ".cfg": "Code", ".env": "Code", ".r": "Code", ".m": "Code",
    # ---- Data ----------------------------------------------------------- #
    ".db": "Data", ".sqlite": "Data", ".sqlite3": "Data", ".mdb": "Data",
    ".parquet": "Data", ".pkl": "Data", ".pickle": "Data", ".h5": "Data",
    ".npy": "Data", ".npz": "Data", ".bin": "Data", ".dat": "Data",
    # ---- Executables ----------------------------------------------------- #
    ".exe": "Executables", ".msi": "Executables", ".dll": "Executables",
    ".sys": "Executables", ".apk": "Executables", ".app": "Executables",
    ".deb": "Executables", ".rpm": "Executables", ".jar": "Executables",
    # ---- Fonts ----------------------------------------------------------- #
    ".ttf": "Fonts", ".otf": "Fonts", ".woff": "Fonts", ".woff2": "Fonts",
    ".eot": "Fonts",
}

#: Full default configuration document.
DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "categories": list(DEFAULT_CATEGORIES),
    "extension_map": dict(DEFAULT_EXTENSION_MAP),
    "ai": {
        "enabled": True,
        "model": "gpt-4o-mini",
        "base_url": "",
        "api_key": "",
        "api_key_env": "OPENAI_API_KEY",
        "batch_size": 20,
        "timeout": 30,
        "max_files": 400,
        "temperature": 0.0,
    },
    "scan": {
        "exclude_names": [
            ".git", ".svn", ".hg", "__pycache__", "node_modules", "$RECYCLE.BIN",
            "System Volume Information", ".file_organizer_trash",
            ".file_organizer_workspace",
        ],
        "follow_symlinks": False,
        "skip_zero_byte": False,
    },
    "clean": {
        "top_large_files": 10,
    },
    "safety": {
        "trash_dir_name": ".file_organizer_trash",
        "require_delete_confirmation": True,
    },
}


def _deep_merge(base: Dict[str, Any], override: Any) -> Dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    merged: Dict[str, Any] = dict(base)
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


class Config(object):
    """Load, query and persist the organizer configuration."""

    def __init__(self, path: Optional[PathLike] = None) -> None:
        """Create a config bound to ``path`` (defaults to ``~/.file_organizer/config.json``)."""
        if path is None:
            base = Path.home() / ".file_organizer"
            self.path: Path = base / "config.json"
        else:
            self.path = Path(str(path)).expanduser()
        self.data: Dict[str, Any] = {}
        self.load()

    # -- lifecycle -------------------------------------------------------- #
    def load(self) -> Dict[str, Any]:
        """Load the JSON file, merge it over :data:`DEFAULT_CONFIG`, and write it back if new."""
        raw = read_json(self.path, default=None)
        if isinstance(raw, dict):
            self.data = _deep_merge(DEFAULT_CONFIG, raw)
        else:
            self.data = _deep_merge(DEFAULT_CONFIG, {})
            self.save()
        self._normalize()
        return self.data

    def save(self) -> bool:
        """Persist the current configuration as UTF-8 JSON."""
        return write_json(self.path, self.data)

    def _normalize(self) -> None:
        """Coerce loaded values into the types the rest of the code expects."""
        extension_map = self.data.get("extension_map")
        if not isinstance(extension_map, dict):
            extension_map = {}
        clean_map: Dict[str, str] = {}
        for key, value in extension_map.items():
            ext = str(key).strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = "." + ext
            clean_map[ext] = str(value).strip() or CATEGORY_OTHERS
        self.data["extension_map"] = clean_map

        categories = self.data.get("categories")
        if not isinstance(categories, list) or not categories:
            categories = list(DEFAULT_CATEGORIES)
        categories = [str(item).strip() for item in categories if str(item).strip()]
        if CATEGORY_OTHERS not in categories:
            categories.append(CATEGORY_OTHERS)
        self.data["categories"] = categories

        ai = self.data.get("ai")
        if not isinstance(ai, dict):
            ai = dict(DEFAULT_CONFIG["ai"])
            self.data["ai"] = ai
        try:
            ai["batch_size"] = max(1, min(20, int(ai.get("batch_size", 20))))
        except (TypeError, ValueError):
            ai["batch_size"] = 20
        try:
            ai["max_files"] = max(0, int(ai.get("max_files", 400)))
        except (TypeError, ValueError):
            ai["max_files"] = 400
        try:
            ai["timeout"] = max(1, int(ai.get("timeout", 30)))
        except (TypeError, ValueError):
            ai["timeout"] = 30

        scan = self.data.get("scan")
        if not isinstance(scan, dict):
            scan = dict(DEFAULT_CONFIG["scan"])
            self.data["scan"] = scan
        names = scan.get("exclude_names")
        scan["exclude_names"] = [str(item) for item in names] if isinstance(names, list) else []

    # -- generic access ---------------------------------------------------- #
    def get(self, dotted_key: str, default: Any = None) -> Any:
        """Read a nested key using dot notation, e.g. ``ai.batch_size``."""
        node: Any = self.data
        for part in str(dotted_key).split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, dotted_key: str, value: Any) -> None:
        """Write a nested key using dot notation."""
        parts = [part for part in str(dotted_key).split(".") if part]
        if not parts:
            return
        node: Dict[str, Any] = self.data
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value

    # -- typed shortcuts ---------------------------------------------------- #
    @property
    def categories(self) -> List[str]:
        """Allowed category names."""
        value = self.data.get("categories")
        return value if isinstance(value, list) else list(DEFAULT_CATEGORIES)

    @property
    def extension_map(self) -> Dict[str, str]:
        """Mapping of lower-cased extension -> category."""
        value = self.data.get("extension_map")
        return value if isinstance(value, dict) else {}

    def category_for_extension(self, extension: str) -> Optional[str]:
        """Return the configured category for ``extension`` or ``None``."""
        if not extension:
            return None
        return self.extension_map.get(str(extension).strip().lower())

    @property
    def excluded_names(self) -> List[str]:
        """Directory names that the scanner must never descend into."""
        value = self.get("scan.exclude_names", [])
        return [str(item) for item in value] if isinstance(value, list) else []

    @property
    def trash_dir_name(self) -> str:
        """Name of the recycle bin directory created inside the target root."""
        return str(self.get("safety.trash_dir_name", ".file_organizer_trash"))

    @property
    def ai_settings(self) -> Dict[str, Any]:
        """Snapshot of the ``ai`` section with normalized values."""
        section = self.get("ai", {})
        if not isinstance(section, dict):
            section = {}
        merged: Dict[str, Any] = dict(DEFAULT_CONFIG["ai"])
        merged.update(section)
        # Environment overrides the file, which itself overrides the defaults.
        env_model = os.environ.get("FILE_ORGANIZER_AI_MODEL")
        if env_model:
            merged["model"] = env_model
        env_base = os.environ.get("OPENAI_BASE_URL")
        if env_base:
            merged["base_url"] = env_base
        return merged
