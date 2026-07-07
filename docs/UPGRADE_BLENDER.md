# Upgrading the Blender base

Animora is built on top of a forked Blender (currently 5.1.1). When a
new Blender minor or major release ships, this is the procedure for
moving Animora onto it.

The design goal of this repo's layout is that an upgrade should be a
small number of mechanical steps — **never** a "rewrite the AI panel"
or "re-merge a fork of Blender." If you find yourself doing manual
merges of Animora-specific logic into Blender source, treat that as a
regression in this design and push the offending change back out of
`blender-fork/` into the addon or rebrand layer.

## What is and isn't coupled to a Blender version

| Component                            | Coupling                | Effort to upgrade  |
|--------------------------------------|-------------------------|--------------------|
| `addons/animora_panel/` (AI panel)   | Stable `bpy` APIs only  | None (auto)        |
| `ai-backend/` (FastAPI service)      | Not coupled at all      | None               |
| `addons/animora_panel/auth/`         | Not coupled at all      | None               |
| `website/`                           | Not coupled at all      | None               |
| `assets/branding/` (icons, splash)   | Not coupled             | None               |
| `patches/animora-native-full.patch` + `patches/native-overlay/` | Coupled to source lines | ~15 min (3-way merge + overlay copy) |
| `scripts/rebrand.py` string map      | Coupled if upstream changes labels | ~10 min audit |
| `scripts/animora_config.py` constants | Coupled (by design)    | One-line bump      |
| `installer/windows/inno/Animora.iss` | Coupled (one #define)   | One-line bump      |

The AI panel is the largest body of Animora-specific code and it sits
in `addons/animora_panel/` — completely outside the Blender fork tree.
`scripts/rebrand.py` copies it into the fork's bundled addons dir at
build time, so the fork is treated as a **build artifact, not a source
of truth.** Re-checking out the fork at a new tag never destroys
Animora-specific work.

## Procedure

Assume you're moving from Blender X.Y → X.(Y+1) (e.g. 5.1 → 5.2).

### 1. Bump the version constants

Two files, identical value:

```python
# scripts/animora_config.py
BLENDER_VERSION = "5.2"
BLENDER_FULL_VERSION = "5.2.0"
```

```
; installer/windows/inno/Animora.iss
#define BlenderVersion "5.2"
#define MyAppVersion   "5.2.0"
```

That's it. `sync_addon.py`, `rebrand.py`, and the Inno installer all
read from these.

### 2. Re-fetch the Blender source at the new tag

```bash
# blender-fork/ is .gitignored. Wipe and re-clone at the target tag.
rm -rf blender-fork
git clone --depth 1 --branch v5.2.0 https://projects.blender.org/blender/blender.git blender-fork

# Pull precompiled libs (vulkan, OpenEXR, etc.)
cd blender-fork
make.bat update    # Windows; or `make update` on macOS/Linux
cd ..
```

### 3. Re-apply the native delta

```bash
cd blender-fork
git apply --ignore-whitespace ../patches/animora-native-full.patch
cd ..
cp -r patches/native-overlay/* blender-fork/
```

If the patch reports conflicts/rejects, the upstream code at the patch
sites moved. Fix the conflicts in the working tree, then regenerate the
patch so the next upgrader gets the corrected version:

```bash
cd blender-fork
git diff --binary <new-baseline-tag> > ../patches/animora-native-full.patch
```

`--binary` is required — several patched files are binary
(`splash.png`, `startup.blend`, the `.ico`s); a plain `git diff` shows
"Binary files differ" and silently drops them. `patches/native-overlay/`
holds files that aren't a diff against anything upstream (they're new):
`source/blender/editors/space_animora/` (the native AI editor),
`release/datafiles/splash_2x.png`, `release/datafiles/fonts/Geist.woff2`.
Do not add `scripts/addons_core/animora_panel/` there — `rebrand.py`
regenerates it every build from `addons/animora_panel/`; capturing it
here would just create a second, driftable copy. See `patches/README.md`
for the full safe re-clone procedure.

### 4. Audit `scripts/rebrand.py` for stale string mappings

The rebrand script substitutes `"Blender" → "Animora"` across the
Blender source. If upstream changed any of the user-facing labels
(`"About Blender"` → `"About this Blender"`, say), the mapping in
`STRING_REPLACEMENTS` will silently miss them.

Audit:

```bash
python scripts/rebrand.py --dry-run | tee rebrand-dryrun.log
# Then grep blender-fork after a build for any remaining user-visible
# "Blender" strings:
grep -rn '"Blender' blender-fork/build/ 2>/dev/null | head -40
```

Add any missed strings to `STRING_REPLACEMENTS`.

### 5. Build

```bash
python scripts/build.py            # rebrand → cmake → compile → package
```

`rebrand.py` will inject `addons/animora_panel/` into
`blender-fork/scripts/addons_core/animora_panel/` automatically as
step 1 — no manual copy needed.

### 6. Smoke-test on a clean machine

This is the most important step. The OpenGL regression
([CLAUDE.md / stage_for_installer.py docstring]) was invisible on the
dev machine and only surfaced on a different laptop. Get the new
installer onto at least one machine you've never built on, and verify:

- It installs without errors
- It launches without "OpenGL 4.3 or higher required"
- The Animora AI panel appears in the N-panel of the 3D viewport
- The panel can connect to `ai-backend/` and stream a response

If any of those fail, do not ship the upgrade.

## What if the AI panel needs version-specific code?

The AI panel currently uses only stable `bpy` APIs (no
`bpy.app.version` checks anywhere). If a future Blender release breaks
an API the panel depends on, the policy is:

1. **Prefer feature detection** over version checks:
   ```python
   if hasattr(bpy.types.Scene, "view_layers"):
       ...  # 2.80+ path
   else:
       ...  # legacy fallback
   ```
2. **Last resort, gate on version:**
   ```python
   if bpy.app.version >= (5, 2, 0):
       ...
   ```
   Keep these gates in one place (a `compat.py` module) so the next
   upgrader can find and audit them.

Never branch on version inside operator/panel `register()` functions;
the import-time cost compounds and the branches are hard to keep tested.
