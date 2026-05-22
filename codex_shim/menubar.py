"""codex-shim menu bar app.

Run with:
    python -m codex_shim.menubar

Or after `pip install -e .`:
    codex-shim-app
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import rumps

from .cli import (
    CODEX_CONFIG_BACKUP_PATH,
    CODEX_CONFIG_PATH,
    DEFAULT_PORT,
    MANAGED_BEGIN,
    PID_PATH,
    RUNTIME_DIR,
    _healthy,
    _pid_running,
    _read_pid,
    _resolve_model_slug,
    _restore_if_managed,
    generate,
    install_codex_config,
    start,
    stop,
)
from .settings import DEFAULT_FACTORY_SETTINGS, FactorySettings

# ---------------------------------------------------------------------------
# Icons — simple Unicode glyphs work well in the macOS menu bar
# ---------------------------------------------------------------------------
ICON_RUNNING = "⚡"   # shim is up
ICON_STOPPED = "◌"   # shim is down / no proxy
ICON_BUSY    = "◌"   # transitioning

SETTINGS_PATH = DEFAULT_FACTORY_SETTINGS
LOG_PATH = RUNTIME_DIR / "shim.log"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_model() -> str | None:
    """Return the model slug written into ~/.codex/config.toml by the shim, or None."""
    if not CODEX_CONFIG_PATH.exists():
        return None
    text = CODEX_CONFIG_PATH.read_text()
    if MANAGED_BEGIN not in text:
        return None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("model ="):
            return s.split("=", 1)[1].strip().strip('"')
    return None


def _load_models():
    try:
        return FactorySettings(SETTINGS_PATH).load()
    except Exception:
        return []


def _shim_running() -> bool:
    return _pid_running(_read_pid()) and _healthy(DEFAULT_PORT)


def _open_in_editor(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("{}\n")
    subprocess.Popen(["open", str(path)])


# ---------------------------------------------------------------------------
# Log window — shows tail of shim.log in a native text alert
# ---------------------------------------------------------------------------

def _show_log(_sender=None) -> None:
    if not LOG_PATH.exists():
        rumps.alert("No log yet", "The shim has not produced any log output yet.")
        return
    lines = LOG_PATH.read_text(errors="replace").splitlines()
    tail = "\n".join(lines[-40:]) or "(empty)"
    rumps.alert(title="Shim log (last 40 lines)", message=tail)


# ---------------------------------------------------------------------------
# Model config window
# ---------------------------------------------------------------------------

class ModelConfigWindow:
    """Simple dialog to add/edit a custom model entry."""

    def show(self, existing: dict[str, Any] | None = None) -> dict[str, Any] | None:
        fields = ["Display name", "Model ID", "Provider (openai / generic-chat-completion-api / anthropic)",
                  "Base URL", "API Key", "Max context limit (optional)"]
        defaults = ["", "", "generic-chat-completion-api", "", "", ""]
        if existing:
            defaults = [
                existing.get("displayName", ""),
                existing.get("model", ""),
                existing.get("provider", "generic-chat-completion-api"),
                existing.get("baseUrl", ""),
                existing.get("apiKey", ""),
                str(existing.get("maxContextLimit", "") or ""),
            ]

        responses = []
        for field, default in zip(fields, defaults):
            w = rumps.Window(
                message=field,
                title="Add / Edit Model",
                default_text=default,
                ok="Next",
                cancel="Cancel",
            )
            resp = w.run()
            if not resp.clicked:
                return None
            responses.append(resp.text.strip())

        display_name, model_id, provider, base_url, api_key, ctx = responses
        if not model_id or not provider or not base_url:
            rumps.alert("Missing fields", "Model ID, Provider, and Base URL are required.")
            return None

        entry: dict[str, Any] = {
            "model": model_id,
            "provider": provider,
            "baseUrl": base_url,
            "displayName": display_name or model_id,
        }
        if api_key:
            entry["apiKey"] = api_key
        if ctx.isdigit():
            entry["maxContextLimit"] = int(ctx)
        return entry


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class CodexShimApp(rumps.App):
    def __init__(self):
        super().__init__(ICON_STOPPED, quit_button=None)
        self._lock = threading.Lock()
        self._build_menu()
        # Poll status every 3 s
        self._timer = rumps.Timer(self._poll, 3)
        self._timer.start()
        # Reflect current state immediately
        self._poll(None)

    # ------------------------------------------------------------------
    # Menu construction
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        self.menu.clear()
        models = _load_models()
        active = _current_model()

        # --- Model selector ---
        if models:
            for m in models:
                item = rumps.MenuItem(
                    m.display_name,
                    callback=self._on_model_select,
                )
                item.state = (m.slug == active)
                self.menu.add(item)
            # GPT-5.5 passthrough
            passthrough = rumps.MenuItem("GPT-5.5 (ChatGPT passthrough)", callback=self._on_model_select)
            passthrough.state = (active == "gpt-5.5")
            self.menu.add(passthrough)
        else:
            self.menu.add(rumps.MenuItem("⚠ No models configured", callback=self._open_settings))

        self.menu.add(rumps.separator)

        # --- Control ---
        self._item_toggle = rumps.MenuItem("Start shim", callback=self._on_toggle)
        self.menu.add(self._item_toggle)
        self.menu.add(rumps.MenuItem("Restart shim", callback=self._on_restart))

        self.menu.add(rumps.separator)

        # --- Status line (non-clickable label) ---
        self._item_status = rumps.MenuItem("Status: checking…")
        self.menu.add(self._item_status)

        self.menu.add(rumps.separator)

        # --- Config / tools ---
        self.menu.add(rumps.MenuItem("Edit models (settings.json)", callback=self._open_settings))
        self.menu.add(rumps.MenuItem("Open Codex config", callback=self._open_codex_config))
        self.menu.add(rumps.MenuItem("View shim log", callback=_show_log))

        self.menu.add(rumps.separator)

        # --- Quit ---
        self.menu.add(rumps.MenuItem("Quit (restore Codex config)", callback=self._on_quit))

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_model_select(self, sender: rumps.MenuItem) -> None:
        models = _load_models()
        # Match by display name
        target_slug = None
        if sender.title == "GPT-5.5 (ChatGPT passthrough)":
            target_slug = "gpt-5.5"
        else:
            for m in models:
                if m.display_name == sender.title:
                    target_slug = m.slug
                    break
        if target_slug is None:
            rumps.alert("Unknown model", f"Could not resolve model: {sender.title}")
            return

        was_running = _shim_running()
        try:
            generate(SETTINGS_PATH, DEFAULT_PORT)
            if was_running:
                install_codex_config(SETTINGS_PATH, DEFAULT_PORT, target_slug)
            else:
                # Not running — just write config so it's ready when started
                install_codex_config(SETTINGS_PATH, DEFAULT_PORT, target_slug)
        except Exception as exc:
            rumps.alert("Error switching model", str(exc))
            return

        self._rebuild_and_poll()

    def _on_toggle(self, _sender) -> None:
        with self._lock:
            if _shim_running():
                stop()
            else:
                generate(SETTINGS_PATH, DEFAULT_PORT)
                active = _current_model() or "gpt-5.5"
                install_codex_config(SETTINGS_PATH, DEFAULT_PORT, active)
                start(SETTINGS_PATH, DEFAULT_PORT)
        self._rebuild_and_poll()

    def _on_restart(self, _sender) -> None:
        with self._lock:
            stop()
            generate(SETTINGS_PATH, DEFAULT_PORT)
            active = _current_model() or "gpt-5.5"
            install_codex_config(SETTINGS_PATH, DEFAULT_PORT, active)
            start(SETTINGS_PATH, DEFAULT_PORT)
        self._rebuild_and_poll()

    @rumps.clicked("Edit models (settings.json)")
    def _open_settings(self, _sender=None) -> None:
        _open_in_editor(SETTINGS_PATH)

    @rumps.clicked("Open Codex config")
    def _open_codex_config(self, _sender=None) -> None:
        _open_in_editor(CODEX_CONFIG_PATH)

    def _on_quit(self, _sender) -> None:
        stop()
        _restore_if_managed()
        rumps.quit_application()

    # ------------------------------------------------------------------
    # Status polling
    # ------------------------------------------------------------------

    def _poll(self, _timer) -> None:
        running = _shim_running()
        pid = _read_pid()
        active = _current_model()
        self.title = ICON_RUNNING if running else ICON_STOPPED
        if hasattr(self, "_item_status"):
            if running:
                self._item_status.title = f"Running  •  pid {pid}  •  {active or '?'}"
            else:
                self._item_status.title = "Stopped"
        if hasattr(self, "_item_toggle"):
            self._item_toggle.title = "Stop shim" if running else "Start shim"
        # Refresh checkmarks on model items
        models = _load_models()
        slug_by_display = {m.display_name: m.slug for m in models}
        slug_by_display["GPT-5.5 (ChatGPT passthrough)"] = "gpt-5.5"
        for key, item in self.menu.items():
            if key in slug_by_display:
                item.state = (slug_by_display[key] == active)

    def _rebuild_and_poll(self) -> None:
        """Rebuild the full menu then sync status."""
        self._build_menu()
        self._poll(None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = CodexShimApp()
    app.run()


if __name__ == "__main__":
    main()
