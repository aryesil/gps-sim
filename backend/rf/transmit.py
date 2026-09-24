from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

import numpy as np

from backend import config

# AD9361/AD9363 TX minimum -- shared by both the "pluto" and "bladerf"
# backends, since bladeRF 2.0 micro uses the same transceiver.
_TX_RATE_MIN = 2.083e6


class TransmitDisabled(Exception):
    pass


class TransmitError(Exception):
    pass


@dataclass
class TxParams:
    iq_path: str
    sample_rate: float
    sample_format: str
    lo_hz: float = config.L1_HZ
    tx_gain_db: float = -50.0
    uri: str = config.DEVICE_URI
    # Which hardware backend `uri` is reached through -- "pluto" (default,
    # PlutoSDR/AD936x IIO clones) or "bladerf" (Nuand bladeRF 2.0 micro,
    # same AD9361/AD9363 transceiver as Pluto, different driver stack --
    # see backend/rf/backends/).
    kind: str = "pluto"
    chunk_samples: int = 262144
    # Which of the AD9361/AD9363's two TX ports this session drives. TX1
    # and TX2 share one card (one LO, one sample rate -- see
    # backend/rf/dual_tx.py); the slot only selects the physical output
    # port and its independent gain, not a separate device.
    slot: str = "TX1"
    # Extra linear multiplier applied AFTER the automatic DAC leveling
    # below (level_dbfs). 1.0 = leave the leveled signal alone.
    #
    # History (KNOWN_ISSUES I2): this used to be the ONLY scaling, on the
    # belief that libiio's iio_channel_convert() MSB-aligns 12-bit samples
    # inside a 16-bit word. It does not on this path: pyadi-iio's tx()
    # casts the complex array to int16 and writes the raw bytes into the
    # DMA buffer (iio_buffer write, no convert), and the AD9361 DAC takes
    # the 12 MOST significant bits of each 16-bit word (the reason ADI's
    # own Pluto examples scale by 2**14). A +-1331 gps-sdr-sim sample
    # therefore drove the DAC at +-83 LSB (~-28 dBFS), an int8 file at +-7
    # LSB -- a few DAC codes carrying the whole constellation. bladeRF's
    # SC16Q11 is the opposite: +-2047 full scale, so a native int16 file
    # (RMS ~-15 dBFS of 32767) wrapped around the 12-bit field.
    tx_scale: float = 1.0
    # RF-frontend LO-offset support (backend/rf/frontend/rf_frontend.py):
    # non-zero only when the caller resolved a lo_offset_mode other than
    # DISABLED. Mixed into the stream in stream() below via a per-session
    # NCOMixer -- the on-disk IQ file is never touched.
    baseband_offset_hz: float = 0.0
    # Automatic DAC leveling: the stream is scaled so its composite RMS sits
    # at this many dB below the selected backend's DAC full scale
    # (backends' DAC_FULL_SCALE), independent of the file's own sample
    # format or generator. The scale is measured once, from the first
    # non-silent chunk, and then held fixed for the whole session (no AGC
    # breathing). -15 dBFS RMS keeps a Gaussian-like GNSS composite's
    # per-rail peaks ~8 sigma below clipping. None = legacy raw pass-through.
    # Output power is then deterministic: DAC full-scale power +
    # level_dbfs + tx_gain_db.
    level_dbfs: float | None = -15.0
    # AD936x analog TX low-pass (RF) bandwidth. None = the sample rate, so
    # the filter passes the whole generated band and still cuts DAC images.
    # Before this was applied the device kept whatever bandwidth the last
    # user left programmed (possibly narrower than the signal).
    tx_rf_bandwidth_hz: float | None = None
    # Reference-oscillator error of the SDR in ppm (+ = oscillator runs
    # fast). A stock PlutoSDR's 40 MHz crystal is only +-25 ppm: at L1 that
    # is up to +-39 kHz carrier and +-25 chip/s code-rate error seen by the
    # receiver as a huge clock drift. Programmed into the AD9361 driver's
    # xo_correction, which re-derives both the LO and the sample clock.
    # Pluto only; measure it once (e.g. receiver-reported clock drift).
    xo_ppm: float = 0.0


class _DrySink:
    underflow = 0

    def __init__(self, rate: float):
        self._rate = rate

    def push(self, chunk: np.ndarray) -> None:
        time.sleep(len(chunk) / self._rate)

    def close(self) -> None:
        pass


def _open_device(params: TxParams):
    # TX1/TX2 share one AD9361/AD9363 card (one LO, one sample rate) --
    # dual_tx owns the shared context and the synchronized dual-channel
    # feed; see its module docstring. A TransmitError it raises for a
    # LO/rate conflict between the two slots propagates unchanged (see the
    # try/except in stream() below).
    from backend.rf import dual_tx
    return dual_tx.acquire(params.slot, params.uri, params.lo_hz,
                            params.sample_rate, params.tx_gain_db,
                            params.kind,
                            rf_bandwidth_hz=params.tx_rf_bandwidth_hz,
                            xo_ppm=params.xo_ppm)


def _dac_full_scale(kind: str) -> float:
    from backend.rf.backends import BACKENDS
    backend = BACKENDS.get(kind)
    if backend is None:
        raise TransmitError(f"unknown SDR kind {kind!r}: must be one of {sorted(BACKENDS)}")
    return float(backend.DAC_FULL_SCALE)


def _rms(chunk: np.ndarray) -> float:
    c = np.asarray(chunk)
    return float(np.sqrt(np.mean(c.real.astype(np.float64) ** 2
                                 + c.imag.astype(np.float64) ** 2))) if c.size else 0.0


