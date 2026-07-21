"""
v1.3 — admin usage visibility tests.

Covers:
  - TokenUsage.cost_usd() pricing math (known models + unknown-model
    fallback, never raises).
  - usage_ledger.is_admin_email() allowlist check.
  - usage_ledger._aggregate_rows() — the pure aggregation logic behind
    the /admin/usage read path, unit-tested without a live Supabase
    project.
  - usage_ledger.record_usage_event() — best-effort insert: verifies
    the row shape sent to Supabase, and that a failure (missing
    service-role key, or the POST itself raising) never propagates —
    this is the single most important regression check, since this
    runs on every LLM call's hot path.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ANIMORA_ENV", "dev")
os.environ.setdefault("ANIMORA_LLM_PROVIDER", "anthropic")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-placeholder")

_PKG_DIR = Path(__file__).resolve().parent.parent
if "ai_backend" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "ai_backend", _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    _pkg = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    sys.modules["ai_backend"] = _pkg
    _spec.loader.exec_module(_pkg)  # type: ignore[union-attr]

from ai_backend import usage_ledger
from ai_backend.anthropic_client import TokenUsage
from ai_backend.config import settings

# ── TokenUsage.cost_usd ────────────────────────────────────────────────


def test_cost_usd_known_model():
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    # Sonnet: $3/Mtok input + $15/Mtok output = $18 for 1M+1M tokens.
    assert usage.cost_usd("claude-sonnet-4-6") == 18.0


def test_cost_usd_zero_usage_is_zero():
    usage = TokenUsage()
    assert usage.cost_usd("claude-opus-4-7") == 0.0


def test_cost_usd_cache_read_cheaper_than_input():
    read_heavy = TokenUsage(cache_read_input_tokens=1_000_000)
    input_heavy = TokenUsage(input_tokens=1_000_000)
    assert read_heavy.cost_usd("claude-sonnet-4-6") < input_heavy.cost_usd("claude-sonnet-4-6")


def test_cost_usd_unknown_model_falls_back_not_raises():
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    # Must not raise for a model not in MODEL_PRICING — falls back to
    # Sonnet-tier rates rather than crashing the usage.recorded emit.
    cost = usage.cost_usd("claude-some-future-model-nobody-added-yet")
    assert cost == 18.0  # same as the Sonnet fallback rates


# ── usage_ledger.is_admin_email ───────────────────────────────────────


def test_is_admin_email_empty_is_never_admin(monkeypatch):
    monkeypatch.setattr(settings, "animora_admin_emails", "founder@animora.tech")
    assert usage_ledger.is_admin_email("") is False


def test_is_admin_email_matches_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "animora_admin_emails", "founder@animora.tech, ops@animora.tech")
    assert usage_ledger.is_admin_email("founder@animora.tech") is True
    assert usage_ledger.is_admin_email("ops@animora.tech") is True
    assert usage_ledger.is_admin_email("random@user.com") is False


def test_is_admin_email_case_insensitive(monkeypatch):
    monkeypatch.setattr(settings, "animora_admin_emails", "Founder@Animora.Tech")
    assert usage_ledger.is_admin_email("founder@animora.tech") is True


def test_is_admin_email_empty_allowlist_admits_nobody(monkeypatch):
    monkeypatch.setattr(settings, "animora_admin_emails", "")
    assert usage_ledger.is_admin_email("anyone@anywhere.com") is False


# ── usage_ledger._aggregate_rows ──────────────────────────────────────


def test_aggregate_rows_empty():
    result = usage_ledger._aggregate_rows([])
    assert result["total_calls"] == 0
    assert result["total_cost_usd"] == 0.0
    assert result["by_user"] == {}
    assert result["by_model"] == {}


def test_aggregate_rows_sums_across_users_and_models():
    rows = [
        {"user_id": "u1", "model": "claude-opus-4-7", "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01},
        {"user_id": "u1", "model": "claude-sonnet-4-6", "input_tokens": 200, "output_tokens": 100, "cost_usd": 0.02},
        {"user_id": "u2", "model": "claude-opus-4-7", "input_tokens": 300, "output_tokens": 150, "cost_usd": 0.03},
    ]
    result = usage_ledger._aggregate_rows(rows)
    assert result["total_calls"] == 3
    assert result["total_input_tokens"] == 600
    assert result["total_output_tokens"] == 300
    assert result["total_cost_usd"] == 0.06

    assert result["by_user"]["u1"]["calls"] == 2
    assert result["by_user"]["u1"]["input_tokens"] == 300
    assert result["by_user"]["u2"]["calls"] == 1

    assert result["by_model"]["claude-opus-4-7"]["calls"] == 2
    assert result["by_model"]["claude-sonnet-4-6"]["calls"] == 1


def test_aggregate_rows_handles_missing_fields_gracefully():
    # A malformed/partial row (e.g. a schema drift) must not crash the
    # whole aggregate — missing numeric fields default to 0.
    rows = [{"user_id": "u1", "model": "claude-opus-4-7"}]
    result = usage_ledger._aggregate_rows(rows)
    assert result["total_calls"] == 1
    assert result["total_input_tokens"] == 0
    assert result["by_user"]["u1"]["input_tokens"] == 0


# ── usage_ledger.record_usage_event — best-effort, never raises ──────


async def test_record_usage_event_skips_without_service_role_key(monkeypatch):
    monkeypatch.setattr(settings, "supabase_service_role_key", "")
    # Must return cleanly with no attempt to call out at all.
    with patch("ai_backend.usage_ledger.httpx.AsyncClient") as mock_client_cls:
        await usage_ledger.record_usage_event({"session_id": "s1", "user_id": "u1"})
        mock_client_cls.assert_not_called()


async def test_record_usage_event_sends_expected_row_shape(monkeypatch):
    monkeypatch.setattr(settings, "supabase_service_role_key", "fake-service-role-key")

    captured = {}

    class _FakeResponse:
        status_code = 201
        text = ""

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, *, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _FakeResponse()

    with patch("ai_backend.usage_ledger.httpx.AsyncClient", return_value=_FakeClient()):
        await usage_ledger.record_usage_event({
            "session_id": "s1",
            "user_id": "u1",
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": 100, "output_tokens": 50,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            },
            "cost_usd": 0.0123,
            "elapsed_ms": 500,
            "attempts": 1,
        })

    assert captured["json"]["user_id"] == "u1"
    assert captured["json"]["session_id"] == "s1"
    assert captured["json"]["model"] == "claude-opus-4-7"
    assert captured["json"]["input_tokens"] == 100
    assert captured["json"]["cost_usd"] == 0.0123
    assert "usage_events" in captured["url"]
    # Never the anon key — service-role is required to bypass RLS on a
    # table with no anon/authenticated grants.
    assert captured["headers"]["apikey"] == "fake-service-role-key"


async def test_record_usage_event_swallows_http_error_response(monkeypatch):
    monkeypatch.setattr(settings, "supabase_service_role_key", "fake-key")

    class _FakeResponse:
        status_code = 500
        text = "internal server error"

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _FakeResponse()

    with patch("ai_backend.usage_ledger.httpx.AsyncClient", return_value=_FakeClient()):
        # Must not raise even on a 500 from Supabase.
        await usage_ledger.record_usage_event({"session_id": "s1", "user_id": "u1"})


async def test_record_usage_event_swallows_network_exception(monkeypatch):
    monkeypatch.setattr(settings, "supabase_service_role_key", "fake-key")

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise ConnectionError("network unreachable")

    with patch("ai_backend.usage_ledger.httpx.AsyncClient", return_value=_FakeClient()):
        # A real usage.recorded event must survive a total network failure.
        await usage_ledger.record_usage_event({"session_id": "s1", "user_id": "u1"})


# ── usage_ledger.fetch_usage_aggregate ─────────────────────────────────


async def test_fetch_usage_aggregate_raises_without_service_role_key(monkeypatch):
    monkeypatch.setattr(settings, "supabase_service_role_key", "")
    try:
        await usage_ledger.fetch_usage_aggregate()
        raised = False
    except RuntimeError:
        raised = True
    assert raised, "fetch_usage_aggregate should raise, not silently return, when misconfigured"


async def test_fetch_usage_aggregate_filters_by_user_id(monkeypatch):
    monkeypatch.setattr(settings, "supabase_service_role_key", "fake-key")
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"user_id": "u1", "model": "claude-opus-4-7",
                      "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001}]

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, *, headers, params):
            captured["params"] = params
            return _FakeResponse()

    with patch("ai_backend.usage_ledger.httpx.AsyncClient", return_value=_FakeClient()):
        result = await usage_ledger.fetch_usage_aggregate(user_id="u1")

    assert captured["params"]["user_id"] == "eq.u1"
    assert result["total_calls"] == 1


# ── /admin/usage route — called directly as a function, no HTTP layer ──
# (no TestClient dependency exists in this repo's test infra today; the
# route is a thin wrapper over validate_token/is_admin_email/
# fetch_usage_aggregate, each already unit-tested above/elsewhere —
# this locks down the wrapper's own auth-gating logic.)


async def test_admin_usage_route_missing_auth_header_is_401():
    import pytest
    from fastapi import HTTPException

    from ai_backend.main import admin_usage

    with pytest.raises(HTTPException) as excinfo:
        await admin_usage(authorization="", user_id=None)
    assert excinfo.value.status_code == 401


async def test_admin_usage_route_invalid_token_is_401(monkeypatch):
    import pytest
    from fastapi import HTTPException

    import ai_backend.main as main_mod
    from ai_backend.auth_middleware import AuthError

    async def _fake_validate_token(token):
        raise AuthError("bad token", "invalid_token")

    monkeypatch.setattr(main_mod, "validate_token", _fake_validate_token)

    with pytest.raises(HTTPException) as excinfo:
        await main_mod.admin_usage(authorization="Bearer not-a-real-token", user_id=None)
    assert excinfo.value.status_code == 401


async def test_admin_usage_route_non_admin_is_403(monkeypatch):
    import pytest
    from fastapi import HTTPException

    import ai_backend.main as main_mod
    from ai_backend.models import TokenClaims

    async def _fake_validate_token(token):
        return TokenClaims(
            user_id="u1", plan="trial", device_id="d1", exp=9999999999.0,
            email="not-an-admin@example.com",
        )

    monkeypatch.setattr(main_mod, "validate_token", _fake_validate_token)
    monkeypatch.setattr(settings, "animora_admin_emails", "founder@animora.tech")

    with pytest.raises(HTTPException) as excinfo:
        await main_mod.admin_usage(authorization="Bearer some-valid-token", user_id=None)
    assert excinfo.value.status_code == 403


async def test_admin_usage_route_admin_gets_aggregate(monkeypatch):
    import ai_backend.main as main_mod
    from ai_backend.models import TokenClaims

    async def _fake_validate_token(token):
        return TokenClaims(
            user_id="u1", plan="studio", device_id="d1", exp=9999999999.0,
            email="founder@animora.tech",
        )

    async def _fake_fetch_usage_aggregate(user_id=None):
        return {"total_calls": 42, "user_id_filter": user_id}

    monkeypatch.setattr(main_mod, "validate_token", _fake_validate_token)
    monkeypatch.setattr(settings, "animora_admin_emails", "founder@animora.tech")
    monkeypatch.setattr(usage_ledger, "fetch_usage_aggregate", _fake_fetch_usage_aggregate)

    result = await main_mod.admin_usage(authorization="Bearer some-valid-token", user_id="target-user")
    assert result["total_calls"] == 42
    assert result["user_id_filter"] == "target-user"
