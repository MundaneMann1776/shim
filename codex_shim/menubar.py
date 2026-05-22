"""codex-shim macOS menu bar app.

Run with:  codex-shim-app   (after pip install -e .)
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import rumps

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

ICON_RUNNING = "⚡"
ICON_STOPPED = "◌"


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
# "Add API Key" flow
# ---------------------------------------------------------------------------

def _run_add_provider_flow(app: "CodexShimApp") -> None:
    """Multi-step dialog: paste key → auto-detect or pick provider → save."""
    ps = ProvidersSettings(SETTINGS_PATH)

    # Step 1 — paste the key
    w = rumps.Window(
        message="Paste your API key:",
        title="Add Provider",
        default_text="",
        ok="Next",
        cancel="Cancel",
        dimensions=(400, 26),
    )
    resp = w.run()
    if not resp.clicked:
        return
    api_key = resp.text.strip()
    if not api_key:
        rumps.alert("No key entered.")
        return

    # Step 2 — pick provider (pre-select if detectable)
    from .providers import detect_provider
    guessed = detect_provider(api_key)

    provider_options = list(PROVIDER_DEFS.keys())
    provider_names   = [PROVIDER_DEFS[k]["name"] for k in provider_options]
    default_index    = provider_options.index(guessed) if guessed in provider_options else 0

    choice_text = "\n".join(
        f"  {i+1}. {name}" for i, name in enumerate(provider_names)
    )
    hint = f"(detected: {PROVIDER_DEFS[guessed]['name']})" if guessed else "(select one)"

    w2 = rumps.Window(
        message=(
            f"Which provider? {hint}\n\n{choice_text}\n\nEnter the number:"
        ),
        title="Add Provider",
        default_text=str(default_index + 1),
        ok="Add",
        cancel="Cancel",
        dimensions=(300, 26),
    )
    resp2 = w2.run()
    if not resp2.clicked:
        return

    try:
        idx = int(resp2.text.strip()) - 1
        provider_key = provider_options[idx]
    except (ValueError, IndexError):
        rumps.alert("Invalid choice.")
        return

    # Step 3 — save + fetch models
    ps.add_provider(provider_key, api_key)
    invalidate_cache(provider_key, api_key)
    rumps.alert(
        f"{PROVIDER_DEFS[provider_key]['name']} added",
        "Fetching available models in the background…",
    )
    # Kick off model fetch in background so the menu refreshes
    threading.Thread(
        target=lambda: (get_models(provider_key, api_key), app._rebuild_and_poll()),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# "Remove provider" flow
# ---------------------------------------------------------------------------

def _run_remove_provider_flow(app: "CodexShimApp") -> None:
    ps = ProvidersSettings(SETTINGS_PATH)
    providers = ps.get_providers()
    keys = [k for k in providers if providers[k].get("enabled", True)]
    if not keys:
        rumps.alert("No providers configured.")
        return
    names = [PROVIDER_DEFS.get(k, {}).get("name", k) for k in keys]
    listing = "\n".join(f"  {i+1}. {n}" for i, n in enumerate(names))
    w = rumps.Window(
        message=f"Which provider to remove?\n\n{listing}\n\nEnter the number:",
        title="Remove Provider",
        default_text="1",
        ok="Remove",
        cancel="Cancel",
        dimensions=(300, 26),
    )
    resp = w.run()
    if not resp.clicked:
        return
    try:
        idx = int(resp.text.strip()) - 1
        pkey = keys[idx]
    except (ValueError, IndexError):
        rumps.alert("Invalid choice.")
        return
    ps.remove_provider(pkey)
    rumps.alert(f"Removed {PROVIDER_DEFS.get(pkey, {}).get('name', pkey)}.")
    app._rebuild_and_poll()


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
        super().__init__(ICON_STOPPED, quit_button=None)
        self._lock = threading.Lock()
        self._build_menu()
        self._timer = rumps.Timer(self._poll, 3)
        self._timer.start()
        self._poll(None)

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
                self.menu.add(prov_item)
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
        self.menu.add(rumps.MenuItem("Add API key…",       callback=self._on_add_key))
        self.menu.add(rumps.MenuItem("Remove provider…",   callback=self._on_remove_provider))

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
        ps = ProvidersSettings(SETTINGS_PATH)
        provs = ps.get_providers()
        for pkey, pinfo in provs.items():
            api_key = pinfo.get("apiKey", "")
            if api_key:
                invalidate_cache(pkey, api_key)
        threading.Thread(
            target=lambda: (
                [get_models(k, provs[k]["apiKey"]) for k in provs if provs[k].get("apiKey")],
                self._rebuild_and_poll(),
            ),
            daemon=True,
        ).start()
        rumps.alert("Refreshing…", "Model lists will update momentarily.")

    def _on_add_key(self, _sender) -> None:
        _run_add_provider_flow(self)

    def _on_remove_provider(self, _sender) -> None:
        _run_remove_provider_flow(self)

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
        self.title = ICON_RUNNING if running else ICON_STOPPED
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
