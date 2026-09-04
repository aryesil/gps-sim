from backend import lnav_display


def _eph():
    return dict(toe=259200.0, toc=259200.0, sqrtA=5153.7, e=0.004,
               m0=0.1, delta_n=4e-9, omega=0.3, omega0=-0.5, omega_dot=-8e-9,
               i0=0.97, idot=1e-10, cuc=1e-6, cus=8e-6, crc=200.0, crs=-30.0,
               cic=-1e-7, cis=9e-8, af0=1e-4, af1=1e-11, af2=0.0, tgd=-5e-9,
               gps_week=2380, health=0.0)


def test_subframe1_length_and_preamble():
    bits = lnav_display.subframe1_bits(_eph(), tow_count=100, week=2380)
    assert len(bits) == 300
    assert bits[:8] == [1, 0, 0, 0, 1, 0, 1, 1]  # 0x8B


def test_parity_is_deterministic_and_6_bits_effective():
    w = lnav_display.parity(0b101010101010101010101010 << 6, 0, 0)
    assert 0 <= w < (1 << 30)
    assert lnav_display.parity(0b101010101010101010101010 << 6, 0, 0) == w


def test_explain_reports_fields_and_checks():
    out = lnav_display.explain(_eph(), tow_count=100, week=2380)
    sf = out["subframe1"]
    assert sf["preamble_ok"] is True
    assert sf["parity_ok"] is True
    names = {f["name"] for f in sf["fields"]}
    assert {"week_number", "af0", "af1", "af2", "toc", "tgd"} <= names
