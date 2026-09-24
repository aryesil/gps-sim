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

import logging

import numpy as np

from backend.rf._iio_probe import probe_temp_channel
from backend.rf.transmit import TransmitError

_log = logging.getLogger(__name__)

_MUTE_GAIN_DB = -89.75      # AD9361 tx_hardwaregain_chanN minimum (max attenuation)
_ZERO_FLUSH_SAMPLES = 65536  # one block-sized silence push -- see close()
_XO_NOMINAL_HZ = 40e6        # PlutoSDR / AD936x reference oscillator
_RF_BW_MIN_HZ = 200e3        # AD9361 TX analog filter programmable range
_RF_BW_MAX_HZ = 40e6
_KERNEL_BUFFERS = 16

# pyadi-iio's tx() casts the complex array to int16 and writes the raw words
# into the DMA buffer; the AD9361 DAC uses the 12 MOST significant bits of
# each word. Full scale is therefore int16's, not 12-bit's (ADI's Pluto
# examples scale unit signals by 2**14 for 6 dB headroom).
DAC_FULL_SCALE = 32767


def _tx_rate_error_ppm(handle, nominal_hz: float) -> float | None:
    """Exact realised DAC-side sample rate vs nominal, in ppm, from the
    driver's tx_path_rates (TXSAMP is printed rounded; BBPLL is divided by
    an integer chain, so BBPLL / round(BBPLL / TXSAMP) is exact)."""
    try:
        rates = handle._ctrl.attrs["tx_path_rates"].value
        f = dict(kv.split(":") for kv in rates.split())
        bbpll, txs = float(f["BBPLL"]), float(f["TXSAMP"])
        exact = bbpll / round(bbpll / txs)
        return (exact / float(nominal_hz) - 1.0) * 1e6
    except Exception:
        return None


def _phy_channel(handle, name: str):
    ctrl = getattr(handle, "_ctrl", None)
    return ctrl.find_channel(name, True) if ctrl is not None else None


def _set_tx_lo_powerdown(handle, down: bool) -> None:
    # altvoltage1 is the TX LO. gps-sdr-sim's plutoplayer (and other tools)
    # power it DOWN on exit and the setting survives in the driver, so a
    # later pyadi-iio session streams into a dead synthesizer -- no RF at
    # all. Always power it up explicitly; power it down on close as the
    # deterministic "silence" that muting alone cannot guarantee.
    try:
        ch = _phy_channel(handle, "altvoltage1")
        if ch is not None:
            ch.attrs["powerdown"].value = "1" if down else "0"
    except Exception:
        pass


def _count_tx_channels(handle) -> int:
    """Complex TX channels the DDS core really exposes. A stock
    PlutoSDR/AD9363 runs 1R1T (voltage0/1 only); 2 needs a 2R2T mode
    (Pluto Rev C with ``fw_setenv mode 2r2t``, Pluto+, AD9361 boards).
    Enabling channel 1 on a 1T device makes the first buffer push fail."""
    try:
        chans = handle._txdac.channels
        n = sum(1 for c in chans
                if getattr(c, "output", False)
                and str(getattr(c, "id", "")).startswith("voltage"))
        if n >= 2:
            return 2 if n >= 4 else 1
    except Exception:
        pass
    return 2   # unknown (older pyadi, mocks): keep the historic assumption


def n_tx_channels(handle) -> int:
    """Complex TX channels the hardware has."""
    return int(getattr(handle, "_gs_n_tx", 2))


def active_channels(handle) -> list[int]:
    """Channels currently enabled for streaming (write()'s block order)."""
    return list(getattr(handle, "_gs_active", range(n_tx_channels(handle))))


def set_active_channels(handle, chans) -> None:
    """Stream only ``chans``. Every enabled channel costs a full-rate IQ
    stream over the USB/network link: two channels at 2.6 Msps need
    20.8 MB/s, which (with anything else on the link) underruns a
    USB-attached Pluto/LibreSDR. The buffer is torn down so the next tx()
    re-creates it with the new channel mask."""
    chans = sorted(set(int(c) for c in chans))
    if chans == active_channels(handle) and getattr(handle, "_gs_active", None):
        return
    try:
        handle.tx_destroy_buffer()
    except Exception:
        pass
    handle.tx_enabled_channels = chans
    handle._gs_active = chans


