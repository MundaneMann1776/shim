# Shim

**One-click model switcher for Codex Desktop.**

A macOS menubar app that lets you flip Codex Desktop between your **ChatGPT
subscription** and any **BYOK provider** — DeepSeek, OpenRouter, Google,
Anthropic, OpenAI, or anything OpenAI-compatible — without recompiling Codex,
without restarting it, and without re-editing `~/.codex/config.toml` by hand.

Set reasoning effort from the same menu. Drop back to native subscription mode
in one click and the shim gets entirely out of the path.

> Status: tested on Codex Desktop **0.133.x** for macOS arm64 (Apple Silicon).
> The local proxy itself is platform-agnostic Python; the menubar app is macOS-only.

---

## What it actually does

```
        ┌────────────────────────────────────────────┐
        │ ☰  Shim                                    │
        │ ● Active: DeepSeek — DeepSeek-V3.1         │
        │ ─────                                       │
        │ ChatGPT Subscription (native, no shim)     │
        │ ─────                                       │
        │ DeepSeek                              ▶    │
        │ OpenRouter (334 models)               ▶    │
        │ Google                                ▶    │
        │ ─────                                       │
        │ Status: Running • pid 12345                │
        │ Reasoning (high)                      ▶    │
        │ Manage API Keys…                           │
        └────────────────────────────────────────────┘
```

Click a model → Codex's `~/.codex/config.toml` is rewritten with a managed
block pointing at the local shim, the shim is started if needed, and Codex
picks it up on its next request. Click "ChatGPT Subscription" → managed block
is stripped, shim is stopped, Codex talks to chatgpt.com directly via its OAuth
tokens. No middleman, no extra latency, no failure mode if the shim crashes.

---

## Install

Requires macOS 13+, Python 3.11+, and Codex Desktop already installed.

```bash
git clone https://github.com/MundaneMann1776/codex-shim ~/Documents/codex-shim
cd ~/Documents/codex-shim
python3 -m venv .venv
.venv/bin/pip install -e .
bin/build-app                       # compiles the Mach-O launcher → Shim.app
open Shim.app                       # menubar icon appears
```

Add it to **System Settings → General → Login Items** if you want it always
running.

---

## Using it

1. Open **Shim** in your menubar.
2. **Manage API Keys…** → paste your DeepSeek / OpenRouter / Google / etc. key.
   The provider's models show up as submenus within seconds.
3. Click any model. Codex picks it up on the next request.
4. **Reasoning ▶ low / medium / high** sets `model_reasoning_effort` in the
   managed block. Persists across switches.
5. **ChatGPT Subscription (native, no shim)** at any time = full restore. The
   managed block is removed, the shim daemon stops, Codex falls back to its
   OAuth-authenticated chatgpt.com path.

Your `~/.codex/config.toml` is backed up to
`.codex-shim/config.toml.before-codex-shim` on first install and restored on
Quit or on subscription mode.

---

## CLI

Same flows are exposed for headless use:

```
codex-shim start                       # daemon on 127.0.0.1:8765
codex-shim model use <slug>            # switch active model
codex-shim status                      # health check
codex-shim stop                        # restore config + stop daemon
codex-shim restart
codex-shim list                        # show available models + routes
codex-shim app [path]                  # launch Codex Desktop wired into shim
codex-shim codex -- <args>             # exec `codex` CLI through shim
```

All commands accept `--settings <path>` and `--port <port>`.

---

## Picker patch (one-time, optional)

Codex Desktop's frontend uses a Statsig allowlist that hides any model whose
slug isn't on a hardcoded list. The shim's catalog entries fall into the
hidden bucket and won't render in Codex's in-app model picker — even though
the model is functional. Patch flips one boolean in `app.asar`:

```bash
codex-shim patch-app                   # one-time, backs up app.asar first
codex-shim restore-app                 # undo
```

If you only use the menubar to switch (and don't open Codex's own picker
dropdown), this is unnecessary.

---

## How routing works

```
Codex Desktop ── /v1/responses ──▶ shim (127.0.0.1:8765)
                                     │
                                     ├── slug "gpt-5.5" *
                                     │       └─▶ chatgpt.com/backend-api/codex/responses
                                     │           (Bearer access_token from ~/.codex/auth.json)
                                     │
                                     ├── provider "openai" / "generic-…"
                                     │       └─▶ baseUrl/chat/completions
                                     │           (Bearer apiKey)
                                     │
                                     └── provider "anthropic"
                                             └─▶ baseUrl/messages
                                                 (x-api-key, anthropic-version)
```

`*` Only used if you keep the shim-routed subscription entry. Clicking
"ChatGPT Subscription (native, no shim)" bypasses this entirely.

The shim translates Codex's Responses-API request into the upstream's shape
(chat completions or Anthropic Messages) and translates the streamed reply
back. Extended-thinking blocks from Anthropic-shaped upstreams (Claude,
DeepSeek-R1, GLM) round-trip through `reasoning.encrypted_content` items.

---

## Supported providers

| provider key | upstream API | reasoning |
|---|---|---|
| `openai` | OpenAI `/v1/chat/completions` | ✓ (o-series) |
| `generic-chat-completion-api` | OpenAI-shaped chat completions | depends on model |
| `anthropic` | Anthropic `/v1/messages` | ✓ |
| `deepseek` | OpenAI-compatible | ✓ (R1) |
| `openrouter` | OpenAI-compatible (334+ models) | depends on model |
| `google` | Gemini OpenAI-compatible endpoint | ✓ (2.5 Thinking) |

Add a provider in the menubar (**Manage API Keys…**) or by editing
`~/.factory/settings.json` directly.

---

## File layout

```
codex_shim/             python: server, cli, menubar, translation
bin/build-app           compile Shim.app launcher (Mach-O + embedded Python)
assets/AppIcon.icns     menubar app icon source
tests/                  pytest suite
.codex-shim/            generated catalog / config backup / logs / pid (gitignored)
Shim.app/               built menubar app bundle (gitignored)
```

---

## MCP

Codex Desktop forwards three generic MCP tools to every model:

- `list_mcp_resources`
- `list_mcp_resource_templates`
- `read_mcp_resource`

Shim-routed models receive the same MCP tool surface as built-in OpenAI
models. The model is expected to call `list_mcp_resources` to discover what's
available — Codex does not flatten individual MCP server tools into the
function list (that's a Codex client behavior, not a shim limitation).

---

## What the shim does not do

- **Does not** store or copy your API keys into the generated catalog. Keys
  live in `~/.factory/settings.json` and are read fresh on each request.
- **Does not** modify Codex's binary. The optional ASAR patch only flips one
  boolean in `app.asar` and is fully reversible.
- **Does not** touch `~/.codex/auth.json`. Your subscription OAuth tokens are
  read for the (optional) GPT-5.5 passthrough, never written.
- **Does not** auto-update Codex's config without your action. Every change
  comes from a menu click or CLI command.

---

## License

MIT — see `LICENSE`.

Codex Desktop is a trademark of OpenAI. This project is unaffiliated.
