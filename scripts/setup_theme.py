"""
Animora Theme — Vivid Purple-Blue, Zero Grey
- Pure purple/violet palette (no Catppuccin surface greys)
- Covers theme.regions.{sidebars,scrubbing,channels,asset_shelf} (the missing grey sources)
- Covers theme.common.anim (channel/keyframe colours)
- Rounded corners, separated buttons, white icons, purple-tinted text
Run: blender --background --python scripts/setup_theme.py
"""
import bpy

def rgb(h):
    h = h.lstrip('#')
    return (int(h[0:2],16)/255, int(h[2:4],16)/255, int(h[4:6],16)/255)

def rgba(h, a=1.0):
    r,g,b = rgb(h)
    return (r,g,b,a)

def sc(obj, attr, col, a=1.0):
    val = getattr(obj, attr, None)
    if val is None:
        return
    try:
        if hasattr(val, '__len__'):
            setattr(obj, attr, rgba(col, a) if len(val) == 4 else rgb(col))
        else:
            setattr(obj, attr, rgba(col, a))
    except Exception:
        pass

def sf(obj, attr, v):
    try:
        setattr(obj, attr, v)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────
# VIVID PURPLE-BLUE PALETTE — every value has visible purple cast
# ─────────────────────────────────────────────────────────────────────
VOID    = '#0A0815'   # deepest — blackened violet
BG0     = '#13102E'   # main background — deep indigo-violet
BG1     = '#1A1640'   # panels / boxes — rich violet
BG2     = '#231E55'   # raised panels, list rows — vivid violet
BG3     = '#2D2870'   # input fields, button rest — bright violet
BG4     = '#3A3398'   # button hover — vivid purple-indigo
HDR     = '#100C25'   # header — dark violet (not black)

BTN     = '#2D2870'   # button rest (vivid)
BTN_HOV = '#3A3398'   # button hover (still vivid purple)
BTN_SEL = '#6B4FE8'   # button active (electric indigo)
BTN_TXT = '#FFFFFF'

ACCENT  = '#8B7DF7'   # bright violet accent
ACCENT2 = '#A595FF'   # lighter lavender-violet
LAVEN   = '#B4BEFE'   # blue-leaning lavender (highlights)

BORDER  = '#4538A8'   # vivid purple border (NEVER grey)
BORDER_A= '#6B4FE8'   # active border = btn_sel

TEXT    = '#E8E4FF'   # white with violet tint
SUBTEXT = '#B0A8E8'   # secondary text (violet-tinted, NOT grey)
WHITE   = '#FFFFFF'   # pure white for icons

# Status colours
SUCCESS = '#84E5C0'
WARNING = '#F5C76E'
ERROR   = '#F38BA8'

RND     = 0.4         # rounded corners — visible

# ─────────────────────────────────────────────────────────────────────
# WIDGET HELPER
# ─────────────────────────────────────────────────────────────────────
def apply_wcol(w, inner, inner_sel, outline, outline_sel, item, text, text_sel, rnd=RND):
    if w is None:
        return
    sc(w, 'inner',       inner)
    sc(w, 'inner_sel',   inner_sel)
    sc(w, 'outline',     outline)
    sc(w, 'outline_sel', outline_sel)
    sc(w, 'item',        item)
    sc(w, 'text',        text)
    sc(w, 'text_sel',    text_sel)
    sf(w, 'roundness',   rnd)
    sf(w, 'shaded',      False)
    sf(w, 'shadetop',    0)
    sf(w, 'shadedown',   0)

theme = bpy.context.preferences.themes[0]
ui    = theme.user_interface

