# supabase/ — server-side auth source, import plan

**Status: NOT YET IMPORTED.** The source of truth for Animora's server-side auth
artifacts is the live Supabase project (`iyvchfmuyllovfoztbfw`, eu-central-2),
not git. This directory exists to change that. Until the import lands, any
change to these artifacts must be made in the Supabase dashboard **and**
snapshotted here in the same PR.

## Live inventory (verified 2026-07-12, V2 Phase 0 audit)

### Edge functions (Deno)
| Slug | Ver | Purpose |
|---|---|---|
| `auth-handoff-exchange` | v2 | Exchanges `code + PKCE verifier + device_id` from the desktop app for a Supabase session (final step of the loopback sign-in) |
| `delete-account` | v1 | Account deletion path |
| `notify-waitlist` | v3 | Website waitlist notifications |

### Database (public schema, confirmed pieces)
- **RPC `issue_device_handoff(p_device_id, p_code_challenge, p_redirect_uri, p_state, p_device_label)`** — `SECURITY DEFINER`, plpgsql. Verified behavior: requires `auth.uid()`; redirect-URI allowlist (`animora://%` OR `^http://127\.0\.0\.1:[0-9]{1,5}/auth/callback$`); rejects `code_challenge` < 32 chars; **binds device→user on first use — single `device_bindings` row per `device_id`** (INSERT … ON CONFLICT); mints a 5-minute single-use handoff code.
- **Table `device_bindings`** — device_id (unique), user_id, device_label, timestamps (full DDL to capture at import).
- Handoff-code storage table used by issue/exchange (name to confirm at import).

## Import procedure (run by someone with Supabase access)
```bash
npm i -g supabase
supabase login                       # interactive; or SUPABASE_ACCESS_TOKEN env
supabase link --project-ref iyvchfmuyllovfoztbfw

# 1. Edge functions → supabase/functions/<slug>/
supabase functions download auth-handoff-exchange
supabase functions download delete-account
supabase functions download notify-waitlist

# 2. Database schema → supabase/migrations/0000_baseline.sql
supabase db pull                     # pulls public schema incl. RPCs, tables, RLS policies

# 3. Verify + commit
git add supabase/ && git commit -m "Supabase: import live auth artifacts (edge functions + schema baseline)"
```

After import, the deploy path becomes `supabase functions deploy <slug>` /
`supabase db push` from CI or a trusted machine — dashboard edits are then
forbidden (they'd drift from git).

## Cross-repo invariant
The redirect-URI allowlist exists in **two** places: the `issue_device_handoff`
SQL above and the website's device-authorize client check (repo
`tc-byte/animora`). Change both together, and update the
`animora-device-binding` skill if the rules change.

## Related: v1.3 usage_events table
Backend-owned (`ai-backend/usage_ledger.py`), but — since this directory is
still pre-import and the only place ANY Supabase schema is versioned today
is the recovered website repo (`nexdrop1store-png/animora-website`,
`supabase/migrations/`) — its migration lives there too:
`supabase/migrations/20260719120000_usage_events.sql`. Service-role-only
(no anon/authenticated grants); requires `SUPABASE_SERVICE_ROLE_KEY` in the
backend's env, distinct from the `SUPABASE_ANON_KEY` used for auth checks.

## Security notes
- Never commit service-role keys or JWT secrets here — this directory holds
  SOURCE (functions, SQL), not credentials.
- V2 Phase 8 will modify `issue_device_handoff` (tolerant fingerprint scoring,
  trial ledger, rebind path) — do NOT start that work until this import has
  landed, or the changes will be dashboard-only and unreviewable.
