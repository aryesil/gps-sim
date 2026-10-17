"""AD9361 backend for Tezuka firmware's TX complex 4-bit (cs4) mode.

The FPGA image built with CS4TX=1 (maia-sdr ``cs_tx_demux`` + the axi_ad9361
cs4 patch) treats "only voltage1 (Q0) of the DDS core enabled" as cs4: every
16-bit DMA word carries TWO complex samples, ``{s1, s0}`` with ``s = {Q4, I4}``
(I in the low nibble, first sample in the low byte). The FPGA reads one word per
two DAC samples and expands each signed 4-bit value to 16 bits. That quarters
the TX link load compared with 16-bit samples (half of cs8), so ~2x the cs8
sample rate fits over USB or Ethernet.

Quantisation: the stream is limited to +-7 (symmetric, -8 is never produced).
GNSS composites are close to Gaussian, so the level has to sit around 2.5
sigma per rail from the clip (see ``LEVEL_OFFSET_DB``); a lower level wastes the
few codes there are. A/B on a 33-satellite G+C+E composite measured an
acquisition-metric loss of 0.02 dB on average (0.07 dB worst) for 4 bits at that
level.

Everything except the sample path is shared with backends/ad9361.py. pyadi-iio
cannot enable a single IIO channel, so the stream goes through libiio directly:
only ``voltage1`` is enabled on the DDS core and the packed bytes are pushed with
an ``iio.Buffer``. Single TX port only.
"""
from __future__ import annotations

import logging

import numpy as np

from backend.rf.backends import ad9361 as _base
from backend.rf.transmit import TransmitError

_log = logging.getLogger(__name__)

# int4 limits: symmetric, -8 is never produced.
DAC_FULL_SCALE = 7

# The FPGA maps one code to CS4_STEP = 1480 DAC counts (cs_tx_demux), so +-7 is
# -10 dB of the DAC full scale. transmit.py adds this to the requested level:
# -15 dBFS of the DAC then becomes -5 dBFS of the +-7 range, which is where a
# 4-bit quantiser wants a Gaussian signal (about 2.5 sigma per rail to the clip).
LEVEL_OFFSET_DB = 10.0

_MUTE_GAIN_DB = _base._MUTE_GAIN_DB
_ZERO_FLUSH_SAMPLES = _base._ZERO_FLUSH_SAMPLES

set_gain = _base.set_gain
probe_open = _base.probe_open
probe_close = _base.probe_close
probe_info = _base.probe_info
temperature_c = _base.temperature_c


def n_tx_channels(handle) -> int:
    return 1


def active_channels(handle) -> list[int]:
    return [0]


def set_active_channels(handle, chans) -> None:
    if sorted(set(int(c) for c in chans)) != [0]:
        raise TransmitError("cs4 TX mode drives TX1 only")


def pack_cs4(block) -> bytes:
    """Complex block (already scaled to +-DAC_FULL_SCALE) -> packed nibbles.

    Each sample is one byte ``{Q4, I4}`` (two's complement nibbles); two
    consecutive samples form one little-endian 16-bit word, first sample in the
    low byte. An odd block gets one zero sample appended."""
    b = np.asarray(block)
    if b.shape[0] % 2:
        b = np.concatenate([b, np.zeros(1, dtype=b.dtype)])
    lim = float(DAC_FULL_SCALE)
    i = np.clip(np.rint(b.real), -lim, lim).astype(np.int8)
    q = np.clip(np.rint(b.imag), -lim, lim).astype(np.int8)
    out = ((q.astype(np.uint8) & 0x0F) << 4) | (i.astype(np.uint8) & 0x0F)
    return out.astype(np.uint8).tobytes()


def open_tx(uri: str, lo_hz: float, sample_rate: float, *,
            rf_bandwidth_hz: float | None = None, xo_ppm: float = 0.0):
    sdr = _base.open_tx(uri, lo_hz, sample_rate,
                        rf_bandwidth_hz=rf_bandwidth_hz, xo_ppm=xo_ppm)
    try:
        _enter_cs4(sdr)
    except Exception:
        try:
            close(sdr)
        except Exception:
            pass
        raise
    return sdr


def _enter_cs4(sdr) -> None:
    dac = getattr(sdr, "_txdac", None)
    if dac is None:
        raise TransmitError("cs4 TX needs a pyadi-iio handle with a DDS core device")
    found = False
    for ch in dac.channels:
        if not getattr(ch, "output", False):
            continue
        is_q0 = str(getattr(ch, "id", "")) == "voltage1"
        ch.enabled = is_q0
        found = found or is_q0
    if not found:
        raise TransmitError("the DDS core exposes no voltage1 output channel")
    sdr._gs_cs4_buf = None
    sdr._gs_cs4_len = 0


def _buffer(handle, n_words: int):
    buf = getattr(handle, "_gs_cs4_buf", None)
    if buf is not None and handle._gs_cs4_len == n_words:
        return buf
    import iio  # pylibiio, already a dependency of pyadi-iio
    handle._gs_cs4_buf = None          # drop (and destroy) the old buffer first
    handle._gs_cs4_buf = iio.Buffer(handle._txdac, n_words, False)
    handle._gs_cs4_len = n_words
    return handle._gs_cs4_buf


def _push(handle, block) -> None:
    raw = pack_cs4(block)
    buf = _buffer(handle, len(raw) // 2)
    buf.write(bytearray(raw))
    buf.push()


AIR_BYTES_PER_SAMPLE = 1


def air_format_for(sample_rate: float):
    from backend.rf import air
    return air.format_by_name("cs4")


def write_air(handle, blocks) -> None:
    """``blocks``: one packed uint8 block (TX1), see backend.rf.air."""
    raw = blocks[0]
    buf = _buffer(handle, len(raw) // 2)
    buf.write(bytearray(raw))
    buf.push()


def write(handle, blocks) -> None:
    """``blocks``: one complex array (TX1)."""
    _push(handle, blocks[0])


def close(handle) -> None:
    # Same silence sequence as ad9361_cs8.close(): mute, push zeros through the
    # raw path (handle.tx() would re-enable both I and Q), release the buffer,
    # power the LOs down.
    try:
        setattr(handle, "tx_hardwaregain_chan0", _MUTE_GAIN_DB)
    except Exception:
        pass
    try:
        handle.disable_dds()
    except Exception:
        pass
    try:
        _push(handle, np.zeros(_ZERO_FLUSH_SAMPLES, dtype=np.complex64))
    except Exception:
        pass
    handle._gs_cs4_buf = None
    try:
        handle.tx_destroy_buffer()
    except Exception:
        pass
    _base._set_rx_lo_powerdown(handle, True)
    _base._set_tx_lo_powerdown(handle, True)
    _base._set_ensm_mode(handle, "alert")
    _base.leave_tdd(handle)
