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
}

SYSTEMS = ("G", "R", "E", "C", "J", "S")

_SIGNAL_FOR = {"G": "GPS_L1CA", "J": "QZSS_L1CA", "S": "SBAS_L1",
               "E": "GAL_E1", "C": "BDS_B1I"}      # no "R" yet


def signal_for(sys: str) -> Signal:
    return SIGNALS[_SIGNAL_FOR[sys]]


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
