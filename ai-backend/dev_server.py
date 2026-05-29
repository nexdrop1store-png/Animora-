"""
Local dev launcher for the Animora AI backend.

Replaces the production prerequisites (Redis, auth-server-issued JWTs) with
in-process stubs so a developer can launch the backend with one command:

    python dev_server.py

What it does, in order:

  1. Loads ANTHROPIC_API_KEY from ai-backend/.env (via pydantic-settings).
  2. Replaces session_manager.get_redis() with an in-memory dict-Redis stub
     that implements just the operations Animora actually calls.
  3. Replaces auth_middleware.decode_token() with a permissive dev decoder
     that accepts ANY token string and synthesises a "trial" claim. The
     addon's WS hello flow is unchanged.
  4. Boots uvicorn on 127.0.0.1:8000 with the standard FastAPI app.

This file is DEV-ONLY. None of its monkey-patches affect the production
deployment (Fargate runs `uvicorn main:app` directly with real Redis +
real JWTs from auth-server). If you import from this module in production
code, you've made a mistake.

To make the addon use this server, toggle "Dev Mode" in
    Animora > Preferences > Add-ons > Animora > Connection
The addon's ws_client will connect to ws://localhost:8000/ws/{session_id}.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

# Opt into dev mode BEFORE importing ai_backend.config — otherwise the
# safety check in config._enforce_secrets_safety refuses to let the
# process start when JWT_SECRET is still the dev sentinel.
os.environ.setdefault("ANIMORA_ENV", "dev")
from typing import Any

# Bootstrap the package under the underscore name (dir is "ai-backend")
_PKG_DIR = Path(__file__).resolve().parent
if "ai_backend" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "ai_backend", _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    _pkg = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    sys.modules["ai_backend"] = _pkg
    _spec.loader.exec_module(_pkg)  # type: ignore[union-attr]


# ── In-memory Redis stub ────────────────────────────────────────────────
# Implements just the operations Animora's backend actually calls. If
# session_manager or vision_buffer start using new commands, add them
# here. Operations are async to match the redis.asyncio surface.

class _InMemoryRedis:
    """Tiny dict-backed stand-in for redis.asyncio.Redis."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._lists: dict[str, list[Any]] = {}
        self._expires: dict[str, float] = {}

    def _check_expiry(self, key: str) -> None:
        if key in self._expires and self._expires[key] < time.time():
            self._data.pop(key, None)
            self._lists.pop(key, None)
            self._expires.pop(key, None)

    async def get(self, key: str) -> Any:
        self._check_expiry(key)
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    async def setex(self, key: str, ttl: int, value: Any) -> None:
        self._data[key] = value
        self._expires[key] = time.time() + ttl

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self._data:
                self._data.pop(k)
                n += 1
            if k in self._lists:
                self._lists.pop(k)
                n += 1
            self._expires.pop(k, None)
        return n

    async def expire(self, key: str, ttl: int) -> None:
        self._expires[key] = time.time() + ttl

    async def incr(self, key: str) -> int:
        self._check_expiry(key)
        v = int(self._data.get(key, 0)) + 1
        self._data[key] = v
        return v

    async def rpush(self, key: str, *values: Any) -> int:
        lst = self._lists.setdefault(key, [])
        lst.extend(values)
        return len(lst)

    async def lrange(self, key: str, start: int, end: int) -> list[Any]:
        self._check_expiry(key)
        lst = self._lists.get(key, [])
        if end == -1:
            return lst[start:]
        return lst[start:end + 1]

    async def llen(self, key: str) -> int:
        self._check_expiry(key)
        return len(self._lists.get(key, []))

    async def ltrim(self, key: str, start: int, end: int) -> None:
        lst = self._lists.get(key)
        if lst is None:
            return
        if end == -1:
            self._lists[key] = lst[start:]
        else:
            self._lists[key] = lst[start:end + 1]

    def pipeline(self) -> "_Pipeline":
        return _Pipeline(self)


class _Pipeline:
    """Stub pipeline that buffers ops and executes them sequentially."""

    def __init__(self, r: _InMemoryRedis) -> None:
        self._r = r
        self._ops: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def _record(*args, **kwargs):
            self._ops.append((name, args, kwargs))
            return self
        return _record

    async def execute(self) -> list[Any]:
        out = []
        for name, args, kwargs in self._ops:
            method = getattr(self._r, name)
            out.append(await method(*args, **kwargs))
        self._ops.clear()
        return out


_singleton = _InMemoryRedis()


# ── Monkey-patch session_manager.get_redis() ───────────────────────────

import ai_backend.session_manager as _sm


async def _stub_get_redis() -> _InMemoryRedis:
    return _singleton


_sm.get_redis = _stub_get_redis  # type: ignore[assignment]
print("[dev_server] Stubbed Redis with in-memory store.")


# ── Permissive auth decoder for local dev ──────────────────────────────

import ai_backend.auth_middleware as _auth
from ai_backend.models import TokenClaims


def _stub_decode_token(token: str) -> TokenClaims:
    """Accept any token in dev mode. Synthesise a trial-plan claim."""
    return TokenClaims(
        user_id=f"dev-user-{token[:8] if token else 'anon'}",
        plan="trial",
        trial_end=time.time() + 86400 * 365,  # 1 year
        device_id="dev-device",
        seats_used=1,
        exp=time.time() + 86400 * 365,
    )


async def _stub_check_rate_limit(_redis, _user_id: str, _plan: str) -> None:
    return  # no rate limiting in dev


_auth.decode_token = _stub_decode_token  # type: ignore[assignment]
_auth.check_rate_limit = _stub_check_rate_limit  # type: ignore[assignment]
print("[dev_server] Stubbed auth: any token accepted as trial-plan.")


# ── Force-import the app AFTER patching so main.py picks up the stubs ──

import ai_backend.main as _main_module
app = _main_module.app

# Now also patch the names main.py imported directly (it does
# `from .auth_middleware import decode_token, check_rate_limit, ...`
# at module-load time, which captures the originals). Reassign the
# references inside main's namespace too.
_main_module.decode_token = _stub_decode_token  # type: ignore[assignment]
_main_module.check_rate_limit = _stub_check_rate_limit  # type: ignore[assignment]
print("[dev_server] Patched main.py's import bindings.")


# ── Run uvicorn ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    print("[dev_server] Animora AI backend starting on http://127.0.0.1:8000")
    print("[dev_server] WebSocket endpoint: ws://localhost:8000/ws/<session_id>?token=dev")
    print("[dev_server] REST validate:      http://localhost:8000/validate-key")
    print("[dev_server] Health check:       http://localhost:8000/health")
    print("[dev_server] (Ctrl+C to stop)")
    print()
    # WS keepalive — must be > _TOOL_RESULT_WAIT_SEC (180s) in
    # orchestrator/streaming.py + headroom for slow Bedrock Sonnet/Opus
    # calls layered on top. With Sprint 1's SPEC step (18-25s Sonnet) +
    # Sprint 3's asset fetches + the agentic loop dispatching tool_uses
    # the addon may take 30-120s to execute, a single turn can sit in
    # "waiting" state for several minutes.  Drop the keepalive below
    # that and the WS dies mid-turn → addon reconnects → turn is lost.
    # 300s is generous; legitimately stuck turns hit the orchestrator's
    # wall-clock cap (_MAX_AGENT_WALL_CLOCK_SEC=900s) anyway.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
        ws_ping_interval=120.0,
        ws_ping_timeout=300.0,
    )
