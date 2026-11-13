"""Two-layer file classifier.

Layer 1 (local rules)
    A suffix -> category dictionary coming from ``config.json``.  O(1), free,
    deterministic.

Layer 2 (AI fallback)
    Whatever layer 1 could not resolve is sent **in batches of file names only**
    (never file content) to an OpenAI compatible ``/chat/completions`` endpoint.
    Results are persisted in SQLite so the same name is never paid for twice.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .models import CATEGORY_OTHERS, SOURCE_AI, SOURCE_CACHE, SOURCE_RULE, SOURCE_UNKNOWN, FileInfo
from .storage import CacheStore
from .utils import cache_key, chunked

#: Hard cap enforced regardless of what ``config.json`` says.
MAX_BATCH_SIZE = 20

#: Provider-agnostic environment variable checked before any provider-specific
#: name. Setting this one variable is enough to make any OpenAI-compatible key
#: work, regardless of which provider issued it.
GENERIC_API_KEY_ENV = "FILE_ORGANIZER_API_KEY"

#: Common environment variable names used by popular OpenAI-compatible
#: providers. Checked, in order, after ``ai.api_key`` / ``ai.api_key_env`` /
#: :data:`GENERIC_API_KEY_ENV` have all come up empty, so a key the user
#: already has exported for another tool is picked up automatically without
#: requiring ``api_key_env`` to be edited in ``config.json``.
FALLBACK_API_KEY_ENVS = (
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY",
    "DASHSCOPE_API_KEY",
    "ZHIPUAI_API_KEY",
    "SILICONFLOW_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    "MISTRAL_API_KEY",
)

_SYSTEM_PROMPT = (
    "You are a strict file classification engine. "
    "You receive a numbered list of FILE NAMES (never file content). "
    "Reply with a single JSON object and nothing else, mapping each index "
    "(as a string) to exactly one category from the allowed list. "
    "Example: {\"0\": \"Documents\", \"1\": \"Images\"}. "
    "Never invent a category, never add explanations, never use markdown fences."
)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


class ClassificationReport(object):
    """Counters describing how a batch of files was categorized."""

    def __init__(self) -> None:
        """Create an empty report."""
        self.rule = 0
        self.cache = 0
        self.ai = 0
        self.unresolved = 0


class AIClassifier(object):
    """Layer 2: OpenAI compatible classification with SQLite caching."""

    def __init__(
        self,
        config: Optional[Config] = None,
        cache: Optional[CacheStore] = None,
    ) -> None:
        """Create the AI classifier.

        Nothing is imported or validated here, so constructing the object is
        always cheap and safe even without the ``openai`` package installed.
        """
        self.config = config or Config()
        self.cache = cache if cache is not None else CacheStore(None)
        self._client: Any = None
        self._client_error: Optional[str] = None
        self._initialized = False

    # -- configuration ------------------------------------------------------- #
    @property
    def settings(self) -> Dict[str, Any]:
        """Normalized ``ai`` section."""
        return self.config.ai_settings

    @property
    def api_key(self) -> str:
        """Resolve an API key for any OpenAI-compatible provider.

        Any key from any provider works here, as long as a matching
        ``ai.base_url`` is set for that provider (OpenAI itself needs no
        ``base_url``). Resolution order, first non-empty value wins:

        1. ``ai.api_key`` written directly in ``config.json``.
        2. The environment variable named by ``ai.api_key_env`` (defaults to
           ``OPENAI_API_KEY``).
        3. :data:`GENERIC_API_KEY_ENV` -- a provider-agnostic variable name
           that always works, whatever provider issued the key.
        4. :data:`FALLBACK_API_KEY_ENVS` -- common provider-specific variable
           names, so a key already exported for another tool is reused
           automatically.
        """
        configured = str(self.settings.get("api_key", "") or "").strip()
        if configured:
            return configured

        env_name = str(self.settings.get("api_key_env", "OPENAI_API_KEY") or "").strip()
        if env_name:
            key = os.environ.get(env_name, "").strip()
            if key:
                return key

        key = os.environ.get(GENERIC_API_KEY_ENV, "").strip()
        if key:
            return key

        for fallback_env in FALLBACK_API_KEY_ENVS:
            if fallback_env == env_name:
                continue
            key = os.environ.get(fallback_env, "").strip()
            if key:
                return key
        return ""

    @property
    def model(self) -> str:
        """Model name used for the chat completion request."""
        return str(self.settings.get("model", "gpt-4o-mini") or "gpt-4o-mini")

    @property
    def base_url(self) -> str:
        """Optional custom endpoint (Azure/proxy/self-hosted compatible APIs)."""
        return str(self.settings.get("base_url", "") or "").strip()

    @property
    def batch_size(self) -> int:
        """Number of file names per request (clamped to :data:`MAX_BATCH_SIZE`)."""
        try:
            size = int(self.settings.get("batch_size", MAX_BATCH_SIZE))
        except (TypeError, ValueError):
            size = MAX_BATCH_SIZE
        return max(1, min(MAX_BATCH_SIZE, size))

    @property
    def max_files(self) -> int:
        """Upper bound of names sent to the API in a single run (0 = unlimited)."""
        try:
            return max(0, int(self.settings.get("max_files", 400)))
        except (TypeError, ValueError):
            return 400

    # -- client -------------------------------------------------------------- #
    def _ensure_client(self) -> bool:
        """Lazily build the OpenAI client.  Returns ``False`` with a stored reason."""
        if self._initialized:
            return self._client is not None
        self._initialized = True
        try:
            from openai import OpenAI  # type: ignore
        except Exception as exc:  # ImportError or an incompatible old release
            self._client_error = "openai is not installed (%s)" % exc
            return False
        key = self.api_key
        if not key:
            self._client_error = (
                "no API key found (set %s, or %s, or ai.api_key in config.json)"
                % (self.settings.get("api_key_env", "OPENAI_API_KEY"), GENERIC_API_KEY_ENV)
            )
            return False
        kwargs: Dict[str, Any] = {
            "api_key": key,
            "timeout": float(self.settings.get("timeout", 30) or 30),
            "max_retries": 1,
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        try:
            self._client = OpenAI(**kwargs)
            return True
        except Exception as exc:
            self._client_error = "cannot build OpenAI client (%s)" % exc
            self._client = None
            return False

    @property
    def available(self) -> bool:
        """``True`` when a request can actually be attempted."""
        return self._ensure_client()

    @property
    def last_error(self) -> Optional[str]:
        """Human readable reason why the AI layer is disabled, if any."""
        self._ensure_client()
        return self._client_error

    # -- request -------------------------------------------------------------- #
    def _build_messages(self, names: Sequence[str]) -> List[Dict[str, str]]:
        """Build the chat messages for a batch of file names."""
        listing = "\n".join("%d. %s" % (index, name) for index, name in enumerate(names))
        allowed = ", ".join(self.config.categories)
        user_prompt = (
            "Allowed categories: %s\n\n"
            "File names:\n%s\n\n"
            "Reply with the JSON object only." % (allowed, listing)
        )
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

    @staticmethod
    def _extract_json(text: str) -> Any:
        """Parse the model output, tolerating markdown fences and stray text."""
        if not text:
            return None
        candidate = text.strip()
        match = _JSON_BLOCK.search(candidate)
        if match:
            candidate = match.group(1).strip()
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            pass
        # Last resort: grab the outermost JSON object / array.
        for opener, closer in (("{", "}"), ("[", "]")):
            start = candidate.find(opener)
            end = candidate.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(candidate[start:end + 1])
                except (ValueError, TypeError):
                    continue
        return None

    def _validate(self, category: Any) -> Optional[str]:
        """Return ``category`` when it belongs to the allowed list."""
        if not category:
            return None
        text = str(category).strip()
        allowed = {item.lower(): item for item in self.config.categories}
        return allowed.get(text.lower())

    def _request_batch(self, names: Sequence[str]) -> Dict[str, str]:
        """Send one batch and return ``{file_name: category}`` (may be partial)."""
        results: Dict[str, str] = {}
        if self._client is None:
            return results
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=self._build_messages(names),
                temperature=float(self.settings.get("temperature", 0.0) or 0.0),
                max_tokens=max(200, len(names) * 24),
            )
            choices = getattr(response, "choices", None) or []
            content = ""
            if choices:
                message = getattr(choices[0], "message", None)
                content = getattr(message, "content", "") or ""
            payload = self._extract_json(content)
        except Exception as exc:
            self._client_error = "API request failed (%s)" % exc
            return results

        if isinstance(payload, list):
            normalized: Dict[Any, Any] = {}
            for position, item in enumerate(payload):
                if isinstance(item, dict):
                    key = item.get("index", position)
                    normalized[str(key)] = item.get("category")
            payload = normalized
        if not isinstance(payload, dict):
            return results

        for key, value in payload.items():
            try:
                index = int(str(key).strip())
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(names):
                category = self._validate(value)
                if category:
                    results[names[index]] = category
        return results

    # -- public API ------------------------------------------------------------ #
    def classify_names(self, names: Sequence[str]) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Classify file names, using the cache first.

        Returns:
            ``(resolved, sources)`` where ``resolved`` maps a file
            name to its category and ``sources`` maps the same name to
            ``"cache"`` or ``"ai"``.
        """
        resolved: Dict[str, str] = {}
        sources: Dict[str, str] = {}
        pending: List[str] = []

        for name in names:
            key = cache_key(name)
            cached = self.cache.get(key)
            if cached:
                resolved[name] = str(cached)
                sources[name] = SOURCE_CACHE
            else:
                pending.append(name)

        limit = self.max_files
        if limit and len(pending) > limit:
            pending = pending[:limit]

        if not pending:
            return resolved, sources

        if not self._ensure_client():
            return resolved, sources

        for batch in chunked(pending, self.batch_size):
            batch_results = self._request_batch(batch)
            if not batch_results:
                # Stop hammering a failing endpoint.
                break
            for name in batch:
                category = batch_results.get(name)
                if not category:
                    continue
                resolved[name] = category
                sources[name] = SOURCE_AI
                self.cache.set(cache_key(name), category, name, SOURCE_AI)
        return resolved, sources


