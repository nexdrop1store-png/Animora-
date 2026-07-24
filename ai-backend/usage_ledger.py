"""
v1.3 — admin usage visibility.

Deliberately smaller than V2 Phase 6/7's full metering/billing plan
(see .claude/skills/animora-metering-billing/SKILL.md — that's the
Redis-hot-ledger + Postgres-durable-ledger + Stripe contract). This is
a founder-visibility MVP: durable storage of every LLM call's token
usage + estimated cost, queryable in aggregate and per-user, gated by
an env-var email allowlist rather than a real role system.

Two responsibilities:
  1. record_usage_event() — best-effort, swallow-on-failure insert of
     each `usage.recorded` event bus payload (anthropic_client.py) into
     the `usage_events` Supabase table. That table has NO anon/
     authenticated grants (service-role only — internal telemetry, not
     user-facing data), so this requires SUPABASE_SERVICE_ROLE_KEY, a
     distinct and more-privileged secret from auth_middleware.py's
     SUPABASE_ANON_KEY. A dropped row must never break a user's turn —
     every failure path here logs and returns, never raises.
  2. fetch_usage_aggregate() — the read path for the /admin/usage
     route: aggregate + per-user + per-model token/cost totals. This
     one DOES raise on failure — it's an admin-only read, not a
     best-effort background write, so the caller should see the error.

No Supabase client library dependency exists in this backend (only
raw httpx REST calls against PostgREST) — this module follows the
same pattern already established in auth_middleware.py.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .auth_middleware import SUPABASE_URL
from .config import settings

log = logging.getLogger("animora.usage_ledger")

_TABLE = "usage_events"
_INSERT_TIMEOUT_SEC = 5.0
_QUERY_TIMEOUT_SEC = 10.0
# Bounds how many rows a single aggregate query pulls before summing in
# Python — a lean MVP (no server-side aggregate view), not a scalable
# analytics pipeline. Revisit if usage_events outgrows this.
_MAX_ROWS_PER_QUERY = 20_000


def is_admin_email(email: str) -> bool:
    """Allowlist check against ANIMORA_ADMIN_EMAILS. Empty/missing
    email is never an admin, regardless of allowlist contents."""
    if not email:
        return False
    return email.strip().lower() in settings.admin_email_allowlist


async def record_usage_event(payload: dict[str, Any]) -> None:
    """Best-effort insert from the usage.recorded event bus payload.
    Never raises — a dropped usage row must never break a user's turn."""
    if not settings.supabase_service_role_key:
        log.debug("usage_ledger.record skipped: SUPABASE_SERVICE_ROLE_KEY not configured")
        return
    usage = payload.get("usage") or {}
    row = {
        "user_id": payload.get("user_id", "unknown"),
        "session_id": payload.get("session_id", "unknown"),
        "model": payload.get("model", ""),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "cost_usd": payload.get("cost_usd", 0.0),
        "elapsed_ms": payload.get("elapsed_ms", 0),
        "attempts": payload.get("attempts", 1),
    }
    try:
        async with httpx.AsyncClient(timeout=_INSERT_TIMEOUT_SEC) as client:
            resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/{_TABLE}",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json=row,
            )
        if resp.status_code >= 300:
            log.warning(
                "usage_ledger.record failed: HTTP %d %s",
                resp.status_code, resp.text[:200],
            )
    except Exception as exc:
        log.warning("usage_ledger.record failed: %s", exc)


