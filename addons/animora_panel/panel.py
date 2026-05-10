"""
PT_AnimoraPanel — main N-sidebar panel in the VIEW_3D editor.

Layout:
  Header row: logo label + sign-in/plan badge
  Chat history UIList (scrollable)
  Input row: text field + Send + Mic
  Context toggles: Share viewport / scene
  Collapsible secondary panels (Properties, Dope Sheet, Compositor hints)
"""

from __future__ import annotations

import bpy
from bpy.types import Panel, UIList

from . import auth, ws_client
from .preferences import get_prefs


class ANIMORA_UL_ChatHistory(UIList):
    bl_idname = "ANIMORA_UL_chat_history"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        if item.role == "user":
            row = layout.row()
            row.alert = False
            row.label(text=f"You: {item.content[:80]}", icon="USER")
        else:
            row = layout.row()
            row.label(text=item.content[:80], icon="OUTLINER_OB_ARMATURE")


class PT_AnimoraPanel(Panel):
    bl_label = "Animora AI"
    bl_idname = "VIEW3D_PT_animora"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Animora"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        wm = context.window_manager
        prefs = get_prefs()

        # --- Header ---
        header = layout.row(align=True)
        header.scale_y = 1.2
        header.label(text="Animora", icon="OUTLINER_OB_ARMATURE")

        if auth.session.signed_in:
            plan_label = auth.session.plan.capitalize()
            header.label(text=f"{auth.session.email}  [{plan_label}]")
            header.operator("animora.sign_out", text="", icon="PANEL_CLOSE")
        else:
            header.operator("animora.sign_in", text="Sign In", icon="URL")

        layout.separator(factor=0.5)

        # --- Chat history ---
        box = layout.box()
        box.template_list(
            "ANIMORA_UL_chat_history",
            "",
            wm,
            "animora_chat_history",
            wm,
            "animora_chat_index",
            rows=8,
        )

        # --- Input row ---
        row = layout.row(align=True)
        row.prop(wm, "animora_input_text", text="")
        row.operator("animora.send_message", text="", icon="EXPORT")
        row.operator("animora.start_recording", text="", icon="SPEAKER")

        # Connection status
        status_row = layout.row()
        if ws_client.client.connected:
            status_row.label(text="Connected", icon="CHECKMARK")
        elif auth.session.signed_in:
            status_row.label(text="Connecting...", icon="TIME")
        else:
            status_row.label(text="Signed out", icon="ERROR")

        layout.separator(factor=0.5)

        # --- Context toggles ---
        ctx_box = layout.box()
        ctx_box.label(text="AI Context", icon="SCENE_DATA")
        ctx_box.prop(prefs, "share_viewport", toggle=True)
        ctx_box.prop(prefs, "share_scene_graph", toggle=True)


class PT_AnimoraPropertiesHints(Panel):
    bl_label = "AI Suggestions"
    bl_idname = "PROPERTIES_PT_animora_hints"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.label(text="AI suggestions will appear here", icon="INFO")


_classes = [
    ANIMORA_UL_ChatHistory,
    PT_AnimoraPanel,
    PT_AnimoraPropertiesHints,
]


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
