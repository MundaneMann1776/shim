"""Native macOS dialogs for codex-shim menu bar app.

Uses AppKit directly so dialogs look like first-party macOS alerts —
no Python Tk or rumps.Window chrome.
"""
from __future__ import annotations

from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSAlertStyleInformational,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyRegular,
    NSPopUpButton,
    NSTextField,
    NSView,
)
from Foundation import NSMakeRect

from .providers import PROVIDER_DEFS, detect_provider, invalidate_cache, get_models
from .settings import DEFAULT_FACTORY_SETTINGS, ProvidersSettings


# ---------------------------------------------------------------------------
# Focus helpers — menu bar apps run as .accessory; dialogs need .regular
# ---------------------------------------------------------------------------

def _acquire_focus() -> None:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    app.activateIgnoringOtherApps_(True)


def _release_focus() -> None:
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )


# ---------------------------------------------------------------------------
# Key masking
# ---------------------------------------------------------------------------

def _mask(key: str) -> str:
    k = key.strip()
    if len(k) <= 12:
        return k[:4] + "…" if k else "(empty)"
    return k[:10] + "…" + k[-4:]


# ---------------------------------------------------------------------------
# Styled NSTextField helpers
# ---------------------------------------------------------------------------

def _make_label(text: str, x: float, y: float, w: float, h: float = 17) -> NSTextField:
    lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    lbl.setStringValue_(text)
    lbl.setBezeled_(False)
    lbl.setDrawsBackground_(False)
    lbl.setEditable_(False)
    lbl.setSelectable_(False)
    return lbl


def _make_field(placeholder: str, x: float, y: float, w: float, h: float = 24) -> NSTextField:
    field = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    field.setPlaceholderString_(placeholder)
    field.setBezeled_(True)
    field.setDrawsBackground_(True)
    field.setEditable_(True)
    field.setSelectable_(True)
    return field


def _make_popup(titles: list[str], x: float, y: float, w: float, h: float = 26) -> NSPopUpButton:
    popup = NSPopUpButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    popup.removeAllItems()
    for t in titles:
        popup.addItemWithTitle_(t)
    return popup


# ---------------------------------------------------------------------------
# Manage API Keys dialog
# ---------------------------------------------------------------------------

def show_manage_keys(settings_path=DEFAULT_FACTORY_SETTINGS, app=None) -> None:
    """Main entry point — shows existing keys and Add / Remove buttons."""
    _acquire_focus()
    try:
        _manage_keys_loop(settings_path, app)
    finally:
        _release_focus()


def _manage_keys_loop(settings_path, app) -> None:
    ps = ProvidersSettings(settings_path)

    while True:
        providers = ps.get_providers()

        # ── Build the accessory view ──────────────────────────────────────
        ROW_H  = 20
        PAD    = 8
        W      = 380
        n_rows = max(len(providers), 1)
        view_h = n_rows * ROW_H + PAD * 2 + (ROW_H if providers else 0)
        view   = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, view_h))

        if providers:
            # Header row
            y = view_h - PAD - ROW_H
            view.addSubview_(_make_label("Provider",  10, y, 120, ROW_H))
            view.addSubview_(_make_label("Key",      140, y, W - 150, ROW_H))
            y -= ROW_H

            for pkey, pinfo in providers.items():
                name   = PROVIDER_DEFS.get(pkey, {}).get("name", pkey)
                masked = _mask(pinfo.get("apiKey", ""))
                view.addSubview_(_make_label(name,   10, y, 120, ROW_H))
                view.addSubview_(_make_label(masked, 140, y, W - 150, ROW_H))
                y -= ROW_H
        else:
            view.addSubview_(_make_label(
                "No API keys configured yet.", 10, PAD, W - 20, ROW_H,
            ))

        # ── Alert ─────────────────────────────────────────────────────────
        alert = NSAlert.alloc().init()
        alert.setAlertStyle_(NSAlertStyleInformational)
        alert.setMessageText_("API Keys")
        alert.setAccessoryView_(view)
        alert.addButtonWithTitle_("Add Key")
        if providers:
            alert.addButtonWithTitle_("Remove Key")
        alert.addButtonWithTitle_("Done")

        result = alert.runModal()

        if result == NSAlertFirstButtonReturn:            # Add Key
            added = _add_key_dialog(ps)
            if added and app:
                pkey, api_key = added
                invalidate_cache(pkey, api_key)
                import threading
                from PyObjCTools import AppHelper

                def _fetch_then_update(k=pkey, key=api_key):
                    get_models(k, key)
                    AppHelper.callAfter(app._rebuild_and_poll)

                threading.Thread(target=_fetch_then_update, daemon=True).start()
            # loop back to show updated list

        elif providers and result == NSAlertFirstButtonReturn + 1:  # Remove Key
            removed = _remove_key_dialog(ps)
            if removed and app:
                app._rebuild_and_poll()
            # loop back

        else:
            break  # Done


