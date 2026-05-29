"""
Session recorder — Quality Plan §6.2 (practical reframe).

Captures every WS turn as a structured JSON record plus the HD viewport
PNG from each agentic-loop iteration. Records are written to
`recordings/<session_id>/turn_<N>.json` and the PNGs alongside.
Activated only when `ANIMORA_RECORD_SESSIONS=1` (or a truthy value) —
zero overhead in production.

## What gets captured

Per-turn:
  • the user's literal message
  • the intent classifier output (intent, persona, confidence, routing reason)
  • the SPEC (creative brief from Sprint 1A)
  • per-iteration: scene_graph_before, scripts emitted by the LLM,
    tool_use names, tool_results, HD-capture filename, artist's-eye verdict
  • the final_review verdict (Sprint 1B)
  • outcome ("success" | "retry_exhausted" | "error" | "cancelled")

## Why explicit method calls instead of bus subscription

The events bus emits async events in arbitrary order; the recorder
needs a deterministic, in-order representation of the turn for later
mining. Calling `recorder.start_turn() / set_spec() / begin_iteration() /
finalize_turn(outcome)` from main.py keeps the recording shape locked
and makes the recorder trivially testable (mock it, assert call order).

## Downstream consumers

  • `scripts/recordings_to_benchmarks.py` — turns recordings into draft
    eval `Benchmark` entries (filling required_ops from observed
    scripts, expected_intent from the captured intent, etc.).
  • `scripts/recordings_to_few_shot.py` — extracts (spec → script →
    verdict) triples for inclusion in persona prompts as worked
    examples. The MCP literature emphasises this is how a model
    actually learns "the loop" — by seeing it.
  • Manual inspection — `cofounder reviews recordings/<session_id>/`
    to spot model failure modes the eval doesn't catch yet.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("animora.recorder")

# Cross-platform default recordings root. Override via
# ANIMORA_RECORDINGS_DIR. On dev boxes this defaults to a sibling of
# the backend (visible alongside the source); on Fargate it would
# default to `/var/lib/animora/recordings/` — but Fargate must NOT
# enable recording (PII risk), so this default is effectively dev-only.
_DEFAULT_RECORDINGS_ROOT = Path(os.environ.get(
    "ANIMORA_RECORDINGS_DIR",
    str(Path(__file__).resolve().parent.parent / "recordings"),
))


def recording_enabled() -> bool:
    """True iff ANIMORA_RECORD_SESSIONS is set to a truthy value. The
    flag is read each call so toggling it doesn't require a restart in
    dev (handy for ad-hoc capture sessions)."""
    raw = os.environ.get("ANIMORA_RECORD_SESSIONS", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _safe_session_id(session_id: str) -> str:
    """Strip characters that would be problematic in a filesystem path.
    Session IDs are usually UUIDs but we don't trust them blindly —
    a malformed session_id from a future client should never be able
    to write outside the recordings root."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
    return cleaned[:80] or "session_unknown"


