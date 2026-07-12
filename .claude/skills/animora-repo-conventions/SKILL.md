---
name: animora-repo-conventions
description: Use when working anywhere in the Animora monorepo and unsure where code belongs, how to name things, or how to commit — "where do I put this", "which file owns X", "what's the commit style", "where are the tests", "which version number do I bump". Covers layout, subsystem ownership, naming, commit conventions, and the three phase-numbering schemes.
---

# Animora repo conventions

## Where subsystems live

| Concern | Location | Never |
|---|---|---|
| AI panel (canonical source) | `addons/animora_panel/` | edit `blender-fork/scripts/addons_core/animora_panel/` — that copy is a build artifact injected by `scripts/rebrand.py` |
| Backend orchestration | `ai-backend/orchestrator/` (streaming, router, intent, critic, tools, coordinator, memory, spec, final_review) | put orchestration logic in `main.py` — it owns WS transport only |
| Prompts | `ai-backend/prompts/` (master_prompt, personas import from `ai-backend/personas/`) | inline prompt text in streaming.py |
| Script safety | `ai-backend/quality_enforcer.py` (authoritative banlist) | duplicate ban rules elsewhere |
| Eval | `ai-backend/eval/` (benchmarks, runner, scoring, baseline.json) | commit new `*_run.json` dumps to the repo root |
| Auth (desktop) | `addons/animora_panel/auth/` — bpy-free except controller | import bpy in session/loopback/pkce/supabase (they are unit-testable on CI without Blender) |
| Server-side auth (Supabase SQL + edge functions) | `supabase/` (import plan in its README; live source of truth is the Supabase project) | assume it's versioned — check README status first |
| Build/packaging | `scripts/` + `installer/` + `patches/` | Animora-specific logic in `blender-fork/source/` — native changes go through `patches/animora-native-full.patch` + `patches/native-overlay/` |

## Single sources of truth
- Blender base version: `scripts/animora_config.py` (`BLENDER_VERSION`) **and** `installer/windows/inno/Animora.iss` (`BlenderVersion`) **and** `.github/workflows/build.yml` (`BLENDER_TAG`) — bump all together.
- Tool definitions the LLM sees: `ai-backend/orchestrator/tools.py` (`BLENDER_TOOLS`). Addon handlers in `operators.py` must mirror it 1:1.
- Ban lists: `ai-backend/quality_enforcer.py`.
- Live deploy: HF Spaces `eatanimora-animora-backend.hf.space` (addon defaults in `preferences.py`). `fly.toml` is a prepared-but-not-live alternative.

## Naming
- Python: `ruff` (line 100, py311, E501 ignored). Type hints on all signatures. No bare `except:`. No `print()` in production paths — `logging` / `observability.logger()`.
- Blender operators `OT_` prefix, panels `PT_` prefix.
- Env flags gating behavior: `ANIMORA_*` (e.g. `ANIMORA_ENFORCE_LOOP`, `ANIMORA_ENABLE_SPEC`, `ANIMORA_QUALITY_RETRIES`, `ANIMORA_EXEC_MAX_TOKENS`, `ANIMORA_ENV`).
- Logical model names only in code (`claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`); `AnthropicClient` translates per provider. Never hardcode Bedrock IDs outside `llm_provider.py`.

## Commits
History style is `Area: what changed` with a compact body when needed (see `git log`: "Auth: try Supabase (the live provider) before auth.animora.tech", "Windows packaging: Inno pipeline, stale-DLL cleanup…"). Work lands on `main`. Keep commits scoped to one concern; repo-hygiene commits are their own commits.

## Tests
- `pyproject.toml`: `testpaths = ["ai-backend/tests", "addons/tests"]`, `asyncio_mode = "auto"`.
- Backend tests: `ai-backend/tests/test_*.py` — stage/phase-named after the repo-internal plan (see numbering warning below).
- Addon tests: `addons/tests/` — bpy-free modules only (auth, composer_buffer, onboarding logic). bpy-dependent tests skip when Blender is absent.
- Write tests that `assert`, don't `return bool` (legacy tests do; pytest passes them regardless of the bool — don't copy that pattern).

## The three numbering schemes (do not mix)
1. **Repo-internal** "Phase 1–15 / Stage 1–8 / Sprint N" — historical training-plan numbering in code comments, test names, `docs/AI_ARCHITECTURE.md`.
2. **V2 build plan** Phase 0–10 — the founder's V2 execution plan (audit: `docs/V2_PHASE0_AUDIT.md`).
3. **Public roadmap** PHASE_01–10 on animora.tech — marketing-facing.
When writing docs/commits, say which scheme you mean. New code comments should reference files/behavior, not phase numbers.
