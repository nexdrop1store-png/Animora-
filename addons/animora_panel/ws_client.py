"""
WebSocket client for the Animora AI backend.

Maintains a persistent connection, handles reconnect, dispatches:
- Text frames: JSON messages (stream_token, tool_call, session_info, error)
- Binary frames: viewport frame acknowledgements

Thread-safe: sends from any thread, dispatches received events via
bpy.app.timers on the main thread.
"""

from __future__ import annotations

import json
import logging
import queue
import struct
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("animora.ws")

_RECONNECT_DELAY_BASE = 1.0   # seconds
_RECONNECT_DELAY_MAX = 30.0
_PING_INTERVAL = 20.0


class AnimoraWSClient:
    def __init__(self) -> None:
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._send_queue: queue.Queue[bytes | str] = queue.Queue()
        self._stop_event = threading.Event()
        self._connected = False
        self._session_id: str = ""
        self._reconnect_delay = _RECONNECT_DELAY_BASE

        # Callbacks (set by panel/operators)
        self.on_stream_token: Optional[Callable[[str], None]] = None
        self.on_tool_call: Optional[Callable[[dict], None]] = None
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connect(self, url: str, session_id: str, access_token: str) -> None:
        self._url = url
        self._session_id = session_id
        self._access_token = access_token
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="animora-ws"
        )
        self._thread.start()

    def disconnect(self) -> None:
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def send_message(self, text: str, context_flags: dict | None = None) -> None:
        payload = json.dumps({
            "type": "user_message",
            "text": text,
            "context": context_flags or {},
            "session_id": self._session_id,
        })
        self._send_queue.put(payload)

    def send_binary(self, data: bytes) -> None:
        self._send_queue.put(data)

    def send_json(self, obj: dict) -> None:
        self._send_queue.put(json.dumps(obj))

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Internal connection loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._connect_and_serve()
                self._reconnect_delay = _RECONNECT_DELAY_BASE
            except Exception as exc:
                log.warning("WS connection lost: %s — reconnecting in %.1fs", exc, self._reconnect_delay)
                self._connected = False
                self._schedule_callback(self.on_disconnected)
                if not self._stop_event.wait(self._reconnect_delay):
                    self._reconnect_delay = min(self._reconnect_delay * 2, _RECONNECT_DELAY_MAX)

    def _connect_and_serve(self) -> None:
        try:
            import websocket  # websocket-client
        except ImportError:
            log.error("websocket-client not installed — cannot connect")
            self._stop_event.wait(10)
            return

        url = f"{self._url}/{self._session_id}?token={self._access_token}"
        log.info("Connecting to %s", self._url)

        ws = websocket.WebSocket()
        ws.connect(url, timeout=10)
        self._ws = ws
        self._connected = True
        self._schedule_callback(self.on_connected)

        # Resume session if we have an existing session_id
        ws.send(json.dumps({"type": "resume", "session_id": self._session_id}))

        last_ping = time.monotonic()

        while not self._stop_event.is_set():
            # Drain send queue
            while True:
                try:
                    item = self._send_queue.get_nowait()
                    if isinstance(item, bytes):
                        ws.send_binary(item)
                    else:
                        ws.send(item)
                except queue.Empty:
                    break

            # Ping keepalive
            if time.monotonic() - last_ping > _PING_INTERVAL:
                ws.ping()
                last_ping = time.monotonic()

            # Non-blocking receive
            ws.sock.settimeout(0.05)
            try:
                opcode, data = ws.recv_data()
                self._dispatch(opcode, data)
            except websocket.WebSocketTimeoutException:
                pass

        ws.close()

    def _dispatch(self, opcode: int, data: bytes | str) -> None:
        import websocket

        if opcode == websocket.ABNF.OPCODE_BINARY:
            # Binary frames are acknowledgements from backend — currently ignored
            return

        if opcode == websocket.ABNF.OPCODE_TEXT:
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                return
            msg_type = msg.get("type")
            if msg_type == "stream_token":
                token = msg.get("token", "")
                self._schedule_callback(self.on_stream_token, token)
            elif msg_type == "tool_call":
                self._schedule_callback(self.on_tool_call, msg)
            elif msg_type == "error":
                self._schedule_callback(self.on_error, msg.get("message", "Unknown error"))
            elif msg_type == "session_info":
                log.debug("Session info: %s", msg)

    def _schedule_callback(self, cb: Callable | None, *args: Any) -> None:
        if cb is None:
            return
        import bpy

        def _call():
            try:
                cb(*args)
            except Exception as exc:
                log.error("Callback error: %s", exc)
            return None  # don't reschedule

        bpy.app.timers.register(_call, first_interval=0.0)


# Module-level singleton
client = AnimoraWSClient()


def register() -> None:
    pass


def unregister() -> None:
    client.disconnect()
