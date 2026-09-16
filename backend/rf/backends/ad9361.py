"""AD9361/AD9363 backend glue (PlutoSDR and IIO-compatible clones,
including bladeRF 2.0 micro which uses the same transceiver under a
different driver stack -- see backend/rf/backends/bladerf.py). Talks to
the chip through pyadi-iio; this module owns every pyadi-iio-specific
attribute name so dual_tx.py and device.py stay backend-agnostic.

Two separate pyadi-iio classes are used on purpose, matching what was
already true before this module existed: `adi.Pluto` for the lightweight
control-probe link (device.py's connect/disconnect/status), and the
lower-level `adi.ad9361` for the actual dual-channel TX streaming
(dual_tx.py) -- Pluto's convenience wrapper doesn't expose independent
per-channel TX gain the way ad9361 does.
"""
from __future__ import annotations

import numpy as np

from backend.rf._iio_probe import probe_temp_channel
from backend.rf.transmit import TransmitError

_MUTE_GAIN_DB = -89.75      # AD9361 tx_hardwaregain_chanN minimum (max attenuation)
_ZERO_FLUSH_SAMPLES = 65536  # one block-sized silence push -- see close()


def open_tx(uri: str, lo_hz: float, sample_rate: float):
    """Open the shared dual-channel TX context. Raises TransmitError if
    the device silently clamped the requested LO or sample rate."""
    import adi  # pyadi-iio
    sdr = adi.ad9361(uri=uri)
    sdr.tx_enabled_channels = [0, 1]
    sdr.sample_rate = int(sample_rate)
    sdr.tx_lo = int(lo_hz)
    sdr.tx_cyclic_buffer = False
    if abs(sdr.tx_lo - lo_hz) > 1000:
        raise TransmitError(f"device clamped LO to {sdr.tx_lo}")
    if abs(sdr.sample_rate - sample_rate) > 1.0:
        raise TransmitError(f"device clamped rate to {sdr.sample_rate}")
    return sdr


def set_gain(handle, chan: int, gain_db: float) -> None:
    setattr(handle, f"tx_hardwaregain_chan{chan}", float(gain_db))


def write(handle, blocks) -> None:
    handle.tx(blocks)


def close(handle) -> None:
    # KNOWN real-hardware gotcha: with tx_cyclic_buffer=False, the AD9361's
    # TX DMA does not reliably go silent once the app stops feeding it --
    # it can keep repeating the last transferred buffer indefinitely until
    # the whole libiio context is torn down, which tx_destroy_buffer()
    # alone does not guarantee (a documented pyadi-iio/AD9361
    # community-reported behavior, not specific to this app). Force real,
    # deterministic silence before destroying the buffer: mute both
    # channels to the hardware's minimum gain (0 to -89.75 dB, 0.25 dB
    # steps -- AD9361 spec), then push one explicit all-zero block, so if
    # the DMA does repeat its last buffer, it repeats silence, not signal.
    try:
        handle.tx_hardwaregain_chan0 = _MUTE_GAIN_DB
        handle.tx_hardwaregain_chan1 = _MUTE_GAIN_DB
    except Exception:
        pass
    try:
        zero = np.zeros(_ZERO_FLUSH_SAMPLES, dtype=np.complex64)
        handle.tx([zero, zero])
    except Exception:
        pass
    try:
        handle.tx_destroy_buffer()
    except Exception:
        pass


def probe_open(uri: str):
    import adi  # pyadi-iio
    return adi.Pluto(uri=uri)


def probe_info(handle) -> dict:
    """Best-effort hardware identity + temperature. Every field is
    optional: pyadi/driver versions differ, and a missing attr must
    downgrade the readout, never fail the connect."""
    info: dict = {}
    ctrl = getattr(handle, "_ctrl", None)
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
        temp_ch = probe_temp_channel(ctrl)
        if temp_ch is not None:
            raw = float(temp_ch.attrs["input"].value)
            info["temp_c"] = round(raw / 1000.0, 1)
    except Exception:
        pass
    try:
        info["sample_rate"] = int(handle.sample_rate)
        info["tx_hardwaregain_db"] = float(handle.tx_hardwaregain_chan0)
    except Exception:
        pass
    return info
