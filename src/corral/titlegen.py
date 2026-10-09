"""Session title generation through the shared OpenAI-compatible LLM gateway.

Config file (optional): ``~/.config/corral/llm-gateway.json``

Keys:
- ``base_url`` — explicitly configured OpenAI-compatible root
- ``api_key`` — gateway virtual key (required for generation; never logged)
- ``model`` — optional gateway alias override; when omitted the live
  ``/v1/models`` catalog is queried and ``budget-chat`` is preferred when present

Environment overrides: ``CORRAL_LLM_GATEWAY_URL``, ``CORRAL_LLM_GATEWAY_KEY``,
``CORRAL_TITLE_MODEL``, ``CORRAL_LLM_GATEWAY_CONFIG``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

ENV_GATEWAY_URL = "CORRAL_LLM_GATEWAY_URL"
ENV_GATEWAY_KEY = "CORRAL_LLM_GATEWAY_KEY"
ENV_MODEL = "CORRAL_TITLE_MODEL"
ENV_CONFIG = "CORRAL_LLM_GATEWAY_CONFIG"
DEFAULT_GATEWAY_URL = ""
DEFAULT_CONFIG_PATH = Path("~/.config/corral/llm-gateway.json").expanduser()
PREFERRED_MODEL_ALIAS = "budget-chat"
_MODEL_CATALOG_TTL_SECONDS = 60.0

# (fetched_at, chosen_alias); cleared by tests via clear_model_cache().
_model_cache: tuple[float, str] | None = None


def _setting(env_name: str, config_name: str, default: str = "") -> str:
    """Read an environment setting first, then the durable user config."""
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    try:
        config = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    value = config.get(config_name, "") if isinstance(config, dict) else ""
    return str(value).strip() or default


def _config_path() -> Path:
    configured = os.environ.get(ENV_CONFIG, "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_CONFIG_PATH


def gateway_url() -> str:
    return _setting(ENV_GATEWAY_URL, "base_url", DEFAULT_GATEWAY_URL).rstrip("/")


def gateway_key() -> str | None:
    key = _setting(ENV_GATEWAY_KEY, "api_key")
    return key or None


def configured_title_model() -> str | None:
    """Explicit model override from env or config; does not consult the catalog."""
    # CORRAL_TITLE_MODEL is intentionally the only model override. Provider
    # names and fallback mapping remain entirely inside the gateway.
    return _setting(ENV_MODEL, "model") or None


def title_model() -> str | None:
    """Resolve the alias used for the next title request."""
    return configured_title_model() or _live_preferred_model()


def clear_model_cache() -> None:
    """Drop the short-lived live-catalog preference (tests / config changes)."""
    global _model_cache
    _model_cache = None


def _live_preferred_model() -> str | None:
    """Pick a live gateway alias; prefer budget-chat; never invent names."""
    global _model_cache
    now = time.monotonic()
    if _model_cache is not None and now - _model_cache[0] < _MODEL_CATALOG_TTL_SECONDS:
        return _model_cache[1]
    aliases = _fetch_model_aliases()
    if not aliases:
        return None
    chosen = PREFERRED_MODEL_ALIAS if PREFERRED_MODEL_ALIAS in aliases else aliases[0]
    _model_cache = (now, chosen)
    return chosen


def _fetch_model_aliases() -> list[str]:
    request = urllib.request.Request(
        f"{gateway_url()}/models",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read(1_000_000).decode("utf-8"))
    except (OSError, TimeoutError, ValueError, UnicodeError, urllib.error.HTTPError):
        return []
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    if not isinstance(data, list):
        return []
    aliases: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        alias = item.get("id")
        if isinstance(alias, str) and alias.strip():
            aliases.append(alias.strip())
    return aliases


class TitleGenerator(ABC):
    """Compatibility interface consumed by the existing title pipeline."""

    id = "gateway"
    executable = ""

    def is_available(self) -> bool:
        # Both the destination and virtual key must be explicitly configured.
        # The model alias can be resolved from the live catalog at request time.
        return bool(gateway_url()) and gateway_key() is not None

    @abstractmethod
    def generate(self, prompt: str, timeout: int) -> str | None:
        """Return the gateway assistant text, or None on any failure."""


class GatewayTitleGenerator(TitleGenerator):
    """Submit one bounded request to the shared gateway."""

    def generate(self, prompt: str, timeout: int) -> str | None:
        if not self.is_available():
            return None
        key = gateway_key()
        model = title_model()
        if not key or not model:
            return None
        payload = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                # 2048, not 256: openrouter-auto can route to a reasoning model
                # whose thinking eats the whole output budget (finish=length with
                # content=null at 256; single-session prompts still overflow 1024
                # on unlucky samples with 4000+ chars of thinking; verified
                # 2026-09-23). Cost stays ~$0.0006/call.
                "max_tokens": 2048,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{gateway_url()}/chat/completions",
            data=payload,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read(1_000_000).decode("utf-8"))
        except (OSError, TimeoutError, ValueError, UnicodeError, urllib.error.HTTPError):
            return None
        return _assistant_text(body)


def _assistant_text(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    # Some routed models (e.g. via openrouter-auto) return the OpenAI-style
    # content-block list instead of a plain string. Join text blocks; anything
    # else stays a transport failure so the batch retries later.
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        text = "".join(parts)
        return text or None
    return None


_GENERATOR = GatewayTitleGenerator()


def available_generators() -> tuple[TitleGenerator, ...]:
    """Expose the gateway candidate when a virtual key is configured."""
    return (_GENERATOR,) if _GENERATOR.is_available() else ()


def resolve_generator() -> TitleGenerator | None:
    """Compatibility helper for callers expecting one generator."""
    generators = available_generators()
    return generators[0] if generators else None
