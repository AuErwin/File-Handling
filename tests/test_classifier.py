"""Tests for :mod:`file_organizer.classifier`.

Covers the rule-based layer, the overall classification plan (rules first,
``Others`` fallback when AI is off), and the provider-agnostic API key
resolution that lets any OpenAI-compatible key be used.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from file_organizer.classifier import (
    FALLBACK_API_KEY_ENVS,
    GENERIC_API_KEY_ENV,
    AIClassifier,
    Classifier,
)
from file_organizer.config import Config
from file_organizer.models import FileInfo, SOURCE_RULE, SOURCE_UNKNOWN


def _config(tmp_path: Path) -> Config:
    return Config(path=tmp_path / "config.json")


def _info(name: str, tmp_path: Path) -> FileInfo:
    path = tmp_path / name
    path.write_text("x", encoding="utf-8")
    info = FileInfo.from_path(path)
    assert info is not None
    return info


# --------------------------------------------------------------------------- #
# Rule classification / overall plan
# --------------------------------------------------------------------------- #
def test_rule_classifier_known_extension(tmp_path: Path) -> None:
    classifier = Classifier(config=_config(tmp_path))
    info = _info("photo.jpg", tmp_path)

    report = classifier.classify_all([info], use_ai=False)

    assert report.rule == 1
    assert info.category == "Images"
    assert info.classification_source == SOURCE_RULE


def test_classify_all_uses_rules_before_ai(tmp_path: Path) -> None:
    classifier = Classifier(config=_config(tmp_path))
    files: List[FileInfo] = [
        _info("a.mp3", tmp_path),
        _info("b.zip", tmp_path),
        _info("c.py", tmp_path),
    ]

    report = classifier.classify_all(files, use_ai=False)

    assert report.rule == 3
    assert report.ai == 0
    assert report.unresolved == 0
    assert {item.category for item in files} == {"Audio", "Archives", "Code"}


def test_classify_all_unresolved_falls_back_to_others_without_ai(tmp_path: Path) -> None:
    classifier = Classifier(config=_config(tmp_path))
    files: List[FileInfo] = [_info("mystery.unknownext", tmp_path)]

    report = classifier.classify_all(files, use_ai=False)

    assert report.unresolved == 1
    assert files[0].category == "Others"
    assert files[0].classification_source == SOURCE_UNKNOWN


def test_classify_all_unresolved_without_ai_key_falls_back_to_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with ``use_ai=True``, a missing key must degrade gracefully."""
    for env_name in ("OPENAI_API_KEY", GENERIC_API_KEY_ENV) + FALLBACK_API_KEY_ENVS:
        monkeypatch.delenv(env_name, raising=False)

    classifier = Classifier(config=_config(tmp_path))
    files: List[FileInfo] = [_info("mystery.unknownext", tmp_path)]

    report = classifier.classify_all(files, use_ai=True)

    assert report.unresolved == 1
    assert files[0].category == "Others"


# --------------------------------------------------------------------------- #
# Universal API key resolution
# --------------------------------------------------------------------------- #
def test_api_key_prefers_explicit_config_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    config = _config(tmp_path)
    config.set("ai.api_key", "config-key")

    classifier = AIClassifier(config=config)

    assert classifier.api_key == "config-key"


def test_api_key_reads_configured_env_var_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MY_PROVIDER_KEY", "provider-key")
    config = _config(tmp_path)
    config.set("ai.api_key_env", "MY_PROVIDER_KEY")

    classifier = AIClassifier(config=config)

    assert classifier.api_key == "provider-key"


def test_api_key_falls_back_to_generic_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name in ("OPENAI_API_KEY",) + FALLBACK_API_KEY_ENVS:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv(GENERIC_API_KEY_ENV, "generic-key")

    classifier = AIClassifier(config=_config(tmp_path))

    assert classifier.api_key == "generic-key"


@pytest.mark.parametrize("env_name", FALLBACK_API_KEY_ENVS)
def test_api_key_falls_back_to_any_known_provider_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_name: str
) -> None:
    for other in ("OPENAI_API_KEY", GENERIC_API_KEY_ENV) + FALLBACK_API_KEY_ENVS:
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(env_name, "some-universal-key")

    classifier = AIClassifier(config=_config(tmp_path))

    assert classifier.api_key == "some-universal-key"


def test_api_key_empty_when_nothing_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name in ("OPENAI_API_KEY", GENERIC_API_KEY_ENV) + FALLBACK_API_KEY_ENVS:
        monkeypatch.delenv(env_name, raising=False)

    classifier = AIClassifier(config=_config(tmp_path))

    assert classifier.api_key == ""
    assert classifier.available is False
    assert classifier.last_error is not None
