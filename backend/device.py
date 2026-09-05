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

_lock = threading.Lock()
_devices: dict[str, dict] = {}  # uri -> {"handle", "info", "since"}


class DeviceError(Exception):
    pass


def _probe_info(sdr) -> dict:
    """Best-effort hardware identity + temperature. Every field is
    optional: pyadi/driver versions differ, and a missing attr must
    downgrade the readout, never fail the connect."""
    info: dict = {}
    ctrl = getattr(sdr, "_ctrl", None)
    try:
        ctx = ctrl.ctx if ctrl is not None else None
        if ctx is not None:
            info["context"] = getattr(ctx, "name", None)
            attrs = dict(getattr(ctx, "attrs", {}) or {})
            for k in ("hw_model", "hw_serial", "fw_version",
                      "usb,idVendor", "usb,idProduct"):
                if k in attrs:
                    info[k.replace(",", "_")] = attrs[k]
    except Exception:
        pass
    try:
        if ctrl is not None:
            temp_ch = ctrl.find_channel("temp0")
            raw = float(temp_ch.attrs["input"].value)
            info["temp_c"] = round(raw / 1000.0, 1)
    except Exception:
        pass
    try:
        info["sample_rate"] = int(sdr.sample_rate)
        info["tx_hardwaregain_db"] = float(sdr.tx_hardwaregain_chan0)
    except Exception:
        pass
    return info


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
            import adi  # pyadi-iio; needs libiio on the loader path
        except Exception as ex:
            raise DeviceError(f"pyadi-iio / libiio not available: {ex}") from ex
        try:
            sdr = adi.Pluto(uri=uri)
        except Exception as ex:
            raise DeviceError(f"cannot reach SDR at {uri!r}: {ex}") from ex
        entry = {"handle": sdr, "info": _probe_info(sdr),
                 "since": dt.datetime.utcnow().isoformat() + "Z"}
        _devices[uri] = entry
        return _entry(uri, entry)


def _safe_reprobe(handle) -> dict:
    try:
        return _probe_info(handle)
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
