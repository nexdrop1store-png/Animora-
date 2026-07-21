"""
Production-grade Anthropic client wrapper.

Wraps `anthropic.AsyncAnthropic` with the operational concerns a raw SDK
call doesn't give us:

  • Retry with exponential backoff on transient failures
        (APIConnectionError, InternalServerError, APITimeoutError)
  • Honor Anthropic's `retry-after` header on 429s (RateLimitError)
  • Per-request timeout ceiling
  • Cancellation by session_id — the WebSocket `interrupt` message routes
    here to abort an in-flight stream
  • Structured token usage extraction (input, output, cache_create,
    cache_read) → emitted on the event bus and persisted per session
  • Structured error mapping — converts SDK exceptions into typed error
    payloads suitable for the WebSocket ErrorMessage protocol

Hard rules (security):
  • The API key NEVER appears in any log line. We log a fingerprint
    (sha256 prefix) instead.
  • The key is held in-memory for the lifetime of the WS session and
    discarded on disconnect. Not persisted.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import anthropic

from .llm_provider import LLMProvider, client_for, provider_from_env, translate_model
from .observability import logger

log = logger("animora.anthropic")


# ── Retry policy ────────────────────────────────────────────────────────
# Per-request timeout is the WALL CLOCK for ONE stream attempt. It must
# cover the full token-by-token stream of `max_tokens` tokens at Opus's
# ~30 tok/s. The orchestrator now caps max_tokens at 16384 to give complex
# generations (cars, rooms, full scenes) enough budget; at ~30 tok/s that's
# ~540 s worst case. We pick 600 s as the ceiling so the model is never
# choked by the wall clock when it's still actively streaming useful work.
# Typical car-class generations land in 30-80 s, so the ceiling almost never
# matters in practice — it's there for the legitimately heavy turns.
#
# Retries: 3 for rate-limit / transient connection errors (a short backoff
# usually unblocks). Capped at 1 attempt for timeouts — if Anthropic took
# longer than 600 s, repeating the same call is hopeless and burns user
# attention.
_MAX_RETRIES = 3
_MAX_TIMEOUT_RETRIES = 1
_INITIAL_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 8.0
_PER_REQUEST_TIMEOUT_SEC = 600.0

# Rate-limit retries get their own (much longer) schedule. Bedrock TPM
# quotas reset on a rolling minute; a 1s/2s/4s backoff (7s total) is
# guaranteed to fail when the minute window is mid-cycle. Use
# 15/30/60/90 to span ≥3 minutes and reliably catch the next reset.
# Also bump retry count to 4 specifically for rate limits — more
# attempts is cheap when each attempt is just a slept wait.
_RATE_LIMIT_MAX_RETRIES = 4
_RATE_LIMIT_INITIAL_BACKOFF_SEC = 15.0
_RATE_LIMIT_MAX_BACKOFF_SEC = 90.0

# Errors we retry. Anthropic SDK 0.28+ exposes these as a stable hierarchy.
_RETRY_ERRORS: tuple[type[Exception], ...] = tuple(
    getattr(anthropic, name)
    for name in ("APIConnectionError", "APITimeoutError", "InternalServerError")
    if hasattr(anthropic, name)
)

# Errors we never retry — bad key, malformed request, etc.
_TERMINAL_ERRORS: tuple[type[Exception], ...] = tuple(
    getattr(anthropic, name)
    for name in (
        "AuthenticationError",
        "PermissionDeniedError",
        "BadRequestError",
        "NotFoundError",
        "UnprocessableEntityError",
    )
    if hasattr(anthropic, name)
)


# v1.3 — admin usage visibility (deliberately smaller than V2 Phase 6's
# full metering/billing plan; see .claude/skills/animora-metering-billing).
# List prices, $ per MILLION tokens, as of this table's creation — this
# WILL drift when Anthropic changes prices or ships new models; no
# automated staleness detection exists, so treat as needing a periodic
# manual check-in. Cache read/write tokens are priced differently from
# base input tokens (a cache read is materially cheaper) — approximating
# both as base input-token price would meaningfully overcharge a cache-
# heavy turn, so they get their own rates per model family.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {
        "input": 15.0, "output": 75.0,
        "cache_write": 18.75, "cache_read": 1.50,
    },
    "claude-opus-4-6": {
        "input": 15.0, "output": 75.0,
        "cache_write": 18.75, "cache_read": 1.50,
    },
    "claude-sonnet-4-6": {
        "input": 3.0, "output": 15.0,
        "cache_write": 3.75, "cache_read": 0.30,
    },
    "claude-haiku-4-5-20251001": {
        "input": 0.80, "output": 4.0,
        "cache_write": 1.0, "cache_read": 0.08,
    },
}
_FALLBACK_PRICING = {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30}


# ── Result + error types ────────────────────────────────────────────────

@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def cache_hit_ratio(self) -> float:
        total = self.input_tokens + self.cache_read_input_tokens
        return self.cache_read_input_tokens / total if total > 0 else 0.0

    def to_dict(self) -> dict[str, int | float]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_hit_ratio": round(self.cache_hit_ratio, 3),
        }

    def cost_usd(self, model: str) -> float:
        """List-price estimate for this call, in USD. Unrecognized
        models fall back to Sonnet-tier pricing (logged, never raises)
        rather than silently returning 0 — a missing price table entry
        for a new model should show up as a plausible-looking number
        an admin might question, not a suspicious exact zero."""
        rates = MODEL_PRICING.get(model)
        if rates is None:
            log.warning("cost_usd: no pricing entry for model=%r, using fallback rates", model)
            rates = _FALLBACK_PRICING
        return (
            self.input_tokens * rates["input"]
            + self.output_tokens * rates["output"]
            + self.cache_creation_input_tokens * rates["cache_write"]
            + self.cache_read_input_tokens * rates["cache_read"]
        ) / 1_000_000


@dataclass
class StreamResult:
    output_text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    stop_reason: str = ""
    model: str = ""
    elapsed_ms: int = 0
    attempts: int = 1
    cancelled: bool = False

    # Phase 8: full ordered list of assistant content blocks for the
    # agentic multi-step loop. Each entry is the JSON-shaped form of an
    # Anthropic SDK content block:
    #   {"type": "thinking", "thinking": "...", "signature": "..."} OR
    #   {"type": "redacted_thinking", "data": "..."} OR
    #   {"type": "text", "text": "..."} OR
    #   {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
    # Order matters — Anthropic requires the same interleave on the
    # round-trip when this assistant turn is replayed in messages=. With
    # extended thinking enabled, the API REJECTS assistant turns whose
    # tool_use is not preceded by its original thinking block (signature
    # verifies integrity), so thinking blocks must round-trip verbatim.
    assistant_content_blocks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ValidationResult:
    ok: bool
    error_code: str = ""           # "invalid_key" | "rate_limited" | "network" | "permission" | "unknown"
    error_message: str = ""
    model_pinged: str = ""
    elapsed_ms: int = 0


class StreamCancelled(Exception):
    """Raised when a stream was cancelled via cancel(session_id)."""


# ── Helpers ─────────────────────────────────────────────────────────────

def fingerprint_key(api_key: str) -> str:
    """Return a 12-char sha256 prefix for safe logging."""
    if not api_key:
        return "(empty)"
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _classify_error(exc: Exception) -> tuple[str, str]:
    """Map an SDK exception to (error_code, human_message)."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "invalid_key", "Anthropic rejected the API key (authentication failed)."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "permission", "API key lacks permission for this model or feature."
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limited", "Anthropic rate limit hit. Wait and retry."
    if isinstance(exc, anthropic.NotFoundError):
        return "not_found", "Requested model or resource not found."
    if isinstance(exc, anthropic.BadRequestError):
        return "bad_request", f"Malformed request: {exc}"
    if isinstance(exc, _RETRY_ERRORS):
        return "transient", f"Transient Anthropic error: {exc}"
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout", "Request to Anthropic timed out."
    return "unknown", f"Unexpected error: {type(exc).__name__}: {exc}"


