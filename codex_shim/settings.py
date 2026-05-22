from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any


DEFAULT_FACTORY_SETTINGS = Path.home() / ".factory" / "settings.json"

# ---------------------------------------------------------------------------
# Provider-aware settings layer
# ---------------------------------------------------------------------------

class ProvidersSettings:
    """Manages the `providers` section of settings.json.

    Format::

        {
          "providers": {
            "openrouter": {"apiKey": "sk-or-v1-...", "enabled": true},
            "deepseek":   {"apiKey": "sk-...",        "enabled": true}
          },
          "customModels": [...]   ← still used by FactorySettings
        }
    """

    def __init__(self, path: Path = DEFAULT_FACTORY_SETTINGS):
        self.path = Path(path).expanduser()

    # ── raw I/O ───────────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2) + "\n")

    # ── providers ─────────────────────────────────────────────────────────

    def get_providers(self) -> dict[str, dict]:
        """Return {provider_key: {apiKey, enabled}} for all configured providers."""
        data = self._load()
        providers = dict(data.get("providers", {}))
        # Back-compat: synthesise provider entries from existing customModels
        if not providers:
            providers = self._infer_providers_from_custom_models(data)
        return providers

    def add_provider(self, provider_key: str, api_key: str) -> None:
        data = self._load()
        data.setdefault("providers", {})[provider_key] = {
            "apiKey": api_key,
            "enabled": True,
        }
        self._save(data)

    def remove_provider(self, provider_key: str) -> None:
        data = self._load()
        data.get("providers", {}).pop(provider_key, None)
        # Also purge customModels that belong to this provider
        from .providers import PROVIDER_DEFS
        defn = PROVIDER_DEFS.get(provider_key, {})
        base_url = defn.get("base_url", "")
        data["customModels"] = [
            m for m in data.get("customModels", [])
            if not str(m.get("baseUrl", "")).startswith(base_url)
        ]
        self._save(data)

    def set_enabled(self, provider_key: str, enabled: bool) -> None:
        data = self._load()
        providers = data.setdefault("providers", {})
        if provider_key in providers:
            providers[provider_key]["enabled"] = enabled
            self._save(data)

    # ── model upsert (called when user picks a model) ─────────────────────

    def upsert_custom_model(
        self,
        *,
        provider_key: str,
        model_id: str,
        display_name: str,
        context: int,
    ) -> None:
        """Add or replace a model entry in customModels, then persist."""
        from .providers import PROVIDER_DEFS

        defn = PROVIDER_DEFS.get(provider_key)
        if defn is None:
            raise ValueError(f"Unknown provider: {provider_key}")

        providers = self.get_providers()
        prov_info = providers.get(provider_key, {})
        api_key = prov_info.get("apiKey", "")

        entry = {
            "model": model_id,
            "provider": defn["wire"],
            "baseUrl": defn["base_url"],
            "apiKey": api_key,
            "displayName": display_name,
            "maxContextLimit": context,
        }

        data = self._load()
        rows: list[dict] = data.get("customModels", [])
        # replace existing entry for the same model_id, else append
        replaced = False
        for i, row in enumerate(rows):
            if row.get("model") == model_id:
                rows[i] = entry
                replaced = True
                break
        if not replaced:
            rows.append(entry)
        data["customModels"] = rows
        self._save(data)

    # ── back-compat helper ────────────────────────────────────────────────

    @staticmethod
    def _infer_providers_from_custom_models(data: dict) -> dict[str, dict]:
        """Synthesise a providers dict from legacy customModels entries."""
        from .providers import PROVIDER_DEFS

        seen: dict[str, dict] = {}
        for row in data.get("customModels", []):
            base = str(row.get("baseUrl", "")).rstrip("/")
            api_key = str(row.get("apiKey", ""))
            for pkey, defn in PROVIDER_DEFS.items():
                if base.startswith(defn["base_url"]) and pkey not in seen:
                    seen[pkey] = {"apiKey": api_key, "enabled": True}
        return seen
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PROVIDER_NAME = "factory_byok_shim"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "model"


@dataclass(frozen=True)
class FactoryModel:
    slug: str
    model: str
    display_name: str
    provider: str
    base_url: str
    api_key: str = ""
    index: int = 0
    max_context_limit: int | None = None
    max_output_tokens: int | None = None
    no_image_support: bool = False
    extra_headers: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_anthropic(self) -> bool:
        return self.provider == "anthropic"

    @property
    def is_openai_chat(self) -> bool:
        return self.provider in {"openai", "generic-chat-completion-api"}


class FactorySettings:
    def __init__(self, path: Path = DEFAULT_FACTORY_SETTINGS):
        self.path = Path(path).expanduser()

    def load(self) -> list[FactoryModel]:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        rows = data.get("customModels", [])
        model_counts: dict[str, int] = {}
        for row in rows:
            model = str(row.get("model") or "").strip()
            if model:
                model_counts[model] = model_counts.get(model, 0) + 1

        used: set[str] = set()
        models: list[FactoryModel] = []
        for fallback_index, row in enumerate(rows):
            model = str(row.get("model") or "").strip()
            provider = str(row.get("provider") or "").strip()
            base_url = str(row.get("baseUrl") or "").strip().rstrip("/")
            if not model or not provider or not base_url:
                continue

            index = int(row.get("index", fallback_index))
            display_name = str(row.get("displayName") or model).strip()
            slug_base = display_name if model_counts.get(model, 0) > 1 else model
            slug = slugify(slug_base)
            if slug in used:
                slug = f"{slug}-{index}"
            while slug in used:
                slug = f"{slug}-{len(used)}"
            used.add(slug)

            max_context = _int_or_none(row.get("maxContextLimit"))
            max_output = _int_or_none(row.get("maxOutputTokens"))
            extra_headers = {
                str(k): str(v)
                for k, v in (row.get("extraHeaders") or {}).items()
                if v is not None
            }
            models.append(
                FactoryModel(
                    slug=slug,
                    model=model,
                    display_name=display_name,
                    provider=provider,
                    base_url=base_url,
                    api_key=str(row.get("apiKey") or ""),
                    index=index,
                    max_context_limit=max_context,
                    max_output_tokens=max_output,
                    no_image_support=bool(row.get("noImageSupport", False)),
                    extra_headers=extra_headers,
                    raw=row,
                )
            )
        return models

    def by_slug_or_model(self, requested: str) -> FactoryModel | None:
        models = self.load()
        by_slug = {m.slug: m for m in models}
        if requested in by_slug:
            return by_slug[requested]
        matches = [m for m in models if m.model == requested]
        if len(matches) == 1:
            return matches[0]
        return None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def default_model_slug(models: list[FactoryModel]) -> str:
    if not models:
        return "gpt-5.5"
    # Prefer the native ChatGPT passthrough slug first
    return "gpt-5.5"
