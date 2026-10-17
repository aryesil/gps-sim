"""Shared-context dual-channel TX for the AD9361/AD9363's TX1/TX2 ports.

Hardware fact (see the ``_tx_slots`` comment in backend/app.py, and
https://ez.analog.com/rf/wide-band-rf-transceivers/design-support/f/q-a/78602
for ADI's own confirmation): TX1 and TX2 share ONE TX synthesizer and one
DAC sample-rate clock -- they can only ever run at the same LO frequency
and the same sample rate. Gain (``tx_hardwaregain_chan0``/``chan1``) and
baseband content are independent per port; pyadi-iio's ``tx_lo`` and
``sample_rate`` are single, chip-wide attributes with no per-channel
variant (confirmed against adi/ad936x.py).

Genuine simultaneous dual-channel transmit therefore needs ONE libiio
context with both channels enabled, fed in lockstep: pyadi-iio's ``tx()``
takes a list of one array per enabled channel and pushes them as a single
interleaved hardware buffer -- it has no notion of "channel 0 whenever
it's ready, channel 1 separately, at its own cadence". This module owns
that shared context and a background "pump" thread that feeds it a
fixed-size block per port every cycle, decoupled from whatever size and
cadence each of the two producers (a live session generating ~1 s
mega-chunks, a file replay pushing fixed 256k-sample chunks, ...) happens
to push samples in: a slot's incoming chunks are split into
``_BLOCK_SAMPLES``-sized pieces on push and queued; the pump always pulls
exactly one block from each active slot's queue every cycle (zero-filling
and counting an underflow if that slot's queue is momentarily empty), and
feeds silence on a slot nobody has acquired.

Only reached from transmit.py's non-dry-run path -- dry runs never import
this module at all, so they stay decoupled from the whole synchronization
machinery.
"""
from __future__ import annotations

import math
import queue
import threading
import time

import numpy as np

from backend import config
from backend.rf.backends import BACKENDS
from backend.rf.transmit import TransmitError

_BLOCK_SAMPLES = 65536
_TEMP_CONFIRM_S = 0.05  # gap between the confirming re-reads of an over-limit sample
_TEMP_CHECK_S = 2.0      # die-temperature guard period (config.TX_MAX_TEMP_C)
_GET_TIMEOUT_S = 0.5     # how long the pump waits for a slot before feeding silence
_PUT_TIMEOUT_S = 10.0    # how long push() waits for pump to drain before giving up
_QUEUE_DEPTH = 8         # minimum blocks of smoothing headroom per slot
_QUEUE_HEADROOM_S = 0.5   # ...and at least this much signal time at high rates
_PREFILL_S = 0.2         # signal time that must be queued before the first write
_PREFILL_GRACE_S = 0.1   # ...or this long after the first block, whichever is first
_SLOT_CHAN = {"TX1": 0, "TX2": 1}
_CHAN_SLOT = {v: k for k, v in _SLOT_CHAN.items()}


def _queue_depth(sample_rate: float) -> int:
    """Blocks a slot queue holds. 8 blocks are 26 ms at 20 Msps, far less than
    a feeder hiccup (GC, a segment hand-over, the OS scheduler), so keep at
    least _QUEUE_HEADROOM_S of signal.
    """
    blocks = int(math.ceil(_QUEUE_HEADROOM_S * float(sample_rate) / _BLOCK_SAMPLES))
    return max(_QUEUE_DEPTH, blocks)


def _prefill_blocks(sample_rate: float) -> int:
    """Blocks that must be queued before the pump writes anything: the stream
    then starts with a cushion instead of racing the producer from block one."""
    blocks = int(math.ceil(_PREFILL_S * float(sample_rate) / _BLOCK_SAMPLES))
    return max(1, min(blocks, _queue_depth(sample_rate)))


