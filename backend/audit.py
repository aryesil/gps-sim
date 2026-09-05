# backend/audit.py
"""Persistent, append-only audit trail for transmit activity.

Every RF-relevant action (live/file transmit start, manual stop, the
fail-safe auto-stop, transmit finishing) is appended as one JSON line to
config.LOG_DIR/audit.jsonl. Unlike frontend/log.js's in-browser event
list, this survives server restarts and a closed browser tab -- it's the
record a range safety officer or after-action review would need, not a
UI convenience.

Appends are serialized by a lock: two live channels (TX1/TX2) can log
concurrently from different threads, and a torn/interleaved line would
break every reader downstream.
"""
from __future__ import annotations

import datetime as dt
import json
import threading

from backend import config

_lock = threading.Lock()


def log_event(event: str, **fields) -> None:
    """Append one audit record. Never raises -- a logging failure must
    never take down or block an actual transmit."""
    record = {"ts": dt.datetime.utcnow().isoformat() + "Z", "event": event, **fields}
    try:
        with _lock:
            with open(config.LOG_DIR / "audit.jsonl", "a") as f:
                f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def read_events(limit: int = 200) -> list[dict]:
    """Most recent `limit` audit records, newest first."""
    path = config.LOG_DIR / "audit.jsonl"
    if not path.exists():
        return []
    with open(path) as f:
        lines = f.readlines()
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.reverse()
    return out
