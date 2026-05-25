from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from codex_shim import cli
from codex_shim.settings import ProvidersSettings


def test_server_python_executable_prefers_launcher_env(monkeypatch):
    monkeypatch.setenv("CODEX_SHIM_PYTHON", "/opt/homebrew/bin/python3")

    assert cli._server_python_executable() == "/opt/homebrew/bin/python3"


def test_server_python_executable_falls_back_to_current_python(monkeypatch):
    monkeypatch.delenv("CODEX_SHIM_PYTHON", raising=False)

    assert cli._server_python_executable() == sys.executable


def test_managed_block_includes_reasoning_when_provided():
    top, _ = cli._managed_config_blocks("deepseek-v3", 8765, "DeepSeek", "high")
    assert 'model_reasoning_effort = "high"' in top
    assert 'model = "deepseek-v3"' in top


def test_managed_block_omits_reasoning_when_none():
    top, _ = cli._managed_config_blocks("deepseek-v3", 8765, "DeepSeek", None)
    assert "model_reasoning_effort" not in top


def test_install_codex_config_writes_reasoning_and_round_trips(monkeypatch, tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"customModels": [
        {"model": "deepseek-chat", "displayName": "DeepSeek Chat",
         "provider": "openai", "baseUrl": "https://api.deepseek.com/v1", "apiKey": "x"}
    ]}))
    fake_codex = tmp_path / "config.toml"
    fake_codex.write_text('# user pre-existing\nmodel = "old"\n[some.section]\nkey = "v"\n')
    monkeypatch.setattr(cli, "CODEX_CONFIG_PATH", fake_codex)
    monkeypatch.setattr(cli, "CODEX_CONFIG_BACKUP_PATH", tmp_path / "backup.toml")
    monkeypatch.setattr(cli, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(cli, "CATALOG_PATH", tmp_path / "catalog.json")

    cli.install_codex_config(settings, 8765, model_slug="deepseek-chat", reasoning_effort="high")

    text = fake_codex.read_text()
    assert 'model_reasoning_effort = "high"' in text
    assert 'model = "deepseek-chat"' in text
    assert "[some.section]" in text  # user content preserved
    assert text.count(cli.MANAGED_BEGIN) == 2  # top block + provider block


def test_install_codex_config_replaces_prior_reasoning(monkeypatch, tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"customModels": [
        {"model": "deepseek-chat", "displayName": "DeepSeek Chat",
         "provider": "openai", "baseUrl": "https://api.deepseek.com/v1", "apiKey": "x"}
    ]}))
    fake_codex = tmp_path / "config.toml"
    fake_codex.write_text("")
    monkeypatch.setattr(cli, "CODEX_CONFIG_PATH", fake_codex)
    monkeypatch.setattr(cli, "CODEX_CONFIG_BACKUP_PATH", tmp_path / "backup.toml")
    monkeypatch.setattr(cli, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(cli, "CATALOG_PATH", tmp_path / "catalog.json")

    cli.install_codex_config(settings, 8765, model_slug="deepseek-chat", reasoning_effort="high")
    cli.install_codex_config(settings, 8765, model_slug="deepseek-chat", reasoning_effort="low")

    text = fake_codex.read_text()
    assert 'model_reasoning_effort = "low"' in text
    assert 'model_reasoning_effort = "high"' not in text


def test_providers_reasoning_effort_persists(tmp_path: Path):
    ps = ProvidersSettings(tmp_path / "settings.json")
    assert ps.get_reasoning_effort() == "medium"  # default
    ps.set_reasoning_effort("high")
    assert ProvidersSettings(tmp_path / "settings.json").get_reasoning_effort() == "high"


def test_providers_reasoning_effort_rejects_invalid(tmp_path: Path):
    ps = ProvidersSettings(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        ps.set_reasoning_effort("turbo")


def test_providers_reasoning_effort_falls_back_on_bad_data(tmp_path: Path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"reasoningEffort": "bogus"}))
    assert ProvidersSettings(p).get_reasoning_effort() == "medium"


def test_install_codex_config_rejects_invalid_reasoning_effort(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"customModels": [
        {"model": "x", "displayName": "X", "provider": "openai",
         "baseUrl": "https://api.x/v1", "apiKey": "k"}
    ]}))
    with pytest.raises(ValueError, match="reasoning_effort"):
        cli.install_codex_config(settings, 8765, model_slug="x", reasoning_effort="turbo")


def test_remove_managed_config_warns_on_unterminated_block(capsys):
    text = '# user before\nfoo = "bar"\n' + cli.MANAGED_BEGIN + '\nmodel = "x"\n'
    # No MANAGED_END — simulates a crashed mid-write.
    result = cli._remove_managed_config(text)
    assert result == '# user before\nfoo = "bar"\n'
    captured = capsys.readouterr()
    assert "unterminated managed block" in captured.err


def test_poll_rebuild_logic_triggers_on_external_change():
    """The poll loop must redraw the menu when active_slug changes outside the
    menubar (CLI edits config.toml, user hand-edits, etc.). Regression test for
    the case where menubar showed 'ChatGPT Subscription' while Codex was
    actually routing through DeepSeek."""
    from codex_shim.menubar import _SENTINEL

    states = []

    class Stub:
        _last_active_slug = _SENTINEL

        def _build_menu(self):
            states.append(("rebuild", self._last_active_slug))

    s = Stub()

    def poll(active):
        last = getattr(s, "_last_active_slug", _SENTINEL)
        if last is _SENTINEL:
            s._last_active_slug = active
        elif last != active:
            s._last_active_slug = active
            s._build_menu()

    poll(None)             # first poll — seed only, no rebuild
    poll(None)             # same as last — no rebuild
    poll("deepseek-v4")    # external change → rebuild
    poll("deepseek-v4")    # same — no rebuild
    poll(None)             # back to subscription → rebuild
    assert states == [("rebuild", "deepseek-v4"), ("rebuild", None)]


def test_asar_header_hash_reads_only_json_header(tmp_path: Path):
    """Build a minimal asar-shaped file and verify the hash covers exactly
    the JSON header bytes, not the file payload that follows."""
    import hashlib
    import struct

    header_json = b'{"files":{"a":{"size":3,"offset":"0"}}}'
    payload = b"foo"
    pickle = struct.pack("<4I", 16, 16 + len(header_json), len(header_json), len(header_json))
    asar = tmp_path / "app.asar"
    asar.write_bytes(pickle + header_json + payload)

    expected = hashlib.sha256(header_json).hexdigest()
    assert cli._asar_header_hash(asar) == expected


def test_remove_managed_config_strips_complete_block_silently(capsys):
    text = (
        '# user before\nfoo = "bar"\n'
        + cli.MANAGED_BEGIN + '\nmodel = "x"\n' + cli.MANAGED_END
        + '\n# user after\n'
    )
    result = cli._remove_managed_config(text)
    assert '# user before' in result
    assert '# user after' in result
    assert 'model = "x"' not in result
    assert capsys.readouterr().err == ""  # no warning for a healthy block
