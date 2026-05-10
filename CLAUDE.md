# Animora — Claude Code Root Instructions

## Project Overview
Animora is an AI-native 3D creation tool: a full fork and rebrand of Blender with a persistent AI panel, real-time vision system, cloud LLM backend, auth/billing, and website.

## Monorepo Layout
```
Animora/
├── blender-fork/        Blender source tree (git submodule, do not edit tracked files)
├── addons/animora_panel/ Python Blender addon (the AI panel)
├── ai-backend/          FastAPI WebSocket server (LLM orchestration)
├── auth-server/         OAuth 2.0 + PKCE auth service + device binding
├── website/             animora.tech (Next.js 14, App Router)
├── skills/              Claude Code custom skills (tier1/tier2/tier3)
├── assets/              Branding assets (splash, icons, theme, startup.blend)
├── installer/           Platform installer scripts (NSIS, pkgbuild, AppImage)
└── scripts/             Build + rebrand automation
```

## Build Commands
```bash
# Full build (current platform)
python scripts/build.py

# Build specific platform
python scripts/build.py --platform windows
python scripts/build.py --platform macos
python scripts/build.py --platform linux

# Rebrand only (apply Animora assets/strings to blender-fork, no compile)
python scripts/rebrand.py

# Run AI backend locally
cd ai-backend && uvicorn main:app --reload --port 8000

# Run auth server locally
cd auth-server && uvicorn main:app --reload --port 8001

# Run website locally
cd website && npm run dev
```

## Python Conventions (addons, ai-backend, auth-server)
- Python 3.11+
- Type hints on all function signatures
- `ruff` for linting/formatting (line length 100)
- No bare `except:` — always catch specific exceptions
- No `print()` in production paths — use `logging`
- Addon code: follow Blender PEP 8 with `bpy` patterns; operators prefixed `OT_`, panels `PT_`

## TypeScript / Next.js Conventions (website)
- TypeScript strict mode
- App Router only (no pages/ directory)
- Tailwind CSS + shadcn/ui components
- Server components by default; `"use client"` only when needed

## Security Rules
- Never log access tokens, refresh tokens, or raw device fingerprints
- Never commit `.env` files — use `.env.example` with placeholder values
- Blender addon: never store tokens in plaintext files — use `keyring` (OS secure store)
- Backend: all bpy scripts must pass `quality_enforcer.py` before execution
- No `import os`, `import subprocess`, `open(` in LLM-generated scripts

## Key External Services
- **Claude API**: Haiku 4.5 (fast), Sonnet 4.6 (primary), Opus 4.5 (Studio/complex)
- **Deepgram**: Nova-3 voice transcription
- **Stripe**: Subscriptions and billing
- **Redis**: Session state and rate limiting

## Skills Available
Load skills in Claude Code sessions for domain context:
- `/animora-repo-conventions` — this file, naming rules, patterns
- `/blender-fork-build` — cmake flags, rebrand steps
- `/animora-auth-flow` — PKCE flow, device binding, token lifecycle
- `/animora-billing` — Stripe plan IDs, webhook events
- `/animora-vision-system` — Level 1/2/3 architecture and frame specs
- `/animora-llm-routing` — model selection logic
- `/animora-anti-abuse` — fingerprint components, enforcement
- `/animora-addon-api` — bpy operator/panel patterns

## Blender Fork Rules
- `blender-fork/` is a git submodule — never edit tracked files directly
- All branding changes go through `scripts/rebrand.py` (runs at build time, non-destructive)
- Rebrand script copies from `assets/` into build directory before cmake
- Do not add Animora-specific logic inside `blender-fork/source/` — use the addon instead
