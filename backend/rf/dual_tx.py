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

import queue
import threading

import numpy as np

from backend.rf.transmit import TransmitError

_BLOCK_SAMPLES = 65536
_GET_TIMEOUT_S = 0.5     # how long the pump waits for a slot before feeding silence
_PUT_TIMEOUT_S = 10.0    # how long push() waits for pump to drain before giving up
_QUEUE_DEPTH = 8         # ~8 blocks of smoothing headroom per slot
_SLOT_CHAN = {"TX1": 0, "TX2": 1}


def _open_ad9361(uri: str):
    import adi  # pyadi-iio
    return adi.ad9361(uri=uri)


class _Card:
    """The one shared context, live for as long as at least one slot is
    acquired. Not constructed directly -- see acquire()/_release()."""

    def __init__(self, sdr, uri: float, lo_hz: float, sample_rate: float):
        self.sdr = sdr
        self.uri = uri
        self.lo_hz = lo_hz
        self.sample_rate = sample_rate
        self.lock = threading.Lock()          # guards queues/underflow/active below
        self.queues: dict[str, queue.Queue] = {}
        self.underflow: dict[str, int] = {}
        self.active = 0
        self.stop = threading.Event()
        self.pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
        self.pump_thread.start()

    def _pump_loop(self) -> None:
        zero = np.zeros(_BLOCK_SAMPLES, dtype=np.complex64)
        while not self.stop.is_set():
            with self.lock:
                slots = dict(self.queues)
            blocks = []
            for slot in ("TX1", "TX2"):
                q = slots.get(slot)
                if q is None:
                    blocks.append(zero)
                    continue
                try:
                    blocks.append(q.get(timeout=_GET_TIMEOUT_S))
                except queue.Empty:
                    with self.lock:
                        self.underflow[slot] = self.underflow.get(slot, 0) + 1
                    blocks.append(zero)
            self.sdr.tx(blocks)

    def register(self, slot: str) -> None:
        with self.lock:
            self.queues[slot] = queue.Queue(maxsize=_QUEUE_DEPTH)
            self.underflow.setdefault(slot, 0)
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
        try:
            q.put(block, timeout=_PUT_TIMEOUT_S)
        except queue.Full as ex:
            raise TransmitError(
                f"dual TX pump on {slot} appears stuck (queue full after "
                f"{_PUT_TIMEOUT_S}s) -- the other TX slot may have stalled "
                "the shared card") from ex

    def shut_down(self) -> None:
        self.stop.set()
        self.pump_thread.join(timeout=5.0)
        try:
            self.sdr.tx_destroy_buffer()
        except Exception:
            pass


class _SlotSink:
    """Duck-types transmit.py's sink protocol (push/close/underflow),
    exactly like _DrySink and the old single-channel _PyadiSink."""

    def __init__(self, card: _Card, slot: str):
        self._card = card
        self._slot = slot
        self._carry = np.zeros(0, dtype=np.complex64)

    @property
    def underflow(self) -> int:
        with self._card.lock:
            return self._card.underflow.get(self._slot, 0)

    def push(self, chunk: np.ndarray) -> None:
        chunk = np.asarray(chunk, dtype=np.complex64)
        if self._carry.size:
            chunk = np.concatenate([self._carry, chunk])
        n_full = (chunk.size // _BLOCK_SAMPLES) * _BLOCK_SAMPLES
        for i in range(0, n_full, _BLOCK_SAMPLES):
            self._card.enqueue(self._slot, chunk[i:i + _BLOCK_SAMPLES])
        self._carry = chunk[n_full:]

    def close(self) -> None:
        if self._carry.size:
            pad = np.zeros(_BLOCK_SAMPLES, dtype=np.complex64)
            pad[:self._carry.size] = self._carry
            self._card.enqueue(self._slot, pad)
            self._carry = np.zeros(0, dtype=np.complex64)
        _release(self._card, self._slot)


_lock = threading.Lock()
_card: _Card | None = None


def acquire(slot: str, uri: str, lo_hz: float, sample_rate: float,
            tx_gain_db: float) -> _SlotSink:
    """Join (or open) the shared card for ``slot`` ("TX1"/"TX2"). Raises
    TransmitError if the card is already open for a different uri/LO/rate
    -- TX1 and TX2 physically cannot disagree on those."""
    global _card
    if slot not in _SLOT_CHAN:
        raise TransmitError(f"unknown TX slot {slot!r}")
    with _lock:
        if _card is None:
            sdr = _open_ad9361(uri)
            sdr.tx_enabled_channels = [0, 1]
            sdr.sample_rate = int(sample_rate)
            sdr.tx_lo = int(lo_hz)
            sdr.tx_cyclic_buffer = False
            if abs(sdr.tx_lo - lo_hz) > 1000:
                raise TransmitError(f"device clamped LO to {sdr.tx_lo}")
            if abs(sdr.sample_rate - sample_rate) > 1.0:
                raise TransmitError(f"device clamped rate to {sdr.sample_rate}")
            _card = _Card(sdr, uri, lo_hz, sample_rate)
        else:
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
        setattr(_card.sdr, f"tx_hardwaregain_chan{chan}", float(tx_gain_db))
        _card.register(slot)
        return _SlotSink(_card, slot)


def _release(card: _Card, slot: str) -> None:
    global _card
    with _lock:
        last = card.unregister(slot)
        if last:
            card.shut_down()
            if _card is card:
                _card = None