def _aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure aggregation, pulled out from fetch_usage_aggregate so the
    summing logic is unit-testable without a live Supabase project."""
    total_input = total_output = 0
    # Bug 1 — cache tokens were RECORDED per row but never surfaced, so the
    # dashboard under-reported real token volume, worst for cache-heavy Opus
    # execution turns (each iteration re-sends a large cached prompt read
    # back cheaply as cache_read). Aggregate + expose them.
    total_cache_read = total_cache_creation = 0
    total_cost = 0.0
    by_user: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}

    def _bucket() -> dict[str, Any]:
        return {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "cost_usd": 0.0, "calls": 0,
        }

    for row in rows:
        uid = row.get("user_id") or "unknown"
        model = row.get("model") or "unknown"
        in_tok = int(row.get("input_tokens", 0) or 0)
        out_tok = int(row.get("output_tokens", 0) or 0)
        cache_read = int(row.get("cache_read_input_tokens", 0) or 0)
        cache_creation = int(row.get("cache_creation_input_tokens", 0) or 0)
        cost = float(row.get("cost_usd", 0.0) or 0.0)

        total_input += in_tok
        total_output += out_tok
        total_cache_read += cache_read
        total_cache_creation += cache_creation
        total_cost += cost

        for bucket_map, key in ((by_user, uid), (by_model, model)):
            b = bucket_map.setdefault(key, _bucket())
            b["input_tokens"] += in_tok
            b["output_tokens"] += out_tok
            b["cache_read_tokens"] += cache_read
            b["cache_creation_tokens"] += cache_creation
            b["cost_usd"] += cost
            b["calls"] += 1

    return {
        "total_calls": len(rows),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cache_read_tokens": total_cache_read,
        "total_cache_creation_tokens": total_cache_creation,
        "total_cost_usd": round(total_cost, 4),
        "by_user": {k: {**v, "cost_usd": round(v["cost_usd"], 4)} for k, v in by_user.items()},
        "by_model": {k: {**v, "cost_usd": round(v["cost_usd"], 4)} for k, v in by_model.items()},
    }


# Bug 8 — cap how many distinct users we resolve id→email for in one
# aggregate, so a large user base can't fan out into thousands of admin-API
# calls. Early product; revisit with a batch endpoint if it's ever hit.
_MAX_EMAIL_RESOLVE = 200


async def fetch_usage_aggregate(user_id: str | None = None) -> dict[str, Any]:
    """Query usage_events and aggregate token/cost totals — overall,
    per-user, per-model. `user_id` filters to a single user's rows.

    Raises on a genuine query failure — this is an admin-only read
    path, not a best-effort background write; the caller (the
    /admin/usage route) should surface the error, not silently return
    an empty result that looks like "no usage yet"."""
    if not settings.supabase_service_role_key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY not configured")

    params: dict[str, str] = {
        # Bug 1 — cache columns MUST be selected or _aggregate_rows sums
        # zero for them (they exist in the table but weren't being read).
        "select": (
            "user_id,model,input_tokens,output_tokens,"
            "cache_read_input_tokens,cache_creation_input_tokens,cost_usd"
        ),
        "limit": str(_MAX_ROWS_PER_QUERY),
        "order": "created_at.desc",
    }
    if user_id:
        params["user_id"] = f"eq.{user_id}"

    async with httpx.AsyncClient(timeout=_QUERY_TIMEOUT_SEC) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/{_TABLE}",
            headers={
                "apikey": settings.supabase_service_role_key,
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
            },
            params=params,
        )
        resp.raise_for_status()
        rows = resp.json()
        aggregate = _aggregate_rows(rows)
        # Bug 8 — resolve user UUIDs to emails SERVER-SIDE (service-role
        # Auth admin API), so the admin can see WHO is using Animora. We do
        # NOT ship the user table to the client to join in the browser.
        await _attach_emails(client, aggregate)
    return aggregate


async def _attach_emails(client: "httpx.AsyncClient", aggregate: dict[str, Any]) -> None:
    """Best-effort: add an `email` field to each by_user entry, resolved
    via GET /auth/v1/admin/users/{id}. A failure to resolve leaves email
    None — it must never break the aggregate. Never logs the email."""
    by_user: dict[str, Any] = aggregate.get("by_user", {})
    uids = [u for u in by_user if u and u != "unknown"][:_MAX_EMAIL_RESOLVE]
    if not uids:
        return
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
    }

    async def _one(uid: str) -> tuple[str, str | None]:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/auth/v1/admin/users/{uid}", headers=headers,
            )
            if r.status_code == 200:
                return uid, (r.json() or {}).get("email")
        except Exception:
            pass
        return uid, None

    import asyncio
    results = await asyncio.gather(*(_one(u) for u in uids))
    for uid, email in results:
        by_user[uid]["email"] = email
