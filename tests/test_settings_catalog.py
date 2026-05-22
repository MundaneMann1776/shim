from __future__ import annotations

import json

from codex_shim.catalog import catalog_entry
from codex_shim.settings import FactorySettings


def test_duplicate_models_get_unique_display_slugs(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "customModels": [
                    {"model": "gpt-5.5", "displayName": "Fast High", "provider": "openai", "baseUrl": "http://x/v1", "index": 1},
                    {"model": "gpt-5.5", "displayName": "Fast Low", "provider": "openai", "baseUrl": "http://x/v1", "index": 2},
                ]
            }
        )
    )
    models = FactorySettings(settings).load()
    assert [m.slug for m in models] == ["fast-high", "fast-low"]


def test_catalog_preserves_context_and_visibility():
    model = FactorySettingsFixture.one()
    entry = catalog_entry(model)
    assert entry["slug"] == "claude-opus"
    assert entry["visibility"] == "list"
    assert entry["context_window"] == 200000
    assert "free" in entry["available_in_plans"]


def test_catalog_includes_frontend_display_aliases():
    model = FactorySettingsFixture.one()
    entry = catalog_entry(model)
    assert entry["display_name"] == "Claude Opus"
    assert entry["displayName"] == "Claude Opus"
    assert entry["hidden"] is False
    assert entry["supportedReasoningEfforts"][0]["reasoningEffort"] == "low"


class FactorySettingsFixture:
    @staticmethod
    def one():
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "customModels": [
                        {
                            "model": "claude-opus",
                            "displayName": "Claude Opus",
                            "provider": "anthropic",
                            "baseUrl": "http://anthropic",
                            "maxContextLimit": 200000,
                        }
                    ]
                }
            )
        )
        return FactorySettings(path).load()[0]
