# Animora session recording format

Sprint 4 of the Quality Plan introduced an opt-in session recorder
that captures every WebSocket turn as a structured JSON file plus the
HD viewport screenshot from each agentic-loop iteration. The output is
designed to be mined by two downstream scripts:

- [`scripts/recordings_to_benchmarks.py`](../scripts/recordings_to_benchmarks.py)
  — converts recorded turns into draft `Benchmark` entries for the
  eval harness.
- [`scripts/recordings_to_few_shot.py`](../scripts/recordings_to_few_shot.py)
  — extracts (SPEC → script → verdict) triples for inclusion in
  persona prompts as worked examples.

The recorder is also useful as a debugging tool — open
`recordings/<session_id>/turn_003.json` to see exactly what the
classifier picked, what the SPEC was, which iterations ran, and what
the artist's-eye verdict was.

## Enabling the recorder

Set `ANIMORA_RECORD_SESSIONS=1` before launching the backend.

**bash / zsh (macOS / Linux):**
```bash
cd ai-backend
ANIMORA_RECORD_SESSIONS=1 python dev_server.py
```

**PowerShell (Windows):**
```powershell
cd ai-backend
$env:ANIMORA_RECORD_SESSIONS = "1"
python dev_server.py
```

**cmd.exe (Windows):**
```cmd
cd ai-backend
set ANIMORA_RECORD_SESSIONS=1
python dev_server.py
```

Optional: override the output directory.

```bash
# bash
ANIMORA_RECORDINGS_DIR=/var/lib/animora/captures \
ANIMORA_RECORD_SESSIONS=1 \
python dev_server.py
```
```powershell
# PowerShell
$env:ANIMORA_RECORDINGS_DIR = "C:\path\to\captures"
$env:ANIMORA_RECORD_SESSIONS = "1"
python dev_server.py
```

> **Production note:** Fargate deploys must keep this OFF. Recordings can
> contain user prompts and scene graphs that may be subject to retention
> rules.

Default location: `<repo>/recordings/` (sibling of `ai-backend/`).

## Output layout

```
recordings/
  <session_id>/
    turn_000.json
    turn_0_iter_0.png    # HD viewport snapshot from iteration 0
    turn_0_iter_1.png    # iteration 1 (if the agentic loop iterated)
    turn_001.json
    turn_1_iter_0.png
    ...
```

`session_id` is sanitised (alphanumerics + `._-` only, capped at 80
chars) so a malformed session ID can never escape the recordings root.

## Turn JSON schema

Every `turn_NNN.json` is a single JSON object with this shape:

```json
{
  "turn_index": 0,
  "user_message": "create a tropical beach scene at golden hour",
  "started_at": "2026-05-25T19:42:18Z",
  "finished_at": "2026-05-25T19:43:55Z",
  "intent": "dense_scene",
  "persona": "environment_artist",
  "model": "claude-opus-4-7",
  "routing_reason": "execution-default (intent=dense_scene, plan=trial)",
  "spec": {
    "subject": "a tropical beach shoreline at golden hour",
    "framing": {"camera": "low three-quarter front", "lens_mm": 35, "angle": "documentary"},
    "lighting": {
      "time_of_day": "golden hour",
      "key": "warm 5500K sun from camera-right rear",
      "fill": "cool 8000K sky bounce",
      "rim": "subtle orange backlight on foam",
      "mood": "warm, expansive"
    },
    "palette": {
      "dominant": "warm sand and amber",
      "accent": "translucent teal water",
      "neutral": "soft peach sky gradient"
    },
    "composition": {
      "foreground": "wet sand with shells",
      "midground": "breaking waves and foam line",
      "background": "open horizon with atmospheric haze",
      "hero": "breaking wave foam line at wet-dry sand boundary"
    },
    "materials": [
      {"on": "sand", "type": "wet packed sand", "notes": "high roughness, fine grain normal at 0.5m"},
      {"on": "water", "type": "translucent water with foam", "notes": "subsurface, animated"}
    ],
    "density": {
      "scattered": "sparse shells, low density",
      "control": "rotation jitter, no grid"
    },
    "scale_notes": "scene reads at ~12m wide from camera"
  },
  "iterations": [
    {
      "iteration_index": 0,
      "scene_graph_before": {"objects": [], "mode": "OBJECT"},
      "scripts_emitted": [
        "import bpy\nimport bmesh\n..."
      ],
      "tool_use_names": ["use_asset", "execute_blender_script"],
      "tool_results": [
        {"tool_use_id": "tu_abc", "is_error": false, "output": "Applied HDRI..."},
        {"tool_use_id": "tu_def", "is_error": false, "output": "Build complete: ..."}
      ],
      "hd_capture_filename": "turn_0_iter_0.png",
      "artists_eye_verdict": {
        "overall": "pass",
        "summary": "Composition reads, light direction matches the brief.",
        "confidence": 0.86,
        "fix_suggestions": [],
        "failed_check_count": 0
      },
      "duration_ms": 91200,
      "notes": ""
    }
  ],
  "final_review": {
    "verdict": "ship",
    "summary": "",
    "confidence": 0.9,
    "what_works": "Foreground composition and atmospheric layering work.",
    "what_to_fix": ""
  },
  "outcome": "success",
  "error_message": "",
  "script_rescue_triggered": false
}
```

