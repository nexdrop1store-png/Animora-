"""
HTTP client for the backend's POST /validate-key endpoint.

The Settings UI calls this when the user clicks "Test Connection". We
do it from a background thread so Blender's main UI thread doesn't
stall on the network round-trip.

Result is delivered back to the operator via a callable that gets
scheduled on the main thread via bpy.app.timers.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger("animora.api_validator")


@dataclass
class ValidationResult:
    ok: bool
    error_code: str = ""
    error_message: str = ""
    model_pinged: str = ""
    elapsed_ms: int = 0


def validate_async(
    *,
    backend_url: str,
    api_key: str,
    on_result: Callable[[ValidationResult], None],
    timeout_sec: float = 20.0,
) -> None:
    """Fire-and-forget validation. `on_result` is called on Blender's main
    thread when the response (or error) is available.

    `backend_url` is the HTTP origin of the backend (e.g.
    "https://api.animora.tech"). We append "/validate-key" to it.
    """
    def _worker() -> None:
        result = _do_validate(backend_url, api_key, timeout_sec)
        _schedule_on_main_thread(on_result, result)

    threading.Thread(target=_worker, daemon=True, name="animora-validate-key").start()


def _do_validate(backend_url: str, api_key: str, timeout_sec: float) -> ValidationResult:
    if not api_key.strip():
        return ValidationResult(ok=False, error_code="empty", error_message="No key entered.")

    url = backend_url.rstrip("/") + "/validate-key"
    body = json.dumps({"api_key": api_key.strip()}).encode("utf-8")

    # Try httpx first (preferred), then fall back to urllib so we don't
    # take a hard runtime dep beyond the Python stdlib.
    try:
        import httpx
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(url, content=body, headers={"Content-Type": "application/json"})
            resp_data = resp.json()
            return _parse_response(resp.status_code, resp_data)
    except ImportError:
        pass
    except Exception as exc:
        log.warning("httpx validate failed: %s — trying urllib", exc)

    try:
        from urllib import request as urllib_request
        from urllib import error as urllib_error
        req = urllib_request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=timeout_sec) as resp:
            payload = resp.read().decode("utf-8")
            try:
                resp_data = json.loads(payload)
            except json.JSONDecodeError:
                return ValidationResult(ok=False, error_code="bad_response",
                                        error_message="Backend returned non-JSON.")
            return _parse_response(resp.status, resp_data)
    except urllib_error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8")
            resp_data = json.loads(body_text)
            return _parse_response(exc.code, resp_data)
        except Exception:
            return ValidationResult(ok=False, error_code="http_error",
                                    error_message=f"HTTP {exc.code}: {body_text or exc.reason}")
    except Exception as exc:
        return ValidationResult(ok=False, error_code="network",
                                error_message=f"Network error: {exc}")


def _parse_response(status: int, data: dict) -> ValidationResult:
    return ValidationResult(
        ok=bool(data.get("ok", False)) and 200 <= status < 300,
        error_code=str(data.get("error_code", "")),
        error_message=str(data.get("error_message", "")),
        model_pinged=str(data.get("model_pinged", "")),
        elapsed_ms=int(data.get("elapsed_ms", 0)),
    )


def _schedule_on_main_thread(cb: Callable[[ValidationResult], None], result: ValidationResult) -> None:
    """Hop back to Blender's main thread before invoking the callback."""
    import bpy

    def _call() -> Optional[float]:
        try:
            cb(result)
        except Exception as exc:
            log.error("validate result callback failed: %s", exc)
        return None  # one-shot

    bpy.app.timers.register(_call, first_interval=0.0)
