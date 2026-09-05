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
LOG_DIR = pathlib.Path(_str("LOG_DIR", str(_ROOT / "logs")))

GPS_SDR_SIM_BIN = _str("GPS_SDR_SIM_BIN", str(_ROOT / "gps-sdr-sim" / "gps-sdr-sim"))
RINEX_MIRRORS = [
    # BKG serves the combined nav file as "MN" (mixed GNSS nav), not "GN" --
    # confirmed against the live directory listing; georinex's use="G" filter
    # picks the GPS records back out of it.
    "https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{yyyy}/{ddd}/BRDC00WRD_R_{yyyy}{ddd}0000_01D_MN.rnx.gz",
    # CDDIS requires a NASA Earthdata login; without credentials it returns
    # a 200 OK HTML login page instead of the file. ephemeris._download's
    # "RINEX VERSION" content check already rejects that, so this mirror is
    # a no-op until credentials are configured -- kept as a documented,
    # harmless fallback attempt.
    "https://cddis.nasa.gov/archive/gnss/data/daily/{yyyy}/brdc/BRDC00IGS_R_{yyyy}{ddd}0000_01D_GN.rnx.gz",
]

# Precise-ephemeris analysis subsystem (backend/precise.py). SP3 products
# are loaded from a local path by default; PRECISE_SP3_MIRRORS is empty
# unless the operator opts in to best-effort downloads. {gpsweek}/{dow}
# and {yyyy}/{doy} are both offered for the two common IGS naming schemes.
PRECISE_DIR = pathlib.Path(_str("PRECISE_DIR", str(DATA_DIR / "precise")))
PRECISE_SP3_MIRRORS = [m.strip() for m in _str("PRECISE_SP3_MIRRORS", "").split(",") if m.strip()]

ALLOW_TX = _flag("ALLOW_TX", False)
DEVICE_URI = _str("DEVICE_URI", "ip:192.168.2.1")
DEFAULT_SAMPLE_RATE = _float("DEFAULT_SAMPLE_RATE", 2.6e6)
DEFAULT_FORMAT = _str("DEFAULT_FORMAT", "int16")

# Role-based access control: API_KEYS_JSON='{"<key>": "operator"|"viewer"}'.
# Empty (the default) means auth is disabled entirely -- a single-operator
# rig with no configured keys behaves exactly as before RBAC existed.
def _api_keys() -> dict:
    raw = os.environ.get("API_KEYS_JSON", "")
    if not raw.strip():
        return {}
    import json
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}

API_KEYS = _api_keys()

DATA_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "rinex").mkdir(parents=True, exist_ok=True)
PRECISE_DIR.mkdir(parents=True, exist_ok=True)
