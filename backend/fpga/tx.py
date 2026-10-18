"""Live TX with the FPGA engine ('gnss' format, SDR kind "tezuka_gnss").

``GnssTx`` drives one run: soft_reset, command mode on, prefill command
buffers, enable; a feeder thread keeps writing non-cyclic DMA buffers (blocking
on the DMA backpressure) and a poller thread reads STATUS / counters about once
per second and reports them next to the host's own block clock. ``stop``
disables the engine and mutes.

``stream`` is the live-session entry point with the same shape as
``backend.rf.transmit.stream`` (progress events, cancel event, result dict).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable

from backend import config
from backend.fpga import device as dev
from backend.fpga import widths as W
from backend.fpga.stream import CommandStream, DmaBuffer
from backend.rf.transmit import TransmitDisabled, TransmitError

_log = logging.getLogger(__name__)

PREFILL_BLOCKS = 8        # command blocks written before the engine is enabled
MIN_LEAD_BLOCKS = 3       # warn when the host is fewer blocks ahead of play
POLL_S = 1.0
SEGMENT_BLOCKS = 128      # live plan segment: 128 blocks = 8.4 Msamples


class GnssTx:
    """One command-mode run on an already opened DAC DMA.

    ``write(words)`` pushes one non-cyclic buffer (uint32 array) and blocks
    while the DMA ring is full; ``mute()`` silences the analog side.
    """

    def __init__(self, device: dev.GnssDevice, write: Callable, fs: float, *,
                 mute: Callable | None = None, progress_cb: Callable | None = None,
                 prefill_blocks: int = PREFILL_BLOCKS,
                 min_lead_blocks: int = MIN_LEAD_BLOCKS, poll_s: float = POLL_S,
                 clock: Callable[[], float] = time.monotonic,
                 cancel: threading.Event | None = None):
        self.device, self._write, self._mute = device, write, mute
        self.fs = float(fs)
        self.progress_cb = progress_cb
        self.prefill_blocks = prefill_blocks
        self.min_lead_blocks = min_lead_blocks
        self.poll_s = poll_s
        self._clock = clock
        self.cancel = cancel if cancel is not None else threading.Event()
        self.fed_blocks = 0                 # block records written (host clock)
        self.fed_buffers = 0
        self.first_block = None
        self.error: Exception | None = None
        self.t_enable: float | None = None
        self.lead_low = 0
        self.poll_errors = 0
        self.last_status: dev.GnssStatus | None = None
        self.min_lead = None
        self._offset_s = None
        self._feeder = self._poller = None
        self._it = None
        self._lock = threading.Lock()
        self._started = False

    # ---- lifecycle -------------------------------------------------------
    def start(self, buffers: Iterable[DmaBuffer]) -> None:
        d = self.device
        d.check_id()
        d.disable()
        d.soft_reset()
        d.set_cmd_mode(True)
        d.clear_counters()
        self._it = iter(buffers)
        # prefill: the CFG buffer and the first blocks, before the engine runs
        while self.fed_blocks < self.prefill_blocks and not self.cancel.is_set():
            if not self._feed_one():
                break
        d.enable()
        self.t_enable = self._clock()
        self._started = True
        self._feeder = threading.Thread(target=self._feed_loop, daemon=True)
        self._poller = threading.Thread(target=self._poll_loop, daemon=True)
        self._feeder.start()
        self._poller.start()

    def _feed_one(self) -> bool:
        buf = next(self._it, None)
        if buf is None:
            return False
        if buf.cyclic:
            raise TransmitError("cyclic buffers cannot carry a command stream")
        self._write(buf.words)
        with self._lock:
            self.fed_buffers += 1
            if buf.n_blocks:
                if self.first_block is None:
                    self.first_block = buf.first_block
                self.fed_blocks += buf.n_blocks
        return True

    def _feed_loop(self) -> None:
        try:
            while not self.cancel.is_set():
                if not self._feed_one():
                    break
        except Exception as ex:
            self.error = ex
            self.cancel.set()

    def _poll_loop(self) -> None:
        while not self.cancel.wait(self.poll_s):
            self.poll_once()

    @property
    def feeding(self) -> bool:
        return self._feeder is not None and self._feeder.is_alive()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the feeder is done (end of data, error or cancel)."""
        if self._feeder is not None:
            self._feeder.join(timeout)
        return not self.feeding

    def poll_once(self) -> dict | None:
        try:
            st = self.device.status()
        except dev.DeviceError as ex:
            self.poll_errors += 1
            _log.warning("GNSS engine status read failed: %s", ex)
            if self.progress_cb:
                self.progress_cb({"gnss_error": str(ex)})
            return None
        ev = self._event(st)
        if self.progress_cb:
            self.progress_cb(ev)
        return ev

    def _event(self, st: dev.GnssStatus) -> dict:
        prev = self.last_status
        self.last_status = st
        now = self._clock()
        with self._lock:
            fed = self.fed_blocks
        elapsed = (now - self.t_enable) if self.t_enable is not None else 0.0
        play_s = st.play_idx / self.fs
        first = self.first_block or 0
        lead = first + fed - st.play_idx / W.BLOCK     # blocks the host is ahead
        if st.playing and self._offset_s is None:
            self._offset_s = elapsed - play_s           # start-up latency
        lag = (elapsed - play_s - self._offset_s) if self._offset_s is not None else None
        if st.running and fed and lead < self.min_lead_blocks:
            self.lead_low += 1
            _log.warning("GNSS host lead low: %.1f blocks ahead of play", lead)
        if st.running:
            self.min_lead = lead if self.min_lead is None else min(self.min_lead, lead)
        if prev is not None and st.underruns > prev.underruns:
            _log.warning("GNSS engine underrun: %d (+%d)", st.underruns,
                         st.underruns - prev.underruns)
        return {
            "elapsed_s": play_s,
            "samples": st.play_idx,
            "underflow": st.sample_fifo_underflows,
            "device_underrun": st.underruns,
            "clipped": st.clipped,
            "gnss": {
                "running": st.running, "playing": st.playing,
                "underruns": st.underruns, "dropped_late": st.dropped_late,
                "clipped": st.clipped, "chip_late": st.chip_late,
                "bad_words": st.bad_words,
                "sample_fifo_underflows": st.sample_fifo_underflows,
                "cmd_fifo_overruns": st.cmd_fifo_overruns,
                "cmd_fifo_ok": st.cmd_fifo_ok, "queued_words": st.queued_words,
                "play_block": round(st.play_block, 3),
                "fed_blocks": fed, "lead_blocks": round(lead, 2),
                "play_lag_s": None if lag is None else round(lag, 4),
            },
        }

    def stop(self) -> dict:
        """Disable the engine, mute, leave command mode. Idempotent; returns
        the last event of the run."""
        self.cancel.set()
        for t in (self._feeder, self._poller):
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=10.0)
        final = None
        if self._started:
            try:
                self.device.disable()
            except Exception as ex:
                _log.warning("GNSS disable failed: %s", ex)
            try:
                final = self.poll_once()
            except Exception:
                pass
        if self._mute is not None:
            try:
                self._mute()
            except Exception as ex:
                _log.warning("TX mute failed: %s", ex)
        if self._started:
            try:
                self.device.soft_reset()
                self.device.set_cmd_mode(False)
            except Exception as ex:
                _log.warning("leaving command mode failed: %s", ex)
            self._started = False
        return final or {}


