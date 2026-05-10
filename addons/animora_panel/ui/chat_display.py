"""Scrollable chat display helpers — used by PT_AnimoraPanel."""

from __future__ import annotations

import bpy


def draw_chat_message(layout: bpy.types.UILayout, role: str, content: str) -> None:
    col = layout.column(align=True)
    if role == "user":
        col.alert = False
        col.label(text=f"You", icon="USER")
    else:
        col.label(text="Animora", icon="OUTLINER_OB_ARMATURE")

    # Word-wrap: split content into ~60-char chunks
    words = content.split()
    line = ""
    for word in words:
        if len(line) + len(word) + 1 > 60:
            col.label(text=f"  {line}")
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        col.label(text=f"  {line}")


def register() -> None:
    pass


def unregister() -> None:
    pass
