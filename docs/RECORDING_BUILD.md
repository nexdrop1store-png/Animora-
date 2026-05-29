# Recording Build — turnkey Animora installer for the cofounder

A **recording build** is a single `Animora-Setup.exe` that the cofounder
installs and uses with **zero setup**: no sign-in, no Python, no API key to
paste. It bundles a local AI engine that auto-starts, auto-connects, and
**records every turn** to a folder on his Desktop that he zips and sends back.

This exists because the dev VM has no GPU (Animora's viewport can't render a
real scene), so reference sessions (Quality Plan Sprint 4C) must be captured
on the cofounder's real hardware.

---

## How it works

| Piece | What it does |
|---|---|
| [`ai-backend/bundled_server.py`](../ai-backend/bundled_server.py) | The shipped engine entrypoint. In-memory Redis + permissive auth (no Redis, no JWT), forces `ANIMORA_RECORD_SESSIONS=1` + Bedrock, writes recordings to `Desktop\Animora Recordings`. Reads the Bedrock key from `animora_backend.env` next to the exe. |
| [`scripts/freeze_backend.py`](../scripts/freeze_backend.py) + [`ai-backend/animora_backend.spec`](../ai-backend/animora_backend.spec) | Freeze the engine into `build/backend-dist/animora-backend/` with PyInstaller. |
| [`addons/animora_panel/bundle.py`](../addons/animora_panel/bundle.py) | On Animora startup, detects the bundle, launches the engine (`CREATE_NO_WINDOW`), polls `/health`, then auto `dev_signin()` + connect. No sign-in UI. |
| [`installer/windows/inno/Animora.iss`](../installer/windows/inno/Animora.iss) | Ships the frozen engine to `{app}\engine\` and the `bundle_config.json` marker into the addon dir. The marker's presence is what flips the addon into recording mode. |

**Bundle mode is opt-in by artifact presence.** If `build/backend-dist/`
doesn't exist (a normal/production build that never ran the freeze step),
the Inno entries are skipped (`skipifsourcedoesntexist`) and the addon finds
no `bundle_config.json` — so it behaves exactly like today's production
addon. The same `.iss` produces either build.

---

## Build recipe (on a Windows machine with the toolchain)

Run from the repo root, in the same venv that can run `dev_server.py`:

```powershell
# 0. One-time: install the freezer
python -m pip install pyinstaller

# 1. Compile Animora (Blender fork). Slow; needs blender-fork present.
python scripts/build.py --platform windows --config Release

# 2. Stage the Blender tree (rebrand blender.exe -> Animora.exe, strip pdbs)
python scripts/stage_for_installer.py

# 3. Freeze the AI engine -> build/backend-dist/animora-backend/
#    Picks up AWS_BEARER_TOKEN_BEDROCK from ai-backend/.env automatically.
python scripts/freeze_backend.py

# 4. Compile the installer (Inno Setup 6) -> dist/Animora-Setup.exe
ISCC.exe installer\windows\inno\Animora.iss
```

Ship `dist\Animora-Setup.exe`.

> **Before step 4, confirm the key landed:** open
> `build\backend-dist\animora-backend\animora_backend.env` and check
> `AWS_BEARER_TOKEN_BEDROCK=ABSK...` is populated. If it's blank, the freeze
> script logged a warning — paste the key in manually, or fix `ai-backend/.env`
> and re-run step 3.

### Verifying the engine without a GPU (on the dev VM)

You can validate everything except the Animora GUI here:

```powershell
# frozen engine boots + serves health
build\backend-dist\animora-backend\animora-backend.exe
# in another shell:
curl http://127.0.0.1:8000/health      # -> {"status":"ok",...}
```

The full chain (install → launch Animora → engine auto-starts → panel
auto-connects → scene builds → recording lands) **must** be smoke-tested on a
machine with a real GPU — i.e. the cofounder's, or a cloud GPU instance.

---

## What to tell the cofounder

> 1. Run **Animora-Setup.exe** and click through the installer.
> 2. Open **Animora** from the Start menu / desktop icon. Wait a few seconds —
>    the panel on the right shows "Starting Animora's engine…", then
>    "Recording mode — connected".
> 3. In the Animora panel, type what you want to build (e.g. *"build a wooden
>    chair"*, *"make a sci-fi laser pistol"*) and press Enter. Let it finish.
> 4. Build **10 different things** — vary them (a vehicle, a character, a room,
>    a prop, a lit scene, etc.). One per fresh file is cleanest (File → New).
> 5. When done, open the **"Animora Recordings"** folder on your **Desktop**,
>    right-click → **Send to → Compressed (zipped) folder**, and send me the zip.

That's the whole ask. He needs no account, no key, no terminal.

### What gets recorded

Each turn lands in `Desktop\Animora Recordings\cofounder\turn_NNN.json` plus
the HD viewport PNGs. Format is documented in
[`docs/SESSION_FORMAT.md`](SESSION_FORMAT.md). The engine also writes
`animora_engine.log` in that folder — useful if something misbehaves.

---

## ⚠ SECURITY — rotate the Bedrock key after capture

The Bedrock key is shipped **inside** the installer (in `animora_backend.env`
next to the engine exe) and is **extractable** by anyone who has the installer.
This is an accepted trade-off for a turnkey build handed to a trusted
cofounder — **but rotate the key in the AWS Bedrock console once the capture
sessions are done.** Don't post this installer publicly.

## Known limitations

- **Shared Bedrock quota.** The bundled key is the same rate-limited account.
  The engine retries 429s with a 15–90 s backoff (see
  [`anthropic_client.py`](../ai-backend/anthropic_client.py)), but a burst of
  hero-asset turns may be slow. Ask the cofounder to space sessions out, or
  land the AWS quota increase first.
- **GPU required.** The recording build runs the AI fine anywhere, but
  Animora's viewport still needs OpenGL 4.3+ to display results. A GPU-less
  machine will hang on render exactly like the dev VM.
- **Windows only.** macOS/Linux recording builds would need their own freeze +
  installer wiring (not done).
