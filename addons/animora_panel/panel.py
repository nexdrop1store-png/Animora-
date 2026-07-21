"""
Animora AI panel — premium chat surface integrated into SPACE_ANIMORA.

Visual structure (top to bottom):

    HEADER (drawn by ANIMORA_HT_header)
      ✦ Animora AI   ── (history) (settings) (+ New)

    MAIN WINDOW (drawn by ANIMORA_PT_main)
      ┌────────────────────────────────────────────────┐
      │  ◇ avatar + "Hi — I'm Animora."                │  ← when history is empty
      │  Three suggestion cards                        │
      │  Chip suggestion                               │
      │                                                │
      │  ── OR ──                                      │
      │                                                │
      │  ▸ Chat history with full streaming text       │  ← when history exists
      │      • word-wrapped assistant messages          │
      │      • right-aligned user messages              │
      │      • inline quality_notice cards              │
      │                                                │
      │  Status pill (when AI is active)                │
      │    "Animora is thinking..."  or similar         │
      │                                                │
      │  Input field + SEND / STOP                     │
      └────────────────────────────────────────────────┘

When the AI is active (THINKING/STREAMING/EXECUTING/QUALITY_CHECK), a soft
animated indigo/cyan/amber rim pulses around the panel edge — drawn by
border_glow.py via a GPU POST_PIXEL handler. The dot tick on the status
pill is animated by the bpy timer in state.py.

The chat history is rendered with custom `layout.box() + layout.label()`
calls — NOT a UIList. UILists truncate at the row level and don't
visibly stream. The custom rendering shows full content word-wrapped
and updates on every token via area.tag_redraw().
"""

from __future__ import annotations

import sys

import bpy
from bpy.types import Panel

from . import bundle, preview_icons, state, updater, ws_client
from .preferences import get_prefs

# Pixel-to-char ratio at Blender's default UI scale. The default font
# renders at ~7 pixels per character horizontally; multiplied by the
# user's `ui_scale` preference gives the effective rate.
_BASE_PIXELS_PER_CHAR = 7.0
# Reserved pixels for icons, padding, alignment margins around message bodies.
_CHROME_PIXELS = 64
# Floor + ceiling on the dynamic char count to keep layout sane at
# extreme panel widths.
_MIN_WRAP_CHARS = 20
_MAX_WRAP_CHARS = 140

# Below this region width (pixels), hide non-essential chrome — the scene
# strip and dev-tools footer crowd a narrow panel. Threshold picked to
# match the smallest width at which the suggestion cards still read.
_NARROW_REGION_PX = 280


def _pixels_per_char_for_region(context) -> float:
    """The same ui_scale-aware rate _wrap_chars_for_region derives
    internally, exposed separately for v1.2's composer click-to-cursor
    column math (which needs the raw per-char pixel rate, not the
    already-divided-out character count)."""
    try:
        ui_scale = float(context.preferences.system.ui_scale)
    except (AttributeError, TypeError):
        return _BASE_PIXELS_PER_CHAR
    return _BASE_PIXELS_PER_CHAR * max(0.5, ui_scale)


def _wrap_chars_for_region(context) -> int:
    """Compute how many characters fit on one line in the current region.

    Reads `bpy.context.region.width` (pixel width of the AI panel) and
    the user's UI scale preference, then divides out a per-char average.
    This makes message text reflow naturally when the user drags the
    panel wider or narrower — the prior version had a hardcoded 56-char
    limit which truncated content at any width."""
    try:
        region = context.region
        ui_scale = float(context.preferences.system.ui_scale)
    except (AttributeError, TypeError):
        return 56
    if region is None or region.width <= 0:
        return 56
    pixels_per_char = _BASE_PIXELS_PER_CHAR * max(0.5, ui_scale)
    usable_pixels = max(60, region.width - _CHROME_PIXELS)
    chars = int(usable_pixels / pixels_per_char)
    return max(_MIN_WRAP_CHARS, min(_MAX_WRAP_CHARS, chars))


