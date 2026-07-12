# Animora V2 — Phase 0 Audit Report

**Date:** 2026-07-12 · **Auditor:** Claude Code (V2 build plan, Phase 0)
**Method:** Direct inspection of the repo at commit `00e95d4`, local test execution (Python 3.11.9, fresh venv), GitHub Actions API, deployed-site fetch, and machine inspection.
**Machine caveat:** This workstation is NOT the V1 build machine. No Animora install, no `blender-fork/` tree, no build toolchain present. Binary/installed-app checks are marked accordingly.

---

## 1. Headline

**The build plan's picture of V1 is wrong in both directions.**

- It *understates* the AI system: the plan's Phases 2, 3, 4, and most of 5 and 9 are already implemented in this repo — a loop enforcer, deterministic critic with correction loop, five personas with routing, taste-layer components, and a scored eval suite with a quality-aware CI gate.
- It *overstates* operational reality: every CI run ever recorded has failed, no release artifact exists on GitHub, the website in this repo is an empty shell, billing/metering is scaffolding only, and three different production deploy stories coexist in the repo (Fly.io+Bedrock, AWS Fargate, HF Spaces).

The true V2 gap is **not** the AI quality loop. It is: **money (metering + billing), trust (device binding + security), and shipping (green CI, signed installers, a real website).**

**Naming collision warning:** the repo has its own internal "Phase 1–15", "Stage 1–8", and "Sprint" numbering (from `docs/AI_ARCHITECTURE.md` and commit history) that does NOT line up with the V2 build plan's Phase 0–10. This report always means the **build plan's** phases unless it says "repo-internal".

---

## 2. Phase 0 checklist (per the plan)

### 2.1 Rebrand completeness — **PARTIAL (source-level DONE, binary unverified)**

