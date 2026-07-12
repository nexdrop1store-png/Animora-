---
name: blender-python-api
description: Use when writing or reviewing any bpy code — addon handlers, LLM-generated scripts, headless tests, or scripts the backend dispatches. Triggers include "bpy", "Blender API", "operator fails", "context is incorrect", "poll() failed", "run Blender headless", "undo push", "main thread", "mode switch". Covers bpy.data-over-bpy.ops, context requirements, threading, undo, and known pitfalls from Animora's addon.
---

# Blender Python (bpy) patterns for Animora

## Rule 1: `bpy.data` over `bpy.ops`
Operators (`bpy.ops.*`) depend on window/area/mode context and fail silently or raise `RuntimeError: Operator bpy.ops.X.poll() failed, context is incorrect`. Data API is deterministic:
```python
# Bad: context-dependent
bpy.ops.mesh.primitive_cube_add(location=(0,0,0))
# Good: explicit and testable
mesh = bpy.data.meshes.new("Crate")
obj = bpy.data.objects.new("Crate", mesh)
bpy.context.collection.objects.link(obj)
```
When an operator is unavoidable (edit-mode ops, sculpt brushes), switch modes explicitly and restore in `try/finally` — never leave the user in an unexpected mode (see `personas/base.py` shared principles).

## Rule 2: bpy is main-thread-only
Every WS callback / background thread must marshal to the main thread before touching bpy. The addon's pattern (`operators.py:218` `_run_on_main`): queue the callable and drain it from a `bpy.app.timers` timer. Timer interval 0.001 still batches at frame boundaries (`operators.py:953`) — don't expect sub-frame latency. UI refresh after state change: `area.tag_redraw()`.

## Rule 3: one undo entry per user-meaningful action
Blender snapshots at each `bpy.ops.ed.undo_push(message=...)`; a single push before N data-API mutations groups them into ONE Ctrl-Z step. Animora pushes **once per agent iteration**, not per script (`operators.py:587` `_maybe_push_iteration_undo`), labeled from the model's `intent_summary`. If you add a new mutating path, route it through the iteration-undo helper; a second push creates a phantom undo step.

## Rule 4: long scripts must yield and report progress
One big `exec()` freezes the viewport and trips the backend's idle timeout. The addon AST-splits scripts into top-level statements (`operators.py:860-886`), executes them incrementally on the main thread, and sends `tool_progress` pings so `tool_result_coordinator` resets its idle clock (45 s idle / 180 s hard ceiling). Follow this pattern for any new bulk operation.

## Rule 5: headless execution for tests
```bash
blender --background --python path/to/script.py -- --extra-args
# Animora build smoke: Animora.exe --background --version
```
- `bpy.context` is minimal headless: no window, no active area. Anything touching `context.area`/`region`/`space_data` needs a guard or a constructed override: `with bpy.context.temp_override(area=area, region=region): ...`
- Tests that import addon modules directly must only import **bpy-free** modules (`auth/session.py`, `composer_buffer.py` are designed for this). bpy-dependent tests skip when Blender is absent — mirror `ai-backend/tests/test_stage1_harness.py`'s skip markers.

## Known pitfalls (paid for in this repo)
- **EEVEE shader compilation stalls the main thread** on first material view (`operators.py:454` context) — expect the first `viewport_screenshot` after `apply_material` to be slow; don't tighten timeouts around it.
- **Datablock names**: never ship `Cube.001` to the Outliner. Descriptive names are enforced by the eval (`no meaningful .name= assignment` fails the benchmark) and rule #6 of the master prompt.
- **Non-destructive by default**: modifiers configured, not applied; GN for variation; materials via slots; animation in named Actions (`personas/base.py`).
- **LLM script sandbox**: scripts run with `bpy, bmesh, mathutils, math, random` only. Banned imports/builtins per `ai-backend/quality_enforcer.py` (os, subprocess, sys, open, eval, exec, getattr, …). Never widen the sandbox addon-side; the enforcer is the gate and its list is authoritative.
- **`uuid.getnode()` / platform calls** are fine addon-side but belong in bpy-free modules so they stay unit-testable.
