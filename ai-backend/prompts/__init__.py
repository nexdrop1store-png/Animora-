"""
Prompt modules for the Animora AI backend.

Each prompt is its own module so we can version, cache, and A/B-test them
independently of the orchestrator code. See docs/AI_ARCHITECTURE.md §7.
"""

from .master_prompt import MASTER_PROMPT, MASTER_PROMPT_VERSION

__all__ = ["MASTER_PROMPT", "MASTER_PROMPT_VERSION"]
