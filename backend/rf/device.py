"""SDR device connection, held in standby.

"Connect" here means: open a libiio *control* context to the AD936x
(via pyadi-iio, the same library backend/transmit.py streams through) and
keep it open so the UI can show the link is up and read back hardware
identity / die temperature. It deliberately does NOT create a TX buffer,
drive the TX LO, or push a single sample -- the radio stays in standby
until an actual transmit (backend/transmit.py) starts. Opening a control
context is what `iio_attr`/`iio_info` do; it emits no RF.

One held context per URI. The network (`ip:`) backend multiplexes fine
with the separate context transmit.stream() opens later; we never share a
handle across the two paths, so a connect here can't wedge a transmit.
"""
from __future__ import annotations

import datetime as dt
import threading

from backend.rf.backends import ad9361

_lock = threading.Lock()
_devices: dict[str, dict] = {}  # uri -> {"handle", "info", "since"}


class DeviceError(Exception):
    pass


def connect(uri: str) -> dict:
    """Open (or refresh) a standby control link to `uri`. Returns the
    device-status entry."""
    if not uri:
        raise DeviceError("empty device URI")
    with _lock:
        existing = _devices.get(uri)
        if existing is not None:
            existing["info"] = _safe_reprobe(existing["handle"])
            return _entry(uri, existing)
        try:
            handle = ad9361.probe_open(uri)
        except Exception as ex:
            raise DeviceError(f"cannot reach SDR at {uri!r}: {ex}") from ex
        entry = {"handle": handle, "info": _safe_reprobe(handle),
                 "since": dt.datetime.utcnow().isoformat() + "Z"}
        _devices[uri] = entry
        return _entry(uri, entry)


def _safe_reprobe(handle) -> dict:
    try:
        return ad9361.probe_info(handle)
    except Exception:
        return {}


def disconnect(uri: str) -> None:
    with _lock:
        entry = _devices.pop(uri, None)
    # Dropping the last reference closes the libiio context.
    if entry is not None:
        entry["handle"] = None


def _entry(uri: str, entry: dict) -> dict:
    return {"uri": uri, "connected": True,
            "since": entry["since"], "info": entry.get("info", {}),
            "state": "standby"}


def status() -> list[dict]:
    with _lock:
        return [_entry(uri, e) for uri, e in _devices.items()]


def is_connected(uri: str) -> bool:
    with _lock:
        return uri in _devices
