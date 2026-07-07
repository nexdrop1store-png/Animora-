"""
Animora Design System — design tokens.

Single source of truth for the visual language. Every ADS widget reads
its colours, spacing, radius, and typography from this module. Changing
a value here changes it everywhere — no per-widget colour literals
scattered through the codebase.

Convention: RGB values are floats in [0..1] for direct use with the
`gpu.shader.from_builtin("UNIFORM_COLOR")` uniform setter. Append the
alpha channel inline at call sites — most primitives accept (R, G, B, A).
"""

from __future__ import annotations

# ── Colour palette ────────────────────────────────────────────────────
# Animora indigo brand — see assets/branding/. Tuned to read well on
# Blender's default dark theme without competing with the 3D viewport.

# Backgrounds (used as base fills behind elevated surfaces).
BG_BASE = (0.04, 0.05, 0.09)       # deepest — outer canvas
BG_ELEVATED = (0.07, 0.09, 0.15)   # cards, message bubbles
BG_RAISED = (0.10, 0.13, 0.21)     # buttons, interactive surfaces

# Accents.
ACCENT_PRIMARY = (0.55, 0.42, 1.00)   # Animora indigo
ACCENT_CYAN = (0.42, 0.78, 1.00)      # tool-execution states
ACCENT_WARM = (1.00, 0.78, 0.30)      # quality-check states
ACCENT_SUCCESS = (0.30, 0.85, 0.55)   # success / complete
ACCENT_DANGER = (1.00, 0.40, 0.40)    # errors

# Text.
TEXT_PRIMARY = (0.95, 0.96, 0.99)
TEXT_MUTED = (0.55, 0.60, 0.70)


# ── Spacing scale ─────────────────────────────────────────────────────
# 4-based scale, like Tailwind. Use the named constants instead of raw
# pixels so the system scales coherently.

SP_1 = 4
SP_2 = 8
SP_3 = 12
SP_4 = 16
SP_6 = 24
SP_8 = 32


# ── Border radius ─────────────────────────────────────────────────────
RADIUS_SM = 4
RADIUS_MD = 8
RADIUS_LG = 12
RADIUS_PILL = 999  # capsule


# ── Typography (pixel sizes at default UI scale) ──────────────────────
# Phase D loads custom TTF via blf.load(). For now these are reference
# sizes that ADS widgets pass to blf.size().

FONT_XS = 10
FONT_SM = 11
FONT_MD = 13
FONT_LG = 16
FONT_XL = 22


# ── Stroke widths ─────────────────────────────────────────────────────
STROKE_THIN = 1.0
STROKE_MED = 2.0
STROKE_THICK = 4.0
