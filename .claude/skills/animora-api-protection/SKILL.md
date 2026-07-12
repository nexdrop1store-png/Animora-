---
name: animora-api-protection
description: Use when adding/altering backend endpoints or WS message types, or hardening the API — "rate limit", "replay attack", "request signing", "origin check", "DoS", "abuse", "protect the endpoint", "validate-key", "JWT validation", "frame flood". Documents the as-built defenses and the required posture for anything new.
---

# Animora API protection

## As-built defense inventory (backend)
| Defense | Where | Notes |
|---|---|---|
| JWT verify BEFORE `accept()` | `main.py` WS handshake + `auth_middleware.validate_token` | avoids DoS amplification + side-channel leaks; Supabase tokens verified via `GET /auth/v1/user` with anon key; local JWTs enforce `iss=animora-auth`, `aud=animora-backend` (dev mode relaxes) |
| Secrets safety at boot | `config.py::_enforce_secrets_safety` (:128) | refuses known dev secrets / <32 chars unless `ANIMORA_ENV=dev`; setting dev in production is a security incident |
| Plan rate limits | `auth_middleware.check_rate_limit` | Redis buckets, messages/hour+day per plan |
| Per-session message-rate cap | `config.settings.ws_messages_per_minute=1000` | covers ALL frame types (scene_graph/hd_capture floods can't bypass user-message quota) |
| Binary frame size cap | `ws_max_binary_frame_bytes=8MB` | one 100 MB frame can't OOM the box; oversize dropped + logged |
| WS Origin allowlist | `allowed_ws_origins` (animora.tech, localhost:3000; empty Origin = desktop client, permitted) | browsers must match |
| Vision backpressure | `vision_buffer.py` PAUSE_AT/RESUME_AT ring buffer | frame floods degrade gracefully |
| Script gate | `quality_enforcer.validate_script()` pre-dispatch | the LLM-script security boundary; `max_script_length=160k` is a runaway net, NOT the boundary |
| `/validate-key` | Redis per-IP 10/min, generic client errors | specifics go to server logs only |
| Key hygiene | `anthropic_client.fingerprint_key()` | log sha256 prefixes only — never raw keys/tokens/fingerprints |

## Posture for ANY new endpoint or WS message type (checklist)
1. AuthN/AuthZ before work: which claim gates it? Deny by default; verify before `accept()`/body-read where possible.
2. Rate limit: per-user (plan bucket) AND per-IP for unauthenticated surfaces; every unauthenticated endpoint follows the `/validate-key` pattern.
3. Size caps on every input (body, frame, string fields) — pydantic models in `models.py`, no raw dict handling.
4. Error discipline: generic message to client + specific server log. Never echo internal state, stack traces, or which check failed (auth especially — "Invalid or expired session", not "wrong audience").
5. Idempotency for anything with side effects (billing webhooks: event-id dedupe).
6. Log a structured event (`observability.logger()`) with sha256-prefixed identifiers.

## V2 Phase-8 additions (contract, not yet built)
- **Replay defense** for state-changing HTTP calls: client nonce + timestamp window (±120 s) + Redis nonce-seen set (TTL = window). WS is session-bound post-JWT; replay defense targets the HTTP surface (billing, rebind, validate-key POSTs).
- **Request signing** for desktop→backend HTTP: HMAC over method|path|body-sha|timestamp|nonce with a per-session key derived at WS hello — binds HTTP calls to the authenticated session. Rotate with the session.
- **Abuse scoring hook**: IPQS (`IPQS_API_KEY`) on auth + billing endpoints; score gates step-up, not hard-ban.
- Full security test pass before V2 ships: secrets scan (`scripts/check_no_secrets.py` on every staged artifact — already in CI), installer inspection, auth-flow attack run (state reuse, verifier mismatch, redirect tampering), binding bypass attempts, API fuzz of every pydantic model.

## Pitfalls
- The hardcoded Supabase URL + publishable anon key fallbacks in `auth_middleware.py:30-33` are public-by-design values, but env-less fallbacks mask misconfig — production must set both explicitly.
- Don't add endpoints to `dev_server.py` — it stubs auth; prod-shape everything in `main.py` behind real checks.
- Long-poll/streaming responses hold connections — cap concurrent sessions per user before shipping paid (cost + DoS).
