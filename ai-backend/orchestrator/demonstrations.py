"""
Stage 3C — Demonstration capture & library.

The third leg of the hosted-model quality system. Stages 3A (correction
loop) and 3B (best-of-N + offline scoring) make individual builds better
and measurable. 3C harvests the GOOD ones: any build that passes the
deterministic critic above a quality threshold is captured as a
demonstration — the `(prompt → atomic tool-call sequence → critic
score)` triple — into a growing JSON library.

Why this matters for a HOSTED model (no fine-tuning available): the
library is the achievable substitute for a training set. Its
demonstrations can be retrieved by relevance to a new prompt and
injected as in-context few-shot examples — the one form of "learning
from your own successes" that works against Claude via the API. (The
actual injection is deliberately a SEPARATE, opt-in step — it tensions
with the v20 'no examples in the prompt' directive, so we capture the
asset now and decide how to use it later.)

Capture is gated behind ANIMORA_CAPTURE_DEMOS (default off) so normal
runs and tests never write to disk unless asked.

Schema (one JSON file per demonstration under demonstrations/<slug>.json):
    {
      "prompt": str,
      "intent": str,
      "tool_calls": [ {"name": str, "input": {...}}, ... ],
      "critic_score": float,
      "critic_passed": bool,
      "mesh_count": int,
      "timestamp": str (UTC ISO)
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("animora.demonstrations")

_DEFAULT_ROOT = Path(os.environ.get(
    "ANIMORA_DEMONSTRATIONS_DIR",
    str(Path(__file__).resolve().parent.parent.parent / "demonstrations"),
))

# Only builds at or above this critic score AND passing become demos.
# We want EXEMPLARY builds in the library, not merely acceptable ones.
_CAPTURE_SCORE_THRESHOLD = 0.9


def capture_enabled() -> bool:
    raw = os.environ.get("ANIMORA_CAPTURE_DEMOS", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# Stopwords excluded from relevance matching — common verbs/articles that
# don't discriminate one build from another.
_STOPWORDS = frozenset({
    "a", "an", "the", "me", "my", "build", "make", "create", "model",
    "add", "with", "and", "of", "for", "to", "in", "on", "please",
    "some", "that", "this", "it", "is", "are", "into", "scene",
})


def _tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if w not in _STOPWORDS and len(w) > 1
    }


def _slug(text: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (base[:48] or "demo")


@dataclass
class Demonstration:
    prompt: str
    intent: str
    tool_calls: list[dict] = field(default_factory=list)
    critic_score: float = 0.0
    critic_passed: bool = False
    mesh_count: int = 0
    timestamp: str = ""


class DemonstrationLibrary:
    """JSON-backed library of exemplary builds. Cheap to instantiate;
    loads lazily on first read."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root = root_dir or _DEFAULT_ROOT
        self._cache: list[Demonstration] | None = None

    # ── Capture ────────────────────────────────────────────────────────
    def capture(
        self,
        *,
        prompt: str,
        intent: str,
        tool_calls: list[dict],
        critic_score: float,
        critic_passed: bool,
        mesh_count: int,
    ) -> bool:
        """Store this build as a demonstration IFF it's exemplary
        (passed the critic AND score ≥ threshold AND has real geometry).
        Returns True if captured. No-op (returns False) when capture is
        disabled via env. Never raises into the caller."""
        if not capture_enabled():
            return False
        if not critic_passed or critic_score < _CAPTURE_SCORE_THRESHOLD:
            return False
        if mesh_count < 1 or not tool_calls:
            return False
        demo = Demonstration(
            prompt=prompt.strip()[:500],
            intent=intent,
            tool_calls=tool_calls,
            critic_score=round(float(critic_score), 3),
            critic_passed=bool(critic_passed),
            mesh_count=int(mesh_count),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fname = f"{_slug(prompt)}-{int(time.time())}.json"
            (self.root / fname).write_text(
                json.dumps(asdict(demo), indent=2), encoding="utf-8")
            self._cache = None  # invalidate
            log.info("demonstration.captured prompt=%r score=%.2f meshes=%d",
                     prompt[:60], critic_score, mesh_count)
            return True
        except OSError as exc:
            log.warning("demonstration.capture_failed: %s", exc)
            return False

    # ── Retrieval ──────────────────────────────────────────────────────
    def all(self) -> list[Demonstration]:
        if self._cache is not None:
            return self._cache
        out: list[Demonstration] = []
        if self.root.is_dir():
            for jf in sorted(self.root.glob("*.json")):
                try:
                    data = json.loads(jf.read_text(encoding="utf-8"))
                    out.append(Demonstration(**{
                        k: data.get(k) for k in Demonstration.__dataclass_fields__
                    }))
                except (OSError, ValueError, TypeError) as exc:
                    log.debug("demonstration.load_failed %s: %s", jf.name, exc)
        self._cache = out
        return out

    def retrieve_relevant(self, prompt: str, k: int = 3) -> list[Demonstration]:
        """Return up to k demonstrations most relevant to `prompt`,
        ranked by token overlap (subject nouns / adjectives), then by
        critic score. Pure keyword matching — good enough for v1
        few-shot retrieval; a future version could embed."""
        want = _tokens(prompt)
        demos = self.all()
        if not demos:
            return []

        def _rank(d: Demonstration) -> tuple[int, float]:
            overlap = len(want & _tokens(d.prompt))
            return (overlap, d.critic_score)

        ranked = sorted(demos, key=_rank, reverse=True)
        # Only return demos with at least one overlapping token — an
        # unrelated demo is worse than none for few-shot.
        relevant = [d for d in ranked if want & _tokens(d.prompt)]
        return relevant[:k]

    def stats(self) -> dict:
        demos = self.all()
        if not demos:
            return {"count": 0}
        scores = [d.critic_score for d in demos]
        intents: dict[str, int] = {}
        for d in demos:
            intents[d.intent] = intents.get(d.intent, 0) + 1
        return {
            "count": len(demos),
            "mean_score": round(sum(scores) / len(scores), 3),
            "by_intent": intents,
        }
