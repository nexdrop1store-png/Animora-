"""Animora "Refined Indigo" theme — dark blue/purple brand identity.

The palette is canonical here (scripts/setup_theme.py is a thin dev wrapper
around this module). It is applied by ensure_theme() at addon register:

  * once per THEME_VERSION, stamped into AnimoraPreferences.theme_version —
    so existing installs (including userprefs saved with the default grey
    theme) get re-themed exactly once, and the user's later manual theme
    tweaks are never stomped on subsequent launches;
  * "Load Factory Preferences" clears the stamp with everything else, so the
    brand theme comes back on the next launch;
  * never in background mode (headless forwarder / CI runs).

Bump THEME_VERSION when the palette changes to roll it out to all installs.
"""

from __future__ import annotations

import contextlib
import logging

import bpy

log = logging.getLogger("animora.theme")

THEME_VERSION = 2  # v2: chat-bubble box roundness (2026-07-05 UI polish)

# ── Palette — Linear / Vercel / Tailwind indigo, no shouting ────────────
BG0 = "#0D0E1B"      # deepest void
BG1 = "#13141F"      # main background
BG2 = "#1A1B2A"      # panels (subtle elevation)
BG3 = "#22243A"      # input fields, button rest
BG4 = "#2A2D45"      # hover surface
HDR = "#0A0B14"      # darkest header

BTN = "#22243A"      # button rest = input bg (visual consistency)
BTN_SEL = "#6366F1"  # Tailwind indigo-500 — refined, not vivid

ACCENT = "#818CF8"   # indigo-400 (soft)
ACCENT2 = "#A5B4FC"  # indigo-300
LAVEN = "#C7D2FE"    # indigo-200 (highlights)

BORDER = "#2D304A"   # subtle indigo border (NEVER grey)
BORDER_A = "#6366F1"  # active border = BTN_SEL

TEXT = "#E4E4F4"     # off-white (warmer than pure)
SUBTEXT = "#A0A4C0"  # has indigo tint, not grey
WHITE = "#FFFFFF"    # icons

SUCCESS = "#86EFAC"  # green-300
WARNING = "#FCD34D"  # amber-300
ERROR = "#FCA5A5"    # red-300

RND = 0.5            # smooth rounded corners — sleek


# ── Tolerant setters (theme fields vary across Blender versions) ────────
def _rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)


def _rgba(h: str, a: float = 1.0) -> tuple[float, float, float, float]:
    r, g, b = _rgb(h)
    return (r, g, b, a)


def sc(obj, attr: str, col: str, a: float = 1.0) -> None:
    val = getattr(obj, attr, None)
    if val is None:
        return
    try:
        if hasattr(val, "__len__"):
            setattr(obj, attr, _rgba(col, a) if len(val) == 4 else _rgb(col))
        else:
            setattr(obj, attr, _rgba(col, a))
    except Exception:
        pass


def sf(obj, attr: str, v) -> None:
    with contextlib.suppress(Exception):
        setattr(obj, attr, v)


def apply_wcol(w, inner, inner_sel, outline, outline_sel, item, text, text_sel, rnd=RND) -> None:
    if w is None:
        return
    sc(w, "inner", inner)
    sc(w, "inner_sel", inner_sel)
    sc(w, "outline", outline)
    sc(w, "outline_sel", outline_sel)
    sc(w, "item", item)
    sc(w, "text", text)
    sc(w, "text_sel", text_sel)
    sf(w, "roundness", rnd)
    sf(w, "shaded", False)
    sf(w, "shadetop", 0)
    sf(w, "shadedown", 0)


def _apply_space(target) -> None:
    if target is None:
        return
    sc(target, "header", HDR)
    sc(target, "header_text", SUBTEXT)
    sc(target, "header_text_hi", TEXT)
    sc(target, "back", BG0)
    sc(target, "title", TEXT)
    sc(target, "text", TEXT)
    sc(target, "text_hi", WHITE)
    sc(target, "button", BTN)
    sc(target, "button_title", TEXT)
    sc(target, "button_text", SUBTEXT)
    sc(target, "button_text_hi", WHITE)
    sc(target, "navigation_bar", HDR)
    sc(target, "tab_active", BTN_SEL)
    sc(target, "tab_inactive", BG2)
    sc(target, "tab_back", BG1)
    sc(target, "tab_outline", BORDER)


