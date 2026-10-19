from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

import numpy as np

from backend import config
from backend.rf import air

# AD9361/AD9363 TX minimum -- shared by both the "pluto" and "bladerf"
# backends, since bladeRF 2.0 micro uses the same transceiver.
_TX_RATE_MIN = 2.083e6
_PUSH_SLICE = 1 << 19      # samples scaled, clipped and queued per step (see stream())


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

    def push_air(self, data: np.ndarray, bytes_per_sample: int) -> None:
        time.sleep(len(data) / bytes_per_sample / self._rate)

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


def _dac_full_scale(kind: str, sample_rate: float | None = None) -> float:
    from backend.rf.backends import BACKENDS
    backend = BACKENDS.get(kind)
    if backend is None:
        raise TransmitError(f"unknown SDR kind {kind!r}: must be one of {sorted(BACKENDS)}")
    # Backends that choose their sample format from the rate (tezuka) expose
    # the full scale of the format they will pick.
    per_rate = getattr(backend, "dac_full_scale_for", None)
    if per_rate is not None and sample_rate:
        return float(per_rate(sample_rate))
    return float(backend.DAC_FULL_SCALE)


def _level_offset_db(kind: str, sample_rate: float | None = None) -> float:
    """dB added to level_dbfs for backends whose sample format does not span the
    DAC (cs4 maps +-7 to -10 dB of full scale)."""
    from backend.rf.backends import BACKENDS
    backend = BACKENDS.get(kind)
    per_rate = getattr(backend, "level_offset_db_for", None)
    if per_rate is not None and sample_rate:
        return float(per_rate(sample_rate))
    return float(getattr(backend, "LEVEL_OFFSET_DB", 0.0))


def _rms(chunk: np.ndarray) -> float:
    c = np.asarray(chunk)
    if not c.size:
        return 0.0
    re = c.real
    im = c.imag
    mean_sq = float(np.mean(re * re + im * im, dtype=np.float64))
    return float(np.sqrt(mean_sq))


