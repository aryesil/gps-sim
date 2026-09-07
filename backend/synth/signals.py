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
    "BDS_B1I": Signal(config.L1_HZ, 2.046e6, 2046, None, 50.0, "L1", sys="C"),
    "GLO_G1": Signal(1_602_000_000.0, 0.511e6, 511, None, 100.0, "G1", sys="R"),
    # --- L2 band (1227.60 MHz) ---------------------------------------------
    "GPS_L2C": Signal(config.L2_HZ, 1.023e6, 10230, None, 50.0, "L2"),
    "GLO_L2OF": Signal(config.L2_HZ, 0.511e6, 511, None, 100.0, "G2", sys="R"),
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


def signals_for(sys: str, bands=None) -> list[Signal]:
    """Every registered Signal for ``sys`` whose band is in ``bands``
    (all bands when ``bands is None``), ordered by (band, key)."""
    want = None if bands is None else set(bands)
    out = [(key, sig) for key, sig in SIGNALS.items()
           if sig.sys == sys and (want is None or sig.band in want)]
    out.sort(key=lambda kv: (kv[1].band, kv[0]))
    return [sig for _key, sig in out]


def glo_channel_offset_hz(k: int) -> float:
    """GLONASS FDMA channel offset for channel index k.

    Args:
        k: Channel index in range [-7, 6]

    Returns:
        Channel offset in Hz: k * 562_500.0

    Raises:
        ValueError: If k is not in [-7, 6]
    """
    if k not in range(-7, 7):
        raise ValueError(f"k must be in range [-7, 6], got {k}")
    return k * 562_500.0
