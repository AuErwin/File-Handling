"""AI service-provider registry and low level request helpers.

This module knows nothing about file names or classification; it only knows
how to *talk* to one of three OpenAI-compatible-or-not chat APIs:

* ``openai``   -- OpenAI's own Chat Completions API.
* ``deepseek`` -- DeepSeek's API, which is OpenAI-compatible.
* ``claude``   -- Anthropic's Messages API, which is **not** OpenAI compatible
  (separate ``system`` field, different response envelope, different auth
  header), so it is implemented with a small dependency-free HTTP client
  instead of the ``openai`` SDK.

:class:`file_organizer.classifier.AIClassifier` is the only caller; it adds
the caching layer and batching on top of the functions here.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, NamedTuple, Optional

#: Canonical provider identifiers used everywhere in config / UI / code.
PROVIDER_OPENAI = "openai"
PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_CLAUDE = "claude"

#: Static, maintainable fallback model lists -- used when a provider's
#: ``/models`` endpoint cannot be reached, and as the initial content of the
#: model picker before any "获取模型列表" fetch. These are real, current
#: model ids (not placeholders); the UI also lets the user type any other
#: model id by hand, so this list never has to be exhaustive or bang up to
#: date to remain useful.
_FALLBACK_MODELS: Dict[str, List[str]] = {
    PROVIDER_OPENAI: ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o4-mini"],
    PROVIDER_DEEPSEEK: ["deepseek-chat", "deepseek-reasoner"],
    PROVIDER_CLAUDE: [
        "claude-opus-4-1-20250805",
        "claude-sonnet-4-5-20250929",
        "claude-3-5-haiku-20241022",
    ],
}

#: Everything the UI / config layer needs to know about a provider besides
#: its live model list.
PROVIDER_INFO: Dict[str, Dict[str, Any]] = {
    PROVIDER_OPENAI: {
        "label": "ChatGPT（OpenAI）",
        "default_base_url": "https://api.openai.com/v1",
        "default_api_key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "models": _FALLBACK_MODELS[PROVIDER_OPENAI],
        "supports_model_listing": True,
    },
    PROVIDER_DEEPSEEK: {
        "label": "DeepSeek",
        "default_base_url": "https://api.deepseek.com",
        "default_api_key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "models": _FALLBACK_MODELS[PROVIDER_DEEPSEEK],
        "supports_model_listing": True,
    },
    PROVIDER_CLAUDE: {
        "label": "Claude（Anthropic）",
        "default_base_url": "https://api.anthropic.com",
        "default_api_key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-3-5-haiku-20241022",
        "models": _FALLBACK_MODELS[PROVIDER_CLAUDE],
        "supports_model_listing": True,
    },
}

#: Order the provider tabs/dropdown should be shown in.
PROVIDER_ORDER: List[str] = [PROVIDER_CLAUDE, PROVIDER_OPENAI, PROVIDER_DEEPSEEK]

ANTHROPIC_API_VERSION = "2023-06-01"


def provider_label(provider_id: str) -> str:
    """Human readable label for a provider id, falling back to the id itself."""
    info = PROVIDER_INFO.get(provider_id)
    return str(info["label"]) if info else str(provider_id)


def default_base_url(provider_id: str) -> str:
    """The provider's official endpoint, used whenever the user leaves it blank."""
    info = PROVIDER_INFO.get(provider_id)
    return str(info["default_base_url"]) if info else ""


def fallback_models(provider_id: str) -> List[str]:
    """Static, maintainable model list for ``provider_id``."""
    info = PROVIDER_INFO.get(provider_id)
    return list(info["models"]) if info else []


def resolve_base_url(provider_id: str, configured: str) -> str:
    """Return ``configured`` if non-empty, else the provider's official URL."""
    text = str(configured or "").strip()
    return text or default_base_url(provider_id)


class TestResult(NamedTuple):
    """Outcome of a "测试连接" click."""

    ok: bool
    message: str
    model: str = ""
    elapsed_ms: float = 0.0


class ProviderError(Exception):
    """Raised with an already-human-readable (Chinese) message."""