def _clip_iq(chunk: np.ndarray, lim: float) -> tuple[np.ndarray, int]:
    re = np.real(chunk)
    im = np.imag(chunk)
    n = int(np.count_nonzero((np.abs(re) > lim) | (np.abs(im) > lim)))
    if n:
        chunk = np.clip(re, -lim, lim) + 1j * np.clip(im, -lim, lim)
    return chunk, n


def _iter_chunks(path: str, fmt: str, chunk_samples: int):
    # int12 is the native engine's 12-bit range in an int16 container.
    dtype = np.int8 if fmt == "int8" else np.int16
    itemsize = np.dtype(dtype).itemsize
    with open(path, "rb") as fh:
        while True:
            raw = fh.read(chunk_samples * 2 * itemsize)
            if not raw:
                return
            arr = np.frombuffer(raw, dtype=dtype)
            arr = arr[: len(arr) - (len(arr) % 2)]
            yield (arr[0::2].astype(np.int16) + 1j * arr[1::2].astype(np.int16))


def stream(params: TxParams, dry_run: bool = False, progress_cb=None,
           cancel=None, chunk_source=None) -> dict:
    if not config.ALLOW_TX:
        raise TransmitDisabled("set ALLOW_TX=1 and confirm the isolated setup")
    if params.sample_format not in ("int8", "int12", "int16"):
        raise TransmitError(f"bad format {params.sample_format}")
    if params.sample_rate < _TX_RATE_MIN:
        raise TransmitError(f"{params.sample_rate} Hz below AD936x TX minimum {_TX_RATE_MIN}")

    dac_fs = _dac_full_scale(params.kind)
    sink = _DrySink(params.sample_rate)
    if not dry_run:
        try:
            sink = _open_device(params)
        except TransmitError:
            raise
        except Exception as ex:  # ImportError, AttributeError, iio errors, ...
            raise TransmitError(f"device open failed: {ex}") from ex

    mixer = None
    if params.baseband_offset_hz:
        from backend.rf.frontend.nco import NCOMixer
        mixer = NCOMixer(params.baseband_offset_hz, params.sample_rate)
        # An LO offset moves the carrier off the AD9361's own TX LO, which
        # is exactly the situation its internal TX quadrature calibration
        # was tuned for. Best-effort: calibrate() itself never raises (see
        # backend/rf/frontend/calibration.py), it degrades to a reported
        # failure on hardware/driver combinations that don't support it.
        if not dry_run:
            sdr = getattr(sink, "sdr", None)
            if sdr is not None:
                from backend.rf.frontend import calibration, capabilities
                caps = capabilities.detect(sdr)
                cal = calibration.calibrate(
                    sdr, caps, tx_lo_hz=int(params.lo_hz),
                    target_rf_hz=int(params.lo_hz + params.baseband_offset_hz),
                    baseband_offset_hz=int(params.baseband_offset_hz))
                if progress_cb:
                    progress_cb({"elapsed_s": 0.0,
                                 "underflow": int(getattr(sink, "underflow", 0)),
                                 "samples": 0, "calibration": vars(cal)})

    chunks = chunk_source if chunk_source is not None else _iter_chunks(
        params.iq_path, params.sample_format, params.chunk_samples)

    total = 0
    clipped = 0
    # None until measured; the legacy raw path fixes it to 1.0 up front.
    level = None if params.level_dbfs is not None else 1.0
    t0 = time.monotonic()
    try:
        for chunk in chunks:
            if cancel is not None and cancel.is_set():
                break
            if mixer is not None:
                chunk = mixer.mix(chunk)
            if level is None:
                r = _rms(chunk)
                if r > 0.0:
                    level = dac_fs * 10.0 ** (params.level_dbfs / 20.0) / r
            g = (level if level is not None else 1.0) * params.tx_scale
            if g != 1.0:
                chunk = chunk * g
            # Out-of-range floats wrap when the backend casts to int16 --
            # saturate here instead, and count it.
            chunk, n_clip = _clip_iq(chunk, dac_fs)
            clipped += n_clip
            sink.push(chunk)
            total += len(chunk)
            if cancel is not None and cancel.is_set():
                break
            if progress_cb:
                progress_cb({"elapsed_s": total / params.sample_rate,
                             "underflow": int(getattr(sink, "underflow", 0)),
                             "samples": total, "clipped": clipped,
                             "digital_scale": g})
    finally:
        sink.close()
    return {
        "elapsed_s": total / params.sample_rate,
        "underflow": int(getattr(sink, "underflow", 0)),
        "samples": total,
        "clipped": clipped,
        "digital_scale": (level if level is not None else 1.0) * params.tx_scale,
        "dry_run": dry_run,
        "wall_s": time.monotonic() - t0,
    }


class TxSession:
    def __init__(self, params: TxParams, dry_run: bool = False, chunk_source=None):
        self._params = params
        self._dry_run = dry_run
        self._chunk_source = chunk_source
        self._cancel = threading.Event()
        self._thread = None
        self._result = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        """Blocking generator: yields progress dicts until the file ends or stop()."""
        q = queue.Queue()
        def _cb(d): q.put(d)
        def _run():
            try:
                self._result = stream(self._params, dry_run=self._dry_run,
                                      progress_cb=_cb, cancel=self._cancel,
                                      chunk_source=self._chunk_source)
            finally:
                q.put(None)
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        while True:
            d = q.get()
            if d is None:
                break
            yield d

    def stop(self):
        self._cancel.set()
