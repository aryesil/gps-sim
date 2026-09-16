"""Drives the AD9361's own internal TX quadrature calibration -- never a
Python reimplementation of the calibration algorithm itself.

Mechanism: pyadi-iio does not wrap `calib_mode` as a property (verified
against upstream adi/ad936x.py), so this uses the raw libiio *device*
attribute the same way backend/rf/device.py already reads channel attrs
(`ctrl.find_channel("temp0").attrs["input"].value`): `sdr._ctrl.attrs[name]`,
an Attr object with a settable/gettable `.value`. `calib_mode_available`
(ADI driver docs) lists supported trigger modes including "tx_quad"; writing
that value to `calib_mode` triggers a one-shot TX quadrature calibration,
and the driver reverts `calib_mode` away from the trigger value once it
completes -- polled here with a bounded timeout.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass

from backend.rf.frontend.capabilities import DeviceCapabilities

_POLL_INTERVAL_S = 0.05
_POLL_TIMEOUT_S = 5.0
_TRIGGER_VALUE = "tx_quad"


@dataclass
class CalibrationResult:
    success: bool
    calibration_type: str          # "TX_QUAD" | "NONE"
    start_time: str
    end_time: str
    error_message: str | None
    tx_lo_hz: int
    target_rf_hz: int
    baseband_offset_hz: int


def _now() -> str:
    return dt.datetime.utcnow().isoformat() + "Z"


def _result(success, calibration_type, start, error_message,
            tx_lo_hz, target_rf_hz, baseband_offset_hz) -> CalibrationResult:
    return CalibrationResult(
        success=success, calibration_type=calibration_type, start_time=start,
        end_time=_now(), error_message=error_message, tx_lo_hz=tx_lo_hz,
        target_rf_hz=target_rf_hz, baseband_offset_hz=baseband_offset_hz)


def calibrate(sdr, capabilities: DeviceCapabilities, tx_lo_hz: int,
              target_rf_hz: int, baseband_offset_hz: int) -> CalibrationResult:
    start = _now()
    if not capabilities.supports_tx_quad_calibration:
        return _result(False, "NONE", start,
                        "device does not expose calib_mode; TX quadrature "
                        "calibration unavailable on this hardware",
                        tx_lo_hz, target_rf_hz, baseband_offset_hz)
    try:
        attrs = sdr._ctrl.attrs
        available = attrs["calib_mode_available"].value
        if _TRIGGER_VALUE not in available.split():
            return _result(False, "NONE", start,
                            f"{_TRIGGER_VALUE!r} not in calib_mode_available "
                            f"({available!r})",
                            tx_lo_hz, target_rf_hz, baseband_offset_hz)
        attrs["calib_mode"].value = _TRIGGER_VALUE
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        settled = False
        while time.monotonic() < deadline:
            if attrs["calib_mode"].value != _TRIGGER_VALUE:
                settled = True
                break
            time.sleep(_POLL_INTERVAL_S)
        if not settled:
            return _result(False, "TX_QUAD", start,
                            f"calib_mode did not clear {_TRIGGER_VALUE!r} "
                            f"within {_POLL_TIMEOUT_S}s",
                            tx_lo_hz, target_rf_hz, baseband_offset_hz)
        return _result(True, "TX_QUAD", start, None,
                        tx_lo_hz, target_rf_hz, baseband_offset_hz)
    except Exception as ex:
        return _result(False, "TX_QUAD", start, str(ex),
                        tx_lo_hz, target_rf_hz, baseband_offset_hz)
