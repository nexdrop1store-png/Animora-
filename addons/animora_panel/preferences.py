"""
Addon preferences — the full AI Settings surface.

Sections (rendered top-to-bottom in Preferences > Add-ons > Animora):

  1. Anthropic Account
       • API key paste field (write-only — never displayed back)
       • Test Connection button (calls backend /validate-key in background)
       • Connection status indicator
       • Key fingerprint (sha256 prefix — confirms a key IS saved without
         revealing it)

  2. Model & Behavior
       • Default model: auto | haiku | sonnet | opus
       • Temperature
       • Max output tokens
       • Streaming responses on/off

  3. Privacy
       • Share viewport (Level 1 vision stream)
       • Share scene graph (Level 3 sync)

  4. Connection
       • Backend URL (visible only in dev mode)
       • Auth server URL (dev mode only)
       • Dev mode toggle (forces localhost backends)

  5. Debug
       • Log level
       • Verbose request/response logging
"""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences

from . import credentials


# Live status of the most recent "Test Connection" attempt. The settings
# UI reads these to render the indicator. Set by OT_AnimoraValidateApiKey.
class _ConnectionStatus:
    """Module-level mutable status (not a Blender PropertyGroup because
    these are session-only and we don't want them persisted)."""
    state: str = "unknown"          # "unknown" | "testing" | "ok" | "failed"
    message: str = ""
    last_ok_at: float = 0.0


connection_status = _ConnectionStatus()


class AnimoraPreferences(AddonPreferences):
    bl_idname = "animora_panel"

    # ── 1. Anthropic Account ───────────────────────────────────────────
    # Write-only field — bound to a setter that pushes to the keyring and
    # immediately clears its on-screen value (we never display the key).
    pending_api_key: StringProperty(
        name="Anthropic API Key",
        description="Paste your Anthropic API key (sk-ant-...). It's stored in your OS keyring, not on disk.",
        default="",
        subtype="PASSWORD",
    )  # type: ignore[assignment]

    # ── 2. Model & Behavior ────────────────────────────────────────────
    default_model: EnumProperty(
        name="Default Model",
        description="Animora's intent classifier overrides this when 'Auto' is selected.",
        items=[
            ("auto", "Auto (Recommended)", "Pick per-request based on intent + complexity"),
            ("haiku", "Haiku 4.5", "Fast & cheap — short prompts only"),
            ("sonnet", "Sonnet 4.6", "Primary workhorse"),
            ("opus", "Opus 4.5", "Most capable — complex multi-step workflows"),
        ],
        default="auto",
    )  # type: ignore[assignment]

    temperature: FloatProperty(
        name="Temperature",
        description="Sampling temperature. Lower = more deterministic.",
        default=1.0, min=0.0, max=1.0, step=10, precision=2,
    )  # type: ignore[assignment]

    max_output_tokens: IntProperty(
        name="Max Output Tokens",
        description="Cap on the response length per turn.",
        default=4096, min=256, max=16384,
    )  # type: ignore[assignment]

    streaming_enabled: BoolProperty(
        name="Stream Responses",
        description="Show tokens as they arrive (uncheck for batched display)",
        default=True,
    )  # type: ignore[assignment]

    # ── 3. Privacy ─────────────────────────────────────────────────────
    share_viewport: BoolProperty(
        name="Share Viewport",
        default=True,
        description="Stream viewport frames to AI for visual context (Level 1 vision)",
    )  # type: ignore[assignment]

    share_scene_graph: BoolProperty(
        name="Share Scene Graph",
        default=True,
        description="Send scene object data to AI for context (Level 3 sync)",
    )  # type: ignore[assignment]

    # ── 4. Connection ──────────────────────────────────────────────────
    backend_url: StringProperty(
        name="AI Backend URL",
        default="wss://api.animora.tech/ws",
        description="WebSocket URL for the Animora AI backend",
    )  # type: ignore[assignment]

    backend_http_url: StringProperty(
        name="AI Backend HTTP URL",
        default="https://api.animora.tech",
        description="HTTPS origin for REST endpoints (e.g. /validate-key)",
    )  # type: ignore[assignment]

    auth_server_url: StringProperty(
        name="Auth Server URL",
        default="https://auth.animora.tech",
    )  # type: ignore[assignment]

    website_url: StringProperty(
        name="Website URL",
        default="https://animora.tech",
    )  # type: ignore[assignment]

    dev_mode: BoolProperty(
        name="Dev Mode",
        default=False,
        description="Use localhost backends (ws://localhost:8000/ws, http://localhost:8000)",
    )  # type: ignore[assignment]

    # ── 5. Debug ───────────────────────────────────────────────────────
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

    verbose_api_logging: BoolProperty(
        name="Verbose API Logging",
        default=False,
        description="Log every request and response body. Slows Animora and reveals scene data in logs — for debugging only.",
    )  # type: ignore[assignment]

    # ── Render ─────────────────────────────────────────────────────────

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout

        # Section 1: Anthropic Account
        box = layout.box()
        header = box.row()
        header.label(text="Anthropic Account", icon="LOCKED")

        if credentials.has_api_key():
            status_row = box.row()
            status_row.label(text=credentials.status_message(), icon="CHECKMARK")
            actions = box.row(align=True)
            actions.operator("animora.test_connection", icon="LINKED", text="Test Connection")
            actions.operator("animora.clear_api_key", icon="TRASH", text="Remove Key")
        else:
            box.label(text="No API key configured.", icon="ERROR")
            box.label(text="Paste your Anthropic key below to enable AI features:")

        paste_row = box.row(align=True)
        paste_row.prop(self, "pending_api_key", text="")
        paste_row.operator("animora.save_api_key", icon="FILE_TICK", text="Save")

        # Live connection status from the most recent test
        cs = connection_status
        if cs.state == "testing":
            box.label(text="Testing connection…", icon="SORTTIME")
        elif cs.state == "ok":
            box.label(text=f"✓ {cs.message}", icon="CHECKMARK")
        elif cs.state == "failed":
            box.label(text=f"✗ {cs.message}", icon="ERROR")

        # Section 2: Model & Behavior
        layout.separator()
        box = layout.box()
        box.label(text="Model & Behavior", icon="OUTLINER_OB_LIGHT")
        col = box.column(align=True)
        col.prop(self, "default_model")
        col.prop(self, "temperature", slider=True)
        col.prop(self, "max_output_tokens")
        col.prop(self, "streaming_enabled")

        # Section 3: Privacy
        layout.separator()
        box = layout.box()
        box.label(text="Privacy", icon="HIDE_OFF")
        col = box.column(align=True)
        col.prop(self, "share_viewport")
        col.prop(self, "share_scene_graph")

        # Section 4: Connection
        layout.separator()
        box = layout.box()
        box.label(text="Connection", icon="WORLD")
        box.prop(self, "dev_mode")
        if self.dev_mode:
            col = box.column(align=True)
            col.prop(self, "backend_url")
            col.prop(self, "backend_http_url")
            col.prop(self, "auth_server_url")

        # Section 5: Debug
        layout.separator()
        box = layout.box()
        box.label(text="Debug", icon="CONSOLE")
        col = box.column(align=True)
        col.prop(self, "log_level")
        col.prop(self, "verbose_api_logging")

    # ── Helpers consumed elsewhere in the addon ───────────────────────

    def effective_backend_url(self) -> str:
        if self.dev_mode:
            return "ws://localhost:8000/ws"
        return self.backend_url

    def effective_backend_http_url(self) -> str:
        if self.dev_mode:
            return "http://localhost:8000"
        return self.backend_http_url

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