class Classifier(object):
    """Apply local rules, then use :class:`AIClassifier` for unresolved files."""

    def __init__(
        self,
        config: Optional[Config] = None,
        cache: Optional[CacheStore] = None,
    ) -> None:
        """Build the two layers."""
        self.config = config or Config()
        self.ai = AIClassifier(self.config, cache)

    # -- batch ---------------------------------------------------------------- #
    def classify_all(self, files: Sequence[FileInfo], use_ai: bool = True) -> ClassificationReport:
        """Categorize every file: rules first, AI only for the leftovers."""
        report = ClassificationReport()
        unresolved: Dict[str, List[FileInfo]] = {}

        for info in files:
            category = self.config.category_for_extension(info.extension)
            if category:
                info.category = category
                info.classification_source = SOURCE_RULE
                report.rule += 1
            else:
                unresolved.setdefault(info.name, []).append(info)

        if not unresolved:
            return report

        pending_names = sorted(unresolved.keys())
        if use_ai:
            resolved, sources = self.ai.classify_names(pending_names)
            for name, items in unresolved.items():
                category = resolved.get(name) or CATEGORY_OTHERS
                source = sources.get(name, SOURCE_UNKNOWN)
                for info in items:
                    info.category = category
                    info.classification_source = source
                    if source == SOURCE_CACHE:
                        report.cache += 1
                    elif source == SOURCE_AI:
                        report.ai += 1
                    else:
                        report.unresolved += 1
        else:
            for items in unresolved.values():
                for info in items:
                    info.category = CATEGORY_OTHERS
                    info.classification_source = SOURCE_UNKNOWN
                    report.unresolved += 1
        return report