# ---------------------------------------------------------------------------
# Add Key sub-dialog
# ---------------------------------------------------------------------------

def _add_key_dialog(ps: ProvidersSettings) -> tuple[str, str] | None:
    provider_keys  = list(PROVIDER_DEFS.keys())
    provider_names = [PROVIDER_DEFS[k]["name"] for k in provider_keys]

    # Accessory: provider popup + key field stacked vertically
    W = 340
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, 62))

    popup = _make_popup(provider_names, 0, 36, W, 26)
    view.addSubview_(popup)

    field = _make_field("Paste API key here…", 0, 4, W, 26)
    view.addSubview_(field)

    alert = NSAlert.alloc().init()
    alert.setAlertStyle_(NSAlertStyleInformational)
    alert.setMessageText_("Add API Key")
    alert.setInformativeText_(
        "Select the provider and paste your key.\n"
        "The provider will be auto-detected from the key format if possible."
    )
    alert.setAccessoryView_(view)
    alert.addButtonWithTitle_("Add")
    alert.addButtonWithTitle_("Cancel")
    alert.window().setInitialFirstResponder_(field)

    result = alert.runModal()
    if result != NSAlertFirstButtonReturn:
        return None

    api_key = (field.stringValue() or "").strip()
    if not api_key:
        _alert_error("No key entered.", "Please paste a valid API key.")
        return None

    # Auto-detect overrides manual selection when unambiguous
    detected = detect_provider(api_key)
    pkey = detected if detected else provider_keys[popup.indexOfSelectedItem()]

    # Check for duplicates — offer to overwrite
    existing = ps.get_providers().get(pkey, {})
    if existing.get("apiKey"):
        confirm = NSAlert.alloc().init()
        confirm.setMessageText_(f"Replace {PROVIDER_DEFS[pkey]['name']} key?")
        confirm.setInformativeText_(
            f"Existing: {_mask(existing['apiKey'])}\n"
            f"New:      {_mask(api_key)}"
        )
        confirm.addButtonWithTitle_("Replace")
        confirm.addButtonWithTitle_("Cancel")
        if confirm.runModal() != NSAlertFirstButtonReturn:
            return None

    ps.add_provider(pkey, api_key)
    return pkey, api_key


# ---------------------------------------------------------------------------
# Remove Key sub-dialog
# ---------------------------------------------------------------------------

def _remove_key_dialog(ps: ProvidersSettings) -> str | None:
    providers = ps.get_providers()
    if not providers:
        return None

    provider_keys = list(providers.keys())
    titles = [
        f"{PROVIDER_DEFS.get(k, {}).get('name', k)}  —  {_mask(providers[k].get('apiKey', ''))}"
        for k in provider_keys
    ]

    W = 340
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, 30))
    popup = _make_popup(titles, 0, 2, W, 26)
    view.addSubview_(popup)

    alert = NSAlert.alloc().init()
    alert.setAlertStyle_(NSAlertStyleInformational)
    alert.setMessageText_("Remove API Key")
    alert.setInformativeText_("Choose the provider whose key you want to remove.")
    alert.setAccessoryView_(view)
    alert.addButtonWithTitle_("Remove")
    alert.addButtonWithTitle_("Cancel")

    result = alert.runModal()
    if result != NSAlertFirstButtonReturn:
        return None

    pkey = provider_keys[popup.indexOfSelectedItem()]
    ps.remove_provider(pkey)
    return pkey


# ---------------------------------------------------------------------------
# Generic error alert
# ---------------------------------------------------------------------------

def _alert_error(title: str, message: str = "") -> None:
    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    if message:
        a.setInformativeText_(message)
    a.addButtonWithTitle_("OK")
    a.runModal()
