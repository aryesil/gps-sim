"""LIVE_START_LEAD_S otomatik kalibrasyonu: saf mantık + varsayılan davranış."""
import pytest

from backend import config
from backend.session import lead_calib


def test_default_is_fixed_lead_even_with_history(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LIVE_START_LEAD_AUTO", False)
    for _ in range(5):
        lead_calib.record("k", 9.0)
    assert lead_calib.lead_for("k", 0.05) == config.LIVE_START_LEAD_S


def test_auto_without_history_uses_the_fixed_lead(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LIVE_START_LEAD_AUTO", True)
    assert lead_calib.lead_for("none", 0.05) == config.LIVE_START_LEAD_S


def test_auto_uses_median_plus_tx_buffer_plus_margin(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LIVE_START_LEAD_AUTO", True)
    monkeypatch.setattr(config, "LIVE_START_LEAD_MARGIN_S", 0.3)
    for v in (1.2, 1.4, 9.0, 1.3, 1.1):         # 9.0 tek aykırı
        lead_calib.record("k", v)
    assert lead_calib.lead_for("k", 0.05) == pytest.approx(1.3 + 0.05 + 0.3)


def test_suggest_is_clamped_and_ignores_garbage():
    assert lead_calib.suggest_lead_s([100.0], 0.0, default_s=2.5) == 15.0
    assert lead_calib.suggest_lead_s([0.0], 0.0, margin_s=0.0, default_s=2.5) == 0.5
    assert lead_calib.suggest_lead_s([float("nan"), -1.0], 0.1, default_s=2.5) == 2.5


def test_history_keeps_last_five_and_survives_corrupt_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for i in range(8):
        lead_calib.record("k", float(i))
    assert lead_calib.samples_for("k") == [3.0, 4.0, 5.0, 6.0, 7.0]
    (tmp_path / "live_lead.json").write_text("{not json")
    assert lead_calib.samples_for("k") == []
    lead_calib.record("k", 1.0)                  # bozuk dosyanın üstüne yazabilmeli
    assert lead_calib.samples_for("k") == [1.0]


def test_lag_sign():
    assert lead_calib.lag_s(3.0, 0.1, 2.5) == pytest.approx(0.6)     # geride
    assert lead_calib.lag_s(1.0, 0.1, 2.5) == pytest.approx(-1.4)    # ileride


def test_key_depends_on_everything_that_changes_latency():
    a = lead_calib.make_key("pluto", 2.6e6, ["G", "E"], "L1")
    assert a == lead_calib.make_key("pluto", 2.6e6, ["E", "G"], "L1")
    assert a != lead_calib.make_key("bladerf", 2.6e6, ["G", "E"], "L1")
    assert a != lead_calib.make_key("pluto", 5e6, ["G", "E"], "L1")
    assert a != lead_calib.make_key("pluto", 2.6e6, ["G"], "L1")
