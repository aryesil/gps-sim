import sys
import types

import numpy as np
import pytest

from backend.rf.backends import bladerf as bladerf_backend


class _FakeChannel:
    def __init__(self):
        self.frequency = None
        self.sample_rate = None
        self.bandwidth = None
        self.gain = None
        self.enable = None


class _FakeBladeRF:
    """Fake libbladeRF Python-binding device handle. Records every call
    so tests can assert on the backend module's actual hardware-facing
    behaviour without real libbladeRF/hardware."""
    instances: list["_FakeBladeRF"] = []

    def __init__(self, device_identifier=None):
        self.device_identifier = device_identifier
        self.channels = {0: _FakeChannel(), 1: _FakeChannel()}
        self.sync_config_calls: list[dict] = []
        self.sync_tx_calls: list[tuple] = []
        self.closed = False
        self.serial = "ABCDEF0123456789"
        self.fpga_version = "0.15.0"
        self.rfic_temperature = 42.5
        _FakeBladeRF.instances.append(self)

    def Channel(self, ch):
        return self.channels[ch]

    def sync_config(self, layout, fmt, num_buffers, buffer_size,
                     num_transfers, stream_timeout):
        self.sync_config_calls.append(dict(
            layout=layout, fmt=fmt, num_buffers=num_buffers,
            buffer_size=buffer_size, num_transfers=num_transfers,
            stream_timeout=stream_timeout))

    def sync_tx(self, buf, num_samples, timeout_ms=None, meta=None):
        self.sync_tx_calls.append((buf, num_samples))

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_bladerf_module(monkeypatch):
    _FakeBladeRF.instances = []
    fake_bladerf_mod = types.ModuleType("_bladerf")
    fake_bladerf_mod.BladeRF = _FakeBladeRF
    fake_bladerf_mod.CHANNEL_TX = lambda ch: ch
    fake_bladerf_mod.ChannelLayout = types.SimpleNamespace(TX_X1="TX_X1", TX_X2="TX_X2")
    fake_bladerf_mod.Format = types.SimpleNamespace(SC16_Q11="SC16_Q11")
    fake_pkg = types.ModuleType("bladerf")
    fake_pkg._bladerf = fake_bladerf_mod
    monkeypatch.setitem(sys.modules, "bladerf", fake_pkg)
    monkeypatch.setitem(sys.modules, "bladerf._bladerf", fake_bladerf_mod)
    yield


def test_open_tx_configures_both_channels_and_streaming():
    handle = bladerf_backend.open_tx("bladerf-uri", 1575420000.0, 2_600_000.0)
    assert handle.channels[0].frequency == 1575420000.0
    assert handle.channels[1].frequency == 1575420000.0
    assert handle.channels[0].sample_rate == 2_600_000.0
    assert handle.channels[0].bandwidth == 1_300_000.0
    assert handle.channels[0].enable is True
    assert handle.channels[1].enable is True
    assert handle.sync_config_calls
    cfg = handle.sync_config_calls[0]
    assert cfg["layout"] == "TX_X2"
    assert cfg["fmt"] == "SC16_Q11"


def test_set_gain_sets_independent_per_channel_gain():
    handle = bladerf_backend.open_tx("bladerf-uri", 1575420000.0, 2_600_000.0)
    bladerf_backend.set_gain(handle, 0, -10.0)
    bladerf_backend.set_gain(handle, 1, 30.0)
    assert handle.channels[0].gain == -10.0
    assert handle.channels[1].gain == 30.0


def test_write_interleaves_two_channel_blocks_into_sc16q11_buffer():
    handle = bladerf_backend.open_tx("bladerf-uri", 1575420000.0, 2_600_000.0)
    ch0 = np.array([1 + 2j, 3 + 4j], dtype=np.complex64)
    ch1 = np.array([5 + 6j, 7 + 8j], dtype=np.complex64)
    bladerf_backend.write(handle, [ch0, ch1])
    assert len(handle.sync_tx_calls) == 1
    buf, num_samples = handle.sync_tx_calls[0]
    assert num_samples == 2
    decoded = np.frombuffer(buf, dtype=np.int16)
    assert list(decoded) == [1, 2, 5, 6, 3, 4, 7, 8]


def test_close_disables_channels_and_closes_device():
    handle = bladerf_backend.open_tx("bladerf-uri", 1575420000.0, 2_600_000.0)
    bladerf_backend.close(handle)
    assert handle.channels[0].enable is False
    assert handle.channels[1].enable is False
    assert handle.closed is True


def test_probe_info_reads_serial_fw_and_temperature():
    handle = bladerf_backend.probe_open("bladerf-uri")
    info = bladerf_backend.probe_info(handle)
    assert info["hw_serial"] == "ABCDEF0123456789"
    assert info["fw_version"] == "0.15.0"
    assert info["temp_c"] == 42.5


def test_probe_info_degrades_gracefully_on_missing_attrs():
    class _Bare:
        pass
    assert bladerf_backend.probe_info(_Bare()) == {}


def test_open_tx_propagates_backend_failure(monkeypatch):
    fake_bladerf_mod = types.ModuleType("_bladerf")
    class _Boom:
        def __init__(self, device_identifier=None):
            raise RuntimeError("no bladeRF on the bus")
    fake_bladerf_mod.BladeRF = _Boom
    fake_pkg = types.ModuleType("bladerf")
    fake_pkg._bladerf = fake_bladerf_mod
    monkeypatch.setitem(sys.modules, "bladerf", fake_pkg)
    monkeypatch.setitem(sys.modules, "bladerf._bladerf", fake_bladerf_mod)
    with pytest.raises(RuntimeError, match="no bladeRF on the bus"):
        bladerf_backend.open_tx("bladerf-uri", 1575420000.0, 2_600_000.0)
