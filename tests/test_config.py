"""Configuration persistence, normalization, and instance isolation."""
from __future__ import annotations

import json
from pathlib import Path

from file_organizer.config import CATEGORY_OTHERS, DEFAULT_CONFIG, Config


def test_new_config_is_written_and_instances_do_not_share_defaults(tmp_path: Path) -> None:
    first = Config(tmp_path / "first.json")
    second = Config(tmp_path / "second.json")

    assert first.path.is_file()
    first.set("ai.providers.openai.model", "custom")
    first.categories.append("Custom")
    default_model = DEFAULT_CONFIG["ai"]["providers"]["openai"]["model"]
    assert second.get("ai.providers.openai.model") == default_model
    assert "Custom" not in second.categories
    assert "Custom" not in DEFAULT_CONFIG["categories"]


def test_partial_config_is_merged_and_normalized(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "categories": [" Images ", ""],
        "extension_map": {" PDF ": " Documents ", "": "ignored", "jpg": ""},
        "ai": {"batch_size": 100, "max_files": -4, "timeout": "bad"},
        "scan": {"exclude_names": ["cache", 123]},
    }), encoding="utf-8")

    config = Config(path)

    assert config.categories == ["Images", CATEGORY_OTHERS]
    assert config.category_for_extension(".PDF") == "Documents"
    assert config.category_for_extension(".jpg") == CATEGORY_OTHERS
    assert config.get("ai.batch_size") == 20
    assert config.get("ai.max_files") == 0
    assert config.get("ai.timeout") == 30
    assert config.excluded_names == ["cache", "123"]
    assert config.get("missing.key", "fallback") == "fallback"


def test_invalid_config_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{invalid", encoding="utf-8")

    config = Config(path)

    assert config.category_for_extension(".pdf") == "Documents"
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_set_save_reload_and_environment_overrides(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    config = Config(path)
    config.set("custom.nested.value", 42)
    config.set("ai.providers.openai.model", "saved-model")
    assert config.save()

    loaded = Config(path)
    assert loaded.get("custom.nested.value") == 42
    assert loaded.ai_settings["model"] == "saved-model"
    monkeypatch.setenv("FILE_ORGANIZER_AI_MODEL", "env-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    assert loaded.ai_settings["model"] == "env-model"
    assert loaded.ai_settings["base_url"] == "https://example.invalid/v1"
    assert loaded.get("ai.providers.openai.model") == "saved-model"


def test_multi_provider_settings_are_isolated_per_provider(tmp_path: Path) -> None:
    config = Config(tmp_path / "config.json")

    config.set_ai_provider_settings("openai", {"model": "gpt-4o", "api_key": "sk-openai"})
    config.set_ai_provider_settings("claude", {"model": "claude-3-5-haiku-20241022", "api_key": "sk-claude"})
    config.set("ai.provider", "claude")
    assert config.save()

    loaded = Config(config.path)
    assert loaded.ai_provider == "claude"
    assert loaded.ai_settings["model"] == "claude-3-5-haiku-20241022"
    assert loaded.ai_settings["api_key"] == "sk-claude"
    # Switching the active provider never discards the other provider's settings.
    openai_settings = loaded.ai_provider_settings("openai")
    assert openai_settings["model"] == "gpt-4o"
    assert openai_settings["api_key"] == "sk-openai"


def test_legacy_flat_ai_config_is_migrated_to_openai_provider(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "ai": {
            "model": "gpt-4-legacy",
            "base_url": "https://legacy.example/v1",
            "api_key": "legacy-key",
            "api_key_env": "LEGACY_ENV",
        },
    }), encoding="utf-8")

    config = Config(path)

    assert config.ai_provider == "openai"
    settings = config.ai_provider_settings("openai")
    assert settings["model"] == "gpt-4-legacy"
    assert settings["base_url"] == "https://legacy.example/v1"
    assert settings["api_key"] == "legacy-key"
    assert settings["api_key_env"] == "LEGACY_ENV"
    # The old flat keys are gone once migrated so they cannot shadow the new layout.
    assert "model" not in config.get("ai")
