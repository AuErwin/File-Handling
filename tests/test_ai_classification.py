"""AI classification with a local fake client; no network requests or API fees."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from file_organizer.classifier import AIClassifier, Classifier
from file_organizer.config import Config
from file_organizer.models import FileInfo, SOURCE_AI, SOURCE_CACHE
from file_organizer.storage import CacheStore


def test_ai_response_is_validated_cached_and_reused(tmp_path: Path, monkeypatch) -> None:
    config = Config(tmp_path / "config.json")
    config.set("ai.api_key", "test-only")
    config.set("ai.batch_size", 2)
    requests = []

    class FakeCompletions:
        def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='```json\n{"0": "images", "1": "Invented"}\n```'
            ))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda **kwargs: client))
    cache = CacheStore(None)
    classifier = AIClassifier(config, cache)

    resolved, sources = classifier.classify_names(["mystery.foo", "other.foo"])
    assert resolved == {"mystery.foo": "Images"}
    assert sources == {"mystery.foo": SOURCE_AI}
    assert len(requests) == 1
    assert "mystery.foo" in requests[0]["messages"][1]["content"]
    assert "other.foo" in requests[0]["messages"][1]["content"]

    resolved, sources = classifier.classify_names(["mystery.foo"])
    assert resolved == {"mystery.foo": "Images"}
    assert sources == {"mystery.foo": SOURCE_CACHE}
    assert len(requests) == 1


def test_ai_file_limit_and_failed_request_do_not_hammer_endpoint(tmp_path: Path) -> None:
    config = Config(tmp_path / "config.json")
    config.set("ai.max_files", 3)
    config.set("ai.batch_size", 2)
    classifier = AIClassifier(config, CacheStore(None))
    classifier._initialized = True
    calls = []

    class BrokenCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("offline")

    classifier._client = SimpleNamespace(chat=SimpleNamespace(completions=BrokenCompletions()))

    resolved, sources = classifier.classify_names(["a.foo", "b.foo", "c.foo", "d.foo"])

    assert resolved == sources == {}
    assert len(calls) == 1
    assert "offline" in classifier.last_error


def test_ai_accepts_array_response_and_rejects_bad_indices(tmp_path: Path) -> None:
    config = Config(tmp_path / "config.json")
    classifier = AIClassifier(config)
    classifier._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='[{"index": 1, "category": "documents"}, {"index": 9, "category": "Images"}]'
        ))])
    )))

    assert classifier._request_batch(["a.foo", "b.foo"]) == {"b.foo": "Documents"}
    assert classifier._extract_json("prefix {\"0\": \"Images\"} suffix") == {"0": "Images"}
    assert classifier._extract_json("not JSON") is None


def test_overall_classifier_counts_ai_and_cache_sources(tmp_path: Path) -> None:
    config = Config(tmp_path / "config.json")
    cache = CacheStore(None)
    classifier = Classifier(config, cache)
    classifier.ai._initialized = True
    classifier.ai._client = object()
    classifier.ai._request_batch = lambda names: {name: "Images" for name in names}
    paths = [tmp_path / "one.foo", tmp_path / "two.foo"]
    for path in paths:
        path.write_text("x", encoding="utf-8")
    files = [FileInfo.from_path(path) for path in paths]
    assert all(item is not None for item in files)

    first = classifier.classify_all(files, use_ai=True)
    second = classifier.classify_all(files, use_ai=True)

    assert first.ai == 2
    assert second.cache == 2
    assert all(item.category == "Images" for item in files)
