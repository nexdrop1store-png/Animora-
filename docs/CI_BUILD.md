# CI Build — three installers (Windows / macOS / Linux)

The `.github/workflows/build.yml` workflow builds the Animora installers on
each OS's native runner. **There is no cross-compilation** — Blender (and our
native `space_animora` editor + gizmo/UI changes) must be compiled on each
target OS. One push (a `v*` tag) or a manual run produces all three.

## How to run it
- **Manual:** Actions → "Build Installers" → Run workflow (toggle `sign` if
  signing secrets are configured).
- **Release:** push a tag `vX.Y.Z` → builds all three **and** creates a GitHub
  Release with the installers attached.

## What each job does
1. Clone vanilla Blender `v5.1.1` + `git lfs pull` (the binary assets).
2. Apply our native delta: `patches/animora-native-full.patch` +
   `patches/native-overlay/` (the editor, splash, font).
3. `scripts/rebrand.py` — assets, strings, AI-panel inject.
4. `scripts/build.py` — cmake compile (Release).
5. **Secrets gate** — `scripts/check_no_secrets.py` fails the build if any
   credential would ship (no `.env`, no `ABSK…`/`sk-ant-…` keys in the tree).
6. Package: Windows → `stage_for_installer.py` + **Inno** (`Animora.iss`);
   macOS → `installer/macos/build_dmg.sh`; Linux →
   `installer/linux/build_appimage.sh`.
7. Sign/notarize (opt-in, see secrets) → upload artifact.

## Secrets to configure (repo → Settings → Secrets → Actions)
Signing is **optional** — without it the installers ship unsigned (Windows
shows SmartScreen; macOS shows Gatekeeper). For a clean public release:

| Secret | For |
|---|---|
| `WINDOWS_CERT_PATH`, `WINDOWS_CERT_PASSWORD` | Windows Authenticode signing (kills SmartScreen) |
| `MACOS_NOTARIZATION_APPLE_ID`, `…_PASSWORD`, `…_TEAM_ID` | macOS notarization (needs an Apple Developer account, $99/yr) |

**The Bedrock/Anthropic keys are NOT build secrets** — they live only on the
production server. The installer must never contain them; the secrets gate
enforces that.

## Known caveats (this is a heavy build — expect first-run iteration)
- **Disk.** A Blender source build needs ~25 GB. The Linux job reclaims space
  first; **Windows/macOS hosted runners may run out** — if so, switch those
  matrix entries to [larger runners](https://docs.github.com/actions/using-github-hosted-runners/about-larger-runners)
  or self-hosted runners.
- **Time/cost.** 1-3 h per OS; macOS runner minutes bill at 10×. Consider
  caching the cloned fork + `lib/` between runs.
- **`lib/` fetch.** The "Fetch Blender precompiled libraries" step uses
  Blender's `make_update`; the exact flags differ per Blender version and may
  need adjustment on the first real run.
- **The Windows `build.py` packaging path is bypassed** in CI: we call Inno
  directly because `build.py:_package_windows` still references a stale NSIS
  script (`animora.nsi`) that doesn't exist. Fix that in `build.py` when
  convenient so local Windows packaging matches CI.
- **Recording build stays separate.** `freeze_backend.py` (which embeds a key
  for the cofounder's offline captures) is **never** run in this workflow —
  the public installers are key-free by construction.
