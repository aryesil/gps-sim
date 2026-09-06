# tests/test_nmea.py
import pytest

from backend.obs import nmea

_GGA = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
_RMC = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"


def test_parse_gga_extracts_fix_fields():
    d = nmea.parse_gga(_GGA)
    assert d["sentence"] == "GGA"
    assert d["fix_quality"] == 1
    assert d["num_sats"] == 8
    assert d["hdop"] == 0.9
    assert d["alt_m"] == 545.4
    assert d["lat"] == pytest.approx(48.1173, abs=1e-3)
    assert d["lon"] == pytest.approx(11.51667, abs=1e-3)


def test_parse_rmc_extracts_speed_and_status():
    d = nmea.parse_rmc(_RMC)
    assert d["sentence"] == "RMC"
    assert d["status"] == "active"
    assert d["speed_knots"] == 22.4
    assert d["lon"] == pytest.approx(11.51667, abs=1e-3)


def test_parse_gga_rejects_bad_checksum():
    with pytest.raises(nmea.NmeaError):
        nmea.parse_gga(_GGA[:-2] + "00")


def test_parse_dispatches_by_sentence_type():
    assert nmea.parse(_GGA)["sentence"] == "GGA"
    assert nmea.parse(_RMC)["sentence"] == "RMC"


def test_parse_returns_none_for_unknown_or_malformed():
    assert nmea.parse("$GPGSV,1,1,00*79") is None
    assert nmea.parse("not nmea at all") is None
    assert nmea.parse(_GGA[:-2] + "00") is None  # bad checksum -> None, not raise


def test_southern_western_hemisphere_signs_negative():
    body = _GGA.replace(",N,", ",S,").replace(",E,", ",W,")
    cksum = 0
    for ch in body.lstrip("$").split("*")[0]:
        cksum ^= ord(ch)
    fixed = body.split("*")[0] + f"*{cksum:02X}"
    d = nmea.parse_gga(fixed)
    assert d["lat"] < 0
    assert d["lon"] < 0
