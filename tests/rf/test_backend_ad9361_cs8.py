import sys
import types

import numpy as np
import pytest

from backend.rf import dual_tx
from backend.rf.backends import BACKENDS, ad9361_cs8
from backend.rf.transmit import TransmitError


class _Chan:
    def __init__(self, cid, output=True):
        self.id = cid
        self.output = output
        self.enabled = True


class _Dac:
    def __init__(self, ids=("voltage0", "voltage1")):
        self.channels = [_Chan(i) for i in ids]


class _FakeAD9361:
    def __init__(self, uri=None, ids=("voltage0", "voltage1")):
        self._txdac = _Dac(ids)
        self.tx_calls = 0
        self.sample_rate = None
        self.tx_lo = None
        self.tx_rf_bandwidth = None
        self.destroyed = False

    def tx(self, blocks):                 # must never run in cs8 mode
        self.tx_calls += 1

    def tx_destroy_buffer(self):
        self.destroyed = True

    def disable_dds(self):
        pass


class _FakeBuffer:
    instances = []

    def __init__(self, dev, n, cyclic=False):
        self.dev, self.n, self.cyclic = dev, n, cyclic
        self.data = bytearray()
        self.pushes = 0
        _FakeBuffer.instances.append(self)

    def write(self, b):
        self.data = bytearray(b)

    def push(self):
        self.pushes += 1


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    _FakeBuffer.instances = []
    adi = types.ModuleType("adi")
    adi.ad9361 = _FakeAD9361
    iio = types.ModuleType("iio")
    iio.Buffer = _FakeBuffer
    monkeypatch.setitem(sys.modules, "adi", adi)
    monkeypatch.setitem(sys.modules, "iio", iio)
    dual_tx._card = None
    yield
    if dual_tx._card is not None:
        dual_tx._card.shut_down()
        dual_tx._card = None


def test_registered_and_int8_full_scale():
    assert BACKENDS["pluto_cs8"] is ad9361_cs8
    assert ad9361_cs8.DAC_FULL_SCALE == 127


def test_pack_cs8_byte_order_rounding_and_saturation():
    blk = np.array([1.6 - 2.4j, 1e6 - 1e6j, -1e6 + 1e6j, 0j], np.complex64)
    raw = np.frombuffer(ad9361_cs8.pack_cs8(blk), dtype=np.int8)
    assert raw.tolist() == [2, -2, 127, -127, -127, 127, 0, 0]
    # little-endian int16 view: low byte I, high byte Q ({Q8, I8})
    w = np.frombuffer(ad9361_cs8.pack_cs8(np.array([3 + 5j], np.complex64)), "<i2")[0]
    assert (int(w) & 0xFF, int(w) >> 8) == (3, 5)


def test_open_enables_only_voltage0():
    h = ad9361_cs8.open_tx("ip:x", 1.57542e9, 4e6)
    enabled = {c.id: c.enabled for c in h._txdac.channels}
    assert enabled == {"voltage0": True, "voltage1": False}
    assert ad9361_cs8.n_tx_channels(h) == 1
    assert ad9361_cs8.active_channels(h) == [0]


def test_open_without_voltage0_fails():
    _FakeAD9361.__init__.__defaults__ = (None, ("voltage1",))
    try:
        with pytest.raises(TransmitError):
            ad9361_cs8.open_tx("ip:x", 1.57542e9, 4e6)
    finally:
        _FakeAD9361.__init__.__defaults__ = (None, ("voltage0", "voltage1"))


def test_write_pushes_raw_bytes_and_reuses_buffer_per_size():
    h = ad9361_cs8.open_tx("ip:x", 1.57542e9, 4e6)
    a = np.array([10 + 20j, -30 - 40j], np.complex64)
    ad9361_cs8.write(h, [a])
    ad9361_cs8.write(h, [a])
    assert len(_FakeBuffer.instances) == 1
    buf = _FakeBuffer.instances[0]
    assert buf.n == 2 and buf.pushes == 2 and not buf.cyclic
    assert np.frombuffer(buf.data, np.int8).tolist() == [10, 20, -30, -40]
    ad9361_cs8.write(h, [np.zeros(5, np.complex64)])
    assert len(_FakeBuffer.instances) == 2 and _FakeBuffer.instances[1].n == 5
    assert h.tx_calls == 0          # pyadi-iio's tx() would re-enable Q


def test_second_channel_rejected():
    h = ad9361_cs8.open_tx("ip:x", 1.57542e9, 4e6)
    with pytest.raises(TransmitError):
        ad9361_cs8.set_active_channels(h, [0, 1])


def test_close_pushes_silence_and_releases_buffer():
    h = ad9361_cs8.open_tx("ip:x", 1.57542e9, 4e6)
    ad9361_cs8.write(h, [np.ones(4, np.complex64)])
    ad9361_cs8.close(h)
    last = _FakeBuffer.instances[-1]
    assert not any(last.data) and h.destroyed and h.tx_calls == 0
    assert h._gs_cs8_buf is None
