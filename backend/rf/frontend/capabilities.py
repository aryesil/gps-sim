"""Best-effort AD936x/AD9361-class device capability probe.

Mirrors backend/rf/device.py's own _probe_info idiom: every field is
independently optional -- a missing attribute (older driver, a device that
genuinely doesn't expose it, a mocked/partial sdr in tests) degrades that
one field to False, never raises and never assumes every "AD9361-compatible"
device supports every control.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DeviceCapabilities:
    supports_tx_quad_calibration: bool
    supports_tx_lo_control: bool
    supports_tx_hardware_gain: bool
    supports_temperature_readout: bool


def detect(sdr) -> DeviceCapabilities:
    return DeviceCapabilities(
        supports_tx_quad_calibration=_probe_calib_mode(sdr),
        supports_tx_lo_control=_probe_attr(sdr, "tx_lo"),
        supports_tx_hardware_gain=_probe_attr(sdr, "tx_hardwaregain_chan0"),
        supports_temperature_readout=_probe_temperature(sdr),
    )


def _probe_attr(sdr, name: str) -> bool:
    try:
        getattr(sdr, name)
        return True
    except Exception:
        return False


def _probe_calib_mode(sdr) -> bool:
    try:
        ctrl = getattr(sdr, "_ctrl", None)
        if ctrl is None:
            return False
        attrs = getattr(ctrl, "attrs", {}) or {}
        return "calib_mode" in attrs and "calib_mode_available" in attrs
    except Exception:
        return False


def _probe_temperature(sdr) -> bool:
    from backend.rf._iio_probe import probe_temp_channel
    ctrl = getattr(sdr, "_ctrl", None)
    return probe_temp_channel(ctrl) is not None