# ─────────────────────────────────────────────────────────────────────
# ALL WIDGETS — vivid purple, rounded, bordered
# ─────────────────────────────────────────────────────────────────────
apply_wcol(getattr(ui,'wcol_regular',None),     BTN,    BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_tool',None),        BTN,    BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_radio',None),       BG2,    BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_toggle',None),      BTN,    BTN_SEL, BORDER, BORDER_A, BTN_SEL, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_num',None),         BG3,    BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_numslider',None),   BG3,    BTN_SEL, BORDER, BORDER_A, ACCENT,  TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_option',None),      BG2,    BTN_SEL, BORDER, BORDER_A, BTN_SEL, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_choice',None),      BTN,    BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_menu',None),        BTN,    BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_menu_back',None),   BG1,    BG2,     BORDER, BORDER_A, ACCENT2, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_menu_item',None),   BG1,    BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_pulldown',None),    BG1,    BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_tab',None),         BG2,    BTN_SEL, BORDER, BORDER_A, ACCENT2, SUBTEXT, WHITE, 0.35)
apply_wcol(getattr(ui,'wcol_box',None),         BG1,    BG2,     BORDER, BORDER_A, ACCENT2, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_scroll',None),      BG1,    BTN_SEL, BORDER, BORDER_A, BTN_SEL, TEXT,    WHITE, 0.5)
apply_wcol(getattr(ui,'wcol_progress',None),    BG3,    BTN_SEL, BORDER, BORDER_A, BTN_SEL, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_list_item',None),   BG0,    BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT,    WHITE)
apply_wcol(getattr(ui,'wcol_pie_menu',None),    BG1,    BTN_SEL, BORDER, BORDER_A, ACCENT2, TEXT,    WHITE, 0.4)
apply_wcol(getattr(ui,'wcol_tooltip',None),     VOID,   BG2,     BORDER, BORDER_A, ACCENT2, TEXT,    WHITE)

# State colours
sc(ui, 'wcol_state_color_active',    BTN_SEL)
sc(ui, 'wcol_state_color_selected',  ACCENT)
sc(ui, 'wcol_state_color_alert',     ERROR)

# ─────────────────────────────────────────────────────────────────────
# ICONS — pure white
# ─────────────────────────────────────────────────────────────────────
for attr in ('icon_scene','icon_collection','icon_object','icon_object_data',
             'icon_modifier','icon_shading','icon_folder','icon_autokey'):
    sc(ui, attr, WHITE)
sf(ui, 'icon_alpha',            1.0)
sf(ui, 'icon_saturation',       0.0)
sf(ui, 'icon_border_intensity', 0.0)

# ─────────────────────────────────────────────────────────────────────
# THEME.REGIONS — THE MISSING GREY SOURCES (sidebar, timeline, channels)
# ─────────────────────────────────────────────────────────────────────
regions = getattr(theme, 'regions', None)
if regions:
    # Right sidebar / Properties panel area background
    sb = getattr(regions, 'sidebars', None)
    if sb:
        sc(sb, 'back',      BG1)
        sc(sb, 'tab_back',  BG0)

    # Timeline scrubbing bar (the strip below the viewport)
    sc_ = getattr(regions, 'scrubbing', None)
    if sc_:
        sc(sc_, 'back',                 BG0)
        sc(sc_, 'text',                 TEXT)
        sc(sc_, 'time_marker',          ACCENT)
        sc(sc_, 'time_marker_selected', LAVEN)

    # Channel rows (animation channels left column)
    ch = getattr(regions, 'channels', None)
    if ch:
        sc(ch, 'back',          BG1)
        sc(ch, 'text',          TEXT)
        sc(ch, 'text_selected', WHITE)

    # Asset shelf
    ash = getattr(regions, 'asset_shelf', None)
    if ash:
        sc(ash, 'back',         BG1)

# ─────────────────────────────────────────────────────────────────────
# THEME.COMMON — animation channel + keyframe colours (timeline interior)
# ─────────────────────────────────────────────────────────────────────
common = getattr(theme, 'common', None)
if common:
    anim = getattr(common, 'anim', None)
    if anim:
        sc(anim, 'playhead',              LAVEN)
        sc(anim, 'preview_range',         BTN_SEL,  0.3)
        sc(anim, 'scene_strip_range',     ACCENT,   0.3)
        sc(anim, 'channels',              BG2)        # channel row default bg
        sc(anim, 'channels_sub',          BG1)        # sub-channel row bg
        sc(anim, 'channel_group',         BTN,    0.7)
        sc(anim, 'channel_group_active',  BTN_SEL, 0.5)
        sc(anim, 'channel',               BG2)        # individual channel
        sc(anim, 'channel_selected',      BTN_SEL, 0.5)
        sc(anim, 'keyframe',              ACCENT2)
        sc(anim, 'keyframe_selected',     WHITE)

    curves = getattr(common, 'curves', None)
    if curves:
        for attr in ('handle_vect','handle_align','handle_free','handle_auto','handle_auto_clamped',
                     'handle_sel_vect','handle_sel_align','handle_sel_free','handle_sel_auto',
                     'handle_sel_auto_clamped'):
            v = getattr(curves, attr, None)
            if v is not None:
                sc(curves, attr, BTN_SEL)

# ─────────────────────────────────────────────────────────────────────
# SPACE BACKGROUNDS — every editor
# ─────────────────────────────────────────────────────────────────────
def apply_space(target):
    if target is None:
        return
    sc(target, 'header',         HDR)
    sc(target, 'header_text',    SUBTEXT)
    sc(target, 'header_text_hi', TEXT)
    sc(target, 'back',           BG0)
    sc(target, 'title',          TEXT)
    sc(target, 'text',           TEXT)
    sc(target, 'text_hi',        WHITE)
    sc(target, 'button',         BTN)
    sc(target, 'button_title',   TEXT)
    sc(target, 'button_text',    SUBTEXT)
    sc(target, 'button_text_hi', WHITE)
    sc(target, 'navigation_bar', HDR)
    sc(target, 'tab_active',     BTN_SEL)
    sc(target, 'tab_inactive',   BG2)
    sc(target, 'tab_back',       BG1)
    sc(target, 'tab_outline',    BORDER)

SPACES = [
    'view_3d','graph_editor','dopesheet_editor','nla_editor',
    'image_editor','sequence_editor','node_editor','text_editor',
    'outliner','properties','file_browser','info','statusbar',
    'clip_editor','topbar','preferences',
]
for sp_name in SPACES:
    sp = getattr(theme, sp_name, None)
    if sp is None:
        continue
    apply_space(getattr(sp, 'space', None))
    apply_space(sp)

# ─────────────────────────────────────────────────────────────────────
# 3D VIEWPORT — flat purple background (no grey gradient)
# ─────────────────────────────────────────────────────────────────────
vp = theme.view_3d
sp = getattr(vp, 'space', None)
if sp:
    grad = getattr(sp, 'gradients', None)
    if grad:
        sf(grad, 'background_type', 'SINGLE_COLOR')
        sc(grad, 'high_gradient',    BG0)
        sc(grad, 'gradient',         BG0)
    apply_space(sp)

# Replace every grey vp.* property with themed colour
sc(vp, 'grid',                BG3,    0.6)
sc(vp, 'grid_major',          BG4,    0.7)
sc(vp, 'wire',                BORDER, 0.7)
sc(vp, 'wire_edit',           LAVEN,  0.8)
sc(vp, 'clipping_border_3d',  BORDER, 0.5)
sc(vp, 'object_active',       BTN_SEL)
sc(vp, 'object_selected',     LAVEN)
sc(vp, 'vertex',              WHITE,  0.9)
sc(vp, 'vertex_select',       BTN_SEL)
sc(vp, 'vertex_active',       WHITE)
sc(vp, 'edge_select',         BTN_SEL)
sc(vp, 'face_select',         BTN_SEL, 0.2)
sc(vp, 'face_mode_select',    LAVEN,  0.3)
sc(vp, 'editmesh_active',     LAVEN,  0.35)
sc(vp, 'bone_pose',           BTN_SEL, 0.85)
sc(vp, 'bone_pose_active',    LAVEN,  0.9)
sc(vp, 'bone_solid',          ACCENT2)        # was grey
sc(vp, 'bundle_solid',        ACCENT2)        # was grey
sc(vp, 'gp_wire_edit',        ACCENT)         # was grey
sc(vp, 'gp_vertex',           WHITE)
sc(vp, 'gp_vertex_select',    BTN_SEL)
sc(vp, 'view_overlay',        BORDER, 0.5)
sc(vp, 'transform',           WHITE)
sc(vp, 'normal',              LAVEN)
sc(vp, 'vertex_normal',       BTN_SEL)
sc(vp, 'face',                WHITE,  0.02)
sc(vp, 'camera_path',         ACCENT)

# ─────────────────────────────────────────────────────────────────────
# OUTLINER
# ─────────────────────────────────────────────────────────────────────
ol = theme.outliner
sc(ol, 'active',             BTN_SEL)
sc(ol, 'active_object',      BTN_SEL)
sc(ol, 'selected_object',    LAVEN)
sc(ol, 'selected_highlight', BTN, 0.6)
sc(ol, 'match',              ACCENT)
sc(ol, 'row_alternate',      BG2, 0.3)

# ─────────────────────────────────────────────────────────────────────
# NODE EDITOR
# ─────────────────────────────────────────────────────────────────────
ne = theme.node_editor
sc(ne, 'node_selected',  BTN_SEL)
sc(ne, 'node_active',    LAVEN)
sc(ne, 'wire',           BORDER)
sc(ne, 'wire_select',    BTN_SEL)
sc(ne, 'selected_text',  BTN_SEL)
sc(ne, 'grid',           VOID, 0.9)

# ─────────────────────────────────────────────────────────────────────
# DOPESHEET / TIMELINE — explicit so timeline area has zero grey
# ─────────────────────────────────────────────────────────────────────
ds = theme.dopesheet_editor
sc(ds, 'value_sliders',           BTN_SEL)
sc(ds, 'view_sliders',            ACCENT)
sc(ds, 'dopesheet_channel_clear', BG0)
sc(ds, 'dopesheet_channel',       BG1)
sc(ds, 'dopesheet_subchannel',    BG2)
sc(ds, 'channel_group',           BTN)
sc(ds, 'active_channels_group',   BTN_SEL)
sc(ds, 'keyframe_border',         BORDER_A)
sc(ds, 'keyframe_border_selected',ACCENT2)
sc(ds, 'frame_current',           LAVEN)
sc(ds, 'time_keyframe',           ACCENT2)
sc(ds, 'time_scrub_background',   HDR)
sc(ds, 'time_marker_line',        ACCENT)
sc(ds, 'time_marker_line_selected', LAVEN)

ge = theme.graph_editor
sc(ge, 'handle_sel_vect',     BTN_SEL)
sc(ge, 'handle_sel_free',     LAVEN)
sc(ge, 'handle_sel_auto',     ACCENT)
sc(ge, 'channel_group',       BTN, 0.5)
sc(ge, 'active_channels_group', BTN_SEL)

nla = theme.nla_editor
sc(nla, 'nla_track',           BG2)
sc(nla, 'active_action',       BTN_SEL)
sc(nla, 'active_action_unset', BG1)

# ─────────────────────────────────────────────────────────────────────
# SEQUENCE EDITOR
# ─────────────────────────────────────────────────────────────────────
seq = theme.sequence_editor
sc(seq, 'movie',       BTN_SEL)
sc(seq, 'meta',        LAVEN)
sc(seq, 'scene',       ACCENT)
sc(seq, 'audio',       '#74C7EC')
sc(seq, 'effect',      ACCENT2)
sc(seq, 'color',       '#F38BA8')
sc(seq, 'transition',  LAVEN)

# ─────────────────────────────────────────────────────────────────────
# INFO LOG
# ─────────────────────────────────────────────────────────────────────
inf = theme.info
sc(inf, 'info_warning',       WARNING, 0.12)
sc(inf, 'info_error',         ERROR,   0.12)
sc(inf, 'info_info',          BTN_SEL, 0.12)
sc(inf, 'info_debug',         BG2,     0.4)
sc(inf, 'info_operator',      BG2,     0.35)
sc(inf, 'info_property',      BG1,     0.3)
sc(inf, 'info_warning_text',  WARNING)
sc(inf, 'info_error_text',    ERROR)
sc(inf, 'info_info_text',     TEXT)
sc(inf, 'info_debug_text',    SUBTEXT)
sc(inf, 'info_operator_text', SUBTEXT)
sc(inf, 'info_property_text', SUBTEXT)

# ─────────────────────────────────────────────────────────────────────
# TEXT EDITOR
# ─────────────────────────────────────────────────────────────────────
te = theme.text_editor
sc(te, 'line_numbers_background', BG1)
sc(te, 'selected_text',           BTN_SEL, 0.4)
sc(te, 'cursor',                  LAVEN)
sc(te, 'syntax_string',           SUCCESS)
sc(te, 'syntax_comment',          ACCENT, 0.7)
sc(te, 'syntax_builtin',          LAVEN)
sc(te, 'syntax_special',          BTN_SEL)
sc(te, 'syntax_reserved',         ACCENT)
sc(te, 'syntax_numbers',          '#94E2D5')
sc(te, 'syntax_preprocessor',     ERROR)

# ─────────────────────────────────────────────────────────────────────
# SYSTEM
# ─────────────────────────────────────────────────────────────────────
system = bpy.context.preferences.system
sf(system, 'ui_scale',      1.1)
sf(system, 'ui_line_width',  1)

# ─────────────────────────────────────────────────────────────────────
# KEYMAP + SAVE
# ─────────────────────────────────────────────────────────────────────
try:
    bpy.context.preferences.keymap.active_keyconfig = 'Animora'
except Exception:
    pass

bpy.ops.wm.save_userpref()
print("Animora vivid-purple theme saved.")
