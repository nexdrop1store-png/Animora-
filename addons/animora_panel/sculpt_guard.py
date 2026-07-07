"""Sculpting workspace guard — the tab must always be usable.

Vanilla Blender silently falls back to Object Mode when the Sculpting
workspace is activated with no sculptable mesh (e.g. the user or an AI
command deleted the default Cube), leaving the tool shelf without sculpt
brushes — which reads as "sculpting is broken". This guard makes the tab
behave like Blender's own Sculpting new-file template instead:

  1. active object is a visible mesh → enter Sculpt Mode on it
  2. any visible mesh exists → make the first one active, enter Sculpt Mode
  3. empty scene → create a smooth quad sphere at the origin (the sculpting
     template look) and enter Sculpt Mode on it

Implementation notes:
- Workspace changes are observed via bpy.msgbus on (Window, "workspace");
  subscriptions die on file load, so a persistent load_post handler
  re-subscribes (and re-checks, since a file can open straight into the
  Sculpting tab).
- msgbus notifications run with restricted write access — the real work is
  deferred to a one-shot bpy timer on the main thread.
- Every branch is exception-guarded: the guard must never break workspace
  switching, whatever state the scene is in.
"""

from __future__ import annotations

import contextlib
import logging

log = logging.getLogger("animora.sculpt_guard")

_WORKSPACE_NAME = "Sculpting"
_BASEMESH_NAME = "SculptSphere"

_owner = object()  # msgbus subscription owner token
_load_post_registered = False


# ---------------------------------------------------------------------------
# Core behavior
# ---------------------------------------------------------------------------

def _is_sculptable(obj) -> bool:
    return (
        obj is not None
        and obj.type == "MESH"
        and obj.visible_get()
        and not obj.library  # never mode-switch linked data
    )


def _first_visible_mesh(context):
    for obj in context.view_layer.objects:
        if _is_sculptable(obj):
            return obj
    return None


def _create_base_mesh(context):
    """A smooth, subdivided sphere at the origin — the same starting point
    Blender's own 'Sculpting' new-file template provides."""
    import bpy

    bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0.0, 0.0, 0.0))
    obj = context.view_layer.objects.active
    obj.name = _BASEMESH_NAME
    obj.data.name = _BASEMESH_NAME

    mod = obj.modifiers.new(name="Subdivision", type="SUBSURF")
    mod.levels = 4
    mod.render_levels = 4
    bpy.ops.object.modifier_apply(modifier=mod.name)

    # Round the subdivided cube into a sphere.
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.transform.tosphere(value=1.0)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.shade_smooth()
    log.info("Sculpting: created %s base mesh (empty scene)", _BASEMESH_NAME)
    return obj


def _ensure_sculpt_ready() -> None:
    """Main-thread timer body: make the Sculpting workspace functional."""
    import bpy

    context = bpy.context
    window = context.window
    if window is None or window.workspace is None:
        return
    if window.workspace.name.split(".")[0] != _WORKSPACE_NAME:
        return

    obj = context.view_layer.objects.active
    if not _is_sculptable(obj):
        obj = _first_visible_mesh(context)
        if obj is None:
            obj = _create_base_mesh(context)
        # Make it the active, selected object so mode_set targets it.
        context.view_layer.objects.active = obj
        for other in context.selected_objects:
            other.select_set(False)
        obj.select_set(True)

    if obj.mode != "SCULPT":
        bpy.ops.object.mode_set(mode="SCULPT")
        log.info("Sculpting: entered Sculpt Mode on %s", obj.name)


def _deferred_check() -> None:
    import bpy

    def _once():
        try:
            _ensure_sculpt_ready()
        except Exception as exc:
            # Never break workspace switching — log and move on.
            log.warning("Sculpt guard skipped: %s", exc)
        return None

    bpy.app.timers.register(_once, first_interval=0.0)


def _on_workspace_changed(*_args) -> None:
    # msgbus notify context is write-restricted; defer to a timer.
    _deferred_check()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _subscribe() -> None:
    import bpy

    bpy.msgbus.clear_by_owner(_owner)
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Window, "workspace"),
        owner=_owner,
        args=(),
        notify=_on_workspace_changed,
    )


def register() -> None:
    import bpy
    from bpy.app.handlers import persistent

    if bpy.app.background:
        return

    global _load_post_registered, _load_post_handler
    if "_load_post_handler" not in globals():
        @persistent
        def _load_post_handler(_dummy):
            # msgbus subscriptions do not survive a file load.
            _subscribe()
            # The file may open directly into the Sculpting tab.
            _deferred_check()

        globals()["_load_post_handler"] = _load_post_handler

    if not _load_post_registered:
        bpy.app.handlers.load_post.append(globals()["_load_post_handler"])
        _load_post_registered = True

    _subscribe()
    _deferred_check()


def unregister() -> None:
    import bpy

    global _load_post_registered
    handler = globals().get("_load_post_handler")
    if _load_post_registered and handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(handler)
    _load_post_registered = False
    with contextlib.suppress(Exception):
        bpy.msgbus.clear_by_owner(_owner)
