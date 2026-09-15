import sys
import time
import types

import numpy as np
import pytest

from backend.rf import dual_tx, transmit


class _FakeAD9361:
    """Fake pyadi-iio adi.ad9361 handle. Records every tx() call's block
    pair and every constructor call, so tests can assert on the shared
    card's actual hardware-facing behaviour without real IIO/hardware."""
    instances: list["_FakeAD9361"] = []

    def __init__(self, uri=None):
        self.uri = uri
        self.tx_enabled_channels = None
        self.sample_rate = None
        self.tx_lo = None
        self.tx_cyclic_buffer = None
        self.tx_hardwaregain_chan0 = None
        self.tx_hardwaregain_chan1 = None
        self.tx_calls: list[list[np.ndarray]] = []
        self.destroyed = False
        _FakeAD9361.instances.append(self)

    def tx(self, blocks):
        self.tx_calls.append([np.array(b) for b in blocks])
        time.sleep(0.005)   # keep the pump thread from busy-spinning in tests

    def tx_destroy_buffer(self):
        self.destroyed = True


class _BoomAD9361:
    def __init__(self, uri=None):
        raise RuntimeError("no device on the bus")


@pytest.fixture(autouse=True)
def _clean_dual_tx_singleton(monkeypatch):
    _FakeAD9361.instances = []
    fake_module = types.ModuleType("adi")
    fake_module.ad9361 = _FakeAD9361
    monkeypatch.setitem(sys.modules, "adi", fake_module)
    dual_tx._card = None
    yield
    # Safety net: a failed assertion mid-test must never leave a pump
    # thread (and the module singleton) running into the next test.
    if dual_tx._card is not None:
        dual_tx._card.shut_down()
        dual_tx._card = None


def test_acquire_configures_lo_rate_and_gain():
    sink = dual_tx.acquire("TX1", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)
    try:
        dev = _FakeAD9361.instances[0]
        assert dev.uri == "ip:1.2.3.4"
        assert dev.tx_enabled_channels == [0, 1]
        assert dev.sample_rate == 2_600_000
        assert dev.tx_lo == 1575420000
        assert dev.tx_cyclic_buffer is False
        assert dev.tx_hardwaregain_chan0 == -30.0
    finally:
        sink.close()


def test_second_slot_shares_the_same_card():
    s1 = dual_tx.acquire("TX1", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)
    try:
        s2 = dual_tx.acquire("TX2", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -10.0)
        try:
            assert len(_FakeAD9361.instances) == 1   # one shared card, not two
            dev = _FakeAD9361.instances[0]
            assert dev.tx_hardwaregain_chan0 == -30.0   # TX1's own gain
            assert dev.tx_hardwaregain_chan1 == -10.0   # TX2's own, independent
        finally:
            s2.close()
    finally:
        s1.close()


def test_second_slot_rejects_mismatched_lo():
    s1 = dual_tx.acquire("TX1", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)
    try:
        with pytest.raises(transmit.TransmitError, match="share one LO"):
            dual_tx.acquire("TX2", "ip:1.2.3.4", 1227600000.0, 2_600_000.0, -30.0)
    finally:
        s1.close()


def test_second_slot_rejects_mismatched_rate():
    s1 = dual_tx.acquire("TX1", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)
    try:
        with pytest.raises(transmit.TransmitError, match="share one sample rate"):
            dual_tx.acquire("TX2", "ip:1.2.3.4", 1575420000.0, 20_500_000.0, -30.0)
    finally:
        s1.close()


def test_second_slot_rejects_mismatched_uri():
    s1 = dual_tx.acquire("TX1", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)
    try:
        with pytest.raises(transmit.TransmitError, match="share one card"):
            dual_tx.acquire("TX2", "ip:9.9.9.9", 1575420000.0, 2_600_000.0, -30.0)
    finally:
        s1.close()


def test_unknown_slot_rejected():
    with pytest.raises(transmit.TransmitError, match="unknown TX slot"):
        dual_tx.acquire("TX3", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)


def test_device_open_failure_propagates(monkeypatch):
    fake_module = types.ModuleType("adi")
    fake_module.ad9361 = _BoomAD9361
    monkeypatch.setitem(sys.modules, "adi", fake_module)
    with pytest.raises(RuntimeError, match="no device on the bus"):
        dual_tx.acquire("TX1", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)
    assert dual_tx._card is None   # never left half-open