class _Card:
    """The one shared context, live for as long as at least one slot is
    acquired. Not constructed directly -- see acquire()/_release()."""

    def __init__(self, handle, uri: float, lo_hz: float, sample_rate: float,
                 kind: str):
        self.handle = handle
        self.backend = BACKENDS[kind]
        self.kind = kind
        self.uri = uri
        self.lo_hz = lo_hz
        self.sample_rate = sample_rate
        self.lock = threading.Lock()          # guards queues/underflow/active below
        self.queues: dict[str, queue.Queue] = {}
        # Only gaps after the stream has started count: the zero blocks the
        # pump used to feed while the first segment was still being generated
        # are not on the air (nothing is written until data is queued).
        self.underflow: dict[str, int] = {}
        self.min_fill: dict[str, int] = {}     # lowest queue length seen since start
        self.started = False
        self.start_t: float | None = None
        # Device-side lead: seconds of signal written to the board but not yet
        # played, by dead reckoning (written / rate - elapsed since the first
        # write). The board's DMA repeats stale data when it runs dry, which
        # the host-side queue cannot see; lead <= 0 means that happened.
        self.written = 0
        self.min_lead = float("inf")
        self.device_underrun = 0
        self.lead_seen = False
        self.first_data_t: float | None = None
        self.active = 0
        # Pump thread'ini öldüren backend.write hatası (USB kopması, IIO
        # EIO, ...). Üreticiler bunu enqueue()'da görür; aksi halde 10 s
        # "queue full" zaman aşımı gerçek nedeni gizlerdi.
        self.error: Exception | None = None
        self.stop = threading.Event()
        self.pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
        # Üretici tıkanınca pump'ın bekleme süresi ~3 blok süresiyle sınırlı
        # (5 Msps'de ~39 ms). 0.5 s beklemek DAC DMA'sını kuruturdu: ad9361
        # backend notunda belirtildiği gibi tx_cyclic_buffer=False iken DMA
        # son buffer'ı tekrarlar -> alıcıya eski (bayat) IQ gider. Kısa
        # beklemeden sonra sessizlik bloğu beslemek hem hat zamanlamasını
        # korur hem bayat tekrarı önler.
        self.get_timeout = max(0.02, min(_GET_TIMEOUT_S,
                                         3.0 * _BLOCK_SAMPLES / max(sample_rate, 1.0)))
        self.prefill_blocks = _prefill_blocks(sample_rate)
        # Bytes per complex sample when the producer hands over packed air
        # chunks (backend.rf.air); None while the queues carry complex64.
        self.air_bps: int | None = None

    def _ready_to_start(self) -> bool:
        """True once data is queued: a prefill's worth, or the grace period
        after the first block (a short file never reaches the prefill)."""
        first = self.first_data_t
        if first is None:
            return False
        if time.monotonic() - first >= _PREFILL_GRACE_S:
            return True
        with self.lock:
            qs = list(self.queues.values())
        return bool(qs) and all(q.qsize() >= self.prefill_blocks for q in qs)

    def _pump_loop(self) -> None:
        zero = np.zeros(_BLOCK_SAMPLES, dtype=np.complex64)
        zero_air = None
        stop = self.stop
        next_temp = time.monotonic() + _TEMP_CHECK_S
        while not stop.is_set():
            # Isı koruması: ısınan bir kart (soğutucusuz, yüksek hız) çipin
            # sınırına gitmeden yayın durdurulur; hata üreticiye iletilir.
            limit = float(getattr(config, "TX_MAX_TEMP_C", 0.0) or 0.0)
            if limit > 0 and time.monotonic() >= next_temp:
                next_temp = time.monotonic() + _TEMP_CHECK_S
                reader = getattr(self.backend, "temperature_c", None)
                t = reader(self.handle) if reader is not None else None
                if t is not None and t >= limit:
                    # The die sensor throws isolated wild samples (seen: +104 C
                    # and -22 C against a steady ~68 C); trip only when a quick
                    # re-read agrees.
                    for _ in range(2):
                        time.sleep(_TEMP_CONFIRM_S)
                        t2 = reader(self.handle)
                        if t2 is None or t2 < limit:
                            t = None
                            break
                if t is not None and t >= limit:
                    self.error = TransmitError(
                        f"AD9361 die temperature {t:.1f} C reached the "
                        f"{limit:.0f} C limit (TX_MAX_TEMP_C); stream stopped")
                    stop.set()
                    return
            if not self.started:
                if not self._ready_to_start():
                    stop.wait(0.002)
                    continue
                self.started = True
                self.start_t = time.monotonic()
            with self.lock:
                slots = dict(self.queues)
            blocks = []
            for chan in self.backend.active_channels(self.handle):
                slot = _CHAN_SLOT[chan]
                q = slots.get(slot)
                if q is None:
                    blocks.append(zero)
                    continue
                try:
                    blocks.append(q.get(timeout=self.get_timeout))
                    n = q.qsize()
                    if n < self.min_fill.get(slot, n + 1):
                        self.min_fill[slot] = n
                except queue.Empty:
                    if stop.is_set():
                        break
                    with self.lock:
                        self.underflow[slot] = self.underflow.get(slot, 0) + 1
                    if self.air_bps:
                        if zero_air is None:
                            zero_air = np.zeros(_BLOCK_SAMPLES * self.air_bps, dtype=np.uint8)
                        blocks.append(zero_air)
                    else:
                        blocks.append(zero)
            else:
                try:
                    if self.air_bps:
                        self.backend.write_air(self.handle, blocks)
                    else:
                        self.backend.write(self.handle, blocks)
                except Exception as ex:
                    self.error = ex
                    stop.set()
                    return
                self.written += _BLOCK_SAMPLES
                lead = (self.written / self.sample_rate
                        - (time.monotonic() - self.start_t))
                # the first blocks go in faster than real time while the
                # kernel ring fills: a negative lead there is start-up, not
                # starvation. Judge only once the ring has been ahead once.
                if lead >= 0.02:
                    self.lead_seen = True
                if self.lead_seen:
                    if lead < self.min_lead:
                        self.min_lead = lead
                    if lead < 0.0:
                        self.device_underrun += 1

    def ensure_channel(self, chan: int) -> None:
        """Make sure ``chan`` is streaming. Only the channels in use are
        enabled (see the backend's set_active_channels); a second slot
        joining re-creates the buffer with both, which briefly pauses the
        running slot's stream."""
        active = self.backend.active_channels(self.handle)
        if chan in active:
            if self.queues and not self.pump_thread.is_alive():
                self.stop = threading.Event()
                self.pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
                self.pump_thread.start()
            return
        if self.pump_thread.is_alive():
            self.stop.set()
            self.pump_thread.join(timeout=5.0)
            if self.pump_thread.is_alive():
                raise TransmitError("dual TX pump thread did not stop in time to reconfigure channels")
        self.backend.set_active_channels(self.handle, active + [chan])
        if self.queues and not self.pump_thread.is_alive():
            self.stop = threading.Event()
            self.pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
            self.pump_thread.start()

    def register(self, slot: str) -> None:
        with self.lock:
            self.queues[slot] = queue.Queue(maxsize=_queue_depth(self.sample_rate))
            self.underflow.setdefault(slot, 0)
            self.min_fill.pop(slot, None)
            self.active += 1

    def unregister(self, slot: str) -> bool:
        """Returns True if this was the last active slot (caller should
        tear the whole card down)."""
        with self.lock:
            self.queues.pop(slot, None)
            self.active -= 1
            return self.active <= 0

    def enqueue(self, slot: str, block: np.ndarray) -> None:
        with self.lock:
            q = self.queues.get(slot)
        if q is None:
            return  # slot already closed -- drop rather than raise mid-teardown
        if self.error is not None:
            raise TransmitError(f"dual TX pump died: {self.error}") from self.error
        try:
            q.put(block, timeout=_PUT_TIMEOUT_S)
            if self.first_data_t is None:
                self.first_data_t = time.monotonic()
        except queue.Full as ex:
            if self.error is not None:
                raise TransmitError(f"dual TX pump died: {self.error}") from self.error
            raise TransmitError(
                f"dual TX pump on {slot} appears stuck (queue full after "
                f"{_PUT_TIMEOUT_S}s) -- the other TX slot may have stalled "
                "the shared card") from ex

    def shut_down(self) -> None:
        self.stop.set()
        if self.pump_thread.is_alive():
            self.pump_thread.join(timeout=5.0)
        self.backend.close(self.handle)


