"""The tezuka backend picks cs16 (ad9361) or cs8 (ad9361_cs8) from the sample
rate and delegates everything else to that implementation."""
from types import SimpleNamespace

import pytest

from backend import config
from backend.rf import transmit
from backend.rf.backends import BACKENDS, ad9361, ad9361_cs8, tezuka
from backend.rf.transmit import TransmitError


def test_registered():
    assert BACKENDS["tezuka"] is tezuka


@pytest.mark.parametrize("rate,impl,fmt", [
    (2.6e6, ad9361, "cs16"),
    (5e6, ad9361, "cs16"),
    (10e6, ad9361, "cs16"),
    (10.0001e6, ad9361_cs8, "cs8"),
    (15e6, ad9361_cs8, "cs8"),
    (20e6, ad9361_cs8, "cs8"),
    (25e6, ad9361_cs8, "cs8"),
    (27.5e6, ad9361_cs8, "cs8"),
])
def test_format_follows_rate(rate, impl, fmt):
    assert tezuka.impl_for(rate) is impl
    assert tezuka.format_for(rate) == fmt


def test_rate_above_cs8_ceiling_rejected():
    with pytest.raises(TransmitError, match="Msps"):
        tezuka.impl_for(30e6)


def test_thresholds_follow_config(monkeypatch):
    monkeypatch.setattr(config, "TX_CS16_MAX_RATE_HZ", 4e6)
    monkeypatch.setattr(config, "TX_CS8_MAX_RATE_HZ", 30e6)
    assert tezuka.impl_for(5e6) is ad9361_cs8
    assert tezuka.impl_for(30e6) is ad9361_cs8


def test_dac_full_scale_per_format():
    assert tezuka.dac_full_scale_for(5e6) == ad9361.DAC_FULL_SCALE
    assert tezuka.dac_full_scale_for(20e6) == ad9361_cs8.DAC_FULL_SCALE
    assert transmit._dac_full_scale("tezuka", 5e6) == ad9361.DAC_FULL_SCALE
    assert transmit._dac_full_scale("tezuka", 20e6) == ad9361_cs8.DAC_FULL_SCALE
    # backends without a per-rate hook ignore the rate
    assert transmit._dac_full_scale("pluto", 20e6) == ad9361.DAC_FULL_SCALE


def _record(monkeypatch, mod, name):
    calls = []

    def fn(*a, **k):
        calls.append((name, a, k))
        return calls[-1]

    monkeypatch.setattr(mod, name, fn)
    return calls


def test_open_routes_and_remembers_the_implementation(monkeypatch):
    for mod in (ad9361, ad9361_cs8):
        monkeypatch.setattr(mod, "open_tx",
                            lambda *a, _m=mod, **k: SimpleNamespace(src=_m.__name__))
    low = tezuka.open_tx("ip:x", 1.5e9, 5e6)
    high = tezuka.open_tx("ip:x", 1.5e9, 20e6)
    assert low.src.endswith("ad9361") and high.src.endswith("ad9361_cs8")

    wrote = _record(monkeypatch, ad9361, "write")
    wrote8 = _record(monkeypatch, ad9361_cs8, "write")
    tezuka.write(low, ["blk"])
    tezuka.write(high, ["blk"])
    assert len(wrote) == 1 and len(wrote8) == 1

    closed = _record(monkeypatch, ad9361_cs8, "close")
    tezuka.close(high)
    assert closed


def test_cs8_handle_is_single_port(monkeypatch):
    monkeypatch.setattr(ad9361_cs8, "open_tx", lambda *a, **k: SimpleNamespace())
    h = tezuka.open_tx("ip:x", 1.5e9, 20e6)
    assert tezuka.n_tx_channels(h) == 1
    with pytest.raises(TransmitError):
        tezuka.set_active_channels(h, [0, 1])
