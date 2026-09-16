"""Shared best-effort libiio probe helper for backend/rf/device.py and
backend/rf/frontend/capabilities.py -- keeps the "probe and degrade, never
raise" idiom in one place instead of two independent copies."""
from __future__ import annotations


def probe_temp_channel(ctrl):
    """Returns the temp0 IIO channel, or None if this control context has
    none (older driver, a device that genuinely doesn't expose it, a
    mocked/partial sdr in tests)."""
    try:
        if ctrl is None:
            return None
        return ctrl.find_channel("temp0")
    except Exception:
        return None
