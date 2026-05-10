"""Redis-backed session state manager."""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import redis.asyncio as aioredis

from .config import settings

_redis: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


def _session_key(session_id: str) -> str:
    return f"session:{session_id}"


def _history_retention_seconds(plan: str) -> int:
    mapping = {
        "trial": settings.history_trial_days * 86400,
        "standard": settings.history_standard_days * 86400,
        "studio": settings.history_studio_days * 86400,
    }
    return mapping.get(plan, settings.history_trial_days * 86400)


async def get_session(session_id: str) -> dict[str, Any]:
    r = await get_redis()
    raw = await r.get(_session_key(session_id))
    if raw:
        return json.loads(raw)
    return {
        "session_id": session_id,
        "conversation_history": [],
        "scene_context": {},
        "created_at": time.time(),
        "last_active": time.time(),
        "plan": "trial",
        "user_id": "",
    }


async def save_session(session_id: str, data: dict[str, Any]) -> None:
    r = await get_redis()
    data["last_active"] = time.time()
    ttl = _history_retention_seconds(data.get("plan", "trial"))
    await r.setex(_session_key(session_id), ttl, json.dumps(data))


async def append_turn(session_id: str, role: str, content: str, plan: str) -> None:
    data = await get_session(session_id)
    data["conversation_history"].append({"role": role, "content": content, "ts": time.time()})
    data["plan"] = plan
    await save_session(session_id, data)


async def update_scene_context(session_id: str, graph: dict) -> None:
    data = await get_session(session_id)
    history = data.get("scene_graph_history", [])
    history.append({"ts": time.time(), "graph": graph})
    history = history[-settings.scene_graph_history_size:]
    data["scene_graph_history"] = history
    data["scene_context"] = graph
    await save_session(session_id, data)


async def delete_session(session_id: str) -> None:
    r = await get_redis()
    await r.delete(_session_key(session_id))
