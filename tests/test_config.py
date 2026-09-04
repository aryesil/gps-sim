import importlib
import os


def test_constants_have_expected_values():
    cfg = importlib.import_module("backend.config")
    assert cfg.L1_HZ == 1575.42e6
    assert cfg.CA_CHIP_HZ == 1.023e6
    assert cfg.CA_CODE_LEN == 1023
    assert cfg.NAV_BIT_HZ == 50
    assert cfg.C == 299792458.0
    assert cfg.ALLOW_TX is False
    assert cfg.DEFAULT_SAMPLE_RATE == 2.6e6


def test_env_override(monkeypatch):
    monkeypatch.setenv("ALLOW_TX", "1")
    monkeypatch.setenv("DEVICE_URI", "ip:10.0.0.5")
    import backend.config as cfg
    importlib.reload(cfg)
    assert cfg.ALLOW_TX is True
    assert cfg.DEVICE_URI == "ip:10.0.0.5"
    importlib.reload(cfg)  # restore