### Field reference

| Field | Type | Meaning |
|---|---|---|
| `turn_index` | int | Zero-indexed turn within the session |
| `user_message` | str | The user's literal text, capped at 4 000 chars |
| `started_at` / `finished_at` | ISO-8601 UTC | Bracket the turn |
| `intent` | str | The intent classifier's verdict (e.g. `hard_surface_model`, `dense_scene`) |
| `persona` | str | The persona routed to (e.g. `environment_artist`) |
| `model` | str | The model name picked by the router (logical name, e.g. `claude-opus-4-7`) |
| `routing_reason` | str | Why the router picked the model (e.g. `execution-default`) |
| `spec` | object \| null | The SPEC from `orchestrator/spec.py`. `null` for conversational turns where the SPEC step is skipped |
| `iterations` | array | One entry per agentic-loop iteration, in order |
| `iterations[*].scripts_emitted` | array[str] | Every `execute_blender_script` script body emitted on that iteration (full content, not truncated) |
| `iterations[*].tool_use_names` | array[str] | All tool names the model called (`execute_blender_script`, `use_asset`, `request_final_review`, etc.) |
| `iterations[*].tool_results` | array[obj] | Captured tool_result frames — `is_error`, `output` (capped at 2 KB), `error` |
| `iterations[*].hd_capture_filename` | str \| null | PNG filename in the same dir; null when no HD frame landed |
| `iterations[*].artists_eye_verdict` | object \| null | The per-iteration quality check verdict (Sonnet vision) |
| `final_review` | object \| null | The whole-scene art-director verdict from `orchestrator/final_review.py` (Sprint 1B) |
| `outcome` | str | One of `success`, `retry_exhausted`, `error`, `cancelled` |
| `error_message` | str | Populated when `outcome == "error"` |
| `script_rescue_triggered` | bool | True when the streaming.py script-rescue guard fired (Sprint 3 follow-up) |

## What's NOT captured

- **Anthropic API request/response bodies** — those carry the API key
  fingerprint in headers and would explode recording size. The relevant
  fields (intent, persona, model, scripts) are already on the
  `TurnRecord`.
- **WebSocket frame timestamps** — wall-clock latencies live on
  iterations via `duration_ms`; per-message timing isn't preserved.
- **Token usage** — recorded by the orchestrator's bus events
  (`llm.stream_completed`) but not stored on the recording; the eval
  harness re-derives it from script length.
- **User keystrokes / partial messages** — only the finalised user
  message that triggered the turn is stored.

## Redaction rules

Recordings are *not* sanitised automatically. Before sharing or
mining a recording from a real user session:

1. **Strip the user_message** if it might contain PII (names, addresses,
   trade-secret terms). The default capture preserves the literal text.
2. **Review the SPEC and scripts** for any subject the user might have
   asked Animora to model that would be sensitive (a logo, a private
   asset, an identifying portrait).
3. **HD captures** can show whatever was on the user's viewport at the
   moment of capture. Treat them like screenshots — same retention
   rules as any other user-visible Animora output.

The cofounder-driven 10-session capture for benchmark mining is
non-sensitive by design (synthetic prompts crafted for variety), so no
redaction needed there.

## Downstream consumers

### `scripts/recordings_to_benchmarks.py`

Reads a directory of recordings and emits a Python file with draft
`Benchmark(...)` entries one could paste into
`ai-backend/eval/benchmarks.py` after review. The script does NOT
auto-commit benchmarks — every entry needs a human review pass to
confirm the regex patterns match the model's actual behaviour.

```bash
python scripts/recordings_to_benchmarks.py \
  --recordings recordings/cofounder_2026_05/ \
  --out /tmp/draft_benchmarks.py
```

### `scripts/recordings_to_few_shot.py`

Extracts (SPEC → script → verdict) triples and renders them as a
multi-line string suitable for pasting into a persona prompt as a
worked example. Limited to recordings where `final_review.verdict ==
"ship"` so the few-shot examples only carry quality work.

```bash
python scripts/recordings_to_few_shot.py \
  --recordings recordings/cofounder_2026_05/ \
  --persona environment_artist \
  --out /tmp/env_artist_few_shot.txt
```

## How recordings drive the Continuous workstream

The Quality Plan's Continuous workstream (§6.3, §6.4 reframed) treats
eval scores as the reward signal. Recordings feed that loop:

1. **Run the eval** → identify failures
2. **Capture sessions** that exercise the failure modes
3. **Mine the recordings** for the SPEC the model received vs the
   script it emitted vs the artist's-eye verdict
4. **Update the persona prompt** with a few-shot example that
   demonstrates the desired behavior (extracted via
   `recordings_to_few_shot.py`)
5. **Re-run the eval** → confirm the change moved the score
6. **Repeat monthly** — eval baseline grows by ≥1 benchmark/month
   from captured sessions

The recorder doesn't change the loop — it just makes step 3 cheap.