| Evidence | Location |
|---|---|
| Comprehensive C/C++ string replacement map (app name, About, window title, quit/save dialogs, GPU-error MessageBox, signal handlers, AppData path `\Animora Technologies\Animora\`) | `scripts/rebrand.py` |
| Native delta incl. `space_animora` editor (built as `bf_editor_space_animora` per build log) | `patches/animora-native-full.patch` (356 KB) + `patches/native-overlay/source/` |
| Installer with Animora branding, wizard bitmaps, file association | `installer/windows/inno/Animora.iss`, `animora_register_anim.reg` |
| CI stage step smoke-checks `Animora.exe` and `Animora-launcher.exe` | `.github/workflows/build.yml` (staging step) |
| Addon: zero user-visible "Blender" strings (all remaining mentions are dev comments) | grep over `addons/animora_panel/**/*.py` |
| Git history: "de-Blender About/Help strings", "Panel branding: real Animora logo" | commits `4676e04`, `70887dd` |

**Gaps:** cannot verify the built binary on this machine (no fork tree, no install). The incremental build log still shows `blender.exe` / `BlendThumb.dll` in the build tree — renaming happens at staging (`scripts/stage_for_installer.py`), which I could not execute here. GPL compliance surface (`license.txt` in installer) present; full "no user-visible blender + GPL credit only" claim needs a packaged-build smoke test.

### 2.2 Auth — **DONE (client + backend verify), with server-side pieces outside this repo**

| Evidence | Location |
|---|---|
| Loopback PKCE flow: PKCE + state gen, one-shot listener on `127.0.0.1:0`, constant-time state compare | `auth/controller.py`, `auth/loopback.py`, `auth/pkce.py:34` (`hmac.compare_digest`) |
| Rotating refresh token in OS keyring (service `"animora"`), memory-only fallback, transient-failure token retention | `auth/session.py:71-136` |
| Supabase exchange (`auth-handoff-exchange` edge function client) | `auth/supabase.py` |
| Fullscreen 3-slide onboarding gate; signed-in users bypass | `onboarding.py` (28.6 KB) |
| Backend validates Supabase access tokens via `GET /auth/v1/user` | `ai-backend/auth_middleware.py:48-84` |
| Dedicated tests, all passing locally | `addons/tests/test_auth_{controller,loopback,pkce,session}.py` |

**Gaps / risks:**
- Every Supabase-authenticated user is hardcoded `plan="free"` (`auth_middleware.py:35`) — "paid tiers are server-authoritative later" is a code comment, i.e., V2 work.
- The website sign-in pages (`/signin`, `DeviceAuthorize.tsx`) and the Supabase RPC/edge functions (`issue_device_handoff`, `auth-handoff-exchange`) referenced by CLAUDE.md are **not in this repo**. The desktop flow depends on server-side code whose source location is unknown to this audit.
- Default `SUPABASE_URL` + publishable anon key hardcoded as fallbacks (`auth_middleware.py:30-33`). Anon key is public-by-design, but env-less fallbacks mask misconfiguration.
- Ghost auth-server: `.env.example` (`AUTH_SERVER_URL`, `DATABASE_URL`, JWT expiry vars) and root `requirements.txt` reference an `auth-server/` that does not exist.

### 2.3 AI panel — what it actually does today — **FAR beyond one-shot generation**

The plan's Loop 2 (inspect → plan → execute one step → capture → critique → correct → advance → final review) is substantially **implemented and code-enforced**:

| Loop element | Status | Evidence |
|---|---|---|
| Inspect | DONE | `get_scene_info`, `get_object_info` tools (`orchestrator/tools.py`); scene graph + JSON-patch diff (`scene_intelligence.py`, `scene_diff.py`) |
| Execute one step | DONE (enforced) | Repo-internal "Stage 1 loop enforcer" (`streaming.py:100-127,719-760`): at most ONE refinement mutation per iteration; excess mutations get synthesized "deferred" tool_results; `_ENFORCE_LOOP` default ON (env `ANIMORA_ENFORCE_LOOP=0` escape hatch) |
| Capture | DONE (forced) | `viewport_screenshot` tool; enforcer **injects a screenshot** after any mutating iteration (`streaming.py:1288-1360`, `enforcer.screenshot.injected` event); addon vision streaming (`vision.py`, 13-byte header protocol, Redis ring buffer) |
| Critique | DONE | Deterministic scene critic (`orchestrator/critic.py`, 30 KB) + artist's-eye vision check (`prompts/artists_eye.py`, `orchestrator/quality.py`) at checkpoints (`request_final_review` tool) |
| Correct | DONE | Stage 3A critique→correct loop, bounded `_MAX_CRITIC_CORRECTIONS=2` (`streaming.py:525-534,1628+`); Phase-5.5 quality retry (default 2, WS events `quality.retrying/succeeded/exhausted`) |
| Advance / re-read | DONE | Agentic loop ≤5–8 iterations (`_MAX_AGENT_ITERATIONS`), fresh scene graph per call (`get_live_scene_graph`, `streaming.py:176`) |
| Mechanical quality gates | DONE | First-step foundation gate (`streaming.py:1424`), scene-floor part-count gate (`:1472`), material-completeness gate (`:1524`), finished-by-default lighting+camera gate (`:1583`) — each single-shot with corrective injected messages |
| UX | DONE | Streaming chat panel (`panel.py`), suggestion chips (`suggest_next_steps`), border glow, custom GPU-drawn chrome (`ads/`), sculpt guard, composer buffer |

Tool surface (post "MCP pivot", `orchestrator/tools.py`): 3 inspect + 6 create/modify atomic ops + `set_world` + `execute_animora_code` escape hatch + `render_preview`/`render_final` + `use_asset` (PolyHaven CC0 fetcher, `assets/{catalog,fetcher,query}.py`) + `suggest_next_steps` + `request_final_review`. All 12 atomic handlers implemented addon-side (`operators.py:1709-2150`).

### 2.4 Existing harness pieces — **ALL FIVE plan-Phase-2 functions exist**

| Plan function | Repo equivalent | Evidence |
|---|---|---|
| `read_scene_graph` | `get_scene_info` | `tools.py:77`; addon `_atomic_get_scene_info` (`operators.py:1709`) |
| `read_object` | `get_object_info` | `tools.py:375`; addon handler present |
| `capture_viewport` | `viewport_screenshot` (+ `render_preview`/`render_final`) | `tools.py:88`; `operators.py:1728` |
| `execute_python` (single undo entry, stdout/stderr, timeout) | `execute_animora_code` | Quality-enforcer AST gate before dispatch (`quality_enforcer.py`); ONE `bpy.ops.ed.undo_push` per agent iteration (`operators.py:587-609`); AST-split statement runner with progress pings (`operators.py:860-886`); idle-aware timeout, 180 s hard ceiling (`tool_result_coordinator.py:60`) |
| `fetch_asset` | `use_asset` | `tools.py:451`; `assets/fetcher.py` (CDN fetch + local cache); addon apply logic (`operators.py:1386+`) |

**Loop-enforcer nuance vs the plan:** enforcement lives in the **backend orchestrator**, not the addon executor, and deliberately gates only *refinement* mutations (foundation blockout may batch — the original strict gate starved 22-mutation builds). `test_stage1_harness.py` covers block behavior; 3 of its cases skip without Blender (`bpy unavailable`) — the "provably blocked" verify-bar is met at unit level, needs one in-app smoke to fully close.

### 2.5 Repo hygiene — **MIXED: strong tests, red CI, no releases**

| Item | Status | Evidence |
|---|---|---|
| Unit tests | **GREEN locally: 238 passed, 3 skipped (bpy-dependent), 0 failed, 5.1 s** | Fresh venv, `ANIMORA_ENV=dev`, no API key needed for the suite |
| CI | **RED — 100% failure rate, all 8 recorded runs** (7 eval + 1 build, 2026-05-29 → 2026-07-07) | GitHub Actions API; latest eval run `28866987558` fails at "Run eval harness vs baseline" in **~1 second** → instant crash, almost certainly missing `ANTHROPIC_API_KEY` secret (or unpinned-dep import error), NOT a quality regression |
| Releases | **NONE** — no GitHub releases, no tags with artifacts | `/releases` API: empty |
| Build scripts | Complete pipeline exists: `build.py`, `rebrand.py`, `sync_addon.py`, `stage_for_installer.py`, `check_no_secrets.py`, full 3-OS CI workflow with signing hooks | `scripts/`, `.github/workflows/build.yml` |
| V1 build reality | `build_log_v1.0.txt` ends in **disk-full compile failure**; `build_log_incremental.txt` shows a later successful ninja link + install (incl. `space_animora`) on a now-absent tree (`Desktop\Animora\build`) | repo root logs |
| Lint | ~216 ruff findings (style-dominant: SIM105 36, E402 27, I001 27, F401 21; **B023 function-uses-loop-variable ×16 is a real-bug class worth triage**) | `ruff check . --statistics` |
| Test hygiene nit | 25 warnings: several tests `return bool` instead of `assert` (pytest counts them as passing regardless of the bool) | pytest output |
| Deps | `requirements.txt` unpinned (`anthropic>=0.28.0` etc.) — reproducibility risk | `ai-backend/requirements.txt` |
| Stray/ghost refs | `auth-server/` referenced but absent; `eval_v1_*` dumps at `ai-backend/` root; build logs at repo root; CLAUDE.md drift (says Opus 4.5 & Fargate; code says Opus 4.7 & Fly/Bedrock; describes website as full Next.js app) | various |

### 2.6 Public roadmap cross-check — **BLOCKED, need founder input**

- `https://animora.tech/roadmap` returns only the header "Animora — AI-Native 3D Creation Studio" to a terminal fetch — the page is JS-rendered (or the route doesn't exist and the shell is served).
- The repo's `website/src/` contains **only** `layout.tsx` + `globals.css` — no roadmap page, no signin, no pricing, no API routes. `package.json` declares `stripe`/`zod`/shadcn deps that nothing imports. Whatever animora.tech actually serves is **not built from this repo's website/** (note: deployed `<title>` says "…Studio", repo layout.tsx says "AI-Native 3D Creation" — they differ).
- **Per the plan: founder, please paste the current roadmap page contents** (and tell me where the deployed site's source lives).

---

## 3. V2 build-plan phases vs repo reality

| Plan phase | Status | One-line evidence |
|---|---|---|
| 1 — Skill library | **NOT DONE** | No `.claude/skills/` in repo; CLAUDE.md exists and is good but drifting |
| 2 — Harness + loop enforcer | **DONE (verify-bar ~90%)** | §2.4; bpy-dependent tests need a Blender machine to un-skip |
| 3 — Orchestrator + system prompt | **DONE** | Static master prompt with `cache_control` (`context_builder.py`), persona block, Haiku rolling session memory (`memory.py`), live scene state refreshed per call, model tiering (`router.py`: Opus 4.7 execution / Sonnet / Haiku), inline artist's-eye + retry |
| 4 — Personas | **DONE with mapping caveat** | 5 personas + router exist: generalist, hard_surface, environment, character, lighting_td (+ `mesh_repair_recipes`). Plan's list says "Materials" where repo has "generalist" — reconcile in Phase 4 |
| 5 — Taste layer | **PARTIAL — built but dark** | Spec/brief builder exists (`prompts/spec_builder.py`, `orchestrator/spec.py`) but `ANIMORA_ENABLE_SPEC` **defaults OFF** (`streaming.py:91`); composition rules (`prompts/composition_rules.py`) + final review (`orchestrator/final_review.py`) present; art-director-review-before-user not fully wired as a mandatory step |
| 6 — Cost control + metering | **PARTIAL — cost bounding yes, metering no** | Per-call `TokenUsage` incl. cache ratios (`anthropic_client.py:97-112`), 8k/16k output caps, iteration caps, retry budgets, Haiku triage for questions, prompt caching, checkpoint-batched vision. **Missing:** per-user token ledger, plan budget decrement/block-at-zero, hard per-task $ ceiling, panel usage meter (grep: none) |
| 7 — Billing + plans | **NOT DONE** | Stripe = `.env.example` placeholders + unused website dep; plan gates exist (`check_plan_access`: Opus→Studio; per-plan rate limits `config.py:68-73`) but live auth hardcodes `plan="free"`; no webhooks, no entitlement service, no upgrade/cancel flows |
| 8 — Trial protection + security | **PARTIAL/NOT DONE** | `device_id` plumbed through handoff + JWT claims; JWT hardening (iss/aud, dev-secret refusal `config.py:128-158`); WS origin allowlist, frame-size caps, per-session msg limits; script sandbox banlist. **Missing:** server-side device binding/lockout/rebind, trial window enforcement, abuse scoring (IPQS key unused), binary hardening, the full security test pass |
| 9 — Eval suite | **PARTIAL — suite real, gate dead** | 31 benchmarks, deterministic + critic scoring, recorded baselines (`eval/baseline.json`, v1 report 8/12), quality-aware regression gate incl. cost-waste tripwire (Stage 7/8) wired into `eval.yml`. **But CI has never passed** → the merge gate currently gates nothing; "beat-the-MCP" composition benchmark named in eval.yml comments — presence in `benchmarks.py` to verify in Phase 9 |
| 10 — Paid release | **NOT DONE** | No signed installers, no releases, updater story absent, V1 distribution channel unknown to this audit |

---

## 4. Critical findings (fix-first candidates, pending approval)

1. **CI is 100% red and the eval gate is dead** — likely one missing repo secret (`ANTHROPIC_API_KEY`). Until green, every quality guarantee the plan relies on is theater. (Also pin deps.)
2. **"V1 shipped and live" is unverifiable from this repo** — no releases, failed v1.0 build log, vanished build tree, unknown website source, deploy story split three ways (fly.toml Fly.io+Bedrock+`ANIMORA_ENV=production` vs CLAUDE.md Fargate vs commit `173b794` HF Spaces). Need founder ground truth on: what users have, from where, talking to which backend.
3. **The billing/trust stack (plan Phases 6–8) is the real V2 build** — everything else is completion/hardening of an already-strong core.
4. **Server-side auth artifacts live outside the repo** (Supabase RPC + edge function, website auth pages) — V2's device binding and entitlements will modify them; they must be brought under version control here first.
5. **Website needs to exist** — trial→paid (plan Phase 7 verify bar: "full trial→paid transition against the live site") is impossible against an empty `website/src`.

## 5. Questions for the founder (blocking Phase 0 close-out)

1. Paste the current **animora.tech/roadmap** contents (page is unreadable from the terminal).
2. Where does the **deployed animora.tech source** live? (This repo's `website/` is a stub.)
3. Which backend is **actually live** today — Fly.io (`animora-backend`), HF Spaces, or Fargate? Bedrock or direct Anthropic?
4. How was **V1 distributed** (direct download? what installer artifact?) and roughly how many installs?
5. Where are the **Supabase edge functions / SQL** (`issue_device_handoff`, `auth-handoff-exchange`) versioned?
6. Confirm persona mapping for Phase 4: keep **generalist** and add Materials duties to it, or build a distinct **Materials** persona per the plan?

## 6. Recommended Phase 1 adjustments (for approval discussion — no work started)

- The 12 planned skills map cleanly onto existing subsystems; several (`animora-product-loop`, `animora-orchestrator`, `animora-quality-gates`, `animora-personas`) should be **written from the code as-built**, not from scratch.
- Add to Phase 1 scope: fix CI secret + pin deps (cheap, unblocks everything), update CLAUDE.md drift (models, deploy, website reality, the repo-internal vs plan numbering glossary).
- Confirm V3 exclusions stand: nothing found in-repo contradicts them (no cloud rendering / collab / asset-library code present).

---

## 7. Addendum — founder answers verified (2026-07-12)

All six blocking questions were answered; each was verified with read-only API calls where possible. **Access tokens used for this verification were pasted into chat and must be rotated** (Vercel account token + Supabase personal access token). They are deliberately not recorded in this file.

### Q1 — Public roadmap (pasted by founder)
Ten public phases, "1/10 SHIPPED". Cross-check vs repo:
- **PHASE_01 Foundation (COMPLETE)** — all five claims verified TRUE in the repo: AI command panel ✓, scene reading ✓ (`scene_intelligence.py`), NL modeling/material ops ✓ (atomic tools), quality gate ✓ (enforcer + critic + artist's-eye), undo/redo ✓ (per-iteration undo push).
- **PHASE_02 Full Modeling & Materials (IN_PROGRESS)** — fair: geometry/shader nodes reachable only via `execute_animora_code` escape hatch; `mesh_repair_recipes.py` (auto-repair) and `sculpt_guard.py` exist; no dedicated procedural-nodes or sculpting pipeline yet.
- **PHASE_09 Public Launch** (billing, credit system, dashboard, public access) = the V2 build plan's Phases 6–7. Note: shipping V2 paid means public PHASE_09 jumps ahead of public 03–08 — the "building in public" page will need updating when V2 lands.
- **PHASE_10 Platform** (marketplace, collaboration, plugins, enterprise) = V3 exclusions — alignment confirmed.
- **A third numbering scheme.** Public roadmap phases ≠ build-plan phases ≠ repo-internal phases/stages/sprints. All future documents must say which scheme they mean.

### Q2 — Website source: located, and it is NOT this repo
Vercel project **`animora`** (team `taola-classics-projects`), framework **Vite** (not Next.js), linked to GitHub repo **`tc-byte/animora`** — a different account than this monorepo. Production deployment READY 2026-07-05, commit: *"Launch build: migrate to new Supabase, auth + dashboard + downloads, light redesign, GA, perf/security."* So the live site already has auth pages, a user dashboard, and a downloads page.
**Consequences:** (a) this monorepo's `website/` (Next.js stub) is dead code — delete or replace it; (b) CLAUDE.md's website section describes a fiction; (c) V2 billing UI work (plan Phase 7) happens in `tc-byte/animora` or that site migrates here — founder decision needed at Phase 7 planning; access to that repo will be required either way.

### Q3 — Live backend: HuggingFace Spaces, confirmed
`https://eatanimora-animora-backend.hf.space` → `/health` returns `{status: ok, version: 0.3.0}`. Addon defaults point there (`preferences.py:115-121`). `fly.toml` is aspirational (Fly.io + Bedrock, never the live path per founder); CLAUDE.md's "AWS Fargate" is stale. Both to be corrected in Phase 1 doc fixes.

### Q4 — V1 distribution
Founder: V1 users have the software installed; the live site's launch build includes a downloads page. Installer provenance/signing to be verified at plan Phase 10 (no GitHub releases exist; the artifact pipeline in CI has never gone green).

### Q5 — Supabase server-side auth artifacts: located and healthier than assumed
Project `iyvchfmuyllovfoztbfw` (eu-central-2, ACTIVE_HEALTHY — matches the URL hardcoded in `auth_middleware.py`). Live edge functions: `auth-handoff-exchange` (v2), `notify-waitlist` (v3), `delete-account` (v1).
The `issue_device_handoff` RPC was pulled and inspected: `SECURITY DEFINER`, requires authenticated user, enforces redirect-URI allowlist (`animora://` + loopback `127.0.0.1:{port}/auth/callback`), validates PKCE challenge length, and — notably — **binds device→user on first use with one `device_bindings` row per `device_id`**. So plan Phase 8's device-binding foundation already exists server-side. Still missing for Phase 8: tolerant fingerprinting, rebind path for legitimate new machines, account-switch lockout, trial windows, abuse scoring.
**Gap:** none of this SQL / edge-function source is under version control anywhere I can see. Bringing it into this monorepo (`supabase/` dir with migrations + functions) should be an early V2 work item.

### Q6 — Persona decision (recorded)
Founder chose a **distinct Materials persona**. Phase 4 scope therefore: add `materials_artist` persona + router trigger; `generalist` remains the fallback persona. (Repo then has 5 specialists + generalist base.)

### Findings revised by this addendum
- Finding #2 (deploy-story conflict): **resolved** — HF Spaces is live; fly.toml aspirational; Fargate stale.
- Finding #4 (server-side auth outside repo): **located**; version-control gap stands.
- Finding #5 (website missing): **revised** — a live site with auth/dashboard/downloads exists in `tc-byte/animora`; the gap is repo unification + billing surface, not existence.
- Phase 8 status upgraded from "PARTIAL/NOT DONE" to **PARTIAL** (server-side first-use device binding verified in production).

---

*Phase 0 complete, all blockers closed. STOPPED per Loop 1 — awaiting explicit "approved, continue" before Phase 1 (skill library). Recommended Phase 1 riders: CI secret + dep pinning, CLAUDE.md corrections (models, HF deploy, website reality, numbering glossary), delete/replace dead `website/`, plan `supabase/` version-control import.*
