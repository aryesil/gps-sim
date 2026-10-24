"""Reference-oscillator error handling for the AD936x boards.

The driver takes the reference as an integer ``xo_correction`` in Hz (25 ppb steps at
40 MHz). A measured oscillator error is therefore split into the part the driver can
absorb and a small residual that the signal generator pre-compensates in the baseband
(carrier and code rate), so the on-air frequency is right to well below one driver step.
"""
from __future__ import annotations

XO_NOMINAL_HZ = 40e6


def split_osc_error(osc_ppm: float) -> tuple[float, float]:
    """``osc_ppm``: true oscillator fractional error in ppm (+ = runs fast).

    Returns ``(xo_ppm, residual)``: ``xo_ppm`` is the value to give the driver (its
    integer-Hz grid point), ``residual`` the leftover fractional error (dimensionless,
    +: the real reference is higher than the driver assumes, so the carrier comes out
    high). The generator shifts the carrier by ``-residual * f_carrier``, which is what a
    receiver-clock drift of ``+residual`` s/s does in ``backend.models.receiver_clock``.
    """
    true_hz = XO_NOMINAL_HZ * (1.0 + osc_ppm * 1e-6)
    xo_hz = int(round(true_hz))
    xo_ppm = (xo_hz / XO_NOMINAL_HZ - 1.0) * 1e6
    return xo_ppm, true_hz / xo_hz - 1.0
