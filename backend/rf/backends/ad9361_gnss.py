"""AD9361 backend for the FPGA GNSS engine ('gnss' TX format, kind "tezuka_gnss").

With the engine in command mode the DAC DMA no longer carries samples: every
32-bit DMA word is a command word that the PL pops into the engine's command
FIFO (see backend.fpga.stream / cmdfmt). The DMA channels are therefore the
stock cs16 pair (voltage0 + voltage1, one 32-bit word per "sample"); only the
meaning of the words differs. Buffers are NON-cyclic: the engine consumes the
words as they arrive and the DMA backpressure (kernel ring full -> push blocks)
is the flow control. The command mode itself is switched through the engine's
register file, not here (backend.fpga.device).

Everything except the buffer write is shared with backends/ad9361.py. Single
TX port (TX1).
"""
from __future__ import annotations

import numpy as np

from backend.rf.backends import ad9361 as _base
from backend.rf.transmit import TransmitError

DAC_FULL_SCALE = 32767       # the engine outputs 16-bit samples (DAC takes the 12 MSBs)

_MUTE_GAIN_DB = _base._MUTE_GAIN_DB

set_gain = _base.set_gain
probe_open = _base.probe_open
probe_close = _base.probe_close
probe_info = _base.probe_info
temperature_c = _base.temperature_c
kernel_buffers_for = _base.kernel_buffers_for


def n_tx_channels(handle) -> int:
    return 1


def active_channels(handle) -> list[int]:
    return [0]


def set_active_channels(handle, chans) -> None:
    if sorted(set(int(c) for c in chans)) != [0]:
        raise TransmitError("gnss TX mode drives TX1 only")


def open_tx(uri: str, lo_hz: float, sample_rate: float, *,
            rf_bandwidth_hz: float | None = None, xo_ppm: float = 0.0):
    sdr = _base.open_tx(uri, lo_hz, sample_rate,
                        rf_bandwidth_hz=rf_bandwidth_hz, xo_ppm=xo_ppm)
    try:
        _enter_gnss(sdr)
    except Exception:
        try:
            close(sdr)
        except Exception:
            pass
        raise
    return sdr


def _enter_gnss(sdr) -> None:
    dac = getattr(sdr, "_txdac", None)
    if dac is None:
        raise TransmitError("gnss TX needs a pyadi-iio handle with a DDS core device")
    ids = set()
    for ch in dac.channels:
        if not getattr(ch, "output", False):
            continue
        cid = str(getattr(ch, "id", ""))
        on = cid in ("voltage0", "voltage1")
        ch.enabled = on
        if on:
            ids.add(cid)
    if ids != {"voltage0", "voltage1"}:
        raise TransmitError("the DDS core exposes no voltage0/voltage1 output pair")
    # A push blocks while the ring is full (the engine is disabled during the
    # Start Now hold, a few seconds): do not let the 5 s default time it out.
    try:
        sdr._txdac.ctx.set_timeout(_PUSH_TIMEOUT_MS)
    except Exception:
        pass
    sdr.tx_cyclic_buffer = False
    sdr._gs_gnss_buf = None
    sdr._gs_gnss_len = 0


_PUSH_TIMEOUT_MS = 30000
_MIN_BUF_WORDS = 1 << 14      # 64 KiB: one kernel buffer carries CFG and block groups


def _buffer(handle, n: int):
    """One kernel DMA buffer, reused for every push (``push(count)`` sends only
    the first ``count`` words).  Re-creating a Buffer right after destroying
    one fails on libiio ("OSError: [Errno 0]" on every second create), so a
    larger one is only allocated when a push does not fit."""
    buf = getattr(handle, "_gs_gnss_buf", None)
    if buf is not None and handle._gs_gnss_len >= n:
        return buf
    import iio
    handle._gs_gnss_buf = None
    cap = max(n, _MIN_BUF_WORDS)
    last = None
    for _ in range(2):
        try:
            handle._gs_gnss_buf = iio.Buffer(handle._txdac, cap, False)   # cyclic=False
            handle._gs_gnss_len = cap
            return handle._gs_gnss_buf
        except OSError as ex:
            last = ex
    raise last


def write_words(handle, words) -> None:
    """Push one non-cyclic buffer of little-endian uint32 command words.
    Blocks while the kernel ring is full."""
    w = np.ascontiguousarray(words, dtype="<u4")
    buf = _buffer(handle, int(w.size))
    buf.write(bytearray(w.tobytes()))
    buf.push(int(w.size))


def write(handle, blocks) -> None:
    write_words(handle, blocks[0])


def close(handle) -> None:
    # The engine is disabled and flushed by the caller first (GnssTx.stop);
    # here only the analog side is silenced: mute, release the buffer, LO off.
    try:
        setattr(handle, "tx_hardwaregain_chan0", _MUTE_GAIN_DB)
    except Exception:
        pass
    handle._gs_gnss_buf = None
    try:
        handle.tx_destroy_buffer()
    except Exception:
        pass
    _base._set_rx_lo_powerdown(handle, True)
    _base._set_tx_lo_powerdown(handle, True)
    _base._set_ensm_mode(handle, "alert")
    _base.leave_tdd(handle)
