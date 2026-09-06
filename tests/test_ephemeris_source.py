import json
import pathlib

import pytest

from backend import ephemeris
from backend.gpstime import GPSTime
from backend.precise import PreciseEphemerisProvider
from backend.ephemeris_source import (
    build_state_fns, normalise_mode, EphemerisModeError,
)

FIXDIR = pathlib.Path(__file__).parent / "fixtures"
TRUTH = json.loads((FIXDIR / "igs_sample_truth.json").read_text())
WEEK, TOE = TRUTH["gps_week"], TRUTH["toe_sow"]


@pytest.fixture(scope="module")
def eph():
    return ephemeris.parse_rinex(FIXDIR / "brdc_sample.rnx")


@pytest.fixture(scope="module")
def provider():
    p = PreciseEphemerisProvider()
    p.load(FIXDIR / "igs_sample.sp3")
    return p


def test_mode_validation():
    assert normalise_mode(None) == "broadcast"
    assert normalise_mode("PRECISE") == "precise"
    with pytest.raises(EphemerisModeError):
        normalise_mode("kalman")


def test_broadcast_mode_passes_dicts_untouched(eph):
    out, warns = build_state_fns("broadcast", sorted(eph), GPSTime(WEEK, TOE), eph)
    assert set(out) == set(eph)
    assert all(isinstance(v, dict) for v in out.values())
    assert warns == ["ephemeris: broadcast"]


def test_precise_without_provider_fails(eph):
    with pytest.raises(EphemerisModeError):
        build_state_fns("precise", sorted(eph), GPSTime(WEEK, TOE), eph, provider=None)


def test_precise_with_unloaded_provider_fails(eph):
    with pytest.raises(EphemerisModeError):
        build_state_fns("precise", sorted(eph), GPSTime(WEEK, TOE), eph,
                        provider=PreciseEphemerisProvider())


def test_precise_mode_returns_callables(eph, provider):
    prns = [p for (s, p) in provider.satellites() if s == "G"]
    out, warns = build_state_fns("precise", prns, GPSTime(WEEK, TOE), eph,
                                 provider=provider)
    assert all(callable(v) for v in out.values())
    assert any("precise" in w for w in warns)


def test_precise_epoch_out_of_coverage_fails_hard(eph, provider):
    far = GPSTime(WEEK + 2, TOE)
    with pytest.raises(EphemerisModeError):
        build_state_fns("precise", [p for (s, p) in provider.satellites() if s == "G"], far, eph, provider=provider)


def test_precise_out_of_coverage_with_explicit_fallback(eph, provider):
    far = GPSTime(WEEK + 2, TOE)
    out, warns = build_state_fns("precise", [p for (s, p) in provider.satellites() if s == "G"], far, eph,
                                 provider=provider, fallback_to_broadcast=True)
    assert all(isinstance(v, dict) for v in out.values())
    assert any("FELL BACK" in w for w in warns)


def test_missing_prn_errors_by_default(eph, provider):
    prns = [p for (s, p) in provider.satellites() if s == "G"] + [30]        # 30 absent from the SP3 fixture
    with pytest.raises(EphemerisModeError):
        build_state_fns("precise", prns, GPSTime(WEEK, TOE), eph, provider=provider)


def test_missing_prn_skip_is_not_a_silent_fallback(eph, provider):
    prns = [p for (s, p) in provider.satellites() if s == "G"] + [30]
    out, warns = build_state_fns("precise", prns, GPSTime(WEEK, TOE), eph,
                                 provider=provider, on_missing="skip")
    assert 30 not in out                        # omitted, NOT substituted
    assert any("omitted" in w and "30" in w for w in warns)
