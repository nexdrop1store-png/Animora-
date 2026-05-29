"""
Animora Design System (ADS).

Phase A — establishes the token + primitive + canvas layering so future
phases can plug widgets on top without changing the architecture. Currently
ships exactly one ADS-rendered element (the status accent strip) to prove
the pipeline end-to-end; Phases B–D add full widget set (cards, buttons,
inputs, custom typography).

Layering rationale (docs/AI_ARCHITECTURE.md §16 — incoming):
    bpy.types.Panel  →  draws standard layout content (text, prop fields)
    ADS canvas       →  POST_PIXEL handler — draws chrome ON TOP via GPU
    border_glow      →  POST_PIXEL handler — draws active-state rim

ADS owns one POST_PIXEL handler on SpaceAnimora.WINDOW; border_glow keeps
its own so they remain independently togglable.
"""

from . import canvas


def register() -> None:
    canvas.register()


def unregister() -> None:
    canvas.unregister()
