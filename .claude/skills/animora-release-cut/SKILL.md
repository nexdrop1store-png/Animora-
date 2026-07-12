---
name: animora-release-cut
description: Use when cutting an Animora release — version bump, full build, signing, packaging, installer smoke test, publishing — "cut a release", "bump the version", "build the installer", "sign the exe", "release checklist", "tag a version", "smoke test the build". The exact pipeline, in order, with the pitfalls that have already burned a build.
---

# Animora release cut

## 0. Preconditions
- CI green: pytest suite + eval regression gate (`eval.yml`) passing on `main`. A red eval gate blocks the cut — no exceptions.
- Disk: a Blender source build needs ~25 GB free per OS. **A V1 build already died to `IOException: not enough space on the disk` (`build_log_v1.0.txt`)** — check before, not during: `Get-PSDrive C`.
- Secrets present where signing runs: `WINDOWS_CERT_PATH/PASSWORD`, `MACOS_NOTARIZATION_APPLE_ID/PASSWORD/TEAM_ID` (CI: repo secrets; the workflow ships unsigned with a warning if absent).

## 1. Version bump (three files, one commit)
- `scripts/animora_config.py` — `ANIMORA_VERSION` (+ `BLENDER_VERSION` if rebasing the fork)
- `installer/windows/inno/Animora.iss` — `BlenderVersion` / app version
- `.github/workflows/build.yml` — `BLENDER_TAG` (only when rebasing; keep in sync with animora_config)
Commit: `Release: bump to vX.Y.Z`.

## 2. Build (per OS — no cross-compilation exists for Blender)
Local Windows: `python scripts/build.py --platform windows --config Release`
(steps: rebrand → cmake configure → compile → package; piecewise reruns via `--skip-rebrand --skip-compile --smoke-test`).
CI (preferred): Actions → "Build Installers" → workflow_dispatch (sign=true) or push tag `vX.Y.Z`. CI clones the fork at `BLENDER_TAG`, LFS-pulls, applies `patches/animora-native-full.patch` + `patches/native-overlay/`, rebrands, compiles (1–3 h/OS), stages, packages.
The native delta MUST apply cleanly — if `git apply` fails, the fork tag moved or the patch rotted; fix the patch first (see `patches/README.md`), never hand-edit the fork.

## 3. Stage + verify (Windows)
```
python scripts/stage_for_installer.py      # renames/stages runtime, EXCLUDES dev_server.py
build\windows\animora-stage\Animora.exe --background --version           # must run
build\windows\animora-stage\Animora-launcher.exe --background --version  # must run
python scripts/check_no_secrets.py build/windows/animora-stage            # hard gate
```
Then Inno: `ISCC.exe installer\windows\inno\Animora.iss` → `dist/Animora-Setup.exe`. macOS `installer/macos/build_dmg.sh` + notarize/staple; Linux `installer/linux/build_appimage.sh`.

## 4. Clean-machine smoke checklist (per OS, no dev tools installed)
1. Install from the signed artifact (no SmartScreen/Gatekeeper block once signed)
2. Launch → **Animora-only branding**: window title, splash, About, `%APPDATA%\Animora Technologies\Animora\` — zero user-visible "Blender" (GPL credit screen is the only sanctioned mention)
3. Onboarding gate → Sign in via browser loopback → gate dismisses
4. One full AI task ("make a wooden chair") — loop visibly runs: geometry lands, screenshot/critique cycle, clean finish, single Ctrl-Z undoes an iteration
5. (Paid era) trial starts, meter visible, payment path works in test mode
6. Relaunch: session restores silently (no re-sign-in)
File association: `.anim`/startup files open in Animora (`animora_register_anim.reg` applied by installer).

## 5. Publish
- Tag `vX.Y.Z` → CI release job attaches installers to the GitHub Release (requires green build matrix)
- Website downloads page (repo `tc-byte/animora`, Vercel) → point at the new artifacts
- Updater: confirm an existing older install picks up the new version before announcing
- Post-release: verify live backend compatibility (`https://eatanimora-animora-backend.hf.space/health`, addon default URLs in `preferences.py`)

## Known pitfalls
- `blender-fork/` is gitignored (~7 GB): local builds need it cloned + patched first; CI does this fresh each run.
- Build tree still names the binary `blender.exe`; the rename happens at STAGING — never smoke-test from `build/.../bin` directly.
- Stale DLLs in a reused stage dir shipped broken once — staging cleans, so always re-run `stage_for_installer.py` after a recompile (commit `153b912`).
- Hosted runners are disk-tight for Blender: the Linux job reclaims space first; Windows/macOS may need larger/self-hosted runners (`docs/CI_BUILD.md`).
