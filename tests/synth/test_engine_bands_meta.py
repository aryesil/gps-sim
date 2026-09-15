import datetime as dt
import json
import pathlib

import numpy as np

from backend import config
from backend.scenario import ScenarioRequest
from backend.synth import _lib, engine

_MIXED = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_mixed.rnx")
_GPS2 = str(pathlib.Path(__file__).parent.parent / "fixtures" / "brdc_full.rnx")


def test_two_bands_write_two_files(tmp_path):
    code = _lib.code(0, 5, 1023)[0].astype(np.int8)
    sv = _lib.one_sv_spec(code, carrier_hz=1000.0, code_phase0=10.0)
    b1 = _lib.BandSpec()
    _lib.fill_band(b1, str(tmp_path / "L1.bin"), 2_600_000.0, 2, 26000, [sv])
    b2 = _lib.BandSpec()
    _lib.fill_band(b2, str(tmp_path / "G1.bin"), 16_000_000.0, 2, 160000, [sv])
    rc = _lib.run_bands([b1, b2])
    assert rc == 0
    assert (tmp_path / "L1.bin").stat().st_size == 26000 * 4  # int16 IQ
    assert (tmp_path / "G1.bin").stat().st_size == 160000 * 4


def test_abi_version_is_20():
    assert _lib.load_lib().synth_abi_version() == _lib.ABI_VERSION == 24


def test_el_gain_is_monotonic_and_bounded():
    g = [engine._el_gain(e) for e in (5, 20, 45, 70, 90)]
    assert g == sorted(g) and 0.0 < g[0] < g[-1] == 1.0


def test_absent_system_is_dropped_with_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = ScenarioRequest(rinex_path=_GPS2, lat=41.0, lon=29.0, alt=100.0,
                          start=dt.datetime(2024, 1, 1, 12, 0, 0), duration_s=2,
                          sample_rate=6_000_000.0, sample_format="int16",
                          engine="native", systems=["G", "E", "C"])
    outdir = engine.run(req)                       # must not raise
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["provenance"]["systems"] == ["G"]
    assert any("no records" in w for w in meta["provenance"]["warnings"])


def test_nav_override_forces_gps_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    from backend.ephem import ephemeris
    ov = ephemeris.parse_rinex(_GPS2)              # bare-int GPS records
    req = ScenarioRequest(rinex_path="AUTO", lat=41.0, lon=29.0, alt=100.0,
                          start=dt.datetime(2024, 1, 1, 12, 0, 0), duration_s=2,
                          sample_rate=6_000_000.0, sample_format="int16",
                          engine="native", systems=["G", "R"],
                          nav_override=ov)
    outdir = engine.run(req)                       # must not touch "AUTO"
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["provenance"]["systems"] == ["G"]
    assert any("precise" in w for w in meta["provenance"]["warnings"])


# --- part 2: engine.run multi-band output + meta band map -------------------

