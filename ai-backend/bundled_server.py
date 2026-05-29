"""
Animora bundled backend — the SHIPPING local server for recording builds.

Unlike `dev_server.py` (dev-only, explicitly excluded from installers),
THIS entrypoint is meant to be frozen with PyInstaller and shipped inside
the Animora installer so a non-technical user (the cofounder) gets a fully
working, auto-recording backend with zero setup. The Animora addon
auto-launches the frozen exe on startup (see addons/animora_panel/bundle.py).

What it does that dev_server.py does NOT:
  • Forces ANIMORA_RECORD_SESSIONS=1 — recording is always on.
  • Forces ANIMORA_LLM_PROVIDER=bedrock — the recording build uses Bedrock.
  • Reads Bedrock creds from a bundled `animora_backend.env` sitting NEXT TO
    the frozen exe (NOT from ai-backend/.env — that file isn't reliably
    locatable once frozen, because config.py anchors on __file__ which
    resolves inside PyInstaller's _MEIPASS).
  • Writes recordings to "<Desktop>/Animora Recordings" — a user-writable
    folder a non-technical user can find, zip, and send back. ({app} is
    Program Files = read-only for normal users, so recordings can't go there.)
  • Writes a startup log next to the recordings so a silent failure (no
    console window) is still diagnosable.

It is otherwise identical to dev_server.py: an in-memory Redis stub and a
permissive auth decoder, so no Redis server and no JWT/auth-server are
needed. Like dev_server.py, this bypasses auth and MUST NOT run in a real
production deployment — shipping it frozen inside the recording build is
the intended, scoped use.

Self-contained on purpose: it does NOT import dev_server.py (that keeps the
dev-only file out of the frozen bundle and keeps this shipping artifact's
trust boundary explicit).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# ── 0. Resolve base dir (frozen-aware) ──────────────────────────────────
# When frozen by PyInstaller, sys.frozen is True and the bundled data
# files (animora_backend.env) sit next to the exe (sys.executable). When
# run as a plain script for local verification, they sit next to this file.
_FROZEN = bool(getattr(sys, "frozen", False))
_EXE_DIR = Path(sys.executable).resolve().parent if _FROZEN else Path(__file__).resolve().parent
_PKG_DIR = Path(__file__).resolve().parent  # the ai-backend source dir


# ── 1. Recordings directory (user-writable, easy to find) ───────────────
def _resolve_recordings_dir() -> Path:
    """<Desktop>/Animora Recordings, falling back to ~/Animora Recordings
    when there's no Desktop (some locked-down/localized profiles)."""
    home = Path.home()
    desktop = home / "Desktop"
    root = desktop if desktop.is_dir() else home
    rec = root / "Animora Recordings"
    try:
        rec.mkdir(parents=True, exist_ok=True)
    except OSError:
        rec = home / "Animora Recordings"
        rec.mkdir(parents=True, exist_ok=True)
    return rec


_REC_DIR = _resolve_recordings_dir()


# ── 2. Startup logging to a file (no console in the frozen build) ───────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
    handlers=[
        logging.FileHandler(_REC_DIR / "animora_engine.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("animora.bundled_server")
log.info("bundled_server starting (frozen=%s exe_dir=%s rec_dir=%s)", _FROZEN, _EXE_DIR, _REC_DIR)


# ── 3. Load bundled Bedrock creds from animora_backend.env ──────────────
def _load_bundled_env() -> None:
    """Parse a simple KEY=VALUE file next to the exe and seed os.environ.
    Uses setdefault so an explicitly-set OS env var still wins (handy for
    the user overriding the bundled key during testing)."""
    env_path = _EXE_DIR / "animora_backend.env"
    if not env_path.is_file():
        log.warning("bundled env file not found at %s — relying on existing os.environ", env_path)
        return
    loaded = 0
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
            loaded += 1
    log.info("loaded %d keys from %s", loaded, env_path)


_load_bundled_env()


# ── 4. Force the recording-build environment BEFORE importing the app ───
# Critical ordering: ai_backend.config reads these at import time, and the
# provider selection / secrets-safety check both depend on them. Setting
# them here guarantees they're live no matter how config.py resolves its
# own .env (which is unreliable once frozen).
os.environ.setdefault("ANIMORA_ENV", "dev")          # skip JWT secrets-safety check
os.environ["ANIMORA_LLM_PROVIDER"] = "bedrock"        # force Bedrock
os.environ["ANIMORA_RECORD_SESSIONS"] = "1"           # recording always on
os.environ["ANIMORA_RECORDINGS_DIR"] = str(_REC_DIR)  # land recordings on the Desktop
os.environ.setdefault("BEDROCK_AWS_REGION", "us-east-1")

if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
    log.error(
        "AWS_BEARER_TOKEN_BEDROCK is not set and animora_backend.env did not "
        "supply it. The backend will start but every AI turn will fail. "
        "Check that animora_backend.env shipped next to the exe."
    )


# ── 5. Bootstrap the ai_backend package (dir is 'ai-backend', hyphenated) ─
# When run directly (python ai-backend/bundled_server.py) the package isn't
# importable under its underscore name yet, so bootstrap it the same way
# dev_server.py does. When frozen via scripts/freeze_backend.py the package
# is copied to an importable 'ai_backend' dir, so this guard is a no-op.
if "ai_backend" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "ai_backend", _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    _pkg = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    sys.modules["ai_backend"] = _pkg
    _spec.loader.exec_module(_pkg)  # type: ignore[union-attr]


# ── 6. In-memory Redis stub (mirrors dev_server's, self-contained) ──────
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


import ai_backend.session_manager as _sm  # noqa: E402


async def _stub_get_redis() -> _InMemoryRedis:
    return _singleton


_sm.get_redis = _stub_get_redis  # type: ignore[assignment]
log.info("stubbed Redis with in-memory store")


# ── 7. Permissive auth decoder (accept any token as a trial session) ────
import ai_backend.auth_middleware as _auth  # noqa: E402
from ai_backend.models import TokenClaims  # noqa: E402


def _stub_decode_token(token: str) -> TokenClaims:
    """Accept any token. The addon's bundle-mode auto-connect sends 'dev'."""
    return TokenClaims(
        user_id="cofounder",   # stable id → all turns land under recordings/cofounder/
        plan="trial",
        trial_end=time.time() + 86400 * 365,
        device_id="recording-build",
        seats_used=1,
        exp=time.time() + 86400 * 365,
    )


async def _stub_check_rate_limit(_redis, _user_id: str, _plan: str) -> None:
    return  # no per-plan rate limiting in the recording build


_auth.decode_token = _stub_decode_token        # type: ignore[assignment]
_auth.check_rate_limit = _stub_check_rate_limit  # type: ignore[assignment]
log.info("stubbed auth: any token accepted as trial-plan")


# ── 8. Import the app AFTER patching, then re-patch main's bindings ─────
import ai_backend.main as _main_module  # noqa: E402

app = _main_module.app
_main_module.decode_token = _stub_decode_token        # type: ignore[assignment]
_main_module.check_rate_limit = _stub_check_rate_limit  # type: ignore[assignment]
log.info("app imported + main.py bindings patched")


# ── 9. Run ───────────────────────────────────────────────────────────────
def main() -> None:
    import uvicorn

    log.info("Animora engine listening on http://127.0.0.1:8000 (recordings → %s)", _REC_DIR)
    # Keepalive matched to dev_server: must exceed the coordinator's
    # 180s tool-result wait + headroom for slow Bedrock turns.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
        ws_ping_interval=120.0,
        ws_ping_timeout=300.0,
    )


if __name__ == "__main__":
    main()