def apply_refined_indigo() -> None:
    """Apply the full Animora theme to the active preferences (in memory).
    Callers decide when to persist via wm.save_userpref()."""
    theme = bpy.context.preferences.themes[0]
    ui = theme.user_interface

    # Widgets — including wcol_text (search bars / name fields).
    apply_wcol(getattr(ui, "wcol_regular", None), BTN, BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_text", None), BG3, BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_tool", None), BTN, BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_radio", None), BG2, BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_toggle", None), BTN, BTN_SEL, BORDER, BORDER_A, BTN_SEL, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_num", None), BG3, BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_numslider", None), BG3, BTN_SEL, BORDER, BORDER_A, ACCENT, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_option", None), BG2, BTN_SEL, BORDER, BORDER_A, BTN_SEL, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_choice", None), BTN, BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_menu", None), BTN, BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_menu_back", None), BG1, BG2, BORDER, BORDER_A, ACCENT2, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_menu_item", None), BG1, BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_pulldown", None), BG1, BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_tab", None), BG2, BTN_SEL, BORDER, BORDER_A, ACCENT2, SUBTEXT, WHITE, 0.5)
    # Boxes host the chat message cards — extra roundness makes them read
    # as conversation bubbles (panel.py draws them asymmetrically).
    apply_wcol(getattr(ui, "wcol_box", None), BG1, BG2, BORDER, BORDER_A, ACCENT2, TEXT, WHITE, 0.8)
    apply_wcol(getattr(ui, "wcol_scroll", None), BG1, BTN_SEL, BORDER, BORDER_A, BTN_SEL, TEXT, WHITE, 0.5)
    apply_wcol(getattr(ui, "wcol_progress", None), BG3, BTN_SEL, BORDER, BORDER_A, BTN_SEL, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_list_item", None), BG0, BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT, WHITE)
    apply_wcol(getattr(ui, "wcol_pie_menu", None), BG1, BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT, WHITE, 0.5)
    apply_wcol(getattr(ui, "wcol_tooltip", None), HDR, BG2, BORDER, BORDER_A, ACCENT2, TEXT, WHITE)

    sc(ui, "wcol_state_color_active", BTN_SEL)
    sc(ui, "wcol_state_color_selected", ACCENT)
    sc(ui, "wcol_state_color_alert", ERROR)

    # Area edges between editor regions (the previously-grey border lines).
    sc(ui, "editor_border", BORDER)
    sc(ui, "editor_outline", BORDER)
    sc(ui, "editor_outline_active", BTN_SEL)
    sc(ui, "panel_outline", BORDER)
    sc(ui, "widget_emboss", BG2, 0.0)

    # Soft drop-shadows for popup menus and tooltips (cheap on software GL).
    sf(ui, "menu_shadow_width", 3)
    sf(ui, "menu_shadow_fac", 0.4)

    w_tip = getattr(ui, "wcol_tooltip", None)
    if w_tip:
        sc(w_tip, "inner", HDR)
        sc(w_tip, "outline", BTN_SEL)
        sc(w_tip, "text", TEXT)
        sf(w_tip, "roundness", 0.4)

    # Icons — pure white, fully desaturated.
    for attr in ("icon_scene", "icon_collection", "icon_object", "icon_object_data",
                 "icon_modifier", "icon_shading", "icon_folder", "icon_autokey"):
        sc(ui, attr, WHITE)
    sf(ui, "icon_alpha", 1.0)
    sf(ui, "icon_saturation", 0.0)
    sf(ui, "icon_border_intensity", 0.0)

    # theme.regions — sidebar / timeline / channels (the persistent grey).
    regions = getattr(theme, "regions", None)
    if regions:
        sb = getattr(regions, "sidebars", None)
        if sb:
            sc(sb, "back", BG1)
            sc(sb, "tab_back", BG0)
        scrub = getattr(regions, "scrubbing", None)
        if scrub:
            sc(scrub, "back", BG0)
            sc(scrub, "text", TEXT)
            sc(scrub, "time_marker", ACCENT)
            sc(scrub, "time_marker_selected", LAVEN)
        ch = getattr(regions, "channels", None)
        if ch:
            sc(ch, "back", BG1)
            sc(ch, "text", TEXT)
            sc(ch, "text_selected", WHITE)
        ash = getattr(regions, "asset_shelf", None)
        if ash:
            sc(ash, "back", BG1)

    # theme.common — animation/keyframe colours.
    common = getattr(theme, "common", None)
    if common:
        anim = getattr(common, "anim", None)
        if anim:
            sc(anim, "playhead", LAVEN)
            sc(anim, "preview_range", BTN_SEL, 0.25)
            sc(anim, "scene_strip_range", ACCENT, 0.25)
            sc(anim, "channels", BG2)
            sc(anim, "channels_sub", BG1)
            sc(anim, "channel_group", BTN, 0.7)
            sc(anim, "channel_group_active", BTN_SEL, 0.4)
            sc(anim, "channel", BG2)
            sc(anim, "channel_selected", BTN_SEL, 0.4)
            sc(anim, "keyframe", ACCENT2)
            sc(anim, "keyframe_selected", WHITE)
        curves = getattr(common, "curves", None)
        if curves:
            for attr in ("handle_vect", "handle_align", "handle_free", "handle_auto",
                         "handle_auto_clamped", "handle_sel_vect", "handle_sel_align",
                         "handle_sel_free", "handle_sel_auto", "handle_sel_auto_clamped"):
                if getattr(curves, attr, None) is not None:
                    sc(curves, attr, BTN_SEL)

    # Space backgrounds — every editor.
    spaces = [
        "view_3d", "graph_editor", "dopesheet_editor", "nla_editor",
        "image_editor", "sequence_editor", "node_editor", "text_editor",
        "outliner", "properties", "file_browser", "info", "statusbar",
        "clip_editor", "topbar", "preferences",
    ]
    for sp_name in spaces:
        sp = getattr(theme, sp_name, None)
        if sp is None:
            continue
        _apply_space(getattr(sp, "space", None))
        _apply_space(sp)

    # 3D viewport — flat indigo background.
    vp = theme.view_3d
    sp = getattr(vp, "space", None)
    if sp:
        grad = getattr(sp, "gradients", None)
        if grad:
            sf(grad, "background_type", "SINGLE_COLOR")
            sc(grad, "high_gradient", BG0)
            sc(grad, "gradient", BG0)
        _apply_space(sp)

    sc(vp, "grid", BG3, 0.5)
    sc(vp, "grid_major", BG4, 0.6)
    sc(vp, "wire", BORDER, 0.6)
    sc(vp, "wire_edit", LAVEN, 0.7)
    sc(vp, "clipping_border_3d", BORDER, 0.5)
    sc(vp, "object_active", BTN_SEL)
    sc(vp, "object_selected", LAVEN)
    sc(vp, "vertex", WHITE, 0.9)
    sc(vp, "vertex_select", BTN_SEL)
    sc(vp, "vertex_active", WHITE)
    sc(vp, "edge_select", BTN_SEL)
    sc(vp, "face_select", BTN_SEL, 0.2)
    sc(vp, "face_mode_select", LAVEN, 0.25)
    sc(vp, "editmesh_active", LAVEN, 0.3)
    sc(vp, "bone_pose", BTN_SEL, 0.85)
    sc(vp, "bone_pose_active", LAVEN, 0.9)
    sc(vp, "bone_solid", ACCENT2)
    sc(vp, "bundle_solid", ACCENT2)
    sc(vp, "gp_wire_edit", ACCENT)
    sc(vp, "gp_vertex", WHITE)
    sc(vp, "gp_vertex_select", BTN_SEL)
    sc(vp, "view_overlay", BORDER, 0.5)
    sc(vp, "transform", WHITE)
    sc(vp, "normal", LAVEN)
    sc(vp, "vertex_normal", BTN_SEL)
    sc(vp, "face", WHITE, 0.02)
    sc(vp, "camera_path", ACCENT)

    # Navigation gizmos (hand/pan, +/zoom, axes).
    sc(ui, "gizmo_view_align", LAVEN)
    sc(ui, "gizmo_primary", ACCENT)
    sc(ui, "gizmo_secondary", ACCENT2)
    sc(ui, "gizmo_a", BTN_SEL)
    sc(ui, "gizmo_b", ACCENT)
    sc(ui, "gizmo_hi", WHITE)

    # Outliner.
    ol = theme.outliner
    sc(ol, "active", BTN_SEL)
    sc(ol, "active_object", BTN_SEL)
    sc(ol, "selected_object", LAVEN)
    sc(ol, "selected_highlight", BTN, 0.6)
    sc(ol, "match", ACCENT)
    sc(ol, "row_alternate", BG2, 0.25)

    # Node editor.
    ne = theme.node_editor
    sc(ne, "node_selected", BTN_SEL)
    sc(ne, "node_active", LAVEN)
    sc(ne, "wire", BORDER)
    sc(ne, "wire_select", BTN_SEL)
    sc(ne, "selected_text", BTN_SEL)
    sc(ne, "grid", HDR, 0.9)

    # Dopesheet / timeline.
    ds = theme.dopesheet_editor
    sc(ds, "value_sliders", BTN_SEL)
    sc(ds, "view_sliders", ACCENT)
    sc(ds, "dopesheet_channel_clear", BG0)
    sc(ds, "dopesheet_channel", BG1)
    sc(ds, "dopesheet_subchannel", BG2)
    sc(ds, "channel_group", BTN)
    sc(ds, "active_channels_group", BTN_SEL)
    sc(ds, "keyframe_border", BORDER_A)
    sc(ds, "keyframe_border_selected", ACCENT2)
    sc(ds, "frame_current", LAVEN)
    sc(ds, "time_keyframe", ACCENT2)
    sc(ds, "time_scrub_background", HDR)
    sc(ds, "time_marker_line", ACCENT)
    sc(ds, "time_marker_line_selected", LAVEN)

    ge = theme.graph_editor
    sc(ge, "handle_sel_vect", BTN_SEL)
    sc(ge, "handle_sel_free", LAVEN)
    sc(ge, "handle_sel_auto", ACCENT)
    sc(ge, "channel_group", BTN, 0.5)
    sc(ge, "active_channels_group", BTN_SEL)

    nla = theme.nla_editor
    sc(nla, "nla_track", BG2)
    sc(nla, "active_action", BTN_SEL)
    sc(nla, "active_action_unset", BG1)

    # Sequence editor.
    seq = theme.sequence_editor
    sc(seq, "movie", BTN_SEL)
    sc(seq, "meta", LAVEN)
    sc(seq, "scene", ACCENT)
    sc(seq, "audio", "#7DD3FC")
    sc(seq, "effect", ACCENT2)
    sc(seq, "color", "#FBA5C8")
    sc(seq, "transition", LAVEN)

    # Info log.
    inf = theme.info
    sc(inf, "info_warning", WARNING, 0.1)
    sc(inf, "info_error", ERROR, 0.1)
    sc(inf, "info_info", BTN_SEL, 0.1)
    sc(inf, "info_debug", BG2, 0.4)
    sc(inf, "info_operator", BG2, 0.35)
    sc(inf, "info_property", BG1, 0.3)
    sc(inf, "info_warning_text", WARNING)
    sc(inf, "info_error_text", ERROR)
    sc(inf, "info_info_text", TEXT)
    sc(inf, "info_debug_text", SUBTEXT)
    sc(inf, "info_operator_text", SUBTEXT)
    sc(inf, "info_property_text", SUBTEXT)

    # Text editor.
    te = theme.text_editor
    sc(te, "line_numbers_background", BG1)
    sc(te, "selected_text", BTN_SEL, 0.35)
    sc(te, "cursor", LAVEN)
    sc(te, "syntax_string", SUCCESS)
    sc(te, "syntax_comment", ACCENT, 0.7)
    sc(te, "syntax_builtin", LAVEN)
    sc(te, "syntax_special", BTN_SEL)
    sc(te, "syntax_reserved", ACCENT)
    sc(te, "syntax_numbers", "#94E2D5")
    sc(te, "syntax_preprocessor", ERROR)

    # UI smoothness / animation feel. Deliberately NOT touching ui_scale /
    # ui_line_width here — those are per-machine display settings and this
    # runs on every install once per THEME_VERSION.
    view = bpy.context.preferences.view
    sf(view, "smooth_view", 300)
    sf(view, "pie_animation_timeout", 50)
    sf(view, "pie_tap_timeout", 20)
    sf(view, "pie_initial_timeout", 30)

    with contextlib.suppress(Exception):
        bpy.context.preferences.keymap.active_keyconfig = "Animora"


def _save_userpref_once() -> None:
    """One-shot timer: persist prefs outside register() (safe context).
    Note this saves whatever else is pending in the session prefs too."""
    try:
        bpy.ops.wm.save_userpref()
        log.info("Animora theme v%d applied and saved", THEME_VERSION)
    except Exception as exc:
        log.warning("Theme applied but prefs not saved: %s", exc)
    return None


def ensure_theme() -> None:
    """Apply the brand theme once per THEME_VERSION (see module docstring)."""
    if bpy.app.background:
        return
    try:
        from .preferences import get_prefs
        prefs = get_prefs()
    except Exception as exc:
        log.warning("Theme skipped — preferences unavailable: %s", exc)
        return
    if prefs is None or getattr(prefs, "theme_version", 0) >= THEME_VERSION:
        return

    try:
        apply_refined_indigo()
    except Exception as exc:
        log.warning("Theme apply failed: %s", exc)
        return

    prefs.theme_version = THEME_VERSION
    bpy.app.timers.register(_save_userpref_once, first_interval=0.5)
