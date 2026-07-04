from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from jose import jwt


REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "auth-server"
os.environ.setdefault("ANIMORA_ENV", "dev")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load_module("auth_server", PACKAGE_ROOT / "__init__.py")
config = _load_module("auth_server.config", PACKAGE_ROOT / "config.py")
tokens = _load_module("auth_server.tokens", PACKAGE_ROOT / "tokens.py")


def test_issue_access_token_includes_backend_claims():
    token, exp = tokens.issue_access_token(
        user_id="user-123",
        email="user@example.com",
        plan="free",
        device_id="device-456",
    )

    payload = jwt.decode(
        token,
        config.settings.jwt_secret,
        algorithms=[config.settings.jwt_algorithm],
        issuer=config.settings.jwt_issuer,
        audience=config.settings.jwt_audience,
    )

    assert payload["sub"] == "user-123"
    assert payload["user_id"] == "user-123"
    assert payload["email"] == "user@example.com"
    assert payload["device_id"] == "device-456"
    assert payload["plan"] == "free"
    assert payload["iss"] == config.settings.jwt_issuer
    assert payload["aud"] == config.settings.jwt_audience
    assert payload["exp"] == exp
