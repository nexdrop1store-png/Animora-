"""
Model selection: pick Haiku 4.5 / Sonnet 4.6 / Opus 4.7 for each request.

Routing strategy (docs/AI_ARCHITECTURE.md §5.1, revised 2026-05-21):

- **Execution intents default to Opus across every plan.** Anything the user
  wants *done* in the scene (build / modify / animate / render / etc.) routes
  to Opus 4.7 regardless of plan. The trial plan used to fall back to Sonnet
  here, but Sonnet confused near-homophones in technical vocabulary
  ("cuboid" → "ovoid", "cube" → "sphere") which made the trial feel broken.
  Trial users get the same model power as paid plans for execution — the
  pricing gate can come later via rate limiting or context-budget caps if
  needed. Master prompt cache hit ratio is ~99% on these turns so the
  per-token cost delta from always-Opus is modest.

- **Non-execution intents** (questions, simple_edit, unknown) route by
  message size:
  - Short low-complexity question with small context → Haiku.
  - Everything else → Sonnet.

- **Defensive fast-path for primitives.** If the user's message contains a
  primitive verb + noun pair ("create a cube", "make a cylinder", etc.) and
  the classifier returned a non-execution intent (because Haiku misread it),
  we still force Opus. Stops the cube→sphere class of regression cold.

The `__intent_class` value is stuffed into `scene_graph` by streaming.py from
the Phase 4 intent classifier output, so the router can read it cheaply.
"""

from __future__ import annotations

import logging
import re

from ..scene_intelligence import estimate_task_complexity

log = logging.getLogger("animora.router")

MODEL_HAIKU = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_OPUS = "claude-opus-4-7"

# Non-execution intents — conversational or trivial; safe to route by message
# size. Everything NOT in this set is execution (build/modify/animate/...) and
# gets Opus. Mirrors `_VALID_INTENTS` in orchestrator/intent.py — keep aligned
# when adding new intent IDs there.
_NON_EXECUTION_INTENTS = frozenset({
    "simple_edit",
    "question",
    "unknown",
    "",
})

# Defensive fast-path. The intent classifier is Haiku and occasionally mislabels
# obvious execution requests as `simple_edit` or `unknown` — particularly when
# the user types a tight imperative like "make a car" with no scene context.
# When we detect a creation verb followed by a noun-ish word, force Opus
# regardless of what the classifier returned. Cheap regex check; runs only on
# the latest user message (≤ a few hundred chars).
_CREATION_VERB_PATTERN = re.compile(
    r"\b(create|add|make|build|model|generate|spawn|place|insert|draw|design|"
    r"sculpt|render|animate|rig|light|texture|shade|paint|simulate|set\s+up|"
    r"setup)\b",
    re.IGNORECASE,
)


def _is_obvious_creation_request(user_message: str) -> bool:
    """True if the message looks like a build/modify command. Lightweight
    regex check used to override the classifier when it returns a
    non-execution intent for an obvious create-X request."""
    if not user_message:
        return False
    # Cap the scan to a sensible prefix so a long conversation pasted into
    # the input doesn't trigger on a stray "make" deep in the text.
    head = user_message[:240]
    return bool(_CREATION_VERB_PATTERN.search(head))


def select_model(
    user_message: str,
    conversation_history: list[dict],
    scene_graph: dict,
    plan: str,
) -> tuple[str, str]:
    """Return `(model_id, reason)` — reason is for telemetry/logging only.

    Note: `plan` is accepted for future per-plan limits but does NOT gate
    Opus access today. Trial users get the same execution-quality model as
    paid users; rate limiting (not model downgrade) will enforce trial scope
    when billing ships.
    """
    intent_class = str(scene_graph.get("__intent_class", "")).lower()
    is_execution = intent_class not in _NON_EXECUTION_INTENTS

    # Execution intents → Opus on every plan. The classifier handles most
    # cases; the verb-pattern check below catches its misclassifications.
    if is_execution:
        return MODEL_OPUS, f"execution-default (intent={intent_class}, plan={plan})"

    # Classifier said non-execution. Sanity-check: does the message obviously
    # ask for something to be built? If so, override to Opus. This is the
    # safety net that prevents "create a cube" from routing to Haiku/Sonnet
    # and producing a sphere.
    if _is_obvious_creation_request(user_message):
        return MODEL_OPUS, f"creation-verb-override (intent={intent_class}, plan={plan})"

    # Genuinely non-execution path (questions, simple labels, etc.) →
    # cheap routing by message size.
    total_tokens = sum(len(m.get("content", "")) for m in conversation_history) // 4
    total_tokens += len(user_message) // 4
    complexity = estimate_task_complexity(user_message, scene_graph)

    if total_tokens < 1000 and len(user_message) < 120 and complexity < 0.3:
        return MODEL_HAIKU, f"short-low-complexity-question (plan={plan})"

    return MODEL_SONNET, f"non-execution-default (plan={plan})"