# ── The client ──────────────────────────────────────────────────────────

class AnthropicClient:
    """One instance per Animora session. Holds the (BYOK or pooled) key
    in memory for the session lifetime and manages stream cancellation."""

    def __init__(
        self,
        api_key: str,
        *,
        session_id: str = "unknown",
        user_id: str = "unknown",
        timeout_sec: float = _PER_REQUEST_TIMEOUT_SEC,
        max_retries: int = _MAX_RETRIES,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        provider: LLMProvider | None = None,
        aws_region: str = "us-east-1",
    ) -> None:
        # Provider selection: explicit > env var > Anthropic default.
        # In BEDROCK mode the api_key arg is ignored (Bedrock auth is via
        # AWS_BEARER_TOKEN_BEDROCK in the env), so we allow it to be empty.
        if provider is None:
            provider = provider_from_env()
        if provider is LLMProvider.ANTHROPIC and not api_key:
            raise ValueError(
                "AnthropicClient(provider=ANTHROPIC) requires a non-empty "
                "api_key. Set ANIMORA_LLM_PROVIDER=bedrock to use Amazon "
                "Bedrock instead."
            )
        self._provider = provider
        self._key = api_key
        self._session_id = session_id
        self._user_id = user_id  # v1.3 — usage.recorded attribution only
        self._timeout = timeout_sec
        self._max_retries = max_retries
        # We manage retries ourselves so the SDK's own retry doesn't double up
        self._sdk = client_for(provider, api_key=api_key, aws_region=aws_region)
        self._emit = emit
        self._active_task: asyncio.Task | None = None

        log.info(
            "anthropic.client.init",
            extra={
                "session_id": session_id,
                "provider": provider.value,
                "key_fingerprint": fingerprint_key(api_key) if api_key else "bedrock-bearer",
                "timeout_sec": timeout_sec,
                "max_retries": max_retries,
            },
        )

    def _resolve_model(self, logical_name: str) -> str:
        """Translate a logical model name (the IDs router.py returns)
        to the provider-specific ID this client's SDK expects."""
        return translate_model(logical_name, self._provider)

    async def messages_create(self, *, model: str, **kwargs: Any) -> Any:
        """Forward to `self._sdk.messages.create` with the model ID
        translated to the provider-specific form. Use this from any
        orchestrator module that wants a one-shot (non-streamed) call —
        intent.py, quality.py, memory.py — instead of reaching into
        `_sdk.messages.create` directly, which bypasses translation
        and breaks on Bedrock.

        Applies the same rate-limit retry schedule as `stream()` so a
        single Bedrock 429 doesn't silently degrade intent / spec /
        quality / memory calls. Transient connection errors get a
        shorter retry (3× 1s/2s/4s). Terminal errors raise immediately.
        """
        resolved = self._resolve_model(model)
        rate_limit_attempts = 0
        rate_limit_backoff = _RATE_LIMIT_INITIAL_BACKOFF_SEC
        transient_attempts = 0
        transient_backoff = _INITIAL_BACKOFF_SEC
        max_total_attempts = max(self._max_retries, _RATE_LIMIT_MAX_RETRIES) + 1
        last_error: Exception | None = None

        for _ in range(1, max_total_attempts + 1):
            try:
                return await self._sdk.messages.create(model=resolved, **kwargs)
            except _TERMINAL_ERRORS:
                raise
            except anthropic.RateLimitError as exc:
                last_error = exc
                rate_limit_attempts += 1
                retry_after = _extract_retry_after(exc)
                wait = retry_after or rate_limit_backoff
                log.warning(
                    "anthropic.client.messages_create.rate_limited",
                    extra={
                        "session_id": self._session_id,
                        "model": model,
                        "attempt": rate_limit_attempts,
                        "wait_sec": wait,
                        "max_retries": _RATE_LIMIT_MAX_RETRIES,
                    },
                )
                if rate_limit_attempts >= _RATE_LIMIT_MAX_RETRIES:
                    raise
                await asyncio.sleep(wait)
                rate_limit_backoff = min(
                    rate_limit_backoff * 2, _RATE_LIMIT_MAX_BACKOFF_SEC
                )
            except _RETRY_ERRORS as exc:
                last_error = exc
                transient_attempts += 1
                log.warning(
                    "anthropic.client.messages_create.retrying",
                    extra={
                        "session_id": self._session_id,
                        "model": model,
                        "attempt": transient_attempts,
                        "error_type": type(exc).__name__,
                        "wait_sec": transient_backoff,
                    },
                )
                if transient_attempts >= self._max_retries:
                    raise
                await asyncio.sleep(transient_backoff)
                transient_backoff = min(transient_backoff * 2, _MAX_BACKOFF_SEC)

        if last_error:
            raise last_error
        raise RuntimeError(
            "AnthropicClient.messages_create() exhausted retries with no error"
        )

    # ── Public API ──────────────────────────────────────────────────────

    async def validate(self, model: str = "claude-haiku-4-5-20251001") -> ValidationResult:
        """Single-shot sanity check: ask Haiku to produce one token. Cheap
        (well under $0.001). Distinguishes auth errors from rate-limit /
        network problems so the UI can show the right message."""
        resolved_model = self._resolve_model(model)
        started = time.monotonic()
        try:
            await asyncio.wait_for(
                self._sdk.messages.create(
                    model=resolved_model,
                    max_tokens=4,
                    messages=[{"role": "user", "content": "ping"}],
                ),
                timeout=10.0,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            log.info(
                "anthropic.client.validate.ok",
                extra={"session_id": self._session_id, "model": model, "elapsed_ms": elapsed_ms},
            )
            return ValidationResult(ok=True, model_pinged=model, elapsed_ms=elapsed_ms)
        except Exception as exc:
            code, msg = _classify_error(exc)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            log.warning(
                "anthropic.client.validate.failed",
                extra={
                    "session_id": self._session_id,
                    "model": model,
                    "error_code": code,
                    "elapsed_ms": elapsed_ms,
                },
            )
            return ValidationResult(
                ok=False, error_code=code, error_message=msg,
                model_pinged=model, elapsed_ms=elapsed_ms,
            )

    async def stream(
        self,
        *,
        model: str,
        max_tokens: int,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_token: Callable[[str], Awaitable[None]],
        on_tool_call: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None = None,
        on_tool_input_delta: Callable[[int, str], Awaitable[None]] | None = None,
        thinking: dict[str, Any] | None = None,
        output_config: dict[str, Any] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> StreamResult:
        """Stream a completion with retry, timeout, cancellation, and
        usage tracking. Tokens are dispatched via `on_token` as they arrive.
        Tool calls are dispatched after the full message is collected.

        `thinking` (Claude 4.x extended thinking): on Opus 4.7 / Sonnet 4.6
        the supported shape is `{"type": "adaptive"}`, paired with
        `output_config={"effort": "low|medium|high"}` to control how hard
        the model thinks. (The older `{"type": "enabled", "budget_tokens": N}`
        shape from Opus 4.0 / Sonnet 3.7 is rejected by Opus 4.7 with
        `"thinking.type.enabled is not supported for this model"`.)
        None on models that don't support thinking.

        Raises StreamCancelled if cancel() was called during the stream.
        On terminal errors, raises a typed exception the caller maps to
        a WS ErrorMessage."""

        started = time.monotonic()
        last_error: Exception | None = None
        backoff = _INITIAL_BACKOFF_SEC
        # Rate-limit retries are tracked separately so they don't share
        # the generic transient-error budget. Bedrock TPM windows reset
        # on a rolling 60s, so we want to span at least one full reset.
        rate_limit_attempts = 0
        rate_limit_backoff = _RATE_LIMIT_INITIAL_BACKOFF_SEC

        max_total_attempts = max(self._max_retries, _RATE_LIMIT_MAX_RETRIES) + 1
        for attempt in range(1, max_total_attempts + 1):
            try:
                # Wrap the streamed call in a Task so cancel() can abort it
                self._active_task = asyncio.current_task()
                result = await asyncio.wait_for(
                    self._run_stream(model, max_tokens, system, messages, tools, on_token, on_tool_call, thinking, output_config, tool_choice, on_tool_input_delta),
                    timeout=self._timeout,
                )
                self._active_task = None
                result.attempts = attempt
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
                result.model = model

                await self._emit_safe("usage.recorded", {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "model": model,
                    "usage": result.usage.to_dict(),
                    "cost_usd": result.usage.cost_usd(model),
                    "elapsed_ms": result.elapsed_ms,
                    "attempts": attempt,
                })
                log.info(
                    "anthropic.client.stream.completed",
                    extra={
                        "session_id": self._session_id,
                        "model": model,
                        "input_tokens": result.usage.input_tokens,
                        "output_tokens": result.usage.output_tokens,
                        "cache_hit_ratio": round(result.usage.cache_hit_ratio, 3),
                        "elapsed_ms": result.elapsed_ms,
                        "attempts": attempt,
                    },
                )
                return result

            except asyncio.CancelledError:
                # Cancellation propagated up from .cancel() — convert to typed
                log.info("anthropic.client.stream.cancelled", extra={"session_id": self._session_id})
                self._active_task = None
                raise StreamCancelled("Stream cancelled by user interrupt")

            except _TERMINAL_ERRORS as exc:
                # Bad key / bad request — surface immediately, no retry
                code, msg = _classify_error(exc)
                log.error(
                    "anthropic.client.stream.terminal",
                    extra={"session_id": self._session_id, "error_code": code},
                )
                self._active_task = None
                raise

            except anthropic.RateLimitError as exc:
                last_error = exc
                rate_limit_attempts += 1
                retry_after = _extract_retry_after(exc)
                # Honour server-provided Retry-After when present;
                # otherwise use the longer rate-limit backoff schedule
                # (separate from the transient backoff so a single 429
                # doesn't burn the entire retry budget on 7 seconds).
                wait = retry_after or rate_limit_backoff
                log.warning(
                    "anthropic.client.stream.rate_limited",
                    extra={
                        "session_id": self._session_id,
                        "attempt": rate_limit_attempts,
                        "wait_sec": wait,
                        "max_retries": _RATE_LIMIT_MAX_RETRIES,
                    },
                )
                if rate_limit_attempts >= _RATE_LIMIT_MAX_RETRIES:
                    raise
                await asyncio.sleep(wait)
                rate_limit_backoff = min(
                    rate_limit_backoff * 2, _RATE_LIMIT_MAX_BACKOFF_SEC
                )

            except _RETRY_ERRORS as exc:
                last_error = exc
                code, _ = _classify_error(exc)
                log.warning(
                    "anthropic.client.stream.retrying",
                    extra={
                        "session_id": self._session_id, "attempt": attempt,
                        "error_code": code, "wait_sec": backoff,
                    },
                )
                if attempt == self._max_retries:
                    raise
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SEC)

            except asyncio.TimeoutError as exc:
                last_error = exc
                log.warning(
                    "anthropic.client.stream.timeout",
                    extra={"session_id": self._session_id, "attempt": attempt,
                           "timeout_sec": self._timeout,
                           "timeout_retry_cap": _MAX_TIMEOUT_RETRIES},
                )
                # Bail early on timeouts — repeating the same long-running
                # call rarely succeeds and burns user attention. Surface the
                # error so the user can retry on their own.
                if attempt >= _MAX_TIMEOUT_RETRIES:
                    raise
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SEC)

        # Unreachable in practice (every branch raises) but kept for typing
        if last_error:
            raise last_error
        raise RuntimeError("AnthropicClient.stream() exhausted retries with no error")

    def cancel(self) -> bool:
        """Abort the in-flight stream, if any. Returns True if a stream
        was active and got cancelled. Called by main.py when the WS
        receives an `interrupt` message from the addon."""
        task = self._active_task
        if task is None or task.done():
            return False
        task.cancel()
        log.info("anthropic.client.cancel.issued", extra={"session_id": self._session_id})
        return True

    # ── Internals ───────────────────────────────────────────────────────

    async def _run_stream(
        self,
        model: str,
        max_tokens: int,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_token: Callable[[str], Awaitable[None]],
        on_tool_call: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None,
        thinking: dict[str, Any] | None = None,
        output_config: dict[str, Any] | None = None,
        tool_choice: dict[str, Any] | None = None,
        on_tool_input_delta: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> StreamResult:
        result = StreamResult(output_text="")

        # Build the kwargs dict so we only pass `thinking` / `output_config`
        # when the caller explicitly enabled them. Anthropic's SDK rejects
        # None on these parameters; the omit-by-default pattern keeps
        # non-thinking models unchanged.
        # Model ID is translated to the provider-specific form here so
        # callers (router, eval harness, etc.) keep using logical names.
        stream_kwargs: dict[str, Any] = {
            "model": self._resolve_model(model),
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
        }
        if thinking is not None:
            stream_kwargs["thinking"] = thinking
        if output_config is not None:
            stream_kwargs["output_config"] = output_config
        # `tool_choice` lets callers FORCE the model to call a specific
        # tool — used by the script-rescue path in streaming.py when an
        # execution-intent iteration returned text-only (no tool_use).
        # Shape: {"type": "tool", "name": "execute_blender_script"} OR
        # {"type": "any"} (any tool) / {"type": "auto"} (default model
        # choice — equivalent to omitting the param).
        if tool_choice is not None:
            stream_kwargs["tool_choice"] = tool_choice

        async with self._sdk.messages.stream(**stream_kwargs) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta is not None and hasattr(delta, "text") and delta.text is not None:
                        token = delta.text
                        result.output_text += token
                        await on_token(token)
                    # Sprint 4E — `input_json_delta` carries the
                    # tool_use's JSON input as it's typed by the model.
                    # Forced-tool-choice turns emit zero text tokens, so
                    # without this branch the panel sits silent for the
                    # whole 10-30s SDK call. We surface each partial
                    # chunk via on_tool_input_delta(block_index, chunk)
                    # so streaming.py can flip the panel into a
                    # "composing the next step…" state the moment the
                    # model starts typing the call. Non-fatal if absent:
                    # older SDK versions / non-tool turns just don't hit
                    # this path.
                    elif (
                        delta is not None
                        and on_tool_input_delta is not None
                        and hasattr(delta, "partial_json")
                        and delta.partial_json
                    ):
                        block_index = getattr(event, "index", 0) or 0
                        try:
                            await on_tool_input_delta(block_index, delta.partial_json)
                        except Exception as exc:
                            log.debug("on_tool_input_delta callback raised: %s", exc)

            final_msg = await stream.get_final_message()

        result.stop_reason = getattr(final_msg, "stop_reason", "") or ""

        # Extract usage. SDK shapes: final_msg.usage is a Usage object.
        u = getattr(final_msg, "usage", None)
        if u is not None:
            result.usage.input_tokens = getattr(u, "input_tokens", 0) or 0
            result.usage.output_tokens = getattr(u, "output_tokens", 0) or 0
            result.usage.cache_creation_input_tokens = getattr(u, "cache_creation_input_tokens", 0) or 0
            result.usage.cache_read_input_tokens = getattr(u, "cache_read_input_tokens", 0) or 0

        # Serialise the full assistant content block list (Phase 8 loop).
        # The SDK returns typed objects (TextBlock, ToolUseBlock); we
        # convert each to its JSON-shaped dict so the agentic loop can
        # replay this assistant turn back into a follow-up messages.create
        # call. Order MUST be preserved — Anthropic enforces same-shape
        # round-trip when this assistant turn appears in messages=.
        for block in final_msg.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                result.assistant_content_blocks.append({
                    "type": "text",
                    "text": getattr(block, "text", ""),
                })
            elif btype == "tool_use":
                result.assistant_content_blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input),
                })
            elif btype == "thinking":
                # Must round-trip verbatim (signature included) when this
                # assistant turn is replayed alongside a tool_result.
                result.assistant_content_blocks.append({
                    "type": "thinking",
                    "thinking": getattr(block, "thinking", ""),
                    "signature": getattr(block, "signature", ""),
                })
            elif btype == "redacted_thinking":
                result.assistant_content_blocks.append({
                    "type": "redacted_thinking",
                    "data": getattr(block, "data", ""),
                })

        # Collect tool calls
        for block in final_msg.content:
            if block.type == "tool_use":
                tc = {"name": block.name, "id": block.id, "input": dict(block.input)}
                result.tool_calls.append(tc)
                if on_tool_call is not None:
                    await on_tool_call(block.name, block.id, tc["input"])

        return result

    async def _emit_safe(self, event: str, payload: dict[str, Any]) -> None:
        if self._emit is None:
            return
        try:
            await self._emit(event, payload)
        except Exception as exc:
            log.debug("event emit %s failed: %s", event, exc)


def _extract_retry_after(exc: Exception) -> float | None:
    """Pull retry-after seconds from an Anthropic RateLimitError, if present."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