# --------------------------------------------------------------------------- #
# openai / deepseek -- both speak the OpenAI Chat Completions wire format.
# --------------------------------------------------------------------------- #
def _openai_client(provider_id: str, api_key: str, base_url: str, timeout: float) -> Any:
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised only without the package
        raise ProviderError("未安装 openai 库（%s）" % exc) from exc
    kwargs: Dict[str, Any] = {"api_key": api_key, "timeout": float(timeout or 30), "max_retries": 1}
    resolved = resolve_base_url(provider_id, base_url)
    if resolved:
        kwargs["base_url"] = resolved
    try:
        return OpenAI(**kwargs)
    except Exception as exc:
        raise ProviderError("无法创建 %s 客户端（%s）" % (provider_label(provider_id), exc)) from exc


def _describe_openai_exception(exc: Exception) -> str:
    """Map an ``openai`` SDK exception to a short, actionable Chinese reason."""
    try:
        import openai as openai_module  # type: ignore
    except Exception:
        openai_module = None  # type: ignore

    status = getattr(exc, "status_code", None)
    name = type(exc).__name__

    if openai_module is not None:
        if isinstance(exc, getattr(openai_module, "AuthenticationError", ())):
            return "API Key 无效或未提供"
        if isinstance(exc, getattr(openai_module, "PermissionDeniedError", ())):
            return "该 API Key 无权访问所选模型"
        if isinstance(exc, getattr(openai_module, "NotFoundError", ())):
            return "模型不存在，或 API 地址不正确"
        if isinstance(exc, getattr(openai_module, "RateLimitError", ())):
            return "请求过于频繁，或账户余额/配额不足"
        if isinstance(exc, getattr(openai_module, "APITimeoutError", ())):
            return "请求超时，请检查网络或增大超时时间"
        if isinstance(exc, getattr(openai_module, "APIConnectionError", ())):
            return "无法连接到 API 地址，请检查网络或 Base URL 是否正确"
        if isinstance(exc, getattr(openai_module, "BadRequestError", ())):
            return "请求被拒绝，通常是模型名不正确（%s）" % exc
        if isinstance(exc, getattr(openai_module, "InternalServerError", ())):
            return "服务端错误，请稍后重试"

    if status == 401:
        return "API Key 无效或未提供"
    if status == 403:
        return "该 API Key 无权访问所选模型"
    if status == 404:
        return "模型不存在，或 API 地址不正确"
    if status == 429:
        return "请求过于频繁，或账户余额/配额不足"
    text = str(exc)
    if "timed out" in text.lower() or "timeout" in name.lower():
        return "请求超时，请检查网络或增大超时时间"
    if "connection" in text.lower() or "connection" in name.lower():
        return "无法连接到 API 地址，请检查网络或 Base URL 是否正确"
    return text or name


def _openai_chat(
    provider_id: str,
    api_key: str,
    base_url: str,
    model: str,
    messages: List[Dict[str, str]],
    timeout: float,
    max_tokens: int,
    temperature: Optional[float] = None,
) -> str:
    """Send one Chat Completions request, returning the assistant text."""
    client = _openai_client(provider_id, api_key, base_url, timeout)
    kwargs: Dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        kwargs["temperature"] = temperature
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as exc:
        raise ProviderError(_describe_openai_exception(exc)) from exc
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return str(getattr(message, "content", "") or "")


def _openai_list_models(provider_id: str, api_key: str, base_url: str, timeout: float) -> List[str]:
    client = _openai_client(provider_id, api_key, base_url, timeout)
    try:
        page = client.models.list()
    except Exception as exc:
        raise ProviderError(_describe_openai_exception(exc)) from exc
    ids = [str(getattr(item, "id", "")).strip() for item in getattr(page, "data", page) or []]
    return sorted({item for item in ids if item})


# --------------------------------------------------------------------------- #
# claude -- Anthropic's Messages API (separate wire format, no SDK dependency)
# --------------------------------------------------------------------------- #
def _anthropic_request(
    method: str,
    path: str,
    api_key: str,
    base_url: str,
    timeout: float,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = resolve_base_url(PROVIDER_CLAUDE, base_url).rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout or 30)) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        payload: Dict[str, Any] = {}
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            pass
        raise ProviderError(_describe_anthropic_error(exc.code, payload)) from exc
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        if "timed out" in reason.lower():
            raise ProviderError("请求超时，请检查网络或增大超时时间") from exc
        raise ProviderError("无法连接到 API 地址（%s），请检查网络或 Base URL 是否正确" % reason) from exc
    except TimeoutError as exc:
        raise ProviderError("请求超时，请检查网络或增大超时时间") from exc


