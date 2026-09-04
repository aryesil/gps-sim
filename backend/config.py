import os
import pathlib

def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}

def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)

def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))

L1_HZ = 1575.42e6
CA_CHIP_HZ = 1.023e6
CA_CODE_LEN = 1023
NAV_BIT_HZ = 50
MU = 3.986005e14
OMEGA_E_DOT = 7.2921151467e-5
C = 299792458.0
F_REL = -4.442807633e-10
GPS_UTC_LEAP_S = 18.0  # GPS - UTC, valid 2017-01-01 .. (update on next leap second)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = pathlib.Path(_str("DATA_DIR", str(_ROOT / "data")))
OUT_DIR = pathlib.Path(_str("OUT_DIR", str(_ROOT / "out")))

GPS_SDR_SIM_BIN = _str("GPS_SDR_SIM_BIN", str(_ROOT / "gps-sdr-sim" / "gps-sdr-sim"))
RINEX_MIRRORS = [
    "https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{yyyy}/{ddd}/BRDC00WRD_R_{yyyy}{ddd}0000_01D_GN.rnx.gz",
    "https://cddis.nasa.gov/archive/gnss/data/daily/{yyyy}/brdc/BRDC00IGS_R_{yyyy}{ddd}0000_01D_GN.rnx.gz",
]

ALLOW_TX = _flag("ALLOW_TX", False)
DEVICE_URI = _str("DEVICE_URI", "ip:192.168.2.1")
DEFAULT_SAMPLE_RATE = _float("DEFAULT_SAMPLE_RATE", 2.6e6)
DEFAULT_FORMAT = _str("DEFAULT_FORMAT", "int16")

DATA_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "rinex").mkdir(parents=True, exist_ok=True)
