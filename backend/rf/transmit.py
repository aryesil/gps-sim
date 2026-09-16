from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

import numpy as np

from backend import config

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
    chunk_samples: int = 262144
    # Which of the AD9361/AD9363's two TX ports this session drives. TX1
    # and TX2 share one card (one LO, one sample rate -- see
    # backend/rf/dual_tx.py); the slot only selects the physical output
    # port and its independent gain, not a separate device.
    slot: str = "TX1"
    # KNOWN_ISSUES I2 originally assumed gps-sdr-sim's `-b 16` output sits
    # near full int16 scale and defaulted this to 0.25 to protect the
    # AD936x's 12-bit DAC from clipping. Measured against a real generated
    # file, that assumption was wrong: peak amplitude is ~1331 out of
    # int16's +-32767 (~4% of full scale), so there is nothing to clip in
    # the first place. Separately, the 12-bit-in-a-16-bit-word MSB alignment
    # (kernel scan_elements "s12/16>>4" format) is handled inside libiio's
    # own iio_channel_convert() (channel.c, format.shift) using the real
    # hardware format it reads from the driver -- callers write plain
    # int16 values and libiio bit-shifts them into place; no manual
    # shifting or pre-scaling is needed or correct to do here. Default is
    # therefore unity; the knob stays available as a headroom margin for a
    # pathological scenario (e.g. `-p 128` with many simultaneous
    # satellites can push the raw sum toward +-4096) -- lower it only if a
    # real spectrum/power check shows clipping.
    tx_scale: float = 1.0
    # RF-frontend LO-offset support (backend/rf/frontend/rf_frontend.py):
    # non-zero only when the caller resolved a lo_offset_mode other than
    # DISABLED. Mixed into the stream in stream() below via a per-session
    # NCOMixer -- the on-disk IQ file is never touched.
    baseband_offset_hz: float = 0.0


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
                            params.sample_rate, params.tx_gain_db)


def _iter_chunks(path: str, fmt: str, chunk_samples: int):
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
    if params.sample_format not in ("int8", "int16"):
        raise TransmitError(f"bad format {params.sample_format}")
    if params.sample_rate < _TX_RATE_MIN:
        raise TransmitError(f"{params.sample_rate} Hz below AD936x TX minimum {_TX_RATE_MIN}")

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

    chunks = chunk_source if chunk_source is not None else _iter_chunks(
        params.iq_path, params.sample_format, params.chunk_samples)

    total = 0
    t0 = time.monotonic()
    try:
        for chunk in chunks:
            if cancel is not None and cancel.is_set():
                break
            if mixer is not None:
                chunk = mixer.mix(chunk)
            if params.tx_scale != 1.0:
                chunk = chunk * params.tx_scale
            sink.push(chunk)
            total += len(chunk)
            if cancel is not None and cancel.is_set():
                break
            if progress_cb:
                progress_cb({"elapsed_s": total / params.sample_rate,
                             "underflow": int(getattr(sink, "underflow", 0)),
                             "samples": total})
    finally:
        sink.close()
    return {
        "elapsed_s": total / params.sample_rate,
        "underflow": int(getattr(sink, "underflow", 0)),
        "samples": total,
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
