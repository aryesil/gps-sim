# backend/auth.py
"""Role-based access control, gated entirely by config.API_KEYS.

Two roles: "operator" (can start/stop transmit, jog, script a timeline,
listen for receiver feedback, save presets) and "viewer" (read-only --
can watch status/audit/replay but never touch RF or session state).

If config.API_KEYS is empty, require_operator() is a no-op: a rig with
no keys configured behaves exactly as it did before RBAC existed, which
is also what every pre-existing test assumes.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from backend import config


def role_for_request(request: Request) -> str | None:
    if not config.API_KEYS:
        return "operator"  # auth disabled -- everyone is effectively an operator
    key = request.headers.get("X-API-Key")
    return config.API_KEYS.get(key)


def require_operator(request: Request) -> None:
    if role_for_request(request) != "operator":
        raise HTTPException(403, "operator role required (send X-API-Key)")


def require_viewer_or_operator(request: Request) -> None:
    if role_for_request(request) is None:
        raise HTTPException(403, "valid X-API-Key required")