# ---------------------------------------------------------------------------
def default_device(uri: str) -> dev.GnssDevice:
    runner = dev.SshRunner.from_uri(uri, user=config.GNSS_SSH_USER)
    if config.GNSS_CTL:
        return dev.GnssctlDevice(runner, config.GNSS_BASE_ADDR, ctl=config.GNSS_CTL)
    return dev.RemoteDevmem(runner, config.GNSS_BASE_ADDR)


def _live_buffers(params, session, cancel, *, segment_blocks: int, shift: int = 0):
    """Command buffers for a live session: one engine plan per segment, block
    indices continuing across segments (CFG only in the first)."""
    for first_block, band in session.gnss_bands(params.sample_rate, segment_blocks):
        if cancel.is_set():
            return
        cs = CommandStream(band, first_block=first_block,
                           level_dbfs=params.level_dbfs, cfg=(first_block == 0),
                           cfg_nch="max", shift=shift)
        if first_block == 0:
            _log.info("GNSS plan: %d channels (limit %d at %.3f Msps), dropped %d",
                      cs.nch, cs.channel_plan.nch_max, cs.fs / 1e6, len(cs.dropped))
        yield from cs


def stream(params, session, dry_run: bool = False, progress_cb=None, cancel=None,
           device: dev.GnssDevice | None = None, writer=None,
           segment_blocks: int = SEGMENT_BLOCKS) -> dict:
    """Run a live session on the FPGA engine until ``cancel`` is set.

    Hardware path: ``params.kind`` is "tezuka_gnss"; the engine is checked
    (ID register) before the radio is opened. ``dry_run`` uses an in-memory
    FakeDevice and discards the words. ``device`` / ``writer`` (a callable
    taking the uint32 words) can be injected for tests.
    """
    if not config.ALLOW_TX:
        raise TransmitDisabled("set ALLOW_TX=1 and confirm the isolated setup")
    cancel = cancel if cancel is not None else threading.Event()
    handle = backend = None
    if dry_run:
        eng = dev.FakeEngine(keep_samples=False)
        device = device or dev.FakeDevice(eng)
        writer = writer or (lambda w: (device.push_words(w.tobytes()),
                                       time.sleep(len(w) / 4 / 20e6)))
        mute = None
    else:
        from backend.rf.backends import BACKENDS
        backend = BACKENDS["tezuka_gnss"]
        device = device or default_device(params.uri)
        try:
            device.check_id()
        except dev.DeviceError as ex:
            raise TransmitError(f"GNSS engine unavailable: {ex}") from ex
        try:
            handle = backend.open_tx(params.uri, params.lo_hz, params.sample_rate,
                                     rf_bandwidth_hz=params.tx_rf_bandwidth_hz,
                                     xo_ppm=params.xo_ppm)
            backend.set_gain(handle, 0, params.tx_gain_db)
        except TransmitError:
            raise
        except Exception as ex:
            raise TransmitError(f"device open failed: {ex}") from ex
        writer = writer or (lambda w: backend.write_words(handle, w))
        mute = lambda: backend.close(handle)          # noqa: E731
    tx = GnssTx(device, writer, params.sample_rate, mute=mute,
                progress_cb=progress_cb, cancel=cancel)
    t0 = time.monotonic()
    try:
        tx.start(_live_buffers(params, session, cancel, segment_blocks=segment_blocks))
        while not cancel.is_set() and tx.feeding:
            cancel.wait(0.2)
        if tx.error is not None:
            raise TransmitError(f"GNSS feed failed: {tx.error}") from tx.error
    finally:
        last = tx.stop()
    st = tx.last_status
    return {
        "elapsed_s": (st.play_idx / tx.fs) if st else 0.0,
        "samples": st.play_idx if st else 0,
        "underflow": st.sample_fifo_underflows if st else 0,
        "device_underrun": st.underruns if st else 0,
        "clipped": st.clipped if st else 0,
        "dry_run": dry_run,
        "wall_s": time.monotonic() - t0,
        "gapless": bool(st) and st.underruns == 0 and st.sample_fifo_underflows == 0,
        "min_lead_blocks": tx.min_lead,
        "lead_low": tx.lead_low,
        "poll_errors": tx.poll_errors,
        "gnss": (last or {}).get("gnss"),
    }
