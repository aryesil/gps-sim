import sys
import types

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.rf import device
from backend.app import app

client = TestClient(app)


def test_status_starts_as_list():
    assert isinstance(device.status(), list)


def test_connect_rejects_empty_uri():
    with pytest.raises(device.DeviceError):
        device.connect("")


def test_connect_maps_backend_failure_to_device_error(monkeypatch):
    fake = types.ModuleType("adi")

    class _Boom:
        def __init__(self, uri=None):
            raise RuntimeError("no device on the bus")

    fake.Pluto = _Boom
    monkeypatch.setitem(sys.modules, "adi", fake)
    with pytest.raises(device.DeviceError):
        device.connect("ip:192.168.2.1")
    assert not device.is_connected("ip:192.168.2.1")


def test_disconnect_is_idempotent():
    device.disconnect("ip:never-connected")  # must not raise


def test_connect_holds_standby_link(monkeypatch):
    fake = types.ModuleType("adi")

    class _FakePluto:
        def __init__(self, uri=None):
            self.uri = uri
            self.sample_rate = 2600000
            self.tx_hardwaregain_chan0 = -50.0
            self._ctrl = None

    fake.Pluto = _FakePluto
    monkeypatch.setitem(sys.modules, "adi", fake)

    entry = device.connect("ip:10.0.0.9")
    try:
        assert entry["connected"] is True
        assert entry["state"] == "standby"
        assert device.is_connected("ip:10.0.0.9")
        assert any(d["uri"] == "ip:10.0.0.9" for d in device.status())
    finally:
        device.disconnect("ip:10.0.0.9")
    assert not device.is_connected("ip:10.0.0.9")


def test_api_device_endpoints(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(device, "connect",
                        lambda uri: {"uri": uri, "connected": True,
                                     "since": "t", "info": {"hw_model": "Pluto"},
                                     "state": "standby"})
    r = client.post("/api/device/connect", json={"uri": "ip:1.2.3.4"})
    assert r.status_code == 200
    assert r.json()["info"]["hw_model"] == "Pluto"

    r = client.get("/api/device/status")
    assert r.status_code == 200 and "devices" in r.json()

    r = client.post("/api/device/disconnect", json={"uri": "ip:1.2.3.4"})
    assert r.status_code == 200 and r.json()["connected"] is False


def test_api_device_connect_failure_is_502(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)

    def _raise(uri):
        raise device.DeviceError("cannot reach SDR")

    monkeypatch.setattr(device, "connect", _raise)
    r = client.post("/api/device/connect", json={"uri": "ip:1.2.3.4"})
    assert r.status_code == 502
