# V2 Roadmap Notes

Forward-looking notes captured from planning/hotfix sessions, recorded here so
future sessions inherit the context without having to re-derive it. These are
notes to build FROM, not things already built — check the code before
assuming any of this landed.

## Sign-in connect watchdog — related follow-up (from the v1.4.3 hotfix)

v1.4.3 fixed the reported symptom: a WS connect that never resolves after a
fresh, foreground sign-in left the user stuck on "Connecting to Animora"
forever (`addons/animora_panel/auth/controller.py`,
`_signin_connect_watchdog_tick`). That fix is deliberately scoped to ONLY the
window between a successful code exchange and `AuthS.CONNECTED`.

Two related windows call the same `connect_ws()` and could theoretically hang
the same way, but were left alone to keep the hotfix to exactly one thing:

- **App-launch restore path** (`controller.register()` → `connect_ws()` on a
  successful token restore). If the WS then never connects, the panel is not
  gated (the user was already signed in and the gate stays closed), so the
  blast radius is smaller — the AI panel just never shows Connected. Still
  worth a bound eventually so a background retry storm doesn't run forever.
- **WS-auth-rejection retry path** (`_on_ws_auth_rejected`'s `"retry"` action
  → `restore_session_async` → `connect_ws()` on success). Same shape.

Neither of these has a user-facing "Retry" affordance today the way the
onboarding gate naturally does once `AuthS.FAILED` is set (its Sign In button
re-enables automatically). Before adding a watchdog to either path, add a
retry/reconnect affordance to the main panel itself
(`addons/animora_panel/panel.py` only renders a hint for `AuthS.CONNECTING`,
nothing for `AuthS.FAILED`) — otherwise a watchdog there would trade "hangs
silently but might eventually recover" for "gives up with no way back in but
restarting Blender," which is worse.

## B1 — Ghost cursor presentation layer (V2/V3 flourish, cosmetic only)

A purely visual layer that makes Animora look like a living artist is
operating the software, while ALL real work stays programmatic (bpy):

- **Rendering**: `bpy.types.SpaceView3D.draw_handler_add` with `POST_PIXEL`
  for a 2D screen-space cursor sprite + UI highlights, `POST_VIEW` for
  3D-anchored object glows; draw via the `gpu` module (NOT deprecated
  `bgl`); force `area.tag_redraw()` each animation tick.
- **Clock**: one long-lived modal operator holding a
  `wm.event_timer_add(~0.016)` timer owns the draw handler; a thread-safe
  queue receives `{action, target_object, screen_region, duration_hint}`
  events from the AI execution layer.
- **Motion**: human-like paths via WindMouse (gravity/wind/max_step params)
  or cubic Bézier with control-point deviation; smoothstep easing per
  segment; small jitter; project object world positions to screen with
  `bpy_extras.view3d_utils.location_3d_to_region_2d`.
- **Sync trick**: deliberately reveal each bpy result only when the ghost
  cursor "arrives," selling the illusion the cursor did the work.
- **Hard rule**: the layer is fail-safe theater — wrapped so any error
  silently drops the overlay and NEVER blocks or corrupts real execution;
  timers and handlers cleaned up on unregister.

## B2 — AI quality upgrades (priority order for V2 work)

1. Prompt caching on the large stable system preamble (immediate ~90% input
   cost reduction on the loop). Cheapest win; can even ship before V2.
2. Implement the artist's-eye checklist as 8 separate VLM aspect-verifiers
   (silhouette, proportion, topology, placement, composition, materials,
   lighting, technical) scored on multi-view renders; select best-of-N by
   combined verifier score.
3. Verifier-guided test-time scaling with a tunable
   generation:verification compute ratio — more candidate variety at low
   budget, more verification at high budget; expose it as a per-task
   quality dial.
4. BlenderRAG: retrieval over the bpy API docs to ground code generation
   (kills hallucinated-API tracebacks), plus a Voyager-style skill library:
   critic-approved bpy snippets stored, embedded, retrieved, and reused —
   compounding quality with no fine-tuning. Use the `.claude/skills`
   SKILL.md format so it doubles as our Claude Code skill library.
5. Constraint-based object placement: the model emits spatial RELATIONS
   (near / in front of / on top of / aligned / facing), a solver computes
   coordinates — instead of the model guessing raw numbers. Proven to beat
   direct-numeric placement.
6. Role-split models: strongest available mid-tier model as the CRITIC
   (verification is where model alignment with human judgment matters
   most); route generation across cheap/mid/top tiers by step difficulty.
7. Self-debug discipline: on a bpy traceback, feed back a TRUNCATED,
   location-annotated error; cap fix attempts at 2-3, then escalate model
   tier instead of looping.
8. Stand up an internal BlenderGym-style benchmark (start-goal scene pairs
   across geometry / placement / lighting / material tasks) so every
   quality change is measured, not vibes.

Grounding rule for all of it: critique must always be anchored to a real
render or a real execution result — never ungrounded self-reflection.
