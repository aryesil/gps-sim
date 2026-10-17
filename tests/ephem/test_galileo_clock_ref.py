"""Galileo af0 saat referansı (E1-E5b I/NAV vs E1-E5a F/NAV) ve BGD tutarlılığı.

OS SIS ICD 5.1.4-5.1.5: I/NAV af0..af2 E1-E5b iyonosfer-bağımsız (IF)
kombinasyonuna, F/NAV af0..af2 E1-E5a IF kombinasyonuna göredir ve
IF_a = IF_b + Δ, Δ = BGD(E1,E5a) - BGD(E1,E5b). Çift frekanslı (E1/E5a) bir
alıcı iki sinyalden IF saatini çıkarıp mesajdaki af0 ile karşılaştırır; yayın
tarafı Δ'yı uygulamazsa sonuç γ/(γ-1)*Δ ~ 2.26*Δ kadar sapar.
"""
import pytest
import xarray as xr

from backend import config, geometry
from backend.analysis import fnav_decode, fnav_encode, inav_decode, inav_encode
from backend.ephem import ephemeris

_GAMMA = (config.L1_HZ / config.L5_HZ) ** 2
_BGD_A, _BGD_B = 3.0e-9, -1.0e-9
_DELTA = _BGD_A - _BGD_B
_AF0 = 2.0e-4


def _rec(ref, af0=_AF0):
    r = {"system": "E", "prn": 2, "af0": af0, "af1": 0.0, "af2": 0.0,
         "tgd": _BGD_B, "tgd_e5a": _BGD_A, "toe": 0.0, "toc": 0.0}
    if ref:
        r["clk_ref"] = ref
    return r


def _emitted(rec, band):
    """Yayında uydu saati olarak görünen toplam: af0 + svb (engine ile aynı)."""
    return rec["af0"] + geometry.group_delay_bias_s("E", band, rec)


def _if_clock(rec):
    """E1/E5a çift frekanslı alıcının gördüğü IF saati."""
    p1, p5 = _emitted(rec, "L1"), _emitted(rec, "L5")
    return (_GAMMA * p1 - p5) / (_GAMMA - 1.0)


def _inav_af0(rec):
    bits = inav_encode.word_type_4(rec, 0)
    v = int("".join(map(str, bits[68:99])), 2)
    if v >= 1 << 30:
        v -= 1 << 31
    return v * 2.0 ** -34


@pytest.mark.parametrize("ref", ["E5B", "E5A"])
def test_dual_frequency_if_clock_matches_fnav_af0(ref):
    rec = _rec(ref)
    # F/NAV af0, alıcının E1-E5a IF saatidir: yayından çıkarılan IF ile eşleşmeli
    fnav_af0 = fnav_encode._values_word1(rec, 2, 1400, 0)["af0"]
    assert _if_clock(rec) == pytest.approx(fnav_af0, abs=1e-15)


@pytest.mark.parametrize("ref", ["E5B", "E5A"])
def test_single_frequency_e1_matches_inav_af0(ref):
    rec = _rec(ref)
    # E1 tek frekanslı alıcı: I/NAV af0 - BGD(E1,E5b) = yayındaki toplam
    assert _inav_af0(rec) - _BGD_B == pytest.approx(_emitted(rec, "L1"), abs=1e-10)


def test_untagged_record_keeps_legacy_behaviour():
    rec = _rec(None)
    assert geometry.group_delay_bias_s("E", "L1", rec) == pytest.approx(-_BGD_B)
    assert geometry.group_delay_bias_s("E", "L5", rec) == pytest.approx(-_GAMMA * _BGD_A)
    assert fnav_encode._values_word1(rec, 2, 1400, 0)["af0"] == _AF0


def test_clock_shift_directions():
    assert geometry.galileo_clock_shift_s(_rec("E5B"), "E5A") == pytest.approx(_DELTA)
    assert geometry.galileo_clock_shift_s(_rec("E5A"), "E5B") == pytest.approx(-_DELTA)
    assert geometry.galileo_clock_shift_s(_rec("E5B"), "E5B") == 0.0
    assert geometry.galileo_clock_shift_s(None, "E5A") == 0.0
    # F/NAV'dan çözülmüş kayıtta BGD(E1,E5b) yok -> Δ bilinmez, 0
    assert geometry.galileo_clock_shift_s({"clk_ref": "E5A", "tgd": 1e-9}, "E5B") == 0.0


@pytest.mark.parametrize("ds,want", [
    (0x205, "E5B"), (0x102, "E5A"), (0x201, "E5B"), (0x101, "E5A"),
    (0x01, "E5B"), (0x04, "E5B"), (0x02, "E5A"), (0, None),
])
def test_rinex_datasrc_to_clock_reference(ds, want):
    rec = xr.Dataset({"DataSrc": float(ds)})
    assert ephemeris._gal_clock_ref(rec) == want
    assert ephemeris._gal_clock_ref(xr.Dataset({"x": 1.0})) is None


def test_decoders_tag_their_clock_reference():
    f = {"type": 1, "crc_ok": True, "fields": {
        "toc": 0.0, "af0": 1e-4, "af1": 0.0, "af2": 0.0, "tgd": _BGD_A,
        "sisa": 107, "week": 1400, "tow": 0}}
    f2 = {"type": 2, "crc_ok": True, "fields": {
        k: 0.0 for k in ("sqrtA", "e", "m0", "omega_dot", "omega0", "idot")}}
    f3 = {"type": 3, "crc_ok": True, "fields": {
        k: 0.0 for k in ("i0", "omega", "delta_n", "cuc", "cus", "crc", "crs", "toe")}}
    assert fnav_decode.reconstruct_ephemeris([f, f2, f3])["clk_ref"] == "E5A"

    def msg(t, **kw):
        return {"type": t, "crc_ok": True, "fields": kw}
    rec = inav_decode.reconstruct_ephemeris([
        msg(1, iodnav=1, sqrtA=0.0, e=0.0, m0=0.0, toe=0.0),
        msg(2, omega0=0.0, i0=0.0, omega=0.0, idot=0.0),
        msg(3, omega_dot=0.0, delta_n=0.0, cuc=0.0, cus=0.0, crc=0.0, crs=0.0),
        msg(4, prn=2, cic=0.0, cis=0.0, toc=0.0, af0=1e-4, af1=0.0, af2=0.0),
    ])
    assert rec["clk_ref"] == "E5B"
