from backend import scenario


def test_scenario_request_engine_defaults():
    req = scenario.ScenarioRequest(rinex_path="x", lat=0, lon=0, alt=0,
                                   start=__import__("datetime").datetime(2024, 1, 1),
                                   duration_s=1)
    assert req.engine == "gps-sdr-sim"
    assert req.fading is None


def test_bytes_per_sample_int12():
    assert scenario._bytes_per_sample("int12") == 2
    assert scenario._bytes_per_sample("int8") == 1
    assert scenario._bytes_per_sample("int16") == 2
