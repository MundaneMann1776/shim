"""codex-shim macOS menu bar app.

Run with:  codex-shim-app   (after pip install -e .)
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import rumps
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

from .cli import (
    DEFAULT_PORT,
    MANAGED_BEGIN,
    CODEX_CONFIG_PATH,
    PID_PATH,
    RUNTIME_DIR,
    _healthy,
    _pid_running,
    _read_pid,
    _restore_if_managed,
    generate,
    install_codex_config,
    start,
    stop,
)
from .providers import PROVIDER_DEFS, get_models, group_openrouter_models, invalidate_cache
from .settings import DEFAULT_FACTORY_SETTINGS, FactorySettings, ProvidersSettings, slugify

SETTINGS_PATH = DEFAULT_FACTORY_SETTINGS
LOG_PATH = RUNTIME_DIR / "shim.log"

ICON_RUNNING_SF = "dot.radiowaves.right"
ICON_STOPPED_SF = "circle"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _shim_running() -> bool:
    return _pid_running(_read_pid()) and _healthy(DEFAULT_PORT)


def _active_slug() -> str | None:
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


def _slug_for(model_id: str) -> str:
    return slugify(model_id)


def _open_file(path: Path) -> None:
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("{}\n")
    subprocess.Popen(["open", str(path)])


def _show_log(_sender=None) -> None:
    if not LOG_PATH.exists():
        rumps.alert("No log yet", "The shim has not written any output yet.")
        return
    lines = LOG_PATH.read_text(errors="replace").splitlines()
    rumps.alert("Shim log (last 40 lines)", "\n".join(lines[-40:]) or "(empty)")


# ---------------------------------------------------------------------------
# Model-select callback builder
# ---------------------------------------------------------------------------

def _make_model_callback(
    app: "CodexShimApp",
    provider_key: str,
    model: dict,
) -> callable:
    def _on_select(_sender):
        ps = ProvidersSettings(SETTINGS_PATH)
        try:
            ps.upsert_custom_model(
                provider_key=provider_key,
                model_id=model["id"],
                display_name=model["name"],
                context=model["context"],
            )
        except Exception as exc:
            rumps.alert("Could not save model", str(exc))
            return

        slug = _slug_for(model["id"])
        try:
            generate(SETTINGS_PATH, DEFAULT_PORT)
            install_codex_config(SETTINGS_PATH, DEFAULT_PORT, slug)
            if not _shim_running():
                start(SETTINGS_PATH, DEFAULT_PORT)
        except Exception as exc:
            rumps.alert("Error switching model", str(exc))
            return

        app._rebuild_and_poll()

    return _on_select


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class CodexShimApp(rumps.App):
    def __init__(self):
        super().__init__("Codex Shim", title="", quit_button=None)
        # Hide from Dock — fire via a one-shot timer so the run loop is live.
        def _hide_dock(t):
            t.stop()
            NSApplication.sharedApplication().setActivationPolicy_(
                NSApplicationActivationPolicyAccessory
            )
        rumps.Timer(_hide_dock, 0.05).start()
        self._lock = threading.Lock()
        self._build_menu()
        self._timer = rumps.Timer(self._poll, 3)
        self._timer.start()
        self._poll(None)

    # ------------------------------------------------------------------
    # SF Symbol icon helper
    # ------------------------------------------------------------------

    def _set_sf_icon(self, symbol_name: str) -> None:
        """Set the status bar icon using a native SF Symbol template image.

        Template images auto-adapt to dark/light mode and the active-state
        highlight colour — the correct macOS approach for menu-bar icons.
        Falls back silently (icon stays blank/text) on any error.
        """
        try:
            from AppKit import NSImage
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                symbol_name, None
            )
            if img is None:
                return
            img.setTemplate_(True)
            btn = self._nsapp._nsstatusitem.button()
            btn.setImage_(img)
            btn.setTitle_("")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Menu construction
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        self.menu.clear()
        ps   = ProvidersSettings(SETTINGS_PATH)
        provs = ps.get_providers()
        active = _active_slug()

        any_model_added = False

        # ── GPT-5.5 passthrough (always first) ───────────────────────────
        passthrough = rumps.MenuItem(
            "Codex Subscription  (GPT-5.5)",
            callback=self._on_passthrough,
        )
        passthrough.state = (active == "gpt-5.5")
        self.menu.add(passthrough)

        self.menu.add(rumps.separator)

        # ── Per-provider model sections ───────────────────────────────────
        for pkey, pinfo in provs.items():
            if not pinfo.get("enabled", True):
                continue
            api_key = pinfo.get("apiKey", "")
            defn = PROVIDER_DEFS.get(pkey)
            if defn is None or not api_key:
                continue

            models = get_models(pkey, api_key)

            if pkey == "openrouter":
                groups = group_openrouter_models(models)
                prov_item = rumps.MenuItem(f"OpenRouter  ({len(models)} models)")
                for group_key in sorted(groups.keys()):
                    group_name = group_key.replace("-", " ").title() if group_key != "other" else "Other"
                    group_item = rumps.MenuItem(group_name)
                    for m in groups[group_key]:
                        slug = _slug_for(m["id"])
                        mi = rumps.MenuItem(
                            m["name"],
                            callback=_make_model_callback(self, pkey, m),
                        )
                        mi.state = (slug == active)
                        group_item.add(mi)
                    prov_item.add(group_item)
                if models:
                    self.menu.add(prov_item)
                    any_model_added = True
            else:
                prov_item = rumps.MenuItem(defn["name"])
                for m in models:
                    slug = _slug_for(m["id"])
                    mi = rumps.MenuItem(
                        m["name"],
                        callback=_make_model_callback(self, pkey, m),
                    )
                    mi.state = (slug == active)
                    prov_item.add(mi)
                if models:
                    self.menu.add(prov_item)
                    any_model_added = True

        if not any_model_added and not any(
            PROVIDER_DEFS.get(k) for k in provs if provs[k].get("enabled")
        ):
            self.menu.add(rumps.MenuItem("⚠  No providers — click Add API Key"))

        self.menu.add(rumps.separator)

        # ── Status & controls ─────────────────────────────────────────────
        self._item_status = rumps.MenuItem("Status: checking…")
        self.menu.add(self._item_status)
        self._item_toggle = rumps.MenuItem("Start shim", callback=self._on_toggle)
        self.menu.add(self._item_toggle)
        self.menu.add(rumps.MenuItem("Restart shim", callback=self._on_restart))
        self.menu.add(rumps.MenuItem("Refresh models", callback=self._on_refresh))

        self.menu.add(rumps.separator)

        # ── Provider management ───────────────────────────────────────────
        self.menu.add(rumps.MenuItem("Manage API Keys…",   callback=self._on_manage_keys))

        self.menu.add(rumps.separator)

        # ── Misc ─────────────────────────────────────────────────────────
        self.menu.add(rumps.MenuItem("View shim log",       callback=_show_log))
        self.menu.add(rumps.MenuItem("Open Codex config",   callback=self._on_open_codex_config))
        self.menu.add(rumps.MenuItem("Open settings.json",  callback=self._on_open_settings))

        self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("Quit  (restore Codex config)", callback=self._on_quit))

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_passthrough(self, _sender) -> None:
        try:
            generate(SETTINGS_PATH, DEFAULT_PORT)
            install_codex_config(SETTINGS_PATH, DEFAULT_PORT, "gpt-5.5")
            if not _shim_running():
                start(SETTINGS_PATH, DEFAULT_PORT)
        except Exception as exc:
            rumps.alert("Error", str(exc))
        self._rebuild_and_poll()

    def _on_toggle(self, _sender) -> None:
        with self._lock:
            if _shim_running():
                stop()
            else:
                generate(SETTINGS_PATH, DEFAULT_PORT)
                slug = _active_slug() or "gpt-5.5"
                install_codex_config(SETTINGS_PATH, DEFAULT_PORT, slug)
                start(SETTINGS_PATH, DEFAULT_PORT)
        self._rebuild_and_poll()

    def _on_restart(self, _sender) -> None:
        with self._lock:
            stop()
            generate(SETTINGS_PATH, DEFAULT_PORT)
            slug = _active_slug() or "gpt-5.5"
            install_codex_config(SETTINGS_PATH, DEFAULT_PORT, slug)
            start(SETTINGS_PATH, DEFAULT_PORT)
        self._rebuild_and_poll()

    def _on_refresh(self, _sender) -> None:
        """Force-refresh model lists from all providers (bust cache)."""
        import objc
        ps = ProvidersSettings(SETTINGS_PATH)
        provs = ps.get_providers()
        for pkey, pinfo in provs.items():
            api_key = pinfo.get("apiKey", "")
            if api_key:
                invalidate_cache(pkey, api_key)

        def _fetch_then_update():
            for k in provs:
                if provs[k].get("apiKey"):
                    get_models(k, provs[k]["apiKey"])
            # AppKit must be mutated on the main thread.
            objc.callOnMainThread(self._rebuild_and_poll)

        threading.Thread(target=_fetch_then_update, daemon=True).start()
        rumps.alert("Refreshing…", "Model lists will update momentarily.")

    def _on_manage_keys(self, _sender) -> None:
        from .dialog import show_manage_keys
        show_manage_keys(SETTINGS_PATH, app=self)

    def _on_open_settings(self, _sender=None) -> None:
        _open_file(SETTINGS_PATH)

    def _on_open_codex_config(self, _sender=None) -> None:
        _open_file(CODEX_CONFIG_PATH)

    def _on_quit(self, _sender) -> None:
        stop()
        _restore_if_managed()
        rumps.quit_application()

    # ------------------------------------------------------------------
    # Status polling (every 3 s)
    # ------------------------------------------------------------------

    def _poll(self, _timer) -> None:
        running = _shim_running()
        active  = _active_slug()
        self._set_sf_icon(ICON_RUNNING_SF if running else ICON_STOPPED_SF)
        if hasattr(self, "_item_status"):
            pid = _read_pid()
            self._item_status.title = (
                f"Running  •  pid {pid}  •  {active or '?'}"
                if running else "Stopped"
            )
        if hasattr(self, "_item_toggle"):
            self._item_toggle.title = "Stop shim" if running else "Start shim"

    def _rebuild_and_poll(self) -> None:
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
