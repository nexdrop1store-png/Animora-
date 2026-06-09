# Animora Native Patches

Patches applied on top of vanilla Blender 5.1.1 source to produce the Animora binary.

> ⚠️ **2026-06 — IMPORTANT, READ BEFORE RE-CLONING.** The old `animora-native.patch`
> below claimed the Animora delta was 4 files. That was badly out of date: the
> live fork actually contained **53 modified native source files plus a whole new
> native editor (`space_animora`)** and DNA/RNA registration. A naive re-clone
> would have **destroyed all of it.**
>
> The complete native delta is now captured in the repo:
> - **`patches/animora-native-full.patch`** — all 53 modified `.cc/.h/CMakeLists`
>   files (generated with `--ignore-all-space`, so apply with
>   `git apply --ignore-whitespace`).
> - **`patches/native-overlay/`** — new files a patch can't represent:
>   `source/blender/editors/space_animora/` (the native AI editor), the Animora
>   splash (`release/datafiles/splash_2x.png`), and the Geist font.
>
> The corrupted sculpt brush `.blend`s and icon datafiles were **deliberately
> excluded** — a fresh clone restores upstream versions, which is what fixes the
> grey viewport gizmos + broken sculpting.

## Safe re-clone procedure (use THIS, not the old steps below)

```bash
# 1. Clone vanilla Blender into a FRESH dir (do NOT delete the old fork yet).
git clone --depth 1 --branch v5.1.1 \
    https://projects.blender.org/blender/blender.git blender-fork-fresh

# 2. Apply the complete Animora native delta.
cd blender-fork-fresh
git apply --ignore-whitespace ../patches/animora-native-full.patch
cd ..
# 3. Overlay the new native files (the editor + splash + font).
#    (robocopy on Windows / cp -r on macOS+Linux)
cp -r patches/native-overlay/* blender-fork-fresh/

# 4. String rebrand + AI-panel inject, then build.
python scripts/rebrand.py            # reads FORK_ROOT — point it at the fresh tree
python scripts/build.py              # `make update` pulls lib/ deps, then compiles

# 5. ONLY after the fresh build is verified good: swap.
#    mv blender-fork blender-fork-corrupt-bak ; mv blender-fork-fresh blender-fork
```

This keeps the old (corrupted) fork intact until the fresh one is proven, so we
can never end up with no working tree.

---

## Legacy notes (the original, now-superseded 4-file patch)

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
