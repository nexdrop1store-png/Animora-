---
name: animora-addon-architecture
description: Use when changing the AI panel addon — adding a tool handler, touching the chat UI, WebSocket client, vision capture, onboarding gate, or auth wiring. Triggers include "add a tool to the addon", "panel UI", "ws_client", "operators.py", "onboarding", "viewport capture", "how does the addon dispatch tool calls", "sync_addon". Maps every module and the tool_call → handler → tool_result flow.
---

# Animora AI panel addon architecture

Canonical source: `addons/animora_panel/` (top level). `scripts/rebrand.py` injects it into the fork at build time; `scripts/sync_addon.py` copies it into an installed Animora for fast dev (then Preferences → Add-ons → toggle Animora panel off/on).

## Module map

| Module | Owns |
|---|---|
| `__init__.py` | bl_info + register/unregister of all classes |
| `panel.py` | The chat panel UI (PT_ classes), token streaming display, suggestion chips |
| `operators.py` (118 KB) | OT_ operators, WS message dispatch `_on_tool_call` (:267), the script executor, all 12 `_atomic_*` handlers (:1709–2150), undo grouping (:587), main-thread marshaling (:218) |
| `ws_client.py` | WebSocket lifecycle, reconnect, frame send/receive |
| `vision.py` | Viewport frame capture + streaming (binary frames, 13-byte header `>BHHd`), HD capture |
| `state.py` | Session state properties |
| `ui/` | chat_display, properties (PropertyGroups) |
| `ads/` | Hand-rolled GPU-drawn chrome (canvas, primitives, tokens) — used because bpy.types.Panel can't do the chat look; drawn via `gpu` module |
| `onboarding.py` | Fullscreen 3-slide gate for unauthenticated users; signed-in users bypass silently |
| `auth/` | Loopback PKCE (bpy-free except `controller.py`): `pkce.py`, `loopback.py`, `session.py` (keyring + refresh), `supabase.py` (edge-function exchange) |
| `credentials.py` | Keyring-backed storage helper |
| `preferences.py` | Backend URLs (defaults: `wss://eatanimora-animora-backend.hf.space/ws`), BYOK key entry |
| `api_validator.py` | Background-thread BYOK key validation (hops back to main thread for callback) |
| `sculpt_guard.py` | Blocks AI edits during sculpt sessions |
| `composer_buffer.py` | Chat input buffer logic (bpy-free, unit-tested) |
| `border_glow.py` | "AI is working" viewport border effect |
| `preview_icons.py` / `icons/` | Panel branding |
| `bundle.py` | Dev-mode bundled-backend launcher glue |

## The tool-call round trip (memorize this)
1. Backend `streaming.py:_on_tool_call` → `main.py:send_tool_call` → WS `tool_call` frame `{tool, tool_use_id, input}`.
2. Addon `ws_client` receives → `operators.py:_on_tool_call(msg)` (:267) on the main thread.
3. `_maybe_push_iteration_undo(iteration, user_intent)` — ONE undo entry per iteration.
4. Dispatch: atomic tools → `_atomic_<name>` handler (10–30-line bpy wrapper, returns immediately, posts one-line chat confirmation); `execute_animora_code` → `_execute_script` (:790) with the AST-split runner + `tool_progress` pings.
5. Addon sends `tool_result` `{tool_use_id, output, error}`; backend `ToolResultCoordinator.resolve()` unblocks the agentic loop.

## Adding a new tool = 4 synchronized edits
1. Schema in `ai-backend/orchestrator/tools.py` (`BLENDER_TOOLS`).
2. Handler `_atomic_<name>` in `operators.py` + registration in the dispatch table in `_on_tool_call`.
3. Classification: if it mutates the scene, add to `_LOOP_ENFORCER_MUTATION_TOOLS` in `streaming.py` and decide REFINEMENT vs foundation (see animora-product-loop skill).
4. Teach the model: master prompt / persona guidance if usage rules matter.
Missing any one of these yields "Unknown tool" errors or an enforcer bypass.

## Constraints
- Modules that need unit tests must stay bpy-free (import pattern: `auth/__init__.py` deliberately does NOT import controller).
- The panel has NO sign-in surface — sign-in lives in the onboarding gate only.
- Never store tokens in plaintext: `keyring` service `"animora"` (`auth/session.py`); >512-char access tokens stay memory-only.
- All user-visible strings say Animora, never Blender.
