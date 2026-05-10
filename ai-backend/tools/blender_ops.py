"""
Tool result handlers for Blender operations.

These functions are called server-side after the addon reports
a tool_result back over WebSocket. They format the result for
the next LLM turn.
"""

from __future__ import annotations


def format_script_result(output: str, error: str) -> str:
    if error:
        return f"Script error: {error}"
    return f"Script executed successfully. Output: {output or '(none)'}"


def format_object_info(obj_data: dict) -> str:
    if not obj_data:
        return "Object not found in scene."
    return (
        f"Object '{obj_data.get('name')}': type={obj_data.get('type')}, "
        f"location={obj_data.get('location')}, "
        f"modifiers={obj_data.get('modifiers', [])}"
    )
