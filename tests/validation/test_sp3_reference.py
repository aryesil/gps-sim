"""The precise (Neville) SP3 interpolator agrees with an independent
least-squares-polynomial interpolator, and reproduces the SP3 nodes."""
import pathlib

import numpy as np
import pytest

from backend import precise, reference
from backend.gpstime import GPSTime

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "igs_sample.sp3"


@pytest.fixture(scope="module")
def provider():
    p = precise.PreciseEphemerisProvider()
    p.load(FIX)
    return p


def test_interpolators_agree_between_nodes(provider):
    sp3 = provider.product
    for prn in sp3.satellites()[:4]:
        rows = sp3.records[prn]
        # a few epochs strictly inside the centred-window region
        lo, hi = rows[0][0], rows[-1][0]
        for t in np.linspace(lo + 0.35 * (hi - lo), lo + 0.65 * (hi - lo), 5):
            st = provider.get_state(prn, GPSTime.from_seconds(float(t)))
            ref = reference.sp3_interp_state(rows, float(t))
            dp = np.linalg.norm(np.array(st.position_ecef_m) - ref["pos"])
            dv = np.linalg.norm(np.array(st.velocity_ecef_mps) - ref["vel"])
            assert dp < 1e-3, (prn, t, dp)          # sub-mm agreement
            assert dv < 1e-5, (prn, t, dv)


def test_reference_reproduces_sp3_nodes(provider):
    sp3 = provider.product
    prn = sp3.satellites()[0]
    rows = sp3.records[prn]
    for r in rows[4:-4:5]:
        ref = reference.sp3_interp_state(rows, r[0])
        assert np.linalg.norm(ref["pos"] - np.array(r[1:4])) < 1e-4


def test_out_of_coverage_raises(provider):
    sp3 = provider.product
    prn = sp3.satellites()[0]
    rows = sp3.records[prn]
    with pytest.raises(ValueError):
        reference.sp3_interp_state(rows, rows[-1][0] + 3600.0)
