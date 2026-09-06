import pytest

from backend.synth import fs_policy, signals


def test_fs_min_gps_only():
    assert fs_policy.fs_min(["GPS_L1CA"]) == pytest.approx(1.023e6)


def test_fs_min_boc_doubles(monkeypatch):
    monkeypatch.setitem(signals.SIGNALS, "TEST_BOC",
                        signals.Signal(1e9, 1.023e6, 1023, (1, 1), 50.0, "L1"))
    assert fs_policy.fs_min(["TEST_BOC"]) == pytest.approx(2.046e6)


def test_validate_fs_rejects_below_min():
    with pytest.raises(ValueError):
        fs_policy.validate_fs(0.9e6, ["GPS_L1CA"])


def test_validate_fs_none_returns_default():
    fs = fs_policy.validate_fs(None, ["GPS_L1CA"])
    assert fs >= 2 * 1.023e6
    assert fs == pytest.approx(2.6e6)


def test_validate_fs_passthrough_when_valid():
    assert fs_policy.validate_fs(5.0e6, ["GPS_L1CA"]) == 5.0e6
