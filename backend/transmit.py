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
    # gps-sdr-sim's `-b 16` output sits near full int16 scale (+-32767), but
    # pyadi-iio hands samples straight to the AD936x's 12-bit DAC with no
    # scaling of its own (KNOWN_ISSUES I2) -- unscaled, that clips/wraps and
    # distorts the L1 spectrum. Attenuate before tx() by default; lower this
    # further (e.g. 0.0625) if a spectrum check still shows clipping.
    tx_scale: float = 0.25


class _DrySink:
    underflow = 0

    def __init__(self, rate: float):
        self._rate = rate

    def push(self, chunk: np.ndarray) -> None:
        time.sleep(len(chunk) / self._rate)

    def close(self) -> None:
        pass


def _open_device(params: TxParams):
    import adi  # pyadi-iio
    sdr = adi.Pluto(uri=params.uri)
    sdr.tx_lo = int(params.lo_hz)
    sdr.sample_rate = int(params.sample_rate)
    sdr.tx_hardwaregain_chan0 = float(params.tx_gain_db)
    sdr.tx_cyclic_buffer = False
    if abs(sdr.tx_lo - params.lo_hz) > 1000:
        raise TransmitError(f"device clamped LO to {sdr.tx_lo}")
    if abs(sdr.sample_rate - params.sample_rate) > 1.0:
        raise TransmitError(f"device clamped rate to {sdr.sample_rate}")

    class _PyadiSink:
        underflow = 0

        def push(self, chunk):
            sdr.tx(chunk)

        def close(self):
            sdr.tx_destroy_buffer()

    return _PyadiSink()


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
           cancel=None) -> dict:
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

    total = 0
    t0 = time.monotonic()
    try:
        for chunk in _iter_chunks(params.iq_path, params.sample_format, params.chunk_samples):
            if cancel is not None and cancel.is_set():
                break
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
    def __init__(self, params: TxParams, dry_run: bool = False):
        self._params = params
        self._dry_run = dry_run
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
                                      progress_cb=_cb, cancel=self._cancel)
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
