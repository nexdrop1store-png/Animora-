"""
LLM Orchestrator — routes messages to the right Claude model and
streams responses back to the WebSocket client.

Model selection:
  Haiku 4.5  — short, quick queries (< 4k tokens, simple)
  Sonnet 4.6 — default (Free / Standard plans)
  Opus 4.5   — complex tasks, Studio plan only
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import anthropic

from .config import settings
from .quality_enforcer import validate_script
from .scene_intelligence import build_scene_context, estimate_task_complexity

log = logging.getLogger("animora.orchestrator")

MODEL_HAIKU = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_OPUS = "claude-opus-4-5"

BLENDER_TOOLS = [
    {
        "name": "execute_blender_script",
        "description": (
            "Execute a Python script in the user's Blender/Animora session. "
            "Use bpy to create, modify, or query objects. "
            "The script runs in the user's active session context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "Valid Python bpy script. Must not import os/subprocess/socket.",
                }
            },
            "required": ["script"],
        },
    },
    {
        "name": "get_object_info",
        "description": "Query detailed information about a named object in the scene.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "render_preview",
        "description": "Trigger a preview render and return the result image to the AI.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "suggest_next_steps",
        "description": "Show the user a list of suggested next actions in the UI.",
        "input_schema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of 2–5 short suggested actions.",
                }
            },
            "required": ["steps"],
        },
    },
]

SYSTEM_PROMPT_BASE = """You are Animora AI, an expert 3D artist assistant embedded inside Animora (a Blender-based tool).

Your role:
- Help users create, modify, and render 3D content using natural language
- Generate Blender Python (bpy) scripts via the execute_blender_script tool
- Explain 3D concepts clearly
- Proactively suggest next steps

Rules:
- Never generate scripts with import os, import subprocess, open(), or eval()
- Keep scripts focused and minimal — do exactly what was asked
- If unsure, ask a clarifying question rather than guessing
- Always explain what your script will do before running it

Current scene context:
{scene_context}
"""


def _select_model(
    user_message: str,
    conversation_history: list[dict],
    scene_graph: dict,
    plan: str,
) -> str:
    if plan not in ("standard", "studio"):
        # Trial: always Sonnet
        return MODEL_SONNET

    # Estimate token count (rough)
    total_tokens = sum(len(m.get("content", "")) for m in conversation_history) // 4
    total_tokens += len(user_message) // 4

    complexity = estimate_task_complexity(user_message, scene_graph)

    if plan == "studio" and complexity > 0.8:
        return MODEL_OPUS

    if total_tokens < 1000 and len(user_message) < 120 and complexity < 0.3:
        return MODEL_HAIKU

    return MODEL_SONNET


async def stream_response(
    user_message: str,
    conversation_history: list[dict],
    scene_context_str: str,
    plan: str,
    scene_graph: dict,
    send_token_cb,
    send_tool_call_cb,
) -> str:
    """
    Stream LLM response, yielding tokens and tool calls via callbacks.
    Returns the full assistant text response.
    """
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    model = _select_model(user_message, conversation_history, scene_graph, plan)
    log.info("Routing to model: %s (plan=%s)", model, plan)

    system_prompt = SYSTEM_PROMPT_BASE.format(scene_context=scene_context_str)

    messages = conversation_history[-20:] + [{"role": "user", "content": user_message}]

    full_response = ""
    tool_calls_to_retry: list[dict] = []

    async with client.messages.stream(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=messages,
        tools=BLENDER_TOOLS,
    ) as stream:
        async for event in stream:
            if event.type == "content_block_delta":
                if hasattr(event.delta, "text"):
                    token = event.delta.text
                    full_response += token
                    await send_token_cb(token)

            elif event.type == "content_block_start":
                if hasattr(event.content_block, "type") and event.content_block.type == "tool_use":
                    pass  # Tool use collected in final message

        final_msg = await stream.get_final_message()

    # Process tool use blocks
    for block in final_msg.content:
        if block.type == "tool_use":
            tool_input = block.input
            if block.name == "execute_blender_script":
                script = tool_input.get("script", "")
                result = validate_script(script)
                if not result.ok:
                    log.warning("Script rejected: %s", result.reason)
                    await send_token_cb(f"\n\n[Script blocked: {result.reason}]")
                    continue
            await send_tool_call_cb(block.name, block.id, tool_input)

    return full_response
