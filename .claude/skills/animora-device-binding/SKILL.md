---
name: animora-device-binding
description: Use when working on device fingerprinting, trial protection, account-device binding, rebind/lockout flows, or abuse defense — "device binding", "fingerprint changed", "one account per device", "trial abuse", "rebind", "lockout", "device_bindings table", "second account refused". Documents the as-built binding (client fingerprint + Supabase RPC) and the V2 Phase-8 contract.
---

# Animora device binding + trial protection

## As built today
**Client fingerprint** — `addons/animora_panel/auth/session.py::compute_device_fingerprint()` (:179): hash components = CPU (`platform.processor()`), RAM GB (POSIX `os.sysconf`, absent on Windows), hostname (`platform.node()`), MAC (`uuid.getnode()`), plus Windows `MachineGuid` (HKLM\SOFTWARE\Microsoft\Cryptography) or `/etc/machine-id`. Docstring warning: **"Unchanged from V1 so the server-side device_bindings row keeps matching"** — any change to the recipe strands every existing binding; migrations must map old→new server-side.

**Server binding (verified in production Supabase, Phase-0 audit)** — RPC `public.issue_device_handoff(p_device_id, p_code_challenge, p_redirect_uri, p_state, p_device_label)`:
- `SECURITY DEFINER`; requires `auth.uid()` (authenticated browser session)
- redirect allowlist: `animora://%` OR `^http://127\.0\.0\.1:[0-9]{1,5}/auth/callback$`
- PKCE `code_challenge` ≥ 32 chars; 5-min single-use handoff code
- **Binds on first use: one `device_bindings` row per `device_id`** (INSERT … ON CONFLICT path) → a second account on a bound device is refusable server-side
- Companion edge function: `auth-handoff-exchange` (code+verifier+device_id → session). Also live: `delete-account`, `notify-waitlist`.
- ⚠️ None of this SQL/edge source is in git yet — see `supabase/README.md` import plan. Change procedure until imported: edit in Supabase dashboard, then snapshot into `supabase/` in the same PR.
- The redirect-URI allowlist exists in TWO places: the RPC SQL and the website's device-authorize client check (repo `tc-byte/animora`) — change both together.

## V2 Phase-8 contract (to build)
1. **Tolerant fingerprint**: current recipe is brittle (RAM upgrade, MAC randomization, hostname rename each shift it). Target: component-wise match scoring server-side — store components hashed individually; K-of-N match (e.g. ≥3 of 5 with MachineGuid weighted highest) = same device → transparent re-issue; partial match → step-up (email confirm) → rebind; low match → new device path.
2. **Trial protection window**: `trial_end` in claims + `device_bindings.created_at`; a device whose binding ever carried a trial cannot start a second trial (per-device trial ledger, not per-account).
3. **Account-switch lockout**: binding a device to account B within N days of account A ⇒ cooldown + risk flag (parameters server-side, tunable without client release).
4. **Honest rebind path**: user-initiated "this is my new machine" → verify via signed-in browser session → move binding, keep audit row (old binding tombstoned, never deleted — abuse forensics).
5. **Abuse risk scoring**: IPQS (`IPQS_API_KEY` reserved in `.env.example`) + velocity signals (bindings/IP/day, trials/payment-fingerprint) → score on `issue_device_handoff`; high score = manual-review queue, not silent ban.
Verify bars (V2 plan): second account on bound device refused; honest rebind works; zero client-side secrets; no critical/high findings open.

## Rules
- The fingerprint is IDENTIFICATION, not authentication — it accompanies real auth (Supabase session), never replaces it.
- Never log raw fingerprints or tokens — sha256 prefixes only (`anthropic_client.fingerprint_key()` pattern).
- Enforcement lives server-side (RPC/edge/backend). Client-side refusals are UX sugar an attacker deletes.
- `device_id` rides in `TokenClaims` (`models.py`) — backend features (metering, session limits) should key trust decisions on the SERVER-verified binding, not the claim alone.
