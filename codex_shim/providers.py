"""Provider registry and dynamic model fetching with a 1-hour disk cache."""
from __future__ import annotations

import json
import ssl
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CACHE_PATH = Path.home() / ".factory" / "model_cache.json"
CACHE_TTL = 3600  # seconds


# ---------------------------------------------------------------------------
# HTTP + cache helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, headers: dict[str, str], timeout: int = 10) -> Any:
    ctx = ssl.create_default_context()
    req = Request(url, headers={"Accept": "application/json", **headers})
    with urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode())


def _cache_load() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _cache_save(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, indent=2))


def _cached(key: str, fetch_fn) -> list[dict]:
    cache = _cache_load()
    entry = cache.get(key, {})
    if entry.get("ts", 0) + CACHE_TTL > time.time():
        return entry.get("data", [])
    try:
        data = fetch_fn()
    except Exception as exc:
        print(f"[providers] fetch failed for {key}: {exc}")
        return entry.get("data", [])   # return stale on error
    cache[key] = {"ts": time.time(), "data": data}
    _cache_save(cache)
    return data


def invalidate_cache(provider_key: str, api_key: str) -> None:
    cache = _cache_load()
    cache.pop(f"{provider_key}:{api_key[:8]}", None)
    _cache_save(cache)


# ---------------------------------------------------------------------------
# Model fetchers — each returns list[{"id", "name", "context"}]
# ---------------------------------------------------------------------------

_OPENAI_CHAT_PREFIXES = ("gpt-4", "gpt-3.5", "o1", "o3", "o4", "chatgpt")
_OPENAI_EXCLUDE = (
    "instruct", "0301", "0314", "0613",
    "tts", "whisper", "dall", "embed",
    "babbage", "davinci", "curie", "ada",
    "realtime", "audio",
)


def _is_openai_chat(model_id: str) -> bool:
    m = model_id.lower()
    if any(x in m for x in _OPENAI_EXCLUDE):
        return False
    return any(m.startswith(p) for p in _OPENAI_CHAT_PREFIXES)


def fetch_openai_models(api_key: str) -> list[dict]:
    def _fetch():
        data = _get_json(
            "https://api.openai.com/v1/models",
            {"Authorization": f"Bearer {api_key}"},
        )
        models = sorted(
            [m for m in data.get("data", []) if _is_openai_chat(m["id"])],
            key=lambda m: m["id"],
        )
        return [{"id": m["id"], "name": m["id"], "context": 128_000} for m in models]

    return _cached(f"openai:{api_key[:8]}", _fetch)


def fetch_anthropic_models(api_key: str) -> list[dict]:
    def _fetch():
        data = _get_json(
            "https://api.anthropic.com/v1/models",
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        return [
            {
                "id": m["id"],
                "name": m.get("display_name", m["id"]),
                "context": 200_000,
            }
            for m in data.get("data", [])
            if "claude" in m["id"].lower()
        ]

    return _cached(f"anthropic:{api_key[:8]}", _fetch)


def fetch_deepseek_models(api_key: str) -> list[dict]:
    _FALLBACK = [
        {"id": "deepseek-chat",     "name": "DeepSeek Chat",     "context": 64_000},
        {"id": "deepseek-reasoner", "name": "DeepSeek Reasoner", "context": 64_000},
        {"id": "deepseek-v4-pro",   "name": "DeepSeek V4 Pro",   "context": 128_000},
    ]

    def _fetch():
        try:
            data = _get_json(
                "https://api.deepseek.com/models",
                {"Authorization": f"Bearer {api_key}"},
            )
            result = [
                {"id": m["id"], "name": m.get("id", m["id"]), "context": 64_000}
                for m in data.get("data", [])
            ]
            return result or _FALLBACK
        except Exception:
            return _FALLBACK

    return _cached(f"deepseek:{api_key[:8]}", _fetch)


# Featured sub-providers shown in the OpenRouter submenu.
# Models from other providers still appear under "Other".
_OR_FEATURED = (
    "anthropic", "openai", "google", "meta-llama",
    "deepseek", "mistralai", "moonshotai", "qwen",
    "x-ai", "cohere", "perplexity",
)


def fetch_openrouter_models(api_key: str) -> list[dict]:
    def _fetch():
        data = _get_json(
            "https://openrouter.ai/api/v1/models",
            {"Authorization": f"Bearer {api_key}"},
        )
        models = []
        for m in data.get("data", []):
            mid = m.get("id", "")
            if ":free" in mid:   # skip free-tier duplicates
                continue
            models.append({
                "id": mid,
                "name": m.get("name") or mid,
                "context": m.get("context_length") or 128_000,
            })
        models.sort(key=lambda m: m["id"])
        return models

    return _cached(f"openrouter:{api_key[:8]}", _fetch)


def group_openrouter_models(models: list[dict]) -> dict[str, list[dict]]:
    """Group OpenRouter models by their provider prefix (before '/')."""
    groups: dict[str, list[dict]] = {}
    for m in models:
        prefix = m["id"].split("/")[0] if "/" in m["id"] else "other"
        # collapse obscure providers into "Other"
        bucket = prefix if prefix in _OR_FEATURED else "other"
        groups.setdefault(bucket, []).append(m)
    return groups


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDER_DEFS: dict[str, dict] = {
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com",
        "wire": "openai",
        "key_hint": "sk-...",
        "fetch": fetch_openai_models,
    },
    "anthropic": {
        "name": "Anthropic",
        "base_url": "https://api.anthropic.com",
        "wire": "anthropic",
        "key_hint": "sk-ant-...",
        "fetch": fetch_anthropic_models,
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "wire": "generic-chat-completion-api",
        "key_hint": "sk-...",
        "fetch": fetch_deepseek_models,
    },
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "wire": "generic-chat-completion-api",
        "key_hint": "sk-or-v1-...",
        "fetch": fetch_openrouter_models,
    },
}


def detect_provider(api_key: str) -> str | None:
    """Best-effort provider detection from API key format."""
    k = api_key.strip()
    if k.startswith("sk-ant-"):
        return "anthropic"
    if k.startswith("sk-or-v1-"):
        return "openrouter"
    return None  # can't distinguish OpenAI vs DeepSeek automatically


def get_models(provider_key: str, api_key: str) -> list[dict]:
    """Fetch (or return cached) models for a provider."""
    defn = PROVIDER_DEFS.get(provider_key)
    if defn is None:
        return []
    return defn["fetch"](api_key)
