"""AI suggestion panels injected into Dope Sheet, Graph Editor, Compositor."""

from __future__ import annotations

import bpy
from bpy.types import Panel


class PT_AnimoraDopeSheetHints(Panel):
    bl_label = "AI Suggestions"
    bl_idname = "DOPESHEET_PT_animora"
    bl_space_type = "DOPESHEET_EDITOR"
    bl_region_type = "UI"
    bl_category = "Animora"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        self.layout.label(text="Animation AI suggestions", icon="INFO")


class PT_AnimoraGraphEditorHints(Panel):
    bl_label = "AI Suggestions"
    bl_idname = "GRAPH_PT_animora"
    bl_space_type = "GRAPH_EDITOR"
    bl_region_type = "UI"
    bl_category = "Animora"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        self.layout.label(text="Curve AI suggestions", icon="INFO")


class PT_AnimoraCompositorHints(Panel):
    bl_label = "AI Suggestions"
    bl_idname = "NODE_PT_animora_compositor"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Animora"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return (
            context.space_data is not None
            and getattr(context.space_data, "tree_type", "") == "CompositorNodeTree"
        )

    def draw(self, context: bpy.types.Context) -> None:
        self.layout.label(text="Compositor AI suggestions", icon="INFO")


_classes = [
    PT_AnimoraDopeSheetHints,
    PT_AnimoraGraphEditorHints,
    PT_AnimoraCompositorHints,
]


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
