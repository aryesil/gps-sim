# backend/ws_hub.py
"""Multi-operator broadcast: every audit event (backend/audit.py) --
transmit start/stop, timeline steps, the fail-safe auto-stop -- is pushed
to every connected /ws/events client in real time, so more than one
operator's browser tab sees the same shared state instead of only its
own SSE stream.

audit.log_event() runs on worker threads (transmit/live-session threads),
but WebSocket sends must happen on the asyncio event loop FastAPI/uvicorn
runs on. set_loop() captures that loop once at app startup; broadcast()
schedules the actual send onto it via run_coroutine_threadsafe, which is
the documented way to call into an asyncio loop from a plain thread.
"""
from __future__ import annotations

import asyncio

from fastapi import WebSocket

_loop: asyncio.AbstractEventLoop | None = None
_clients: set[WebSocket] = set()


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


async def register(ws: WebSocket) -> None:
    await ws.accept()
    _clients.add(ws)


def unregister(ws: WebSocket) -> None:
    _clients.discard(ws)


async def _broadcast_async(payload: dict) -> None:
    dead = []
    for ws in list(_clients):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


def broadcast(payload: dict) -> None:
    """Safe to call from any thread, including before the event loop has
    started (e.g. in a unit test that never wires up the app's startup
    event) -- both cases are a silent no-op rather than an error, because
    a broadcast failure must never take down the audit call that
    triggered it."""
    if _loop is None or not _clients:
        return
    try:
        asyncio.run_coroutine_threadsafe(_broadcast_async(payload), _loop)
    except RuntimeError:
        pass
