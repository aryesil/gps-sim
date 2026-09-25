from __future__ import annotations

from dataclasses import dataclass

from backend import config


@dataclass(frozen=True)
class Signal:
    carrier_hz: float
    chip_rate_hz: float
    code_len: int
    boc: tuple | None
    nav_sym_hz: float
    band: str
    sys: str = "G"
    sub_carrier_hz: float = 0.0


SIGNALS = {
    "GPS_L1CA": Signal(config.L1_HZ, config.CA_CHIP_HZ, config.CA_CODE_LEN,
                       None, config.NAV_BIT_HZ, "L1"),
    "QZSS_L1CA": Signal(config.L1_HZ, 1.023e6, 1023, None, 50.0, "L1", sys="J"),
    "SBAS_L1": Signal(config.L1_HZ, 1.023e6, 1023, None, 250.0, "L1", sys="S"),
    "GAL_E1": Signal(config.L1_HZ, 1.023e6, 4092, (1, 1), 250.0, "L1", sys="E",
                     sub_carrier_hz=1.023e6),
    # B1I shares the "L1" band id with GPS/Galileo but sits on its own
    # carrier; bands.band_centre widens the L1 output to span both.
    "BDS_B1I": Signal(config.B1I_HZ, 2.046e6, 2046, None, 50.0, "L1", sys="C"),
    "GLO_G1": Signal(1_602_000_000.0, 0.511e6, 511, None, 100.0, "G1", sys="R"),
    # --- L2 band (1227.60 MHz) ---------------------------------------------
    "GPS_L2C": Signal(config.L2_HZ, 0.5115e6, 10230, None, 50.0, "L2"),
    # GLONASS's own FDMA plan puts L2OF's k=0 channel at 1246.00 MHz (ICD
    # L1/L2, k*437.5 kHz per channel) -- an entirely different physical
    # carrier from GPS L2C's 1227.60 MHz (~18.75 MHz away), same relationship
    # as G1 (1602.00 MHz) is to GPS L1 (1575.42 MHz). Literal constant, not
    # config.L2_HZ, matching GLO_G1's own-literal convention above.
    "GLO_L2OF": Signal(1_246_000_000.0, 0.511e6, 511, None, 100.0, "G2", sys="R"),
    # --- L5 band (1176.45 MHz) -------------------------------------------
    "GPS_L5I": Signal(config.L5_HZ, 10.23e6, 10230, None, 50.0, "L5"),
    "GPS_L5Q": Signal(config.L5_HZ, 10.23e6, 10230, None, 0.0, "L5"),
    "QZSS_L5I": Signal(config.L5_HZ, 10.23e6, 10230, None, 50.0, "L5", sys="J"),
    "QZSS_L5Q": Signal(config.L5_HZ, 10.23e6, 10230, None, 0.0, "L5", sys="J"),
    "GAL_E5AI": Signal(config.L5_HZ, 10.23e6, 10230, None, 50.0, "L5", sys="E"),
    "GAL_E5AQ": Signal(config.L5_HZ, 10.23e6, 10230, None, 0.0, "L5", sys="E"),
    "BDS_B2AD": Signal(config.L5_HZ, 10.23e6, 10230, None, 200.0, "L5", sys="C"),
    "BDS_B2AP": Signal(config.L5_HZ, 10.23e6, 10230, None, 0.0, "L5", sys="C"),
    "IRNSS_L5": Signal(config.L5_HZ, 1.023e6, 1023, None, 50.0, "L5", sys="I"),
}

SYSTEMS = ("G", "R", "E", "C", "J", "S", "I")

_SIGNAL_FOR = {"G": "GPS_L1CA", "J": "QZSS_L1CA", "S": "SBAS_L1",
               "E": "GAL_E1", "C": "BDS_B1I", "R": "GLO_G1"}


def signal_for(sys: str) -> Signal:
    return SIGNALS[_SIGNAL_FOR[sys]]



# GLONASS's own FDMA bands never share a user-facing "bands" string with
# their band tag ("G1"/"G2", not "L1"/"L2" -- they need their own centre
# frequency/output file, physically distant from GPS's L1/L2). L1<->G1 stays
# unaliased here (an existing, already-tested contract: an explicit
# bands=["L1"] request contributes nothing for GLONASS, only the
# bands-unset per-system default does -- see app._internal_bands_for).
# L2<->G2 gets the alias below so GLONASS L2OF becomes reachable at all: it
# has no bands-unset default (that slot is G1's), so without this an
# explicit bands=["L2"] request would never resolve any GLONASS signal.
_GLO_FDMA_ALIAS = {"L2": "G2"}


def signals_for(sys: str, bands=None) -> list[Signal]:
    """Every registered Signal for ``sys`` whose band is in ``bands``
    (all bands when ``bands is None``), ordered by (band, key)."""
    want = None if bands is None else set(bands)
    if want is not None and sys == "R":
        want = want | {_GLO_FDMA_ALIAS[b] for b in want if b in _GLO_FDMA_ALIAS}
    out = [(key, sig) for key, sig in SIGNALS.items()
           if sig.sys == sys and (want is None or sig.band in want)]
    out.sort(key=lambda kv: (kv[1].band, kv[0]))
    return [sig for _key, sig in out]


def glo_channel_offset_hz(k: int, step_hz: float = 562_500.0) -> float:
    """GLONASS FDMA channel offset for channel index k.

    Args:
        k: Channel index in range [-7, 6]
        step_hz: per-channel spacing -- 562.5 kHz on G1 (L1OF, the default,
            every existing caller's value), 437.5 kHz on G2 (L2OF).

    Returns:
        Channel offset in Hz: k * step_hz

    Raises:
        ValueError: If k is not in [-7, 6]
    """
    if k not in range(-7, 7):
        raise ValueError(f"k must be in range [-7, 6], got {k}")
    return k * step_hz