def _wrap_lines(text: str, width: int) -> list[str]:
    """Word-wrap `text` to chunks <= `width` chars each. Preserves
    paragraph breaks. Never splits inside a word — overlong words go on
    their own line. Returns empty list for empty input.

    `width` is computed per-frame by `_wrap_chars_for_region(context)`
    so the wrap is responsive to the user resizing the panel."""
    if not text:
        return []
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")  # preserve blank line
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            if current and len(current) + 1 + len(word) > width:
                lines.append(current)
                current = word
            elif current:
                current = current + " " + word
            else:
                current = word
        if current:
            lines.append(current)
    return lines


def composer_preview_lines(text: str, region_width: int) -> list[str]:
    """Wrapped lines of the FULL prompt for the composer preview.

    The bpy string field is single-line — long prompts scroll horizontally
    and become unreadable. When the prompt no longer fits the field, the
    composer shows it fully wrapped above the field (and the GPU glass
    card grows to match — ads/canvas.py calls this same function so the
    two layers can never disagree). Empty list = prompt fits, no preview.
    """
    if not text:
        return []
    # Field width ≈ region minus chrome minus the [+] and [▲] buttons.
    chars = int((region_width - _CHROME_PIXELS - 76) / _BASE_PIXELS_PER_CHAR)
    chars = max(_MIN_WRAP_CHARS, min(_MAX_WRAP_CHARS, chars))
    if len(text) <= chars and "\n" not in text:
        return []
    lines = _wrap_lines(text, chars)
    if len(lines) > 10:
        hidden = len(lines) - 9
        lines = lines[:9] + [f"… (+{hidden} more lines)"]
    return lines


def _brand_icon_kwargs() -> dict:
    """icon kwargs for the Animora logo, falling back to no icon if the
    preview collection failed to load (get_icon returns 0)."""
    icon_id = preview_icons.get_icon("animora")
    return {"icon_value": icon_id} if icon_id else {}


# ---------------------------------------------------------------------------
# Header — branded strip with right-aligned controls
# ---------------------------------------------------------------------------

class ANIMORA_HT_header(bpy.types.Header):
    """Header strip — minimal: brand on the left, settings + new on the right.

    Previous iterations packed "AI Studio" label + a live state chip
    ("AI READY" / "AI ACTIVE") into the header. At default panel width
    (~420px) this clipped the rightmost buttons. The state chip now
    lives in the main panel body via `_draw_status_pill` (which is
    where the user looks for active state anyway), and the redundant
    "AI Studio" subtitle is removed.

    Resulting layout fits in ~180px and stays readable at any width:

        [ANIMORA]   ─── spacer ───   [⚙] [+ New]
    """

    bl_space_type = "ANIMORA"

    @classmethod
    def poll(cls, context):
        # Hidden while the onboarding gate owns the ANIMORA space.
        from . import onboarding
        return not onboarding.gate_active()

    def draw(self, context):
        layout = self.layout

        left = layout.row(align=True)
        left.label(text="ANIMORA", **_brand_icon_kwargs())

        layout.separator_spacer()

        right = layout.row(align=True)
        right.operator("animora.quick_settings", text="", icon="PREFERENCES", emboss=False)
        right.separator(factor=0.4)
        right.operator("animora.new_conversation", text="New", icon="ADD")


# ---------------------------------------------------------------------------
# Main panel
# ---------------------------------------------------------------------------

