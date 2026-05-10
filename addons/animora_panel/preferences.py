"""Addon preferences — backend URL, log level, dev mode toggle."""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import AddonPreferences


class AnimoraPreferences(AddonPreferences):
    bl_idname = "animora_panel"

    backend_url: StringProperty(
        name="AI Backend URL",
        default="wss://api.animora.tech/ws",
        description="WebSocket URL for the Animora AI backend",
    )  # type: ignore[assignment]

    auth_server_url: StringProperty(
        name="Auth Server URL",
        default="https://auth.animora.tech",
    )  # type: ignore[assignment]

    website_url: StringProperty(
        name="Website URL",
        default="https://animora.tech",
    )  # type: ignore[assignment]

    log_level: EnumProperty(
        name="Log Level",
        items=[
            ("DEBUG", "Debug", ""),
            ("INFO", "Info", ""),
            ("WARNING", "Warning", ""),
            ("ERROR", "Error", ""),
        ],
        default="INFO",
    )  # type: ignore[assignment]

    dev_mode: BoolProperty(
        name="Dev Mode",
        default=False,
        description="Use localhost backend (ws://localhost:8000/ws)",
    )  # type: ignore[assignment]

    share_viewport: BoolProperty(
        name="Share Viewport",
        default=True,
        description="Stream viewport frames to AI for visual context",
    )  # type: ignore[assignment]

    share_scene_graph: BoolProperty(
        name="Share Scene Graph",
        default=True,
        description="Send scene object data to AI for context",
    )  # type: ignore[assignment]

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.prop(self, "dev_mode")
        if self.dev_mode:
            layout.prop(self, "backend_url")
            layout.prop(self, "auth_server_url")
        layout.prop(self, "log_level")
        layout.separator()
        layout.label(text="Privacy")
        layout.prop(self, "share_viewport")
        layout.prop(self, "share_scene_graph")

    def effective_backend_url(self) -> str:
        if self.dev_mode:
            return "ws://localhost:8000/ws"
        return self.backend_url

    def effective_auth_url(self) -> str:
        if self.dev_mode:
            return "http://localhost:8001"
        return self.auth_server_url


def get_prefs() -> AnimoraPreferences:
    import bpy
    return bpy.context.preferences.addons["animora_panel"].preferences  # type: ignore[return-value]


def register() -> None:
    bpy.utils.register_class(AnimoraPreferences)


def unregister() -> None:
    bpy.utils.unregister_class(AnimoraPreferences)
