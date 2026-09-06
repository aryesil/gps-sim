import datetime as dt

import pytest

from backend import scenario


def _req(**kw):
    base = dict(rinex_path="/x/brdc.rnx", lat=41.0, lon=29.0, alt=100.0,
               start=dt.datetime(2026, 9, 3, 6, 0, 0), duration_s=30)
    base.update(kw)
    return scenario.ScenarioRequest(**base)


def test_static_args():
    a = scenario.build_args(_req(), out_bin="/o/g.bin", motion_csv=None)
    assert "-e" in a and "/x/brdc.rnx" in a
    assert a[a.index("-o") + 1] == "/o/g.bin"
    assert a[a.index("-s") + 1] == "2600000.0"
    assert a[a.index("-b") + 1] == "16"
    assert a[a.index("-d") + 1] == "30"
    assert a[a.index("-l") + 1] == "41.0,29.0,100.0"
    # -t is shifted by GPS-UTC leap seconds (gps-sdr-sim -t is GPS timescale)
    assert a[a.index("-t") + 1] == "2026/09/03,06:00:18"


def test_ionosphere_disabled_by_default():
    a = scenario.build_args(_req(), out_bin="/o/g.bin", motion_csv=None)
    assert "-i" in a


def test_ionosphere_enabled_omits_flag():
    a = scenario.build_args(_req(ionosphere=True), out_bin="/o/g.bin", motion_csv=None)
    assert "-i" not in a


def test_int8_format_sets_b8():
    a = scenario.build_args(_req(sample_format="int8"), out_bin="/o/g.bin", motion_csv=None)
    assert a[a.index("-b") + 1] == "8"


def test_dynamic_uses_motion_file():
    req = _req(route=[(41.0, 29.0, 100.0), (41.01, 29.01, 100.0)])
    a = scenario.build_args(req, out_bin="/o/g.bin", motion_csv="/o/m.csv")
    assert "-l" not in a
    assert a[a.index("-x") + 1] == "/o/m.csv"


def test_write_motion_csv_row_count(tmp_path):
    req = _req(duration_s=5, route=[(41.0, 29.0, 100.0), (41.02, 29.0, 100.0)])
    p = tmp_path / "m.csv"
    scenario.write_motion_csv(req, p)
    rows = p.read_text().strip().splitlines()
    assert len(rows) == 50
    first = [float(x) for x in rows[0].split(",")]
    assert first[0] == 0.0
    assert first[1:3] == [41.0, 29.0]


def test_write_motion_csv_requires_route(tmp_path):
    with pytest.raises(ValueError):
        scenario.write_motion_csv(_req(), tmp_path / "m.csv")


def test_route_llh_at_endpoints_and_midpoint():
    route = [(41.0, 29.0, 100.0), (41.2, 29.4, 200.0)]
    assert scenario.route_llh_at(route, 30, 0.0) == route[0]
    assert scenario.route_llh_at(route, 30, 30.0) == pytest.approx(route[1])
    # ~halfway through the run is ~halfway along the single segment (the
    # k/(n-1) mapping puts it a hair past 0.5, same as the motion CSV).
    mid = scenario.route_llh_at(route, 30, 15.0)
    assert mid == pytest.approx((41.1, 29.2, 150.0), rel=5e-3)
    assert route[0][0] < mid[0] < route[1][0]


def test_route_llh_at_hits_middle_waypoint():
    route = [(0.0, 0.0, 0.0), (10.0, 20.0, 30.0), (0.0, 0.0, 0.0)]
    assert scenario.route_llh_at(route, 40, 20.0) == pytest.approx(route[1],
                                                                   rel=5e-3)


def test_route_llh_at_clamps_out_of_range():
    route = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)]
    assert scenario.route_llh_at(route, 10, -5.0) == route[0]
    assert scenario.route_llh_at(route, 10, 999.0) == pytest.approx(route[1])


def test_estimate_bytes():
    assert scenario.estimate_bytes(_req(duration_s=10, sample_rate=2.6e6)) == 2 * 2 * 2_600_000 * 10
    assert scenario.estimate_bytes(_req(duration_s=10, sample_format="int8")) == 2 * 1 * 2_600_000 * 10
