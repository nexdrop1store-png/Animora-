# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview
Animora is an AI-native 3D creation tool: a full fork and rebrand of Blender with a persistent AI panel, real-time vision system, cloud LLM backend, auth/billing, and website.

## Monorepo Layout
```
Animora/
├── addons/animora_panel/    CANONICAL AI panel source (Python)
│                            Injected into blender-fork at build time by
│                            scripts/rebrand.py. The fork copy is a build
│                            artifact — never edit it directly.
├── blender-fork/            Blender source (GITIGNORED, ~7 GB — see below)
├── ai-backend/              FastAPI WebSocket server (LLM orchestration)
│   ├── orchestrator/        streaming, router, intent, personas, context_builder,
│   │                        tools, tool_result_coordinator, quality, memory, events
│   ├── personas/            base + generalist + environment_artist + hard_surface_artist
│   │                        + lighting_td + mesh_repair_recipes
│   ├── prompts/             master_prompt + intent_classifier + artists_eye
│   ├── eval/                benchmarks + runner (CC-eval-scenes harness)
│   ├── tests/               smoke tests (test_call, test_ws, test_phase4_classifier,
│   │                        test_phase5_quality, test_phase15_e2e)
│   ├── vision_buffer.py     Redis ring buffer for viewport frames + HD captures
│   ├── scene_intelligence.py + scene_diff.py   scene-graph context + JSON-patch diff
│   ├── quality_enforcer.py  pre-execution AST + regex validator for bpy scripts
│   ├── anthropic_client.py  retry/timeout/cancel wrapper + token tracking
│   ├── key_source.py        BYOK vs pooled-key abstraction
│   └── dev_server.py        local-only launcher (stubs Redis + JWT — never ships)
│                            Desktop auth lives in addons/animora_panel/auth/
│                            (loopback PKCE against Supabase — see below)
├── docs/                    AI_ARCHITECTURE.md (~30 KB plan) + RUN_LOCAL.md
├── website/                 animora.tech (Next.js 14, App Router, TS strict)
├── assets/                  Branding (splash, icons, theme, startup.blend)
├── installer/windows/inno/  Inno Setup scripts + VC++ Redist bundle
├── scripts/                 build.py, rebrand.py, sync_addon.py, stage_for_installer.py,
│                            build_default_startup.py, setup_theme.py
└── patches/                 animora-native-full.patch + native-overlay/ (Animora delta over upstream Blender)
```

**The AI panel's canonical source is `addons/animora_panel/` (top level).** `scripts/rebrand.py` copies it into `blender-fork/scripts/addons_core/animora_panel/` at build time. This is the design that decouples Animora from any specific Blender version — see `docs/UPGRADE_BLENDER.md`. Animora is the product; the AI panel is one component, implemented as a Blender addon for technical reasons but is part of Animora, not a third-party plugin.

**Single source of truth for the Blender base version**: `scripts/animora_config.py` (`BLENDER_VERSION`) and `installer/windows/inno/Animora.iss` (`BlenderVersion`). Bump both together. `sync_addon.py` and `rebrand.py` read from the config module.

## blender-fork is NOT in this repo
`blender-fork/` is `.gitignore`d (7+ GB tree). Clone separately from `projects.blender.org` and apply `patches/animora-native-full.patch` + `patches/native-overlay/`. See `patches/README.md`. Never edit tracked upstream files; Animora-specific logic belongs in the addon, not in `blender-fork/source/`.

## Build & Run Commands
```bash
# Full build (auto-detects current platform; runs rebrand → cmake → compile → package)
python scripts/build.py
python scripts/build.py --platform {windows|macos|linux} --config {Release|Debug}
python scripts/build.py --skip-rebrand --skip-compile --smoke-test    # piecewise re-runs

# Rebrand only (asset injection + string patching into blender-fork; no compile)
python scripts/rebrand.py

# Dev shortcut: copy edited AI panel source into the installed Animora's path
# WITHOUT a full cmake/Inno rebuild. Reads BLENDER_VERSION from animora_config.py.
python scripts/sync_addon.py                  # defaults to configured version
python scripts/sync_addon.py --version 5.2    # override for cross-version testing
# Then in Animora: Edit > Preferences > Add-ons > toggle Animora panel off/on.

# AI backend — local dev (in-memory Redis stub, any token accepted)
cd ai-backend && python dev_server.py    # → http://127.0.0.1:8000

# AI backend — production-shaped (requires real Redis + JWT_SECRET; ANIMORA_ENV=dev to bypass)
cd ai-backend && uvicorn main:app --reload --port 8000

# LLM provider switch (default: anthropic). Set in ai-backend/.env:
#   ANIMORA_LLM_PROVIDER=bedrock       — use Amazon Bedrock (dev/CI when Anthropic credits are tight)
#   AWS_BEARER_TOKEN_BEDROCK=ABSK...   — long-term Bedrock API key
#   BEDROCK_AWS_REGION=us-east-1
# Model translation is transparent (router uses logical names like claude-opus-4-7;
# AnthropicClient maps them to us.anthropic.claude-opus-4-6-v1 on Bedrock).
# Full guide: docs/BEDROCK.md.

# Website (local)
cd website && npm run dev
```

