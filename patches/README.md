# Animora Native Patches

Patches applied on top of vanilla Blender 5.1.1 source to produce the Animora binary.

## Why patches and not a full fork?

The Blender source tree is ~7 GB and contains files that exceed GitHub's 100 MB single-file limit. Maintaining it as a vendored copy in this repo would make clones impractical and run into LFS bandwidth limits. Instead, this repo ships:

- The Animora-specific code (addons, backend, auth, website, scripts, assets)
- Small surgical patches against upstream Blender
- A build script (`scripts/rebrand.py` + `scripts/build.py`) that fetches Blender, applies patches, and produces the Animora binary

## Setup

```bash
# Clone Blender source separately (NOT into this repo)
git clone --depth 1 --branch v5.1.1 https://projects.blender.org/blender/blender.git blender-fork

# Apply Animora native patches
cd blender-fork
git apply ../patches/animora-native.patch
cd ..

# Apply string-level rebrand (non-destructive, runs every build)
python scripts/rebrand.py

# Build
python scripts/build.py
```

## Patch contents

`animora-native.patch` modifies four files:

| File | Change |
|---|---|
| `source/blender/windowmanager/intern/wm_window.cc` | Window title shows `"Animora"` (no version number) |
| `source/blender/windowmanager/intern/wm_init_exit.cc` | Terminal quit message: `"Blender quit"` → `"Animora quit"` |
| `source/blender/editors/interface/interface_style.cc` | Increase widget spacing (`buttonspacey`, `columnspace`, etc.) |
| `source/blender/editors/interface/interface_layout.cc` | Add gap between aligned buttons so they no longer visually touch |

String-level rebrands (Blender → Animora across menus, dialogs, etc.) are handled separately by `scripts/rebrand.py` and don't need patches — that script is non-destructive and runs before every build.

## Updating patches

When you make new changes to the Blender source:

```bash
cd blender-fork
git diff HEAD -- <changed files> > ../patches/animora-native.patch
```

Keep patches surgical — string changes belong in `scripts/rebrand.py` so they survive Blender version bumps without merge conflicts.
