"""Configuration handling for the file organizer.

The configuration is a single UTF-8 ``config.json``.  It is created with sane
defaults on first use, and merged with those defaults on every load so that an
old / partial file never breaks the program.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ai_providers import PROVIDER_INFO, PROVIDER_OPENAI, resolve_base_url
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
    ".mpg": "Videos", ".mpeg": "Videos", ".rmvb": "Videos",
    # Note: ".ts" is ambiguous (MPEG transport stream vs. TypeScript source).
    # TypeScript projects are far more common in a general downloads folder,
    # so it is classified as "Code" below; that mapping wins.
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
        # Which of ``ai.providers`` below is currently active. Switching this
        # does not discard the other providers' settings.
        "provider": PROVIDER_OPENAI,
        "batch_size": 20,
        "timeout": 30,
        "max_files": 400,
        "temperature": 0.0,
        # Each provider keeps its own model / endpoint / key, so switching
        # providers never loses what was typed for the others.
        "providers": {
            provider_id: {
                "model": info["default_model"],
                "base_url": "",
                "api_key": "",
                "api_key_env": info["default_api_key_env"],
            }
            for provider_id, info in PROVIDER_INFO.items()
        },
    },
    "scan": {
        "exclude_names": [
            ".git", ".svn", ".hg", "__pycache__", "node_modules", "$RECYCLE.BIN",
            "System Volume Information", ".file_organizer_trash",
            ".file_organizer_workspace",
        ],
        "follow_symlinks": False,
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
    """Recursively merge ``override`` into a **deep copy** of ``base``.

    A shallow ``dict(base)`` would keep sharing nested dicts (such as the
    ``ai`` section) with the module-level :data:`DEFAULT_CONFIG`. Any later
    in-place mutation -- :meth:`Config.set` writes through nested dicts --
    would then silently leak into every other :class:`Config` instance that
    has not overridden that section, including ones created later in the
    same process. Deep-copying ``base`` first keeps every instance isolated.
    """
    merged: Dict[str, Any] = copy.deepcopy(base)
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = copy.deepcopy(value) if isinstance(value, (dict, list)) else value
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
            ai = copy.deepcopy(DEFAULT_CONFIG["ai"])
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

        providers = ai.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        normalized_providers: Dict[str, Dict[str, Any]] = {}
        for provider_id, info in PROVIDER_INFO.items():
            section = providers.get(provider_id)
            section = dict(section) if isinstance(section, dict) else {}
            normalized_providers[provider_id] = {
                "model": str(section.get("model") or info["default_model"]).strip() or info["default_model"],
                "base_url": str(section.get("base_url") or "").strip(),
                "api_key": str(section.get("api_key") or ""),
                "api_key_env": str(section.get("api_key_env") or info["default_api_key_env"]).strip()
                or info["default_api_key_env"],
            }
        # One-time migration from the old single-provider layout, where
        # ``ai.model`` / ``ai.base_url`` / ``ai.api_key`` / ``ai.api_key_env``
        # lived directly under ``ai`` and only OpenAI (or an OpenAI-compatible
        # endpoint) was supported.
        legacy_keys = ("model", "base_url", "api_key", "api_key_env")
        if any(key in ai for key in legacy_keys):
            legacy = normalized_providers[PROVIDER_OPENAI]
            if ai.get("model"):
                legacy["model"] = str(ai["model"]).strip() or legacy["model"]
            if ai.get("base_url"):
                legacy["base_url"] = str(ai["base_url"]).strip()
            if ai.get("api_key"):
                legacy["api_key"] = str(ai["api_key"])
            if ai.get("api_key_env"):
                legacy["api_key_env"] = str(ai["api_key_env"]).strip() or legacy["api_key_env"]
            for key in legacy_keys:
                ai.pop(key, None)
        ai["providers"] = normalized_providers

        provider = str(ai.get("provider") or PROVIDER_OPENAI).strip()
        if provider not in PROVIDER_INFO:
            provider = PROVIDER_OPENAI
        ai["provider"] = provider

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
    def ai_provider(self) -> str:
        """Id of the currently selected AI service provider (``openai`` / ``deepseek`` / ``claude``)."""
        provider = str(self.get("ai.provider", PROVIDER_OPENAI) or PROVIDER_OPENAI)
        return provider if provider in PROVIDER_INFO else PROVIDER_OPENAI

    def ai_provider_settings(self, provider_id: Optional[str] = None) -> Dict[str, Any]:
        """Normalized ``model`` / ``base_url`` / ``api_key`` / ``api_key_env`` for one provider.

        ``base_url`` is resolved to the provider's official endpoint when left
        blank, so callers never have to special-case "empty means default".
        """
        target = provider_id if provider_id in PROVIDER_INFO else self.ai_provider
        info = PROVIDER_INFO[target]
        section = self.get("ai.providers.%s" % target, {})
        section = section if isinstance(section, dict) else {}
        model = str(section.get("model") or info["default_model"]).strip() or info["default_model"]
        base_url = str(section.get("base_url") or "").strip()
        api_key = str(section.get("api_key") or "")
        api_key_env = str(section.get("api_key_env") or info["default_api_key_env"]).strip() \
            or info["default_api_key_env"]
        return {
            "provider": target,
            "model": model,
            "base_url": base_url,
            "resolved_base_url": resolve_base_url(target, base_url),
            "api_key": api_key,
            "api_key_env": api_key_env,
        }

    def set_ai_provider_settings(self, provider_id: str, settings: Dict[str, Any]) -> None:
        """Write ``model`` / ``base_url`` / ``api_key`` / ``api_key_env`` for one provider."""
        if provider_id not in PROVIDER_INFO:
            return
        for key in ("model", "base_url", "api_key", "api_key_env"):
            if key in settings:
                self.set("ai.providers.%s.%s" % (provider_id, key), settings[key])

    @property
    def ai_settings(self) -> Dict[str, Any]:
        """Snapshot of the ``ai`` section, flattened for the *active* provider.

        Shared keys (``enabled``, ``batch_size``, ``max_files``, ``timeout``,
        ``temperature``, ``provider``) come first, then the active provider's
        ``model`` / ``base_url`` / ``api_key`` / ``api_key_env`` are merged in
        under those same familiar flat names, so existing callers that only
        ever dealt with one provider keep working unchanged.
        """
        section = self.get("ai", {})
        if not isinstance(section, dict):
            section = {}
        merged: Dict[str, Any] = {
            key: value for key, value in dict(DEFAULT_CONFIG["ai"]).items() if key != "providers"
        }
        merged.update({k: v for k, v in section.items() if k != "providers"})
        merged.update(self.ai_provider_settings(merged.get("provider")))
        # Environment overrides the file, which itself overrides the defaults.
        env_model = os.environ.get("FILE_ORGANIZER_AI_MODEL")
        if env_model:
            merged["model"] = env_model
        env_base = os.environ.get("OPENAI_BASE_URL")
        if env_base:
            merged["base_url"] = env_base
        return merged
