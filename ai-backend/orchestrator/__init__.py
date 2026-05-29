"""
Animora orchestrator package.

Replaces the former single-file `ai-backend/orchestrator.py`. Public surface
(what `main.py` and other callers import) is unchanged: `stream_response`.

Internal structure (Phase 1 of docs/AI_ARCHITECTURE.md):
    streaming.py  — the streaming LLM call + tool-call dispatch (was the body
                    of the old orchestrator.py's `stream_response`)
    router.py     — model selection (Haiku / Sonnet / Opus) by plan + intent
    tools.py      — Anthropic tool definitions (BLENDER_TOOLS)
    personas.py   — persona prompt loader (Phase 4 will populate; Phase 1
                    returns the generalist no-op)
    events.py     — in-process pub/sub bus for orchestrator → quality /
                    memory / telemetry decoupling

Phase 4+ adds: intent.py (Haiku classifier), planner.py (Opus decomposer),
               retry.py (quality-fail retry loop).
"""

from .events import bus
from .streaming import stream_response

__all__ = ["stream_response", "bus"]
