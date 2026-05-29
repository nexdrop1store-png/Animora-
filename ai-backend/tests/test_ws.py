"""
End-to-end WebSocket smoke test — simulates exactly what the Animora addon
does when the user sends a message in the AI panel:

  1. Connect to ws://localhost:8000/ws/<session_id>?token=dev
  2. Send hello { api_key: "<from .env>", animora_version, settings }
  3. Send user_message { text: "Introduce yourself in one sentence" }
  4. Receive streamed tokens
  5. Print the assembled response

If this passes, the addon will work — the addon uses the same wire protocol.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path


def _load_key_from_env() -> str:
    # .env lives in ai-backend/, this file lives in ai-backend/tests/
    env = Path(__file__).resolve().parent.parent / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


async def main() -> int:
    # Make stdout tolerant of non-ASCII tokens (Claude often emits emoji)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

    try:
        import websockets
    except ImportError:
        print("Installing websockets client...")
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "websockets", "-q"], check=True)
        import websockets

    api_key = _load_key_from_env()
    if not api_key:
        print("FAIL: no ANTHROPIC_API_KEY in .env")
        return 1

    session_id = "test-ws-" + uuid.uuid4().hex[:8]
    url = f"ws://localhost:8000/ws/{session_id}?token=dev"
    print(f"Connecting to {url}")

    async with websockets.connect(url) as ws:
        # 1. session_info comes first (server sends it after WS accept)
        info_raw = await ws.recv()
        info = json.loads(info_raw)
        print(f"  session_info: plan={info.get('plan')} key_source={info.get('key_source','?')}")

        # 2. Send hello with the BYOK key (the production path)
        await ws.send(json.dumps({
            "type": "hello",
            "api_key": api_key,
            "animora_version": "0.3.0-test",
            "settings": {"default_model": "auto", "streaming_enabled": True},
        }))

        # 3. Send a tiny scene_graph so the LLM has context
        await ws.send(json.dumps({
            "type": "scene_graph",
            "graph": {
                "scene_name": "Test",
                "frame_current": 1,
                "mode": "OBJECT",
                "active_object": "Cube",
                "objects": [
                    {
                        "name": "Cube", "type": "MESH",
                        "location": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1],
                        "visible": True, "selected": True,
                        "modifiers": [], "materials": [], "vertex_count": 8,
                    },
                ],
                "render": {"engine": "CYCLES", "resolution_x": 1920, "resolution_y": 1080},
            },
        }))

        # 4. Send a user message
        await ws.send(json.dumps({
            "type": "user_message",
            "text": "Briefly introduce yourself in one sentence.",
            "session_id": session_id,
        }))
        print(f"  sent user_message")
        print()
        print("Streamed response:")
        print("  ", end="", flush=True)

        # 5. Receive streamed tokens until we see the end-of-stream signal
        full = []
        async def _read_loop():
            async for raw in ws:
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "stream_token":
                    tok = msg.get("token", "")
                    full.append(tok)
                    sys.stdout.write(tok)
                    sys.stdout.flush()
                elif t == "tool_call":
                    print(f"\n  [tool_call: {msg.get('tool')}]")
                elif t == "error":
                    print(f"\n  [ERROR {msg.get('code')}: {msg.get('message')}]")
                    return False
                elif t == "stream_cancelled":
                    print("\n  [cancelled]")
                    return False
            return True

        try:
            await asyncio.wait_for(_read_loop(), timeout=20.0)
        except asyncio.TimeoutError:
            pass

        print()
        print()
        response = "".join(full)
        print(f"Total response chars: {len(response)}")
        print(f"Response: {response!r}")
        print()

        lower = response.lower()
        leaks_claude_phrase = "i am claude" in lower or "im claude" in lower or "i'm claude" in lower
        leaks_anthropic = "anthropic" in lower
        identifies_as_animora = "animora" in lower
        ok = identifies_as_animora and not leaks_claude_phrase and not leaks_anthropic
        print(f"Brand check: identifies as Animora={identifies_as_animora} | "
              f"leaks Claude={leaks_claude_phrase} | "
              f"leaks Anthropic={leaks_anthropic}")
        print(f"  -> {'PASS' if ok else 'WARN'}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
