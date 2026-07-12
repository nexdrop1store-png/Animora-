---
name: animora-metering-billing
description: Use when building or modifying token metering, plan entitlements, budget ceilings, Stripe billing, or the panel usage meter — "meter tokens per user", "plan limits", "trial expired", "budget ceiling", "runaway task", "Stripe webhook", "entitlements", "block at zero", "top-up", "usage meter in panel". Documents what exists, the server-authority rules, and the build contract for V2 Phases 6–7.
---

# Animora metering + billing

## Status: metering/billing is V2 build-plan Phases 6–7 — mostly NOT built. This skill is the contract.

## What already exists (build on these, don't duplicate)
| Piece | Where |
|---|---|
| Per-call token usage (input/output/cache_create/cache_read + cache ratio) | `anthropic_client.py::TokenUsage` (:97-112), emitted as `usage.recorded` events |
| Plan claims in tokens | `models.py::TokenClaims` — `plan: trial|standard|studio`, `trial_end`, `device_id`, `seats_used` (live Supabase path currently hardcodes `plan="free"` at `auth_middleware.py:35` — the first thing entitlements replaces) |
| Plan gates | `auth_middleware.py::check_plan_access` (Opus → studio only), `check_rate_limit` (Redis token bucket, messages/hour+day per plan, `config.py:68-73`) |
| Cost bounding by construction | iteration cap 8, exec output cap 8k, retry budget 2, Haiku triage for questions, prompt caching, checkpoint-batched vision |
| Stripe config placeholders | `.env.example` (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_STANDARD_PRICE_ID`, `STRIPE_STUDIO_PRICE_ID`) |
| Eval-side cost accounting | Stage-8 scoring: run cost, per-category cost, quality-per-dollar; CI trips on cost-up-no-quality-gain |

## Non-negotiable rules
1. **The server is the only authority on plan status.** The desktop app renders what the backend says; a patched local "paid" flag must change nothing. Entitlements are read server-side per connection/turn — never trusted from client hello frames.
2. **Meter at the same choke point that tracks usage**: the `usage.recorded` path in `anthropic_client`. Every LLM call in a user turn (router, intent, memory compression, artist's-eye, retries) bills the same user ledger — no free side-channels.
3. **Plans (approved V2 pricing)**: Trial — 3-day, device-bound; Standard $29 metered; Studio $79. Overage = transparent top-ups, never silent throttling to a worse model (see router rationale: model downgrade made trial feel broken).
4. **Two ceilings, different jobs**: per-user plan budget (billing-cycle token allowance, decremented live, blocks at zero with an upgrade/top-up message) and per-TASK hard ceiling (kills a runaway agentic turn regardless of remaining plan budget; surfaces `task_budget_exceeded` to panel).
5. **Ledger storage**: Redis for the hot counter (same instance as rate limits), Supabase Postgres for the durable cycle ledger + Stripe linkage. Reconcile on cycle close; Redis loss must never mint free tokens (rebuild from Postgres watermark).

## Build order that works (Phase 6 → 7)
1. Per-user ledger keyed `usage:{user_id}:{cycle}` decremented in `usage.recorded`; expose remaining in `session_info` + a WS `usage.update` event.
2. Panel live meter consuming `usage.update` (panel currently has NO usage UI — grep confirms).
3. Per-task ceiling in the streaming loop (sum of iteration usage vs cap; cancel path already exists via coordinator cancel_event).
4. Stripe: Checkout for plan purchase, webhooks (`checkout.session.completed`, `customer.subscription.updated/deleted`) → entitlements table → JWT/plan lookup per connection. Webhook handler verifies `STRIPE_WEBHOOK_SECRET`, is idempotent (event-id dedupe), and lives server-side only.
5. Upgrade/downgrade/cancel + top-ups; trial→paid transition test against the live site.
Verify bars (from the V2 plan): modeled-cost range holds on a medium scene; ceiling kills a runaway task; metering decrements correctly and blocks at zero; patched local flag changes nothing.

## Pitfalls
- Rate limits (messages/day) are NOT metering (tokens) — keep both; they fail differently.
- BYOK sessions bypass pooled-key cost but still consume plan features — decide metering policy explicitly and encode it, don't leave it implicit.
- Cache reads are ~10× cheaper than input tokens — meter with weights, or a cached-heavy turn overcharges users.
- `seats_used` exists in claims for future multi-seat; ignore it in V2 (Studio is single-seat) but don't remove it.