class _AnimoraMainPanelMixin:
    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        prefs = get_prefs()

        history_len = len(wm.animora_chat_history)

        # Responsive wrap width — computed once per draw from region.width.
        # Passed down to message renderers so text reflows when the user
        # drags the panel wider.
        self._wrap_width = _wrap_chars_for_region(context)
        self._pixels_per_char = _pixels_per_char_for_region(context)

        outer = layout.column(align=False)
        outer.scale_y = 1.0

        # Narrow-region heuristic: when the user has dragged the panel
        # very small, drop non-essential chrome (scene strip, dev footer)
        # so the conversation + input area aren't squeezed off-screen.
        try:
            region_width = context.region.width if context.region else 0
        except AttributeError:
            region_width = 0
        is_narrow = 0 < region_width < _NARROW_REGION_PX

        # ── Scene-view metadata strip (matches the reference design) ───
        # Gives the user immediate context for what scene the AI is
        # operating against. Hidden at narrow widths where the icons +
        # text would wrap or clip.
        if not is_narrow:
            self._draw_scene_strip(outer, context)

        self._draw_update_banner(outer)

        if history_len == 0 and state.state.current == state.S.IDLE:
            self._draw_onboarding(outer, context)
        else:
            self._draw_conversation(outer, wm)

        outer.separator(factor=0.6)

        # Connection strip. Sign-in lives EXCLUSIVELY in the onboarding gate
        # (onboarding.py) — the panel never shows a sign-in affordance. All
        # it surfaces is transient connection status while a session exists.
        if bundle.is_bundle_mode():
            # Recording build: no sign-in. The engine auto-starts and the
            # session auto-connects; show progress instead of a button.
            self._draw_bundle_status(outer)

        elif state.state.auth_status == state.AuthS.CONNECTING:
            hint = outer.row()
            hint.scale_y = 0.85
            hint.label(
                text=state.state.auth_message or "Connecting to Animora…",
                icon="SORTTIME",
            )

        # Status pill — shown whenever the AI is active or just completed
        self._draw_status_pill(outer)

        # Quality notice — inline card under chat when the artist's-eye check flagged something
        self._draw_quality_notice(outer)

        # Input area
        self._draw_input(outer, wm)

        # Feedback — always reachable; opens the website feedback page in the
        # system browser (attaches the user's account if signed in there).
        fb = outer.row()
        fb.scale_y = 0.85
        fb.operator("animora.feedback", text="Send Feedback", icon="HELP")

        # Dev tools footer — Self-Test button. Only shown in dev mode
        # to avoid cluttering the production UX. Hidden at narrow widths.
        if prefs.dev_mode and not is_narrow:
            self._draw_dev_footer(outer)

    # --- bundle (recording build) connection status -----------------------

    def _draw_bundle_status(self, layout) -> None:
        """Recording-build status line — replaces the sign-in button. Shows
        the engine auto-start / auto-connect progress, or a plain-language
        error card if the engine didn't come up."""
        phase, detail = bundle.get_status()
        connected = ws_client.client.connected

        if connected and state.state.auth_status == state.AuthS.CONNECTED:
            row = layout.row(align=True)
            row.scale_y = 0.9
            row.label(text="Recording mode — connected", icon="REC")
            layout.separator(factor=0.5)
            return

        if phase == "failed":
            box = layout.box()
            box.alert = True
            col = box.column(align=True)
            col.label(text="Animora's engine didn't start", icon="ERROR")
            col.scale_y = 0.85
            col.label(text="Close Animora completely and reopen it.")
            if detail:
                col.label(text=detail)
            layout.separator(factor=0.5)
            return

        # starting / waiting / connecting
        msg = {
            "starting": "Starting Animora's engine…",
            "waiting": "Starting Animora's engine…",
            "connecting": "Connecting…",
        }.get(phase, "Starting Animora's engine…")
        row = layout.row()
        row.scale_y = 0.9
        row.label(text=msg, icon="SORTTIME")
        layout.separator(factor=0.5)

    # --- scene strip (matches reference design's metadata bar) ------------

    def _draw_scene_strip(self, layout, context) -> None:
        """The narrow bar under the header showing what scene + render
        engine + object count the AI is operating against. Mirrors the
        reference design's 'SCENE VIEW | beach_scene.blend | 5 Objects |
        Cycles | LIVE' strip."""
        scene = context.scene
        if scene is None:
            return
        strip = layout.row(align=True)
        strip.scale_y = 0.85
        strip.label(text="SCENE", icon="SCENE_DATA")
        strip.label(text=f"{scene.name}")
        strip.separator(factor=0.4)
        n_objs = len([o for o in scene.objects if o.visible_get()])
        strip.label(text=f"{n_objs} objects")
        strip.separator(factor=0.4)
        eng = scene.render.engine.split("_")[-1].title() if scene.render else "?"
        strip.label(text=eng)
        strip.separator_spacer()
        # Live indicator — green dot when vision stream is connected
        if ws_client.client.connected:
            strip.label(text="LIVE", icon="REC")
        layout.separator(factor=0.3)

    # --- onboarding / empty state -----------------------------------------

    def _draw_onboarding(self, layout, context):
        layout.separator(factor=2.0)

        avatar_row = layout.row()
        avatar_row.alignment = "CENTER"
        avatar_row.scale_y = 2.2
        avatar_row.scale_x = 2.2
        avatar_row.label(text="", **_brand_icon_kwargs())

        layout.separator(factor=0.6)

        greet = layout.row()
        greet.alignment = "CENTER"
        greet.scale_y = 1.05
        greet.label(text="Hi — I'm Animora.")

        sub = layout.row()
        sub.alignment = "CENTER"
        sub.scale_y = 1.4
        sub.label(text="What are we building today?")

        layout.separator(factor=1.4)

        suggestions = [
            ("icon_chair", "Add a low-poly chair"),
            ("icon_sun", "Light the scene like golden hour"),
            ("icon_loop", "Animate the cube spinning"),
        ]

        # Responsive: 2-column grid when there's plenty of width, single
        # column when narrow. `grid_flow` reflows automatically — that's
        # Blender's nearest equivalent of CSS grid auto-fit.
        wide = self._wrap_width >= 70
        if wide:
            grid = layout.grid_flow(row_major=True, columns=2, even_columns=True, align=False)
        else:
            grid = layout.column(align=False)

        for icon_name, prompt in suggestions:
            card = grid.box()
            card.scale_y = 1.3
            row = card.row(align=True)
            icon_id = preview_icons.get_icon(icon_name)
            if icon_id:
                row.label(text="", icon_value=icon_id)
            else:
                row.label(text="", icon="DOT")
            op = row.operator("animora.send_suggested", text=prompt, emboss=False)
            op.prompt = prompt

        layout.separator(factor=1.0)

        chip_row = layout.row()
        chip_row.alignment = "CENTER"
        chip = chip_row.box()
        chip.scale_y = 0.9
        chip_inner = chip.row()
        chip_inner.alignment = "CENTER"
        chip_op = chip_inner.operator(
            "animora.send_suggested",
            text="Change the mesh density of the floor plane.",
            emboss=False,
        )
        chip_op.prompt = "Change the mesh density of the floor plane."

    # --- conversation rendering -------------------------------------------

    def _draw_conversation(self, layout, wm):
        """Custom message-by-message rendering. UIList is intentionally NOT
        used here — it truncates rows and breaks visible streaming. We
        iterate the collection directly so every token append immediately
        shows when the area redraws."""
        history = wm.animora_chat_history
        if len(history) == 0:
            return

        # Container box gives a subtle frame around the chat region
        convo = layout.column(align=False)
        convo.scale_y = 1.0

        # Sprint 1 Deep: bumped from 12 → 30 so hero turns (which can
        # emit 22 ⏵ tool.start lines + ~8 ✓ result lines + narration)
        # don't truncate mid-build. Older entries are still in
        # wm.animora_chat_history for the backend's history sync.
        # Render-cost is O(N visible) which stays bounded.
        _VISIBLE_LIMIT = 30
        total = len(history)
        visible_turns = list(history)[-_VISIBLE_LIMIT:]
        if total > _VISIBLE_LIMIT:
            # Truncation header so it's clear there's more above.
            hdr = convo.row()
            hdr.alignment = "CENTER"
            hdr.label(text=f"… {total - _VISIBLE_LIMIT} earlier entries hidden …", icon="DOT")
            convo.separator(factor=0.4)

        for i, item in enumerate(visible_turns):
            is_user = (item.role == "user")
            is_last_assistant = (
                not is_user
                and i == len(visible_turns) - 1
                and state.state.current in (state.S.STREAMING, state.S.THINKING)
            )

            # Chat-bubble asymmetry: user messages hug the right at ~80%
            # width, assistant messages hug the left at ~90% — the layout
            # itself reads as a conversation, like modern AI clients.
            if is_user:
                split = convo.split(factor=0.20)
                split.column()  # left gutter
                self._draw_user_message(split.column(), item.content)
            else:
                split = convo.split(factor=0.90)
                self._draw_assistant_message(
                    split.column(), item.content, is_streaming=is_last_assistant
                )
                split.column()  # right gutter

            convo.separator(factor=0.3)

    def _draw_user_message(self, layout, content: str) -> None:
        box = layout.box()
        box.scale_y = 0.95
        head = box.row(align=True)
        head.alignment = "RIGHT"
        head.label(text="You", icon="USER")
        body = box.column(align=True)
        body.scale_y = 0.85
        # Bubble is ~80% of the region wide (see _draw_conversation).
        wrap = max(_MIN_WRAP_CHARS, int(self._wrap_width * 0.78))
        for line in _wrap_lines(content, wrap):
            row = body.row(align=True)
            row.alignment = "RIGHT"
            row.label(text=line or " ")

    def _draw_assistant_message(self, layout, content: str, *, is_streaming: bool) -> None:
        box = layout.box()
        box.scale_y = 0.95
        head = box.row(align=True)
        head.alignment = "LEFT"
        head.label(text="Animora", **_brand_icon_kwargs())
        if is_streaming:
            # "Currently typing" indicator: a single dot sweeping across
            # three positions (KITT style) — calmer than the old grow/shrink
            # dot bar, and immune to the tick-3 negative-repeat glitch.
            frames = ("●  ·  ·", "·  ●  ·", "·  ·  ●", "·  ●  ·")
            head.label(text=frames[state.state.dot_tick % 4])

        body = box.column(align=True)
        body.scale_y = 0.85

        # If the assistant message is empty and streaming hasn't started
        # yet, show a quiet thinking line so the box isn't a void.
        if not content and is_streaming:
            row = body.row(align=True)
            row.alignment = "LEFT"
            row.label(text="Thinking" + "." * (state.state.dot_tick % 4))
            return

        # Bubble is ~90% of the region wide (see _draw_conversation).
        wrap = max(_MIN_WRAP_CHARS, int(self._wrap_width * 0.88))
        for line in _wrap_lines(content, wrap):
            row = body.row(align=True)
            row.alignment = "LEFT"
            row.label(text=line or " ")

        # Streaming cursor appended below the latest line
        if is_streaming and content:
            cursor_row = body.row(align=True)
            cursor_row.alignment = "LEFT"
            blink = "▍" if state.state.dot_tick % 2 == 0 else " "
            cursor_row.label(text=blink)

    # --- status pill ------------------------------------------------------

    def _draw_status_pill(self, layout) -> None:
        cur = state.state.current
        if cur == state.S.IDLE:
            return

        text, icon = state.label()
        # COMPLETE / ERROR show without dots; ACTIVE states have dots
        # already baked into the label by state.label().

        pill = layout.box()
        pill.scale_y = 0.9
        row = pill.row(align=True)
        row.alignment = "LEFT"

        if cur in state.ACTIVE_STATES:
            # Status only — the square stop button lives on the input row
            # (modern chat layout), not duplicated here.
            row.label(text=text, icon=icon)
            # Sub-line: detail like the intent_summary or tool name
            if state.state.message:
                sub = pill.row(align=True)
                sub.scale_y = 0.7
                sub.alignment = "LEFT"
                sub.label(text=state.state.message[:80])

        elif cur == state.S.COMPLETE:
            row.label(text=text, icon=icon)
            if state.state.message:
                sub = pill.row(align=True)
                sub.scale_y = 0.7
                sub.label(text=state.state.message[:80])

        elif cur == state.S.ERROR:
            row.alert = True
            row.label(text=text, icon=icon)
            if state.state.message:
                sub = pill.row(align=True)
                sub.scale_y = 0.8
                sub.alert = True
                sub.label(text=state.state.message[:120])

    # --- quality notice ---------------------------------------------------

    def _draw_quality_notice(self, layout) -> None:
        notice = state.state.quality_notice
        if not notice:
            return

        sev = str(notice.get("severity", "info")).lower()
        icon = {"warning": "ERROR", "error": "CANCEL", "info": "INFO"}.get(sev, "INFO")

        box = layout.box()
        box.scale_y = 0.9
        head = box.row(align=True)
        head.alignment = "LEFT"
        head.label(text="Quality check", icon=icon)

        summary = str(notice.get("summary", ""))
        for line in _wrap_lines(summary, self._wrap_width):
            r = box.row(align=True)
            r.scale_y = 0.8
            r.alignment = "LEFT"
            r.label(text=line)

        fixes = notice.get("fix_suggestions", []) or []
        if fixes:
            box.separator(factor=0.3)
            sub_head = box.row(align=True)
            sub_head.scale_y = 0.75
            sub_head.label(text="Suggested fixes:")
            for fix in fixes[:3]:
                for line in _wrap_lines(f"• {fix}", self._wrap_width):
                    r = box.row(align=True)
                    r.scale_y = 0.75
                    r.alignment = "LEFT"
                    r.label(text=line)

    # --- dev tools footer -------------------------------------------------

    def _draw_dev_footer(self, layout) -> None:
        """Tiny dev-only diagnostics row. Self-Test runs three scripts
        through the same execution path the AI uses, without touching
        the LLM — confirms the addon-side execution pipeline is healthy."""
        layout.separator(factor=0.5)
        row = layout.row(align=True)
        row.scale_y = 0.9
        row.label(text="Dev tools", icon="CONSOLE")
        row.operator("animora.self_test", text="Run Self-Test", icon="PLAY")

    # --- update banner ------------------------------------------------------

    def _draw_update_banner(self, layout) -> None:
        """"Update available" card — only rendered once a background
        check (updater.refresh_cache_async, kicked off from
        operators.register() a few seconds after launch, and re-armed
        each redraw here) confirms a newer PUBLISHED release exists.
        Silent no-op before the first check completes or if it failed —
        an update check must never show a false positive or an error
        banner for something this low-stakes."""
        updater.refresh_cache_async()  # no-op if a check is already in flight
        release = updater.get_cached_release()
        if not updater.update_available(release):
            return

        box = layout.box()
        row = box.row(align=True)
        row.label(text=f"Update available — v{release.get('version', '?')}", icon="INFO")
        if sys.platform == "win32":
            row.operator("animora.update_now", text="Update Now", icon="IMPORT")
        else:
            # Auto-update is Windows-only today (see updater.py's module
            # docstring) — point elsewhere rather than show a button
            # that can't work.
            row.operator("wm.url_open", text="Download").url = "https://animora.tech/download"
        box.separator(factor=0.3)

    # --- input area -------------------------------------------------------

    def _draw_input(self, layout, wm):
        from . import operators as ops_module

        input_card = layout.box()
        input_card.scale_y = 1.0
        is_ready = state.auth_can_send()

        # Pending attachment chips — one row per file, removable.
        attachments = ops_module.pending_attachments()
        for idx, att in enumerate(attachments):
            chip = input_card.row(align=True)
            chip.scale_y = 0.85
            chip_icon = "IMAGE_DATA" if att.get("kind") == "image" else "TEXT"
            chip.label(text=att["name"], icon=chip_icon)
            size_kb = max(1, att["size"] // 1024)
            chip.label(text=f"{size_kb} KB")
            remove = chip.operator("animora.remove_attachment", text="", icon="X", emboss=False)
            remove.index = idx

        wrap_chars = self._wrap_width or 56

        if ops_module.composer_active():
            # LIVE multiline editing (OT_AnimoraComposer modal). The buffer is
            # mirrored into animora_input_text on every keystroke, so we can
            # draw the whole prompt wrapped here — with a blinking caret — and
            # it grows as you type. Enter sends; Shift+Enter = newline.
            lines, crow, ccol, caret_on = ops_module.composer_display(wrap_chars)
            editor = input_card.column(align=True)
            editor.scale_y = 0.9
            for i, line in enumerate(lines):
                shown = line
                if i == crow and caret_on:
                    ccol = max(0, min(ccol, len(line)))
                    shown = line[:ccol] + "▏" + line[ccol:]
                row = editor.row(align=True)
                row.alignment = "LEFT"
                # v1.2 — click-to-position: each row is the SAME composer
                # operator (emboss=False so it still reads as plain text,
                # same technique the idle-draft view below already uses),
                # carrying which row was clicked + the exact wrap geometry
                # this frame drew with. See OT_AnimoraComposer.invoke():
                # modal()'s LEFTMOUSE handler already ends the current
                # editing session and PASS_THROUGHs every click (so the
                # button under the cursor still fires) — re-invoking the
                # same operator on the clicked row, instead of always
                # resetting the caret to end-of-text, is what fixes it.
                click = row.operator("animora.composer", text=shown or " ",
                                      emboss=False)
                click.click_row = i
                click.click_wrap_chars = wrap_chars
                click.click_pixels_per_char = self._pixels_per_char
            hint = input_card.row()
            hint.scale_y = 0.7
            hint.label(text="Enter to send · Shift+Enter for a new line · Esc to close",
                       icon="INFO")

            actions = input_card.row(align=True)
            actions.scale_y = 1.4
            a = actions.row(align=True)
            a.enabled = is_ready or state.is_active()
            a.operator("animora.attach_file", text="", icon="ADD")
            send = actions.row(align=True)
            send.enabled = is_ready
            send.operator("animora.send_message", text="Send", icon="TRIA_UP")
            return

        # Idle: click-to-compose. There is NO expand button — clicking the
        # input area drops straight into the auto-growing multiline composer
        # (OT_AnimoraComposer). Any existing draft is shown fully wrapped so
        # the whole prompt is always visible, editing or not.
        current_text = getattr(wm, "animora_input_text", "") or ""
        draft = current_text.strip()

        if draft and not state.is_active():
            # Show the full draft wrapped (never truncated); clicking it
            # resumes editing in the composer.
            draft_lines = _wrap_lines(draft, wrap_chars)
            dcol = input_card.column(align=True)
            dcol.scale_y = 0.9
            for line in draft_lines[:12]:
                dcol.operator("animora.composer", text=line or " ", emboss=False)
            input_card.separator(factor=0.2)

        prompt_row = input_card.row(align=True)
        prompt_row.scale_y = 1.7

        attach = prompt_row.row(align=True)
        attach.enabled = is_ready or state.is_active()
        attach.operator("animora.attach_file", text="", icon="ADD")

        # The composer launcher fills the row and looks like a text field.
        field = prompt_row.row(align=True)
        field.enabled = is_ready
        launcher = draft if (draft and not state.is_active()) else ""
        field.operator("animora.composer",
                       text=launcher or "Ask Animora…",
                       icon="GREASEPENCIL")

        if state.is_active():
            stop = prompt_row.row(align=True)
            stop.alert = True
            stop.operator("animora.interrupt", text="■")
        else:
            send = prompt_row.row(align=True)
            send.enabled = is_ready
            send.operator("animora.send_message", text="", icon="TRIA_UP")


class ANIMORA_PT_main(_AnimoraMainPanelMixin, Panel):
    bl_label = ""
    bl_idname = "ANIMORA_PT_main"
    bl_space_type = "ANIMORA"
    bl_region_type = "WINDOW"
    bl_options = {"HIDE_HEADER"}

    @classmethod
    def poll(cls, context):
        # While the onboarding gate is up, its panel owns the ANIMORA space.
        from . import onboarding
        return not onboarding.gate_active()


class VIEW3D_PT_animora_sidebar(_AnimoraMainPanelMixin, Panel):
    bl_label = "Animora"
    bl_idname = "VIEW3D_PT_animora_sidebar"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Animora"

    @classmethod
    def poll(cls, context):
        from . import onboarding
        return (
            context.space_data is not None
            and context.space_data.type == "VIEW_3D"
            and getattr(bpy.types, "SpaceAnimora", None) is None
            and not onboarding.gate_active()
        )


# ---------------------------------------------------------------------------
# Properties editor side panel — keep as-is for now
# ---------------------------------------------------------------------------

class PT_AnimoraPropertiesHints(Panel):
    bl_label = "Animora Suggestions"
    bl_idname = "PROPERTIES_PT_animora_hints"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        self.layout.label(text="AI suggestions will appear here", icon="INFO")


# ---------------------------------------------------------------------------
# Registration — the UIList class is gone; chat rendering is custom now.
# ---------------------------------------------------------------------------

_classes = [
    ANIMORA_HT_header,
    ANIMORA_PT_main,
    VIEW3D_PT_animora_sidebar,
    PT_AnimoraPropertiesHints,
]


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
