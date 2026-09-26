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
B1I_HZ = 1561.098e6  # BeiDou B1I (BDS-SIS-ICD-B1I 3.1), 14.322 MHz below L1
L2_HZ = 1227.60e6   # 120 * 10.23 MHz  -- GPS L2 / GLONASS L2 band centre
L5_HZ = 1176.45e6   # 115 * 10.23 MHz  -- GPS L5 / E5a / B2a / NavIC L5 band centre
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
# are loaded from a local path by default. PRECISE_SP3_MIRRORS defaults to
# anonymous, no-login product mirrors (GFZ, ESA navigation-office, IGN,
# BKG); a download is still only performed when the operator explicitly
# requests one (the /api/precise/load "download" field) or runs a
# precise-ephemeris scenario. Set the env var to override the list, or to
# "" to disable SP3 downloads entirely. Templates (http, https or ftp) may
# use {gpsweek}/{gps_week}, {dow}, {yyyy}, {doy}, {wwwwd}, {hh}
# (ultra-rapid solution hour, defaults to "00"). download_sp3 probes them
# in order until a product carries every requested system (it reads the
# systems from the file, not the name), else keeps the one covering most.
# Tiers: rapid (~17 h latency, final-grade orbits) -> final (~12 d, best)
# -> ultra-rapid (2-day file whose second half is *predicted*).
_DEFAULT_SP3_MIRRORS = (
    # --- multi-GNSS. GFZ's MGEX rapid (GBM0MGXRAP) is the only anonymous
    # full-GRECJ daily product reachable from most networks: GFZ's FTP
    # server serves it the day after. ESA/ESOC's operational orbits
    # (plain HTTP) carry GPS+GLONASS only. The IGN MGEX copies (GFZ0MGXRAP,
    # WUM0MGXFIN) follow for networks that reach igs.ign.fr.
    "ftp://ftp.gfz-potsdam.de/pub/GNSS/products/mgex/{gpsweek}_IGS20/"
    "GBM0MGXRAP_{yyyy}{doy}0000_01D_05M_ORB.SP3.gz,"
    "http://navigation-office.esa.int/products/gnss-products/{gpsweek}/"
    "ESA0OPSRAP_{yyyy}{doy}0000_01D_05M_ORB.SP3.gz,"
    "http://navigation-office.esa.int/products/gnss-products/{gpsweek}/"
    "ESA0OPSFIN_{yyyy}{doy}0000_01D_05M_ORB.SP3.gz,"
    "https://igs.ign.fr/pub/igs/products/mgex/{gpsweek}/"
    "GFZ0MGXRAP_{yyyy}{doy}0000_01D_05M_ORB.SP3.gz,"
    "https://igs.ign.fr/pub/igs/products/mgex/{gpsweek}/"
    "WUM0MGXFIN_{yyyy}{doy}0000_01D_15M_ORB.SP3.gz,"
    # --- GPS-only IGS Operational products -- last-resort fallback.
    "https://igs.bkg.bund.de/root_ftp/IGS/products/{gpsweek}/"
    "IGS0OPSRAP_{yyyy}{doy}0000_01D_15M_ORB.SP3.gz,"
    "https://igs.ign.fr/pub/igs/products/{gpsweek}/"
    "IGS0OPSRAP_{yyyy}{doy}0000_01D_15M_ORB.SP3.gz,"
    "https://igs.bkg.bund.de/root_ftp/IGS/products/{gpsweek}/"
    "IGS0OPSFIN_{yyyy}{doy}0000_01D_15M_ORB.SP3.gz,"
    "https://igs.ign.fr/pub/igs/products/{gpsweek}/"
    "IGS0OPSFIN_{yyyy}{doy}0000_01D_15M_ORB.SP3.gz,"
    "https://igs.bkg.bund.de/root_ftp/IGS/products/{gpsweek}/"
    "IGS0OPSULT_{yyyy}{doy}{hh}00_02D_15M_ORB.SP3.gz,"
    "https://igs.ign.fr/pub/igs/products/{gpsweek}/"
    "IGS0OPSULT_{yyyy}{doy}{hh}00_02D_15M_ORB.SP3.gz"
)
PRECISE_DIR = pathlib.Path(_str("PRECISE_DIR", str(DATA_DIR / "precise")))
PRECISE_SP3_MIRRORS = [m.strip() for m in
                       _str("PRECISE_SP3_MIRRORS", _DEFAULT_SP3_MIRRORS).split(",")
                       if m.strip()]
# Ultra-rapid products for epochs no daily product covers yet (today and
# usually yesterday): precise.download_sp3_ultra picks the newest solution
# issued >= 30 min before the epoch. {gpsweek}/{dow}/{yyyy}/{doy}/{hh} are
# the solution's first epoch. GFZ issues every 3 h with G/R/E, ESA every
# 6 h with G/R. "" disables the fallback.
_DEFAULT_SP3_ULTRA_MIRRORS = (
    "ftp://ftp.gfz-potsdam.de/pub/GNSS/products/ultra/w{gpsweek}/"
    "gfu{gpsweek}{dow}_{hh}.sp3.gz,"
    "http://navigation-office.esa.int/products/gnss-products/{gpsweek}/"
    "ESA0OPSULT_{yyyy}{doy}{hh}00_02D_05M_ORB.SP3.gz"
)
PRECISE_SP3_ULTRA_MIRRORS = [
    m.strip() for m in
    _str("PRECISE_SP3_ULTRA_MIRRORS", _DEFAULT_SP3_ULTRA_MIRRORS).split(",")
    if m.strip()]

ALLOW_TX = _flag("ALLOW_TX", False)
RF_FRONTEND_ENABLED = _flag("RF_FRONTEND_ENABLED", False)
DEVICE_URI = _str("DEVICE_URI", "ip:192.168.3.1")
# Measured reference-oscillator error of the TX SDR in ppm (see
# transmit.TxParams.xo_ppm); a request body's "xo_ppm" overrides it.
DEVICE_XO_PPM = _float("DEVICE_XO_PPM", 0.0)
# Live start_utc "now": how far ahead of the wall clock segment 0 is
# stamped -- the delay from session start until its first sample leaves
# the antenna (first-segment generation + host/kernel TX buffering).
# Measured 2.5 s on a LibreSDR (AD9361, USB) at 2.6 Msps.
LIVE_START_LEAD_S = _float("LIVE_START_LEAD_S", 2.5)
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