def _utc_iso() -> str:
    """UTC timestamp in ISO 8601 with milliseconds. Used so recordings
    sort chronologically when inspected as a flat list."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class IterationRecord:
    """One trip through the agentic loop. Multiple per turn for hero
    assets that need 2-3 iterations."""
    iteration_index: int
    scene_graph_before: dict | None = None
    scripts_emitted: list[str] = field(default_factory=list)
    tool_use_names: list[str] = field(default_factory=list)
    tool_inputs: list[dict] = field(default_factory=list)
    """Sprint 4D — MCP-style atomic tool inputs captured per call. Each
    entry is `{"name": str, "input": dict}` where input has been size-
    capped and sensitive fields stripped. Lets recordings_to_few_shot.py
    learn from atomic-tool sequences the same way it learns from scripts."""
    tool_results: list[dict] = field(default_factory=list)
    hd_capture_filename: str | None = None
    artists_eye_verdict: dict | None = None
    duration_ms: int = 0
    notes: str = ""


@dataclass
class TurnRecord:
    """One user → assistant turn within a session."""
    turn_index: int
    user_message: str
    started_at: str = ""
    finished_at: str = ""
    intent: str = ""
    persona: str = ""
    model: str = ""
    routing_reason: str = ""
    spec: dict | None = None
    iterations: list[IterationRecord] = field(default_factory=list)
    final_review: dict | None = None
    outcome: str = ""              # "success" | "retry_exhausted" | "error" | "cancelled"
    error_message: str = ""        # populated when outcome == "error"
    script_rescue_triggered: bool = False  # Sprint 3 follow-up signal


class SessionRecorder:
    """One recorder per WS session. Holds the in-flight TurnRecord
    until `finalize_turn` writes it to disk + clears state.

    Safe to instantiate even when recording is disabled — all methods
    short-circuit. Callers should NOT branch on `recording_enabled()`
    themselves; let the recorder skip cheaply.

    Not thread-safe (and doesn't need to be — Animora's WS handler is
    single-coroutine per session)."""

    def __init__(
        self,
        session_id: str,
        *,
        root_dir: Path | None = None,
    ) -> None:
        self.session_id = session_id
        self.root = root_dir or _DEFAULT_RECORDINGS_ROOT
        self.session_dir = self.root / _safe_session_id(session_id)
        self.turn_count = 0
        self._current_turn: TurnRecord | None = None
        self._current_iter: IterationRecord | None = None
        self._iter_started_monotonic: float = 0.0
        self._enabled = recording_enabled()
        if self._enabled:
            try:
                self.session_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning(
                    "recorder.mkdir_failed session=%s dir=%s exc=%s",
                    session_id, self.session_dir, exc,
                )
                self._enabled = False
        # Resume counter from existing files so a WS reconnect (which
        # creates a fresh recorder for the same session_id) appends
        # rather than overwriting turn_000.json. dev_server always
        # stubs the session as "dev-user", so EVERY reconnect would
        # collide without this — that's the bug behind "I ran 2 prompts
        # but only see 1 recording."
        if self._enabled and self.session_dir.is_dir():
            existing_indices: list[int] = []
            for jf in self.session_dir.glob("turn_*.json"):
                stem = jf.stem  # e.g. "turn_007"
                try:
                    existing_indices.append(int(stem.split("_", 1)[1]))
                except (ValueError, IndexError):
                    continue
            if existing_indices:
                self.turn_count = max(existing_indices) + 1
                log.info(
                    "recorder.resumed session=%s next_turn=%d (found %d prior turn files)",
                    session_id, self.turn_count, len(existing_indices),
                )

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── Turn lifecycle ─────────────────────────────────────────────────

    def start_turn(self, user_message: str) -> None:
        if not self._enabled:
            return
        # If a previous turn was never finalized (crash mid-turn), drop
        # it silently — the captured fragment isn't safe to use anyway.
        if self._current_turn is not None:
            log.warning(
                "recorder.start_turn: previous turn (idx=%d) was not finalized — discarding",
                self._current_turn.turn_index,
            )
        self._current_turn = TurnRecord(
            turn_index=self.turn_count,
            user_message=user_message[:4000],  # cap at 4k to bound disk usage
            started_at=_utc_iso(),
        )
        self.turn_count += 1

    def set_intent(self, *, intent: str, persona: str, model: str, routing_reason: str = "") -> None:
        if not self._enabled or self._current_turn is None:
            return
        self._current_turn.intent = intent
        self._current_turn.persona = persona
        self._current_turn.model = model
        self._current_turn.routing_reason = routing_reason[:240]

    def set_spec(self, spec_data: dict | None) -> None:
        if not self._enabled or self._current_turn is None or spec_data is None:
            return
        # We keep the full spec — it's bounded (~1.5 KB max per the
        # SPEC_SCHEMA_DOC limits in spec_builder.py) and the field is
        # the most valuable signal for few-shot extraction.
        self._current_turn.spec = dict(spec_data)

    # ── Iteration lifecycle ─────────────────────────────────────────────

    def begin_iteration(self, *, scene_graph_before: dict | None = None) -> None:
        if not self._enabled or self._current_turn is None:
            return
        idx = len(self._current_turn.iterations)
        self._current_iter = IterationRecord(
            iteration_index=idx,
            scene_graph_before=dict(scene_graph_before) if isinstance(scene_graph_before, dict) else None,
        )
        self._iter_started_monotonic = time.monotonic()

    def add_script(self, script: str) -> None:
        if not self._enabled or self._current_iter is None:
            return
        # Scripts can be 10-20 KB on hero assets. We keep the full
        # body — that's the primary signal for benchmark mining.
        self._current_iter.scripts_emitted.append(script)

    def add_tool_use(self, name: str) -> None:
        if not self._enabled or self._current_iter is None:
            return
        self._current_iter.tool_use_names.append(name)

    def add_tool_input(self, name: str, tool_input: dict) -> None:
        """Sprint 4D — capture the full typed input of an atomic tool
        call. Lets recordings_to_few_shot.py extract (intent → tool
        sequence) triples for persona few-shot blocks. Skips the
        execute_animora_code body — that's already captured via
        `add_script()` to avoid duplication.

        Sanitization: drop the `script` field (already captured), cap
        string fields at 2 KB, and serialise to a plain dict so the
        JSON dump round-trips safely."""
        if not self._enabled or self._current_iter is None:
            return
        cleaned: dict[str, Any] = {}
        for k, v in (tool_input or {}).items():
            if k == "script":
                continue  # captured separately via add_script
            if isinstance(v, str):
                cleaned[k] = v[:2000]
            elif isinstance(v, (int, float, bool)) or v is None:
                cleaned[k] = v
            elif isinstance(v, (list, tuple)):
                # Keep number/string lists (locations, palettes, names)
                # as-is; cap length at 64 to bound payload size.
                cleaned[k] = list(v)[:64]
            elif isinstance(v, dict):
                # One-level dict copy (modifier params etc.). Don't
                # recurse — addon-side schemas are flat.
                cleaned[k] = {
                    sk: (sv[:500] if isinstance(sv, str) else sv)
                    for sk, sv in v.items()
                }
        self._current_iter.tool_inputs.append({"name": name, "input": cleaned})

    def add_tool_result(self, result: dict) -> None:
        if not self._enabled or self._current_iter is None:
            return
        # Cap tool_result output text — addon stdout can be verbose on
        # successful builds. Errors stay short by nature.
        cleaned: dict[str, Any] = {}
        for k, v in result.items():
            if isinstance(v, str):
                cleaned[k] = v[:2000]
            elif isinstance(v, (int, float, bool)) or v is None:
                cleaned[k] = v
        self._current_iter.tool_results.append(cleaned)

    def set_artists_eye(self, verdict: dict | None) -> None:
        if not self._enabled or self._current_iter is None or verdict is None:
            return
        self._current_iter.artists_eye_verdict = dict(verdict)

    def write_hd_capture(self, png_bytes: bytes) -> None:
        """Save the HD capture PNG alongside the JSON. Called once per
        iteration AFTER the addon's HD capture lands. Filename stored
        on the IterationRecord so consumers can find the binary."""
        if not self._enabled or self._current_iter is None:
            return
        turn_idx = self._current_turn.turn_index if self._current_turn else -1
        filename = f"turn_{turn_idx}_iter_{self._current_iter.iteration_index}.png"
        path = self.session_dir / filename
        try:
            path.write_bytes(png_bytes)
            self._current_iter.hd_capture_filename = filename
        except OSError as exc:
            log.warning(
                "recorder.hd_capture_write_failed session=%s path=%s exc=%s",
                self.session_id, path, exc,
            )

    def end_iteration(self, notes: str = "") -> None:
        if not self._enabled or self._current_iter is None or self._current_turn is None:
            return
        self._current_iter.duration_ms = int(
            (time.monotonic() - self._iter_started_monotonic) * 1000
        )
        if notes:
            self._current_iter.notes = notes[:300]
        self._current_turn.iterations.append(self._current_iter)
        self._current_iter = None

    # ── Final review + outcome ─────────────────────────────────────────

    def set_final_review(self, verdict: dict | None) -> None:
        if not self._enabled or self._current_turn is None or verdict is None:
            return
        self._current_turn.final_review = dict(verdict)

    def mark_script_rescue(self) -> None:
        if not self._enabled or self._current_turn is None:
            return
        self._current_turn.script_rescue_triggered = True

    def finalize_turn(self, outcome: str, *, error_message: str = "") -> Path | None:
        """Write the in-flight TurnRecord to disk. Returns the JSON
        path on success, None when recording is disabled or no turn
        is in progress."""
        if not self._enabled or self._current_turn is None:
            return None
        # If we got here mid-iteration (crash, cancel), close it first
        # so we don't lose its partial data.
        if self._current_iter is not None:
            self.end_iteration(notes="(implicit close at turn finalize)")
        self._current_turn.finished_at = _utc_iso()
        self._current_turn.outcome = outcome
        self._current_turn.error_message = error_message[:600]
        out_path = self.session_dir / f"turn_{self._current_turn.turn_index:03d}.json"
        try:
            payload = asdict(self._current_turn)
            out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            log.info(
                "recorder.turn_finalized session=%s turn=%d outcome=%s path=%s",
                self.session_id, self._current_turn.turn_index, outcome, out_path,
            )
        except OSError as exc:
            log.warning(
                "recorder.write_failed session=%s path=%s exc=%s",
                self.session_id, out_path, exc,
            )
            out_path = None
        self._current_turn = None
        return out_path