class _SlotSink:
    """Duck-types transmit.py's sink protocol (push/close/underflow),
    exactly like _DrySink and the old single-channel _PyadiSink."""

    def __init__(self, card: _Card, slot: str):
        self._card = card
        self._slot = slot
        self._carry = np.zeros(0, dtype=np.complex64)
        self._carry_air = np.zeros(0, dtype=np.uint8)

    @property
    def sdr(self):
        """The shared card's real hardware handle -- lets a caller (see
        transmit.py's TX quadrature calibration trigger, an AD9361-only
        capability) reach it without depending on dual_tx's internal _Card
        layout. Named `sdr` for that caller's benefit; the card's own
        attribute is the backend-agnostic `handle`."""
        return self._card.handle

    @property
    def underflow(self) -> int:
        with self._card.lock:
            return self._card.underflow.get(self._slot, 0)

    @property
    def queue_s(self) -> float | None:
        """Lowest queue fill (seconds of signal) since the last call, or None
        before the stream has started. A value near 0 means the pump was about
        to run dry; it is reset on read so each progress report covers its own
        interval."""
        card = self._card
        with card.lock:
            n = card.min_fill.pop(self._slot, None)
        if n is None:
            return None
        return n * _BLOCK_SAMPLES / card.sample_rate

    @property
    def device_underrun(self) -> int:
        """Writes after which the board had nothing left to play (dead
        reckoning, see _Card.written). The air signal then holds stale samples
        -- for the GNSS receiver a CW at the carrier."""
        return self._card.device_underrun

    @property
    def lead_s(self) -> float | None:
        """Lowest device-side lead (seconds) since the last read; None before
        the first write."""
        card = self._card
        v = card.min_lead
        card.min_lead = float("inf")
        return None if v == float("inf") else v

    def push(self, chunk: np.ndarray) -> None:
        chunk = np.asarray(chunk, dtype=np.complex64)
        if self._carry.size:
            chunk = np.concatenate([self._carry, chunk])
        n_full = (chunk.size // _BLOCK_SAMPLES) * _BLOCK_SAMPLES
        for i in range(0, n_full, _BLOCK_SAMPLES):
            self._card.enqueue(self._slot, chunk[i:i + _BLOCK_SAMPLES])
        self._carry = chunk[n_full:]

    def push_air(self, data: np.ndarray, bytes_per_sample: int) -> None:
        """Queue packed samples (backend.rf.air). Same fixed block size in
        samples as push(); a block is _BLOCK_SAMPLES * bytes_per_sample bytes."""
        card = self._card
        if card.air_bps is None:
            if not hasattr(card.backend, "write_air"):
                raise TransmitError(f"{card.kind} cannot take packed air chunks")
            card.air_bps = int(bytes_per_sample)
        elif card.air_bps != bytes_per_sample:
            raise TransmitError("packed chunk format changed mid-stream")
        data = np.asarray(data, dtype=np.uint8)
        if self._carry_air.size:
            data = np.concatenate([self._carry_air, data])
        blk = _BLOCK_SAMPLES * bytes_per_sample
        n_full = (data.size // blk) * blk
        for i in range(0, n_full, blk):
            card.enqueue(self._slot, data[i:i + blk])
        self._carry_air = data[n_full:]

    def close(self) -> None:
        if self._carry_air.size:
            blk = _BLOCK_SAMPLES * self._card.air_bps
            pad = np.zeros(blk, dtype=np.uint8)
            pad[:self._carry_air.size] = self._carry_air
            self._card.enqueue(self._slot, pad)
            self._carry_air = np.zeros(0, dtype=np.uint8)
        if self._carry.size:
            pad = np.zeros(_BLOCK_SAMPLES, dtype=np.complex64)
            pad[:self._carry.size] = self._carry
            self._card.enqueue(self._slot, pad)
            self._carry = np.zeros(0, dtype=np.complex64)
        _release(self._card, self._slot)


_lock = threading.Lock()
_card: _Card | None = None


def acquire(slot: str, uri: str, lo_hz: float, sample_rate: float,
            tx_gain_db: float, kind: str = "pluto", *,
            rf_bandwidth_hz: float | None = None,
            xo_ppm: float = 0.0) -> _SlotSink:
    """Join (or open) the shared card for ``slot`` ("TX1"/"TX2"). Raises
    TransmitError if the card is already open for a different
    uri/LO/rate/kind -- TX1 and TX2 physically cannot disagree on those."""
    global _card
    if slot not in _SLOT_CHAN:
        raise TransmitError(f"unknown TX slot {slot!r}")
    if kind not in BACKENDS:
        raise TransmitError(f"unknown SDR kind {kind!r}: must be one of {sorted(BACKENDS)}")
    with _lock:
        if _card is None:
            backend = BACKENDS[kind]
            handle = backend.open_tx(uri, lo_hz, sample_rate,
                                     rf_bandwidth_hz=rf_bandwidth_hz,
                                     xo_ppm=xo_ppm)
            if _SLOT_CHAN[slot] >= backend.n_tx_channels(handle):
                backend.close(handle)
                raise TransmitError(
                    f"{slot} unavailable: the {kind} at {uri!r} exposes "
                    f"only {backend.n_tx_channels(handle)} TX channel(s) "
                    "(a stock PlutoSDR is 1R1T -- use TX1, or switch the "
                    "firmware to 2r2t)")
            backend.set_active_channels(handle, [_SLOT_CHAN[slot]])
            _card = _Card(handle, uri, lo_hz, sample_rate, kind)
        else:
            if _card.kind != kind:
                raise TransmitError(
                    f"TX1/TX2 share one physical card: already open as "
                    f"{_card.kind!r}, cannot also open as {kind!r}")
            if _card.uri != uri:
                raise TransmitError(
                    f"TX1/TX2 share one card: already open on {_card.uri!r}, "
                    f"cannot also open {uri!r}")
            if abs(_card.lo_hz - lo_hz) > 1000:
                raise TransmitError(
                    f"TX1/TX2 share one LO: already transmitting at "
                    f"{_card.lo_hz:.0f} Hz, cannot also start at {lo_hz:.0f} Hz "
                    "-- match the running slot's band or stop it first")
            if abs(_card.sample_rate - sample_rate) > 1.0:
                raise TransmitError(
                    f"TX1/TX2 share one sample rate: already transmitting at "
                    f"{_card.sample_rate:.0f} Hz, cannot also start at "
                    f"{sample_rate:.0f} Hz -- match the running slot's rate "
                    "or stop it first")
        chan = _SLOT_CHAN[slot]
        if chan >= _card.backend.n_tx_channels(_card.handle):
            raise TransmitError(
                f"{slot} unavailable: the shared card exposes only "
                f"{_card.backend.n_tx_channels(_card.handle)} TX channel(s)")
        card = _card
        try:
            card.backend.set_gain(card.handle, chan, tx_gain_db)
        except Exception:
            # Kart bu çağrıda açıldıysa ve hiç slot kaydı yoksa kapat; yoksa
            # _card bayat kalır, sonraki acquire kapalı/yarım kartı yeniden
            # kullanır.
            if card.active <= 0:
                card.shut_down()
                _card = None
            raise
        card.register(slot)
        try:
            card.ensure_channel(chan)
        except Exception:
            if card.unregister(slot):
                card.shut_down()
                _card = None
            raise
        return _SlotSink(card, slot)


def _release(card: _Card, slot: str) -> None:
    global _card
    with _lock:
        last = card.unregister(slot)
        if last:
            card.shut_down()
            if _card is card:
                _card = None