## Tests
Pytest is configured (`pyproject.toml`: `testpaths = ["ai-backend/tests", "addons/tests"]`, `asyncio_mode = "auto"`).
```bash
pytest ai-backend/tests                                         # all
pytest ai-backend/tests/test_phase5_quality.py -k quality       # single file / -k filter
pytest ai-backend/tests/test_ws.py::test_streaming -x           # single test, stop on first fail

# Bare-script smokes (need dev_server running where noted)
python ai-backend/tests/test_call.py                            # AnthropicClient + master prompt
python ai-backend/tests/test_ws.py                              # full WS protocol — needs dev_server
python ai-backend/tests/test_phase4_classifier.py               # 11-case intent classifier accuracy
python ai-backend/tests/test_phase5_5_retry.py                  # retry helpers (no API calls)
```

## Quality eval (Phase 9)
The eval harness measures single-shot output quality. CI gates PRs that touch the AI surface against `ai-backend/eval/baseline.json`.
```bash
# Local — full suite (~$0.60, 5-15min) → baseline + report + JSON dump
python ai-backend/eval/runner.py --output-baseline ai-backend/eval/baseline.json \
    --output ai-backend/eval/baseline_report.md --json ai-backend/eval/baseline_run.json

# Local — single benchmark or category (cheap iteration)
python ai-backend/eval/runner.py --filter primitive.cube
python ai-backend/eval/runner.py --filter vehicle

# Re-score a saved JSON dump (offline, no API cost)
python ai-backend/eval/runner.py --skip-llm --input-json prior_run.json

# Regression gate (matches what CI runs)
python ai-backend/eval/runner.py --baseline ai-backend/eval/baseline.json --fail-on-regress
```
Scoring rules live in `ai-backend/eval/scoring.py`. Benchmarks in `ai-backend/eval/benchmarks.py`. Full guide: `docs/EVAL.md`. CI workflow: `.github/workflows/eval.yml`.

**The harness measures single-shot quality only.** It can't measure Phase 5.5 retry's contribution because it doesn't pass a `ToolResultCoordinator` — the agentic loop exits after iteration 0 before retry can fire. Retry validation is currently manual panel smoke (see `docs/EVAL.md`).

## Lint
```bash
ruff check .                # lint
ruff format .               # format
```
`pyproject.toml` defines: line-length 100, py311 target, ignores E501. First-party packages: `animora_panel`, `ai_backend`.

## LLM provider abstraction (`ai-backend/llm_provider.py`)
The orchestrator, router, eval harness, and tests all use **logical** model names (`claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`). `AnthropicClient` translates them to provider-specific IDs at the SDK boundary:
- `anthropic` provider → IDs pass through unchanged
- `bedrock` provider → `_BEDROCK_MODEL_MAP` looks them up (e.g., `claude-opus-4-7` → `us.anthropic.claude-opus-4-6-v1` since 4.7 is gated on most Bedrock accounts; the `us.` prefix is the cross-region inference profile required for on-demand invocation)

Any new caller that needs a non-streamed Anthropic call MUST use `anthropic_client.messages_create(...)` instead of `client._sdk.messages.create(...)`. The latter bypasses translation and silently breaks on Bedrock.

## Architecture: the request loop
1. Addon (Blender) opens a WebSocket to `/ws/{session_id}?token=...` and sends a `hello` frame with the user's Anthropic API key (BYOK) — or omits it to use the pooled key from `.env`.
2. `auth_middleware.decode_token()` verifies the JWT and applies plan-based rate limits BEFORE `accept()` (avoids DoS amplification + side-channel info leaks).
3. On each user message, `orchestrator.streaming.stream_response()`:
   - calls `orchestrator.intent` (Haiku-powered classifier) to pick a persona,
   - builds a layered system prompt via `orchestrator.context_builder` = `master_prompt` (stable) + persona (`personas/*.py`) + scene context (`scene_intelligence` + `scene_diff`) + optional vision frame (`vision_buffer`),
   - selects a model via `orchestrator.router` (Haiku 4.5 / Sonnet 4.6 / Opus 4.5),
   - streams the response and dispatches tool calls back to the addon through `tool_result_coordinator`.
4. Any bpy script the LLM emits is validated by `quality_enforcer.validate_script()` (AST + regex banlist) BEFORE it is dispatched.
5. `orchestrator.quality.run_artists_eye_check()` post-validates output against the `artists_eye` prompt.
6. (Phase 5.5) If `ANIMORA_QUALITY_RETRIES > 0` (default 2), a failing artist's-eye verdict inside the agentic loop triggers `orchestrator.retry.build_revision_user_message()` — appended to `accumulated_messages` so the next iteration re-emits a revised `tool_use`. WS events: `quality.retrying`, `quality.retry_succeeded`, `quality.retry_exhausted`. main.py's background quality check is skipped when the inline path ran (avoids double-billing the Sonnet vision call).

