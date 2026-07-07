# Animora Native Patches

Patches applied on top of vanilla Blender 5.1.1 source to produce the Animora binary.

> ⚠️ **2026-07 — IMPORTANT, READ BEFORE RE-CLONING.** The complete native delta
> is captured in the repo:
> - **`patches/animora-native-full.patch`** — **70 modified** `.cc/.h/.rc/
>   CMakeLists` files (generated with `git diff --binary`, so apply with
>   `git apply --ignore-whitespace`; `--binary` matters because several
>   patched files are binary — see the gotcha below).
> - **`patches/native-overlay/`** — new files a patch can't represent:
>   `source/blender/editors/space_animora/` (the native AI editor) and the
>   Geist font.
>
> **`release/datafiles/splash.png` and `release/datafiles/startup.blend` are
> deliberately excluded from the patch.** Both are Git-LFS-tracked
> (`diff=lfs` in `.gitattributes`), and `git diff --binary` does not produce
> a usable patch hunk for LFS-filtered paths — applying the earlier patch
> silently left the *pointer* content in place instead of the real asset.
> Both are instead reproduced by `scripts/rebrand.py`'s `copy_assets()` /
> `STARTUP_COPY`, which plainly `shutil.copy2`s them from
> `assets/branding/splash.png` and `assets/startup/startup.blend` — no git
> diff involved, no LFS interaction, no gotcha. (`assets/startup/startup.blend`
> itself was missing from the repo until 2026-07 — `STARTUP_COPY` was a
> silent no-op the whole time this comment didn't exist. Fixed by populating
> that source file from the working fork tree.)
>
> The corrupted sculpt brush `.blend`s and icon datafiles were **deliberately
> excluded** — a fresh clone restores upstream versions, which is what fixes the
> grey viewport gizmos + broken sculpting.
>
> **Verifying a patch regen**: `git apply`'s binary-hunk support is required —
> plain GNU `patch` cannot parse `GIT binary patch` hunks and will silently
> leave binary files (the `.ico`s, etc.) at their pre-patch content with no
> error. Always test-apply with `git apply`, never `patch`.

## Safe re-clone procedure

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

## Why patches and not a full fork?

The Blender source tree is ~7 GB and contains files that exceed GitHub's 100 MB single-file limit. Maintaining it as a vendored copy in this repo would make clones impractical and run into LFS bandwidth limits. Instead, this repo ships:

- The Animora-specific code (addons, backend, auth, website, scripts, assets)
- A single native patch + overlay directory against upstream Blender
- A build script (`scripts/rebrand.py` + `scripts/build.py`) that fetches Blender, applies the patch, and produces the Animora binary

String-level rebrands (Blender → Animora across menus, dialogs, etc.) are handled separately by `scripts/rebrand.py` and don't need to be in the patch — that script is non-destructive and runs before every build, so it survives a Blender version bump without merge conflicts. Only genuine structural changes (new files, DNA/RNA registration, widget-spacing tweaks, etc.) belong in `animora-native-full.patch`.

## Updating the patch

When you make new changes to the Blender source, regenerate from the current
working tree (HEAD is always the pristine upstream baseline — this repo's
`blender-fork` never gets its own commits):

```bash
cd blender-fork
git diff --binary HEAD -- . \
    ':(exclude)release/datafiles/splash.png' \
    ':(exclude)release/datafiles/startup.blend' \
    > ../patches/animora-native-full.patch
cd ..
```

Exclude any other LFS-tracked path (check `.gitattributes` for `filter=lfs`)
that's already reproduced by `rebrand.py`'s `ASSET_COPIES`/`STARTUP_COPY` —
see the warning banner at the top of this file for why. If you add a
genuinely new file (not a modification to an existing upstream file), put it
in `patches/native-overlay/` instead — a diff can't represent a brand-new
file's creation in a way `git apply` reliably re-creates from a bare clone.

**Before trusting a regenerated patch**, run the acceptance test: archive the
current `blender-fork` HEAD tree to a scratch dir (`git archive HEAD | tar -x
-C scratch/`), apply the new patch with `git apply --ignore-whitespace`
(not plain `patch` — see the warning above), copy in `native-overlay/`, and
diff the result against the live `blender-fork` working tree for every file
the patch touches. They should match exactly (modulo line endings).