def open_tx(uri: str, lo_hz: float, sample_rate: float, *,
            rf_bandwidth_hz: float | None = None, xo_ppm: float = 0.0):
    """Open the shared TX context (both ports when the device has two).
    Raises TransmitError if the device silently clamped the requested LO or
    sample rate."""
    import adi  # pyadi-iio
    sdr = adi.ad9361(uri=uri)
    n_tx = _count_tx_channels(sdr)
    sdr._gs_n_tx = n_tx
    sdr._gs_active = list(range(n_tx))
    if xo_ppm:
        # Must precede rate/LO: the driver derives both the BBPLL (sample
        # clock) and the RF synthesizers from this reference value.
        try:
            sdr._ctrl.attrs["xo_correction"].value = str(
                int(round(_XO_NOMINAL_HZ * (1.0 + float(xo_ppm) * 1e-6))))
        except Exception as ex:
            raise TransmitError(f"cannot apply xo_correction: {ex}") from ex
    sdr.tx_enabled_channels = list(range(n_tx))
    sdr.sample_rate = int(sample_rate)
    bw = float(rf_bandwidth_hz) if rf_bandwidth_hz else float(sample_rate)
    try:
        sdr.tx_rf_bandwidth = int(min(max(bw, _RF_BW_MIN_HZ), _RF_BW_MAX_HZ))
    except Exception:
        pass   # a driver without the attribute keeps its default filter
    sdr.tx_lo = int(lo_hz)
    sdr.tx_cyclic_buffer = False
    try:
        # libiio's default 4 kernel buffers x 65536 samples is ~100 ms of
        # cushion at 2.6 Msps; a host-side stall longer than that (GC,
        # GIL held by segment generation) underruns the DAC DMA, which
        # the host never sees. Must be set before the first tx() creates
        # the buffer.
        sdr._txdac.set_kernel_buffers_count(_KERNEL_BUFFERS)
    except Exception:
        pass
    _set_tx_lo_powerdown(sdr, False)
    err = _tx_rate_error_ppm(sdr, sample_rate)
    if err is not None and abs(err) > 0.05:
        # The BBPLL setting the driver picks depends on its previous state;
        # e.g. 2.6 Msps has been seen at 2599998.996 Hz (-0.39 ppm) while
        # the LO stayed exact. The IQ was generated for the nominal rate,
        # so the receiver then sees code and carrier disagree (~115 m/s
        # pseudorange-rate vs Doppler). Re-program once, then report.
        sdr.sample_rate = int(sample_rate) + 1
        sdr.sample_rate = int(sample_rate)
        err = _tx_rate_error_ppm(sdr, sample_rate)
        if err is not None and abs(err) > 0.05:
            _log.warning("AD9361 realised TX rate is off by %.3f ppm from "
                         "%.0f Hz; code/carrier of the stream will disagree",
                         err, sample_rate)
    sdr._gs_rate_err_ppm = err
    if abs(sdr.tx_lo - lo_hz) > 1000:
        raise TransmitError(f"device clamped LO to {sdr.tx_lo}")
    if abs(sdr.sample_rate - sample_rate) > 1.0:
        raise TransmitError(f"device clamped rate to {sdr.sample_rate}")
    return sdr


def set_gain(handle, chan: int, gain_db: float) -> None:
    setattr(handle, f"tx_hardwaregain_chan{chan}", float(gain_db))


def _to_dac(block) -> np.ndarray:
    # pyadi-iio truncates float->int16 and wraps on overflow: round and
    # saturate here so the DAC sees the intended code.
    b = np.asarray(block)
    re = np.clip(np.rint(b.real), -DAC_FULL_SCALE, DAC_FULL_SCALE)
    im = np.clip(np.rint(b.imag), -DAC_FULL_SCALE, DAC_FULL_SCALE)
    return (re + 1j * im).astype(np.complex64)


def write(handle, blocks) -> None:
    """``blocks``: one array per ACTIVE channel, in active_channels() order."""
    n = len(active_channels(handle))
    out = [_to_dac(b) for b in blocks[:n]]
    # pyadi-iio wants a bare array when exactly one channel is enabled.
    handle.tx(out[0] if n == 1 else out)


def close(handle) -> None:
    # KNOWN real-hardware gotcha: with tx_cyclic_buffer=False, the AD9361's
    # TX DMA does not reliably go silent once the app stops feeding it --
    # it can keep repeating the last transferred buffer indefinitely until
    # the whole libiio context is torn down, which tx_destroy_buffer()
    # alone does not guarantee (a documented pyadi-iio/AD9361
    # community-reported behavior, not specific to this app). Force real,
    # deterministic silence before destroying the buffer: mute the
    # channels to the hardware's minimum gain (0 to -89.75 dB, 0.25 dB
    # steps -- AD9361 spec), push one explicit all-zero block, and finally
    # power the TX LO down.
    try:
        for c in range(n_tx_channels(handle)):
            setattr(handle, f"tx_hardwaregain_chan{c}", _MUTE_GAIN_DB)
    except Exception:
        pass
    n = len(active_channels(handle))
    try:
        zero = np.zeros(_ZERO_FLUSH_SAMPLES, dtype=np.complex64)
        handle.tx(zero if n == 1 else [zero] * n)
    except Exception:
        pass
    try:
        handle.tx_destroy_buffer()
    except Exception:
        pass
    _set_tx_lo_powerdown(handle, True)


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
