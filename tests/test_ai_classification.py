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
    config.set("ai.providers.openai.api_key", "test-only")
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


def test_ai_cache_survives_reopening_without_another_request(tmp_path: Path, monkeypatch) -> None:
    config = Config(tmp_path / "config.json")
    config.set("ai.providers.openai.api_key", "test-only")
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"0": "Documents"}'))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda **kwargs: client))
    path = tmp_path / "ai_cache.db"
    with CacheStore(path) as store:
        assert AIClassifier(config, store).classify_names(["project.unknown"])[0] == {
            "project.unknown": "Documents"
        }
    with CacheStore(path) as store:
        resolved, sources = AIClassifier(config, store).classify_names(["project.unknown"])

    assert resolved == {"project.unknown": "Documents"}
    assert sources == {"project.unknown": SOURCE_CACHE}
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


def test_claude_provider_is_dispatched_through_ai_providers(tmp_path: Path, monkeypatch) -> None:
    """Switching ``ai.provider`` to ``claude`` must route requests through the
    Anthropic (non OpenAI-compatible) code path instead of the ``openai`` SDK."""
    config = Config(tmp_path / "config.json")
    config.set("ai.provider", "claude")
    config.set("ai.providers.claude.api_key", "claude-key")
    config.set("ai.providers.claude.model", "claude-3-5-haiku-20241022")

    calls = []

    def fake_chat_once(provider_id, api_key, base_url, model, **kwargs):
        calls.append((provider_id, api_key, model, kwargs.get("system_prompt"), kwargs.get("user_prompt")))
        return '{"0": "Images"}'

    import file_organizer.classifier as classifier_module

    monkeypatch.setattr(classifier_module.ai_providers, "chat_once", fake_chat_once)
    classifier = AIClassifier(config, CacheStore(None))

    resolved, sources = classifier.classify_names(["mystery.foo"])

    assert resolved == {"mystery.foo": "Images"}
    assert sources == {"mystery.foo": SOURCE_AI}
    assert len(calls) == 1
    provider_id, api_key, model, system_prompt, user_prompt = calls[0]
    assert provider_id == "claude"
    assert api_key == "claude-key"
    assert model == "claude-3-5-haiku-20241022"
    assert system_prompt  # Claude gets the instructions via the dedicated system field.
    assert "mystery.foo" in user_prompt


def test_claude_provider_request_failure_is_surfaced_as_last_error(tmp_path: Path, monkeypatch) -> None:
    config = Config(tmp_path / "config.json")
    config.set("ai.provider", "claude")
    config.set("ai.providers.claude.api_key", "claude-key")

    import file_organizer.classifier as classifier_module
    from file_organizer.ai_providers import ProviderError

    def fake_chat_once(*args, **kwargs):
        raise ProviderError("API Key 无效或未提供")

    monkeypatch.setattr(classifier_module.ai_providers, "chat_once", fake_chat_once)
    classifier = AIClassifier(config, CacheStore(None))

    resolved, sources = classifier.classify_names(["mystery.foo"])

    assert resolved == sources == {}
    assert "API Key 无效" in classifier.last_error


def test_test_connection_and_list_models_delegate_to_ai_providers(tmp_path: Path, monkeypatch) -> None:
    config = Config(tmp_path / "config.json")
    config.set("ai.provider", "deepseek")
    config.set("ai.providers.deepseek.api_key", "ds-key")

    import file_organizer.classifier as classifier_module
    from file_organizer.ai_providers import TestResult

    seen = {}

    def fake_test_connection(provider_id, api_key, base_url, model, timeout):
        seen["test"] = (provider_id, api_key, model)
        return TestResult(True, "连接成功", model=model, elapsed_ms=12.0)

    def fake_list_models(provider_id, api_key, base_url, timeout):
        seen["list"] = (provider_id, api_key)
        return ["deepseek-chat", "deepseek-reasoner"]

    monkeypatch.setattr(classifier_module.ai_providers, "test_connection", fake_test_connection)
    monkeypatch.setattr(classifier_module.ai_providers, "list_models", fake_list_models)
    classifier = AIClassifier(config, CacheStore(None))

    result = classifier.test_connection()
    models = classifier.list_models()

    assert result.ok is True
    assert seen["test"] == ("deepseek", "ds-key", "deepseek-chat")
    assert models == ["deepseek-chat", "deepseek-reasoner"]
    assert seen["list"] == ("deepseek", "ds-key")


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
