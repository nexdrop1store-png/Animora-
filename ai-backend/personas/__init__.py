"""
Per-persona prompt modules (Environment Artist, Hard Surface Artist, etc.).

Phase 1: empty package marker. Phase 4 populates this with:
    environment_artist.py
    hard_surface_artist.py
    lighting_td.py
    generalist.py
(per docs/AI_ARCHITECTURE.md §13 decision 3 — 3 deep + generalist for alpha)

Until then, `orchestrator.personas.GENERALIST` is the only persona and it
appends nothing to the master prompt.
"""