def _clip_iq(chunk: np.ndarray, lim: float) -> tuple[np.ndarray, int]:
    if not chunk.size:
        return chunk, 0
    re = np.real(chunk)
    im = np.imag(chunk)
    if (float(np.max(re)) <= lim and float(np.min(re)) >= -lim
            and float(np.max(im)) <= lim and float(np.min(im)) >= -lim):
        return chunk, 0
    over = (np.abs(re) > lim) | (np.abs(im) > lim)
    n = int(np.count_nonzero(over))
    if n:
        chunk = (np.clip(re, -lim, lim) + 1j * np.clip(im, -lim, lim)).astype(
            chunk.dtype, copy=False)
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
            # Tek geçişte complex64: eski ifade int16 -> complex128 ara dizileri
            # üretiyordu (16 B/örnek, 3 geçiş). int16 değerler float32'de tam
            # temsil edilir; backend'ler zaten complex64'e indirgiyor.
            out = np.empty(len(arr) // 2, dtype=np.complex64)
            out.real = arr[0::2]
            out.imag = arr[1::2]
            yield out


_GATE_PREP_S = 0.25   # signal prepared before a start gate opens (> dual_tx _PREFILL_S)


def stream(params: TxParams, dry_run: bool = False, progress_cb=None,
           cancel=None, chunk_source=None, start_gate=None) -> dict:
    """Push ``chunk_source`` (or the IQ file) to the SDR.

    ``start_gate`` (Start Now scheduling): a callable run once, right before
    the first sample is handed to the sink, after the first ~0.25 s have been
    level-measured, scaled and clipped, so that what follows the gate is only
    queueing. It blocks until the planned start and returns None to cancel the
    run (anything else = go)."""
    if not config.ALLOW_TX:
        raise TransmitDisabled("set ALLOW_TX=1 and confirm the isolated setup")
    if params.sample_format not in ("int8", "int12", "int16"):
        raise TransmitError(f"bad format {params.sample_format}")
    if params.sample_rate < _TX_RATE_MIN:
        raise TransmitError(f"{params.sample_rate} Hz below AD936x TX minimum {_TX_RATE_MIN}")

    dac_fs = _dac_full_scale(params.kind, params.sample_rate)
    level_offset_db = _level_offset_db(params.kind, params.sample_rate)
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
    # Real-time budget: seconds spent per second of signal, per stage. Both
    # must stay well below 1 or the stream cannot be sustained; they are
    # reported with every progress event so a starving pipeline is visible
    # instead of showing up as a mysterious receiver that never locks.
    feed_s = 0.0      # scale + clip (CPU time of the feeder itself)
    wait_s = 0.0      # blocked waiting for the next chunk from the source
    max_load = 0.0
    min_lead = float('inf')
    air_gain = None
    # Start gate: parts prepared before it opens, pushed right after it.
    gate_pending = start_gate is not None
    gate_samples = int(_GATE_PREP_S * params.sample_rate)
    pending: list = []          # (push_fn, data)
    pending_n = 0
    gate_cancelled = False

    def _push(fn, data, n, last_of_chunk):
        """Push now, or hold until the gate opens; False = gate cancelled."""
        nonlocal gate_pending, pending_n, gate_cancelled
        if not gate_pending:
            fn(data)
            return True
        pending.append((fn, data))
        pending_n += n
        if pending_n < gate_samples and not last_of_chunk:
            return True
        gate_pending = False
        if start_gate() is None:
            gate_cancelled = True
            pending.clear()
            return False
        for f, d in pending:
            f(d)
        pending.clear()
        return True

    try:
        it = iter(chunks)
        while True:
            t_w = time.monotonic()
            chunk = next(it, None)
            wait_s += time.monotonic() - t_w
            if chunk is None:
                break
            if cancel is not None and cancel.is_set():
                break
            comp = 0.0
            if isinstance(chunk, air.AirChunk):
                if mixer is not None:
                    raise TransmitError("packed air chunks cannot be frequency-shifted")
                # Already scaled, saturated and packed by the producer process:
                # nothing to compute here, only slice and hand over.
                g = air_gain = chunk.gain
                clipped += chunk.clipped
                chunk_len = chunk.n_samples
                step = _PUSH_SLICE * chunk.bytes_per_sample
                bps = chunk.bytes_per_sample
                for lo in range(0, chunk.data.size, step):
                    piece = chunk.data[lo:lo + step]
                    if not _push(lambda d, b=bps: sink.push_air(d, b), piece,
                                 piece.size // bps, lo + step >= chunk.data.size):
                        break
                    if cancel is not None and cancel.is_set():
                        break
            else:
                if level is None:
                    t_a = time.monotonic()
                    r = _rms(chunk)
                    if r > 0.0:
                        level = dac_fs * 10.0 ** ((params.level_dbfs + level_offset_db) / 20.0) / r
                    comp += time.monotonic() - t_a
                g = (level if level is not None else 1.0) * params.tx_scale
                # A live segment is a full second (240 MB at 30 Msps). Scaling and
                # clipping it in one go takes longer than the sink's queue holds,
                # so the pump ran dry once per segment. Work in slices and push
                # each one, which keeps the queue filling continuously.
                chunk_len = len(chunk)
                for lo in range(0, chunk_len, _PUSH_SLICE):
                    t_a = time.monotonic()
                    part = chunk[lo:lo + _PUSH_SLICE]
                    if mixer is not None:
                        part = mixer.mix(part)
                    if g != 1.0:
                        part = part * g
                    # Out-of-range floats wrap when the backend casts to int16 --
                    # saturate here instead, and count it.
                    part, n_clip = _clip_iq(part, dac_fs)
                    clipped += n_clip
                    comp += time.monotonic() - t_a
                    if not _push(sink.push, part, len(part),
                                 lo + _PUSH_SLICE >= chunk_len):
                        break
                    if cancel is not None and cancel.is_set():
                        break
            if gate_cancelled:
                break
            total += chunk_len
            feed_s += comp
            if cancel is not None and cancel.is_set():
                break
            # CPU time only: sink.push() blocks while the queue is full, which
            # is the normal state of a healthy stream.
            load = comp / max(chunk_len / params.sample_rate, 1e-9)
            max_load = max(max_load, load)
            if progress_cb:
                ev = {"elapsed_s": total / params.sample_rate,
                      "underflow": int(getattr(sink, "underflow", 0)),
                      "samples": total, "clipped": clipped,
                      "digital_scale": g,
                      "feeder_load": round(load, 3)}
                q_s = getattr(sink, "queue_s", None)
                if q_s is not None:
                    ev["queue_s"] = round(float(q_s), 3)
                lead = getattr(sink, "lead_s", None)
                if lead is not None:
                    ev["lead_s"] = round(float(lead), 3)
                    min_lead = min(min_lead, lead)
                ev["device_underrun"] = int(getattr(sink, "device_underrun", 0))
                progress_cb(ev)
    finally:
        sink.close()
    return {
        "elapsed_s": total / params.sample_rate,
        "underflow": int(getattr(sink, "underflow", 0)),
        "samples": total,
        "clipped": clipped,
        "digital_scale": (air_gain if air_gain is not None
                          else (level if level is not None else 1.0) * params.tx_scale),
        "dry_run": dry_run,
        "wall_s": time.monotonic() - t0,
        "feeder_s": round(feed_s, 3),
        "max_feeder_load": round(max_load, 3),
        "source_wait_s": round(wait_s, 3),
        # False when the stream had gaps after it started (zero blocks on the
        # air): such a run says nothing about the signal itself.
        "gapless": (int(getattr(sink, "underflow", 0)) == 0
                    and int(getattr(sink, "device_underrun", 0)) == 0),
        "device_underrun": int(getattr(sink, "device_underrun", 0)),
        "min_lead_s": None if min_lead == float("inf") else round(min_lead, 3),
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