def _describe_anthropic_error(status: int, payload: Dict[str, Any]) -> str:
    error = payload.get("error") if isinstance(payload, dict) else None
    err_type = str((error or {}).get("type", ""))
    err_message = str((error or {}).get("message", ""))
    if status == 401 or err_type == "authentication_error":
        return "API Key 无效或未提供"
    if status == 403 or err_type == "permission_error":
        return "该 API Key 无权访问所选模型"
    if status == 404 or err_type == "not_found_error":
        return "模型不存在，或 API 地址不正确"
    if "credit balance" in err_message.lower():
        return "账户余额不足"
    if status == 429 or err_type == "rate_limit_error":
        return "请求过于频繁，或账户余额/配额不足"
    if err_type == "overloaded_error":
        return "服务繁忙，请稍后重试"
    if err_type == "invalid_request_error":
        return "请求参数错误，通常是模型名不正确（%s）" % (err_message or "")
    return err_message or ("HTTP %s" % status)


def _claude_chat(
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float,
    max_tokens: int,
    temperature: Optional[float] = None,
) -> str:
    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if system_prompt:
        body["system"] = system_prompt
    if temperature is not None:
        body["temperature"] = temperature
    payload = _anthropic_request("POST", "/v1/messages", api_key, base_url, timeout, body)
    blocks = payload.get("content") or []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text") or "")
    return ""


def _claude_list_models(api_key: str, base_url: str, timeout: float) -> List[str]:
    payload = _anthropic_request("GET", "/v1/models?limit=100", api_key, base_url, timeout)
    items = payload.get("data") or []
    ids = [str(item.get("id", "")).strip() for item in items if isinstance(item, dict)]
    return sorted({item for item in ids if item})


# --------------------------------------------------------------------------- #
# Public, provider-dispatching API used by AIClassifier
# --------------------------------------------------------------------------- #
def chat_once(
    provider_id: str,
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float,
    max_tokens: int,
    temperature: Optional[float] = None,
) -> str:
    """Send exactly one (non-streaming) chat request and return the text reply.

    Raises :class:`ProviderError` with an already-human-readable message on
    any failure (bad key, bad model, network error, timeout, ...).
    """
    if not api_key:
        raise ProviderError("未填写 API Key")
    if provider_id == PROVIDER_CLAUDE:
        return _claude_chat(api_key, base_url, model, system_prompt, user_prompt, timeout, max_tokens, temperature)
    if provider_id in (PROVIDER_OPENAI, PROVIDER_DEEPSEEK):
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        return _openai_chat(provider_id, api_key, base_url, model, messages, timeout, max_tokens, temperature)
    raise ProviderError("未知的服务商：%s" % provider_id)


def list_models(provider_id: str, api_key: str, base_url: str, timeout: float) -> List[str]:
    """Fetch the live model list from the provider's official listing endpoint.

    Raises :class:`ProviderError` on failure; callers should fall back to
    :func:`fallback_models` in that case rather than treating it as fatal.
    """
    if not api_key:
        raise ProviderError("未填写 API Key，无法获取模型列表")
    if provider_id == PROVIDER_CLAUDE:
        return _claude_list_models(api_key, base_url, timeout)
    if provider_id in (PROVIDER_OPENAI, PROVIDER_DEEPSEEK):
        return _openai_list_models(provider_id, api_key, base_url, timeout)
    raise ProviderError("未知的服务商：%s" % provider_id)


def test_connection(
    provider_id: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout: float,
) -> TestResult:
    """Send the smallest possible request and report whether it worked."""
    if not str(api_key or "").strip():
        return TestResult(False, "未填写 API Key")
    if not str(model or "").strip():
        return TestResult(False, "未选择 / 未填写模型")
    started = time.monotonic()
    try:
        chat_once(
            provider_id,
            api_key,
            base_url,
            model,
            system_prompt="",
            user_prompt="ping",
            timeout=timeout,
            max_tokens=4,
        )
    except ProviderError as exc:
        return TestResult(False, str(exc), model=model, elapsed_ms=(time.monotonic() - started) * 1000)
    except Exception as exc:  # pragma: no cover - safety net for unexpected errors
        return TestResult(False, str(exc), model=model, elapsed_ms=(time.monotonic() - started) * 1000)
    elapsed_ms = (time.monotonic() - started) * 1000
    return TestResult(True, "连接成功", model=model, elapsed_ms=elapsed_ms)
