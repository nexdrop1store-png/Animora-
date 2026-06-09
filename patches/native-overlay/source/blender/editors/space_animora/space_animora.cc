/* SPDX-FileCopyrightText: 2026 Animora Technologies
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup spanimora
 *
 * Animora — native AI assistant editor (first-class space type).
 *
 * The main region delegates its content to Python panels registered with
 * `bl_space_type = 'ANIMORA'`, `bl_region_type = 'WINDOW'`. The C++ side only
 * provides the area scaffolding (init/free/duplicate) and the standard
 * panel-driven main region (`ED_region_panels_*`).
 */

#include <cstring>

#include "MEM_guardedalloc.h"

#include "BLI_listbase.h"
#include "BLI_string_utf8.h"
#include "BLI_utildefines.h"

#include "BKE_context.hh"
#include "BKE_screen.hh"

#include "ED_screen.hh"
#include "ED_space_api.hh"

#include "WM_api.hh"
#include "WM_types.hh"

#include "UI_resources.hh"
#include "UI_view2d.hh"

#include "BLO_read_write.hh"

#include "DNA_space_types.h"

namespace blender {

/* ---------- Space callbacks ---------- */

static SpaceLink *animora_create(const ScrArea * /*area*/, const Scene * /*scene*/)
{
  ARegion *region;
  SpaceAnimora *spanim;

  spanim = MEM_new<SpaceAnimora>("init Animora");
  spanim->spacetype = SPACE_ANIMORA;

  /* Header region. */
  region = BKE_area_region_new();
  BLI_addtail(&spanim->regionbase, region);
  region->regiontype = RGN_TYPE_HEADER;
  region->alignment = (U.uiflag & USER_HEADER_BOTTOM) ? RGN_ALIGN_BOTTOM : RGN_ALIGN_TOP;

  /* Main window region — panel-driven (Python panels render here). */
  region = BKE_area_region_new();
  BLI_addtail(&spanim->regionbase, region);
  region->regiontype = RGN_TYPE_WINDOW;

  return reinterpret_cast<SpaceLink *>(spanim);
}

static void animora_free(SpaceLink * /*sl*/) {}

static void animora_init(wmWindowManager * /*wm*/, ScrArea * /*area*/) {}

static SpaceLink *animora_duplicate(SpaceLink *sl)
{
  SpaceAnimora *spanim_new = MEM_dupalloc(reinterpret_cast<SpaceAnimora *>(sl));
  return reinterpret_cast<SpaceLink *>(spanim_new);
}

static void animora_operatortypes() {}

static void animora_keymap(wmKeyConfig *keyconf)
{
  WM_keymap_ensure(keyconf, "Window", SPACE_EMPTY, RGN_TYPE_WINDOW);
  WM_keymap_ensure(keyconf, "Animora", SPACE_ANIMORA, RGN_TYPE_WINDOW);
}

static void animora_main_region_init(wmWindowManager *wm, ARegion *region)
{
  wmKeyMap *keymap;

  /* Lock the View2D to vertical scroll only.
   *
   * Without this, Blender's default View2D config maps wheel/pinch to zoom.
   * Touchpad two-finger scroll on the Animora panel was zooming text size up
   * and down instead of scrolling through the conversation. Mirrors the
   * pattern in space_userpref/space_userpref.cc:115 — the Preferences
   * editor uses the same vertical-only setup for the same reason. */
  region->v2d.scroll = V2D_SCROLL_RIGHT | V2D_SCROLL_VERTICAL_HIDE;

  keymap = WM_keymap_ensure(wm->runtime->defaultconf, "Animora", SPACE_ANIMORA, RGN_TYPE_WINDOW);
  WM_event_add_keymap_handler(&region->runtime->handlers, keymap);

  ED_region_panels_init(wm, region);
}

static void animora_main_region_listener(const wmRegionListenerParams *params)
{
  ARegion *region = params->region;
  const wmNotifier *wmn = params->notifier;

  /* Redraw on any AI-relevant change. We listen broadly because the AI panel
   * surface is driven entirely by Python state. */
  switch (wmn->category) {
    case NC_SPACE:
    case NC_WM:
    case NC_WINDOW:
    case NC_SCREEN:
      ED_region_tag_redraw(region);
      break;
  }
}

static void animora_header_region_init(wmWindowManager * /*wm*/, ARegion *region)
{
  ED_region_header_init(region);
}

static void animora_header_region_draw(const bContext *C, ARegion *region)
{
  ED_region_header(C, region);
}

static void animora_header_listener(const wmRegionListenerParams *params)
{
  ED_region_tag_redraw(params->region);
}

static void animora_space_blend_write(BlendWriter *writer, SpaceLink *sl)
{
  writer->write_struct_cast<SpaceAnimora>(sl);
}

/* ---------- Spacetype registration ---------- */

void ED_spacetype_animora()
{
  std::unique_ptr<SpaceType> st = std::make_unique<SpaceType>();
  ARegionType *art;

  st->spaceid = SPACE_ANIMORA;
  STRNCPY_UTF8(st->name, "Animora");

  st->create = animora_create;
  st->free = animora_free;
  st->init = animora_init;
  st->duplicate = animora_duplicate;
  st->operatortypes = animora_operatortypes;
  st->keymap = animora_keymap;
  st->blend_write = animora_space_blend_write;

  /* regions: main window — panel-driven (Python addon supplies the UI). */
  art = MEM_new_zeroed<ARegionType>("spacetype animora main region");
  art->regionid = RGN_TYPE_WINDOW;
  art->keymapflag = ED_KEYMAP_UI | ED_KEYMAP_VIEW2D | ED_KEYMAP_FRAMES;
  art->init = animora_main_region_init;
  art->layout = ED_region_panels_layout;
  art->draw = ED_region_panels_draw;
  art->listener = animora_main_region_listener;
  BLI_addhead(&st->regiontypes, art);

  /* regions: header. */
  art = MEM_new_zeroed<ARegionType>("spacetype animora header region");
  art->regionid = RGN_TYPE_HEADER;
  art->prefsizey = HEADERY;
  art->keymapflag = ED_KEYMAP_UI | ED_KEYMAP_VIEW2D | ED_KEYMAP_FRAMES | ED_KEYMAP_HEADER;
  art->listener = animora_header_listener;
  art->init = animora_header_region_init;
  art->draw = animora_header_region_draw;
  BLI_addhead(&st->regiontypes, art);

  BKE_spacetype_register(std::move(st));
}

}  // namespace blender
