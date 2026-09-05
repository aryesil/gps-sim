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


SIGNALS = {
    "GPS_L1CA": Signal(config.L1_HZ, config.CA_CHIP_HZ, config.CA_CODE_LEN,
                       None, config.NAV_BIT_HZ, "L1"),
}