def test_idle_slot_transmits_silence_while_active_one_streams():
    s1 = dual_tx.acquire("TX1", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)
    try:
        block = (np.ones(dual_tx._BLOCK_SAMPLES, dtype=np.complex64)
                 * (2.0 + 3.0j))
        s1.push(block)
        time.sleep(0.2)
        dev = _FakeAD9361.instances[0]
        assert dev.tx_calls, "pump never called tx()"
        # TX2 was never acquired -- its channel must always be silence.
        for tx1_chunk, tx2_chunk in dev.tx_calls:
            assert np.all(tx2_chunk == 0)
        # At least one call actually carried TX1's real data (not silence).
        assert any(np.any(tx1_chunk != 0) for tx1_chunk, _tx2 in dev.tx_calls)
    finally:
        s1.close()


def test_both_slots_transmit_their_own_content_simultaneously():
    s1 = dual_tx.acquire("TX1", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)
    s2 = dual_tx.acquire("TX2", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)
    try:
        b1 = np.full(dual_tx._BLOCK_SAMPLES, 1.0 + 0.0j, dtype=np.complex64)
        b2 = np.full(dual_tx._BLOCK_SAMPLES, 0.0 + 2.0j, dtype=np.complex64)
        s1.push(b1)
        s2.push(b2)
        time.sleep(0.2)
        dev = _FakeAD9361.instances[0]
        assert any(np.allclose(c1, b1) and np.allclose(c2, b2)
                   for c1, c2 in dev.tx_calls)
    finally:
        s1.close()
        s2.close()


def test_push_splits_into_fixed_blocks_and_close_flushes_zero_padded_carry():
    s1 = dual_tx.acquire("TX1", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)
    n = dual_tx._BLOCK_SAMPLES + 10   # one full block plus a small remainder
    chunk = np.arange(n, dtype=np.complex64)
    s1.push(chunk)
    time.sleep(0.2)
    dev = _FakeAD9361.instances[0]
    full_block_calls = [c1 for c1, _c2 in dev.tx_calls if np.any(c1 != 0)]
    assert full_block_calls, "the full block never reached tx()"
    assert np.array_equal(full_block_calls[0], chunk[:dual_tx._BLOCK_SAMPLES])
    s1.close()   # must flush the 10-sample carry, zero-padded
    time.sleep(0.2)
    tail_calls = [c1 for c1, _c2 in dev.tx_calls
                  if c1.size and np.any(c1 != 0) and c1[0] == chunk[dual_tx._BLOCK_SAMPLES]]
    assert tail_calls, "the trailing partial block was never flushed on close()"
    tail = tail_calls[0]
    assert np.array_equal(tail[:10], chunk[dual_tx._BLOCK_SAMPLES:])
    assert np.all(tail[10:] == 0)


def test_last_slot_close_tears_down_card_and_resets_singleton():
    s1 = dual_tx.acquire("TX1", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)
    dev = _FakeAD9361.instances[0]
    s1.close()
    assert dev.destroyed is True
    assert dual_tx._card is None
    # A fresh acquire on a totally different uri/LO must now succeed
    # (proves the shared state was fully released, not just the slot).
    s2 = dual_tx.acquire("TX1", "ip:9.9.9.9", 1227600000.0, 5_000_000.0, -30.0)
    try:
        assert len(_FakeAD9361.instances) == 2
    finally:
        s2.close()


def test_shut_down_mutes_gain_and_flushes_silence_before_destroying_buffer():
    # Real AD9361 hardware can keep repeating the last transmitted buffer
    # after tx_destroy_buffer() alone (non-cyclic TX DMA underrun behavior
    # -- see dual_tx.py's shut_down() comment). close() must force real
    # silence: mute both channels to the hardware's minimum gain AND push
    # one explicit all-zero block, before tearing the buffer down.
    s1 = dual_tx.acquire("TX1", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)
    block = np.ones(dual_tx._BLOCK_SAMPLES, dtype=np.complex64) * (1.0 + 1.0j)
    s1.push(block)
    time.sleep(0.2)
    dev = _FakeAD9361.instances[0]
    s1.close()
    assert dev.tx_hardwaregain_chan0 == -89.75
    assert dev.tx_hardwaregain_chan1 == -89.75
    assert dev.tx_calls, "no tx() calls recorded"
    last_tx1, last_tx2 = dev.tx_calls[-1]
    assert np.all(last_tx1 == 0), "last tx() call before buffer teardown must be silence"
    assert np.all(last_tx2 == 0)
    assert dev.destroyed is True


def test_underflow_counts_when_a_slot_has_nothing_queued():
    s1 = dual_tx.acquire("TX1", "ip:1.2.3.4", 1575420000.0, 2_600_000.0, -30.0)
    try:
        time.sleep(dual_tx._GET_TIMEOUT_S + 0.2)   # let the pump time out at least once
        assert s1.underflow >= 1
    finally:
        s1.close()