def test_gps_only_run_is_single_band_backcompat(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = ScenarioRequest(rinex_path=_GPS2, lat=41.0, lon=29.0, alt=100.0,
                          start=dt.datetime(2024, 1, 1), duration_s=3,
                          sample_rate=2_600_000.0, sample_format="int16",
                          engine="native")            # systems defaults ("G",)
    outdir = engine.run(req)
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["output"] == "gpssim.bin"
    assert [b["id"] for b in meta["bands"]] == ["L1"]
    assert meta["bands"][0]["systems"] == ["G"]
    assert (outdir / "gpssim.bin").exists()
    assert not (outdir / "gpssim_g1.bin").exists()


def test_glonass_adds_a_second_band_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    # noon: brdc_mixed only carries G01-G03, visible at (41,29) mid-day; at
    # midnight no GPS SV clears the mask and the L1 band would be empty.
    req = ScenarioRequest(rinex_path=_MIXED, lat=41.0, lon=29.0, alt=100.0,
                          start=dt.datetime(2026, 9, 1, 12), duration_s=2,
                          sample_rate=6_000_000.0, sample_format="int16",
                          engine="native", systems=["G", "R"])
    outdir = engine.run(req)
    meta = json.loads((outdir / "meta.json").read_text())
    ids = {b["id"] for b in meta["bands"]}
    assert ids == {"L1", "G1"}
    g1 = next(b for b in meta["bands"] if b["id"] == "G1")
    assert g1["systems"] == ["R"]
    assert (outdir / g1["file"]).exists()
    assert g1["file"] == "gpssim_g1.bin"
    assert g1["fs"] >= 8_900_000.0
    # meta.json still keeps the Phase-1 L1 top-level keys
    assert meta["output"] == "gpssim.bin"
    assert any(s["sys"] == "R" for s in meta["provenance"]["svs"])


def test_glonass_l2of_rides_its_own_g2_band_not_gps_l2c(tmp_path, monkeypatch):
    """SP-5: bands=["L2"] with GLONASS in systems must reach GLO_L2OF via
    the signals_for L2->G2 alias, landing in its OWN band (1246.00 MHz,
    gpssim_g2.bin) -- not GPS L2C's (1227.60 MHz, gpssim_l2.bin)."""
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = ScenarioRequest(rinex_path=_MIXED, lat=41.0, lon=29.0, alt=100.0,
                          start=dt.datetime(2026, 9, 1, 12), duration_s=2,
                          sample_rate=6_000_000.0, sample_format="int16",
                          engine="native", systems=["G", "R"], bands=["L2"])
    outdir = engine.run(req)
    meta = json.loads((outdir / "meta.json").read_text())
    ids = {b["id"] for b in meta["bands"]}
    assert ids == {"L2", "G2"}
    l2 = next(b for b in meta["bands"] if b["id"] == "L2")
    g2 = next(b for b in meta["bands"] if b["id"] == "G2")
    assert l2["systems"] == ["G"] and l2["centre_hz"] == 1_227_600_000.0
    assert g2["systems"] == ["R"] and g2["centre_hz"] == 1_246_000_000.0
    assert (outdir / "gpssim_l2.bin").exists()
    assert (outdir / "gpssim_g2.bin").exists()


# --- part 3: native engine precise ephemeris for every constellation -------

def _mgex_multi_epoch_sp3(rx_ecef, n_epochs=13, interval_s=900.0):
    """A synthetic MGEX SP3 (same P-record shape as tests.test_precise's
    ``_build_sp3_mgex``) carrying G/E/C/J satellites parked a few degrees off
    the receiver zenith so they clear the 5 deg mask. Positions are constant
    across ``n_epochs`` epochs, which gives a fully centred Lagrange window at
    the middle epoch and a zero velocity (Doppler) -- all this test needs.
    No ``R`` record: GLONASS needs broadcast FDMA channel numbers, exercised
    separately."""
    u = np.asarray(rx_ecef, float)
    u = u / np.linalg.norm(u)
    east = np.cross([0.0, 0.0, 1.0], u)
    east = east / np.linalg.norm(east)
    north = np.cross(u, east)
    sats = {
        ("G", 1): u * 2.65e7,
        ("E", 11): u * 2.70e7 + east * 1.0e6,
        ("C", 6): u * 2.62e7 + north * 1.2e6,
        ("J", 2): u * 2.68e7 - east * 0.8e6,
    }
    lines = [
        "#dP2026  9  1  0  0  0.00000000      96 ORBIT IGb14 HLM  GFZ",
        "## 2434 259200.00000000   900.00000000 60849 0.0000000000000",
    ]
    for i in range(n_epochs):
        secs = i * interval_s
        mm = int(secs // 60) % 60
        hh = int(secs // 3600)
        lines.append(f"*  2026  9  1 {hh:2d} {mm:2d}  0.00000000")
        for (sysc, prn), xyz in sats.items():
            x, y, z = (c / 1e3 for c in xyz)      # SP3 positions are km
            lines.append(f"P{sysc}{prn:02d}{x:14.6f}{y:14.6f}{z:14.6f}"
                         f"{0.0:14.6f}")
    lines.append("EOF")
    return "\n".join(lines) + "\n"


def test_precise_multi_covers_every_requested_constellation(tmp_path, monkeypatch):
    from backend import geometry
    from backend.ephem import precise
    from backend.gpstime import GPSTime
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)

    lat, lon, alt = 41.0, 29.0, 100.0
    rx = geometry.llh_to_ecef(lat, lon, alt)
    provider = precise.PreciseEphemerisProvider()
    provider.load_text(_mgex_multi_epoch_sp3(rx), source="mgex-multi")
    lo, hi = provider.product.coverage_seconds
    mid = GPSTime.from_seconds((lo + hi) / 2.0)

    payload = {"precise_provider": provider, "week": mid.week,
               "sow": mid.sow, "systems": ("G", "E", "C")}
    req = ScenarioRequest(rinex_path="AUTO", lat=lat, lon=lon, alt=alt,
                          start=dt.datetime(2026, 9, 1, 1, 30, 0), duration_s=2,
                          sample_rate=6_000_000.0, sample_format="int16",
                          engine="native", systems=["G", "E", "C"],
                          nav_override=payload)
    outdir = engine.run(req)                        # must not touch "AUTO"
    meta = json.loads((outdir / "meta.json").read_text())

    assert meta["provenance"]["ephemeris"] == "precise"
    assert set(meta["provenance"]["systems"]) == {"G", "E", "C"}
    l1 = next(b for b in meta["bands"] if b["id"] == "L1")
    assert set(l1["systems"]) == {"G", "E", "C"}
    assert {s["sys"] for s in meta["provenance"]["svs"]} == {"G", "E", "C"}


def test_precise_multi_generates_real_nav_not_a_stub(tmp_path, monkeypatch):
    """Regression for the SP3-fit generalisation in
    backend/ephem/ephemeris_fit.py: before it, engine.run's precise_multi
    branch built bare position/velocity stubs for every system and
    unconditionally skipped nav_message generation, so meta["provenance"]
    ["nav"] came back "none" even for a plain GPS+Galileo precise run --
    the exact "hep sp3 sikinti cikariyor" complaint about the native
    engine. G/E now get a real Gauss-Newton Kepler fit (geometry.SYS_PARAMS
    -parametrised) against their own precise state_fn; R (GLONASS, not
    Keplerian) reuses the broadcast RINEX record already read for glo_k.
    """
    import math

    from backend import geometry
    from backend.ephem import precise
    from backend.gpstime import GPSTime

    monkeypatch.setattr(config, "OUT_DIR", tmp_path)

    week, tow_base, interval, n_epochs = 2434, 259200.0, 900.0, 33
    mid_secs = (n_epochs // 2) * interval           # 14400 s past tow_base
    toe = tow_base + mid_secs
    # A physically plausible Kepler element set (same shape/magnitudes as
    # tests/test_ephemeris_fit.py's _kepler_eph -- GPS-radius orbit) shared
    # by both G and E rows; at tk=0 (t == toe) mu/omega_e_dot drop out of
    # geometry._orbit entirely, so both systems land at the identical ECEF
    # point there regardless of which datum eventually fits them.
    truth = {
        "m0": 0.3, "e": 0.006, "sqrtA": 5153.7, "delta_n": 4.9e-9,
        "i0": 0.97, "idot": -2.5e-10, "omega0": -0.55, "omega_dot": -8.1e-9,
        "omega": 0.72, "cuc": 1.2e-6, "cus": 7.5e-6, "crc": 240.0, "crs": 18.0,
        "cic": -1.0e-7, "cis": 8.0e-8, "toe": toe, "toc": toe,
        "gps_week": week, "af0": 0.0, "af1": 0.0, "af2": 0.0, "tgd": 0.0,
    }
    p0 = np.asarray(geometry.sat_state({**truth, "system": "G"}, toe)[0])
    r = float(np.linalg.norm(p0))
    lat = math.degrees(math.asin(p0[2] / r))
    lon = math.degrees(math.atan2(p0[1], p0[0]))
    alt = 100.0
    rx = np.asarray(geometry.llh_to_ecef(lat, lon, alt), float)
    u = rx / np.linalg.norm(rx)
    north = np.cross(u, np.cross([0.0, 0.0, 1.0], u))

    lines = [
        f"#dP2026  9  1  0  0  0.00000000     {n_epochs:2d} ORBIT IGb14 HLM  SYNTH",
        f"## {week} {tow_base:.8f}   {interval:.8f} 60849 0.0000000000000",
    ]
    for i in range(n_epochs):
        secs = i * interval
        hh, mm = int(secs // 3600), int(secs // 60) % 60
        lines.append(f"*  2026  9  1 {hh:2d} {mm:2d}  0.00000000")
        for sysc, prn in (("G", 1), ("E", 11), ("R", 1)):
            if sysc == "R":
                # Not Keplerian -- frozen near zenith is enough (its nav
                # comes from the broadcast RINEX record, not a fit).
                xyz = u * 2.55e7 - north * 0.9e6
            else:
                xyz = np.asarray(geometry.sat_state(
                    {**truth, "system": sysc}, tow_base + secs)[0])
            x, y, z = (c / 1e3 for c in xyz)
            lines.append(f"P{sysc}{prn:02d}{x:14.6f}{y:14.6f}{z:14.6f}"
                         f"{0.0:14.6f}")
    lines.append("EOF")

    provider = precise.PreciseEphemerisProvider()
    provider.load_text("\n".join(lines) + "\n", source="synth-kepler")
    lo, hi = provider.product.coverage_seconds
    mid = GPSTime.from_seconds((lo + hi) / 2.0)

    payload = {"precise_provider": provider, "week": mid.week,
              "sow": mid.sow, "systems": ("G", "E", "R")}
    req = ScenarioRequest(rinex_path=_MIXED, lat=lat, lon=lon, alt=alt,
                          start=dt.datetime(2026, 9, 1, 4, 0, 0), duration_s=2,
                          sample_rate=6_000_000.0, sample_format="int16",
                          engine="native", systems=["G", "E", "R"],
                          nav_override=payload)
    outdir = engine.run(req)
    meta = json.loads((outdir / "meta.json").read_text())

    assert meta["provenance"]["ephemeris"] == "precise"
    nav = meta["provenance"]["nav"]
    assert isinstance(nav, dict), (
        "nav generation skipped outright for a precise_multi run", nav,
        meta["provenance"]["warnings"])
    assert nav.get("G/L1") == "lnav", nav
    assert nav.get("E/L1") == "inav", nav
    assert nav.get("R/G1") == "strings", nav