### Layered prompt + cache discipline
The master prompt + persona section is deliberately kept identical across turns so Anthropic's prompt cache hits. Single-turn requests see `cache_hit_ratio ≈ 0` in `anthropic.client.stream.completed` logs; the second turn in a session should jump to `~0.99`. Treat changes to `prompts/master_prompt.py` and the persona files as cache-invalidating and review their impact accordingly.

### Vision system (viewport frames)
Binary WS frames from the addon carry a 13-byte header: `>BHHd` = 1B type + 2B width + 2B height + 8B timestamp (see `_VPF_HEADER_FMT` in `ai-backend/main.py`). Frames go into a Redis ring buffer (`vision_buffer.py`) with `PAUSE_AT` / `RESUME_AT` thresholds for backpressure. HD captures use a separate slot. When the LLM needs vision, `context_builder` attaches the latest frame.

### dev_server.py vs main.py
- `dev_server.py` monkey-patches `session_manager.get_redis()` and `auth_middleware.decode_token()` with in-process stubs. It is dev-only.
- `main.py` is what Fargate runs in production — real Redis (ElastiCache), real Supabase-issued JWTs, real Secrets Manager.
- `stage_for_installer.py` explicitly excludes `dev_server.py` from shipped bundles. Never import from it in production code paths.

## Desktop auth (addons/animora_panel/auth/)
Loopback-callback PKCE against Supabase (RFC 8252 §7.3) — no URL-scheme registration, no helper processes:
1. Launch: unauthenticated users are held in a fullscreen 3-slide onboarding gate (`onboarding.py`); signed-in users restore silently and never see it. The AI panel has NO sign-in surface.
2. Sign In (gate slide 3): `auth/controller.begin_sign_in()` generates PKCE+state, binds a one-shot HTTP listener on `127.0.0.1:0` (`auth/loopback.py`), and opens `{website}/signin?next=/auth/device?...&redirect_uri=http://127.0.0.1:{port}/auth/callback`.
3. The website mints a 5-min single-use code (Supabase RPC `issue_device_handoff`, device-binding enforced) and navigates the browser to the loopback URL; the listener verifies `state` (constant-time) and serves a branded success page.
4. The controller exchanges `code+verifier+device_id` at the Supabase Edge Function `auth-handoff-exchange` (`auth/supabase.py`), then connects the WS.
5. `auth/session.py` persists the ROTATING refresh token in the OS keyring (service `"animora"`); access tokens >512 chars stay memory-only. Never refresh the same token from two processes; transient refresh failures NEVER clear tokens — only definitive 4xx rejections do (which reopen the gate at the sign-in slide).
The redirect-URI allowlist lives in TWO places in the website repo (client check in `DeviceAuthorize.tsx` + SQL in `issue_device_handoff`) — change both together.

## Python Conventions (addon, ai-backend)
- Python 3.11+
- Type hints on all function signatures
- `ruff` for linting/formatting (line length 100)
- No bare `except:` — always catch specific exceptions
- No `print()` in production paths — use `logging` / `observability.logger()`
- Addon code: follow Blender PEP 8 with `bpy` patterns; operators prefixed `OT_`, panels `PT_`

## TypeScript / Next.js Conventions (website)
- TypeScript strict mode
- App Router only (no `pages/` directory)
- Tailwind CSS + shadcn/ui
- Server components by default; `"use client"` only when needed

## Security Rules
- Never log access tokens, refresh tokens, raw API keys, or device fingerprints — log only sha256 prefixes via `anthropic_client.fingerprint_key()`.
- Never commit `.env` files — use `.env.example` placeholders.
- Blender addon: never store tokens in plaintext — use `keyring` (OS secure store); see `credentials.py`.
- Backend: every LLM-generated bpy script MUST pass `quality_enforcer.validate_script()` before dispatch.
- Banned imports in LLM scripts: `os`, `subprocess`, `sys`, `shutil`, `socket`, `urllib`, `requests`, `httpx`, `pathlib`, `importlib`, `ctypes`, `multiprocessing`, `threading`, `asyncio`, `pickle`, `marshal`. Banned builtins: `open`, `eval`, `exec`, `compile`, `__import__`, `getattr`, `globals`, `locals`, `vars`, `input`, `breakpoint`. Banned method names: `read_text`, `write_text`, `system`, `popen`, `load_module`, `import_module`, etc. `ai-backend/quality_enforcer.py` is authoritative.
- JWT secrets: production deploys MUST set `JWT_SECRET`. The backend refuses to start with the dev sentinel unless `ANIMORA_ENV=dev` is also set (see `_enforce_secrets_safety` in `ai-backend/config.py`). Setting `ANIMORA_ENV=dev` in production is a security incident.
- `/validate-key`: Redis-backed per-IP rate limit (10/min), generic error messages to clients (specifics go to server logs).

## Key External Services
- **Claude API**: Haiku 4.5 (fast / intent), Sonnet 4.6 (primary), Opus 4.5 (Studio / complex). Routing logic in `ai-backend/orchestrator/router.py`.
- **Deepgram**: Nova-3 voice transcription
- **Stripe**: Subscriptions and billing
- **Redis**: Session state, rate limiting, vision ring buffer
- **Supabase Postgres**: persistent user / device / billing data
- **AWS Fargate**: production backend host
