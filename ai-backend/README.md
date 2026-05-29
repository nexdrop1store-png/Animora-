# ai-backend — Animora orchestrator

The cloud-side AI layer. FastAPI WebSocket server that:
- holds the Anthropic API key (BYOK from the addon, or pooled from `.env`)
- builds the layered system prompt (master + persona + scene context)
- routes between Claude Haiku 4.5 / Sonnet 4.6 / Opus 4.5
- streams responses + dispatches tool calls back to the Animora addon
- enforces quality gates on LLM-generated bpy scripts

For the full architectural plan see [`../docs/AI_ARCHITECTURE.md`](../docs/AI_ARCHITECTURE.md).
For local-loop setup see [`../docs/RUN_LOCAL.md`](../docs/RUN_LOCAL.md).

## Layout

```
ai-backend/
├── main.py                 FastAPI app + WebSocket endpoint
├── dev_server.py           local-only launcher (stubs Redis + JWT)
├── config.py               Settings (env-driven via pydantic-settings)
├── anthropic_client.py     production wrapper: retry / timeout / cancel / token tracking
├── key_source.py           BYOK vs pooled-key abstraction
├── auth_middleware.py      JWT decode + plan-based rate limits
├── session_manager.py      Redis session state + history
├── scene_intelligence.py   scene-graph context builder + complexity heuristic
├── scene_diff.py           JSON-patch + prose diff between consecutive graphs
├── vision_buffer.py        Redis ring buffer for viewport frames + HD captures
├── quality_enforcer.py     pre-execution AST + regex script validator
├── observability.py        structured JSON logging
├── validate.py             POST /validate-key REST endpoint
├── models.py               Pydantic message schemas
│
├── orchestrator/           the streaming loop + glue
│   ├── streaming.py        the main stream_response() function
│   ├── router.py           model selection (Haiku/Sonnet/Opus)
│   ├── intent.py           Haiku-powered intent classifier (runtime)
│   ├── personas.py         intent → persona module routing
│   ├── tools.py            Anthropic tool definitions
│   ├── context_builder.py  layered system prompt + vision attachment
│   └── events.py           in-process pub/sub bus
│
├── personas/               persona system prompts (Phase 4)
│   ├── base.py             shared workflow philosophy
│   ├── generalist.py       fallback
│   ├── environment_artist.py
│   ├── hard_surface_artist.py
│   └── lighting_td.py
│
├── prompts/                LLM prompt modules
│   ├── master_prompt.py    absolute rules + quality standards
│   └── intent_classifier.py the Haiku classifier prompt
│
└── tests/                  end-to-end smoke tests
    ├── test_call.py                direct AnthropicClient ping
    ├── test_ws.py                  full WebSocket protocol
    └── test_phase4_classifier.py   intent classifier accuracy (11 cases)
```

## Running locally

```bash
# One-time
pip install -r requirements.txt

# Start the dev server (in-memory Redis stub, any token accepted as trial)
python dev_server.py
# → http://127.0.0.1:8000/health, ws://localhost:8000/ws/<id>?token=dev
```

For the Animora addon to talk to it: enable **Dev Mode** in `Edit > Preferences > Add-ons > Animora` and the addon's WS URL flips to `ws://localhost:8000/ws`.

## Smoke tests (after dev_server is running where applicable)

```bash
# Direct (no WebSocket needed) — verifies the AnthropicClient + master prompt
python tests/test_call.py

# Full WebSocket protocol (needs dev_server running)
python tests/test_ws.py

# Intent classifier accuracy across 11 domain-specific messages
python tests/test_phase4_classifier.py
```

## Production deploy

The dev server is **not** the production deploy. Production:
- runs `uvicorn main:app` on AWS Fargate (not `dev_server.py`)
- requires real `JWT_SECRET` (the backend refuses to start otherwise)
- connects to real Redis (ElastiCache) and Supabase Postgres
- uses Anthropic key from AWS Secrets Manager, not `.env`

`ANIMORA_ENV=dev` is the ONLY way to start the backend with the dev-sentinel JWT secret. Setting it in production is a security incident.

## Security model

- API key is held in-memory per WS session, never logged in raw form (only sha256 fingerprint via `anthropic_client.fingerprint_key()`).
- Every LLM-generated bpy script passes `quality_enforcer.validate_script()` before being dispatched to the addon.
- Banlist covers direct imports, dynamic imports (`importlib`, `__import__`), file I/O (`pathlib`, `open`), introspection bypasses (`getattr`, `globals`, `locals`, `vars`, `__builtins__`), and dangerous method names (`read_text`, `system`, etc.).
- WebSocket auth runs BEFORE `accept()` to avoid side-channel info leaks and DoS amplification.
- `/validate-key` uses Redis-backed per-IP rate limiting (10/min) and returns generic error messages to clients (specific reasons go to server logs only).
