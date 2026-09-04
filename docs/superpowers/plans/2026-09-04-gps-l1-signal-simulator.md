# GPS L1 C/A Signal Simulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an interactive web tool that turns a map-picked location and start time into a GPS L1 C/A baseband IQ file, shows the expected constellation geometry and correlation picture, verifies the file with an internal software receiver, and streams it to a PlutoSDR-class device over libiio for replay in an isolated setup.

**Architecture:** A FastAPI backend orchestrates live BRDC ephemeris retrieval, a pure-Python geometry engine, the external `gps-sdr-sim` binary for RF synthesis, an IQ inspector and software receiver built on numpy, and a libiio transmit path. A static Leaflet frontend drives it over REST plus Server-Sent Events. `gps-sdr-sim` owns IQ synthesis; every other layer is ours.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, numpy, requests, georinex, python-dateutil, pyadi-iio + libiio; `gps-sdr-sim` (C, built from source); vanilla JS + Leaflet frontend.

**Spec:** `docs/superpowers/specs/2026-09-04-gps-l1-signal-simulator-design.md`

## Global Constraints

- Python 3.11 or newer; all backend modules live under `backend/` as a flat package.
- GPS L1 C/A only. No other constellation, band, or signal.
- L1 carrier `1575.42e6` Hz; C/A chip rate `1.023e6` chips/s; C/A code length `1023` chips; nav bit rate `50` bit/s.
- WGS-84 / IS-GPS-200 constants: `mu = 3.986005e14`, `OMEGA_E_DOT = 7.2921151467e-5`, `c = 299792458.0`, `F_REL = -4.442807633e-10`.
- Default sample rate `2.6e6` Hz; default sample format int16 interleaved I,Q, baseband (LO = L1). int8 is a file-only option.
- `gps-sdr-sim` is built with `-DUSER_MOTION_SIZE=4000`; its binary path is read from `backend/config.py`.
- Transmit is gated: `ALLOW_TX` in `backend/config.py` (default `False`) AND an explicit UI confirmation. With `ALLOW_TX` false, `/api/transmit*` returns 403.
- IQ files stream to the SDR once, start to end, never looped.
- RINEX is fetched from an auth-free mirror; on failure the UI accepts a manual upload.
- Every task is TDD: write the failing test, see it fail, implement minimally, see it pass, commit.
- Test runner: `pytest`. Run from repo root. Backend importable as `from backend import <module>`.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, deps, pytest config |
| `scripts/setup.sh` | Build `gps-sdr-sim`, install Python deps, print system-lib hints |
| `backend/__init__.py` | Marks package |
| `backend/config.py` | Constants, binary path, mirror URLs, `ALLOW_TX`, defaults |
| `backend/ephemeris.py` | `get_ephemeris(date)` — cache + download + georinex parse |
| `backend/geometry.py` | Satellite state (IS-GPS-200), az/el, pseudorange, code phase, Doppler, visibility, DOP |
| `backend/scenario.py` | Build `gps-sdr-sim` args and user-motion CSV |
| `backend/generator.py` | Run `gps-sdr-sim`, parse progress, write `meta.json` |
| `backend/inspector.py` | Read IQ, spectrum, per-PRN acquisition, compare to geometry table |
| `backend/receiver.py` | Acquisition + tracking + least-squares position from IQ |
| `backend/lnav_display.py` | Re-derive LNAV subframe bytes / parity / ephemeris fields for teaching (display only) |
| `backend/transmit.py` | pyadi-iio device setup + raw `iio.Buffer` continuous stream + dry-run + stop |
| `backend/app.py` | FastAPI routes, SSE, static file serving, startup checks |
| `frontend/index.html` | Page shell and panels |
| `frontend/style.css` | Layout and styling |
| `frontend/map.js` | Leaflet map, RX marker, optional route |
| `frontend/skyplot.js` | Polar skyplot + DOP readout |
| `frontend/plots.js` | Spectrum + correlation canvas plots |
| `frontend/transmit.js` | Transmit panel: device fields, start/stop, live elapsed + underflow |
| `frontend/app.js` | Wire panels to REST/SSE, state |
| `tests/fixtures/brdc_sample.rnx` | Small committed RINEX 3 GN file |
| `tests/fixtures/known_geometry.json` | Golden satellite table for the fixture epoch/location |
| `tests/test_config.py` … `tests/test_integration_generate.py` | One test module per backend module + integration |

---

## Task 1: Project scaffold and config

**Files:**
- Create: `pyproject.toml`, `backend/__init__.py`, `backend/config.py`, `scripts/setup.sh`, `README.md`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `backend.config` module exposing constants `L1_HZ=1575.42e6`, `CA_CHIP_HZ=1.023e6`, `CA_CODE_LEN=1023`, `NAV_BIT_HZ=50`, `MU=3.986005e14`, `OMEGA_E_DOT=7.2921151467e-5`, `C=299792458.0`, `F_REL=-4.442807633e-10`; `GPS_SDR_SIM_BIN: str` (path); `RINEX_MIRRORS: list[str]`; `ALLOW_TX: bool`; `DEFAULT_SAMPLE_RATE=2.6e6`; `DEFAULT_FORMAT="int16"`; `DATA_DIR: pathlib.Path`; `OUT_DIR: pathlib.Path`; `DEVICE_URI: str`. All overridable via environment variables of the same name.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import importlib
import os


def test_constants_have_expected_values():
    cfg = importlib.import_module("backend.config")
    assert cfg.L1_HZ == 1575.42e6
    assert cfg.CA_CHIP_HZ == 1.023e6
    assert cfg.CA_CODE_LEN == 1023
    assert cfg.NAV_BIT_HZ == 50
    assert cfg.C == 299792458.0
    assert cfg.ALLOW_TX is False
    assert cfg.DEFAULT_SAMPLE_RATE == 2.6e6


def test_env_override(monkeypatch):
    monkeypatch.setenv("ALLOW_TX", "1")
    monkeypatch.setenv("DEVICE_URI", "ip:10.0.0.5")
    import backend.config as cfg
    importlib.reload(cfg)
    assert cfg.ALLOW_TX is True
    assert cfg.DEVICE_URI == "ip:10.0.0.5"
    importlib.reload(cfg)  # restore
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.config'`

- [ ] **Step 3: Write minimal implementation**

```toml
# pyproject.toml
[project]
name = "gps-sim"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn>=0.29",
    "numpy>=1.26",
    "requests>=2.31",
    "georinex>=1.16",
    "python-dateutil>=2.9",
    "pyadi-iio>=0.0.16",
]

[project.optional-dependencies]
dev = ["pytest>=8", "httpx>=0.27"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

```python
# backend/__init__.py
```

```python
# backend/config.py
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
```

```bash
# scripts/setup.sh
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x gps-sdr-sim/gps-sdr-sim ]; then
  [ -d gps-sdr-sim ] || git clone https://github.com/osqzss/gps-sdr-sim
  gcc -O3 gps-sdr-sim/gpssim.c -lm -o gps-sdr-sim/gps-sdr-sim -DUSER_MOTION_SIZE=4000
fi
echo "gps-sdr-sim built: $(gps-sdr-sim/gps-sdr-sim 2>&1 | head -1 || true)"

python -m pip install -e ".[dev]"

echo "System libs for transmit (install if you will use the SDR path):"
echo "  macOS:  brew install libiio libad9361"
echo "  Debian: apt install libiio-dev libad9361-dev"
```

```markdown
<!-- README.md -->
# GPS L1 C/A Signal Simulator

Interactive scenario workbench: pick a place and time on a map, generate a GPS L1
C/A baseband IQ file with `gps-sdr-sim`, inspect it, verify it with an internal
software receiver, and stream it to a PlutoSDR-class SDR for replay.

## Setup

    ./scripts/setup.sh
    python -m uvicorn backend.app:app --reload

Open http://127.0.0.1:8000

## Safety

Transmitting in the GPS band over the air is illegal and disrupts real receivers.
Use a cabled or shielded (Faraday) setup only. When feeding a receiver over
cable, use low TX gain (-40 to -60 dB) plus 30-60 dB in-line attenuation.
Transmit is disabled unless `ALLOW_TX=1` and you confirm the isolated setup in
the UI.

## Manual hardware acceptance checklist

1. `ALLOW_TX=1 python -m uvicorn backend.app:app`
2. Generate a static scenario for your location, current UTC, 300 s, 2.6 Msps int16.
3. Run "Receiver check" — expect a fix within 100 m of the marker.
4. Connect SDR TX to the receiver antenna port through >= 40 dB attenuation.
5. Transmit panel: URI, LO 1575.42 MHz, rate 2.6 Msps, TX gain -50 dB, tick the
   isolated-setup confirmation, Start.
6. Record: receiver TTFF, reported position vs marker, sustained underflow count.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
chmod +x scripts/setup.sh
git add pyproject.toml backend/__init__.py backend/config.py scripts/setup.sh README.md tests/test_config.py
git commit -m "feat: project scaffold and config module"
```

---

## Task 2: RINEX ephemeris retrieval and parsing

**Files:**
- Create: `backend/ephemeris.py`, `tests/fixtures/brdc_sample.rnx`
- Test: `tests/test_ephemeris.py`

**Interfaces:**
- Consumes: `backend.config` (`DATA_DIR`, `RINEX_MIRRORS`).
- Produces:
  - `parse_rinex(path: str | pathlib.Path) -> dict[int, dict]` — maps PRN (int, 1-32) to an ephemeris dict with float keys: `toe, toc, sqrtA, e, m0, delta_n, omega, omega0, omega_dot, i0, idot, cuc, cus, crc, crs, cic, cis, af0, af1, af2, tgd, gps_week, health`. When a PRN has several epochs, keep the one whose `toe` is nearest the file's midday.
  - `get_ephemeris(date: datetime.date, download: bool = True) -> dict[int, dict]` — returns `parse_rinex` output for a cached file, downloading from `RINEX_MIRRORS` into `DATA_DIR/rinex/<name>` if absent and `download` is True. Raises `EphemerisUnavailable` if no cache and all mirrors fail.
  - `save_uploaded_rinex(date, raw_bytes) -> pathlib.Path` — writes bytes (optionally gz) into the cache under the canonical name so `get_ephemeris(date, download=False)` then works.
  - Exception class `EphemerisUnavailable(Exception)`.

- [ ] **Step 1: Write the failing test**

Obtain the fixture first: download one real daily file, trim to ~8 GPS satellites to keep it small, save as `tests/fixtures/brdc_sample.rnx` (RINEX 3, GPS-only, one epoch block near 12:00). Commit it with the test.

```python
# tests/test_ephemeris.py
import datetime as dt
import gzip
import pathlib

import pytest

from backend import ephemeris

FIX = pathlib.Path(__file__).parent / "fixtures" / "brdc_sample.rnx"


def test_parse_rinex_returns_prn_keyed_ephemeris():
    eph = ephemeris.parse_rinex(FIX)
    assert len(eph) >= 6
    prn = sorted(eph)[0]
    e = eph[prn]
    for k in ("toe", "sqrtA", "e", "m0", "delta_n", "omega", "omega0",
              "omega_dot", "i0", "idot", "cuc", "cus", "crc", "crs",
              "cic", "cis", "af0", "af1", "af2", "tgd"):
        assert isinstance(e[k], float)
    assert 5150.0 < e["sqrtA"] < 5160.0        # GPS semi-major axis sqrt, metres^0.5
    assert 0.0 <= e["e"] < 0.03


def test_get_ephemeris_uses_cache_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(ephemeris.config, "DATA_DIR", tmp_path)
    (tmp_path / "rinex").mkdir()
    date = dt.date(2026, 9, 3)
    ephemeris.save_uploaded_rinex(date, FIX.read_bytes())
    eph = ephemeris.get_ephemeris(date, download=False)
    assert len(eph) >= 6


def test_get_ephemeris_raises_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(ephemeris.config, "DATA_DIR", tmp_path)
    (tmp_path / "rinex").mkdir()
    with pytest.raises(ephemeris.EphemerisUnavailable):
        ephemeris.get_ephemeris(dt.date(2000, 1, 1), download=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ephemeris.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.ephemeris'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/ephemeris.py
from __future__ import annotations

import datetime as dt
import gzip
import io
import pathlib

import georinex as gr
import requests

from backend import config


class EphemerisUnavailable(Exception):
    pass


_VARMAP = {
    "toe": "Toe", "toc": None, "sqrtA": "sqrtA", "e": "Eccentricity",
    "m0": "M0", "delta_n": "DeltaN", "omega": "omega", "omega0": "Omega0",
    "omega_dot": "OmegaDot", "i0": "Io", "idot": "IDOT",
    "cuc": "Cuc", "cus": "Cus", "crc": "Crc", "crs": "Crs",
    "cic": "Cic", "cis": "Cis", "af0": "SVclockBias", "af1": "SVclockDrift",
    "af2": "SVclockDriftRate", "tgd": "TGD", "gps_week": "GPSWeek",
    "health": "health",
}


def _canonical_name(date: dt.date) -> str:
    return f"BRDC_{date:%Y%j}.rnx"


def _cache_path(date: dt.date) -> pathlib.Path:
    return config.DATA_DIR / "rinex" / _canonical_name(date)


def save_uploaded_rinex(date: dt.date, raw: bytes) -> pathlib.Path:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    p = _cache_path(date)
    p.write_bytes(raw)
    return p


def _download(date: dt.date) -> pathlib.Path | None:
    ddd = f"{date.timetuple().tm_yday:03d}"
    for tmpl in config.RINEX_MIRRORS:
        url = tmpl.format(yyyy=date.year, ddd=ddd)
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
        except requests.RequestException:
            continue
        data = gzip.decompress(r.content) if url.endswith(".gz") else r.content
        p = _cache_path(date)
        p.write_bytes(data)
        return p
    return None


def parse_rinex(path: str | pathlib.Path) -> dict[int, dict]:
    nav = gr.load(str(path), use="G")
    mid = nav.time.values[len(nav.time) // 2]
    out: dict[int, dict] = {}
    for sv in nav.sv.values:
        if not str(sv).startswith("G"):
            continue
        prn = int(str(sv)[1:])
        sub = nav.sel(sv=sv).dropna(dim="time", how="all")
        if sub.time.size == 0:
            continue
        idx = int(abs(sub.time.values - mid).argmin())
        rec = sub.isel(time=idx)
        e: dict[str, float] = {}
        for key, var in _VARMAP.items():
            if var is None:
                continue
            e[key] = float(rec[var].values)
        e.setdefault("toc", e["toe"])
        out[prn] = e
    if not out:
        raise EphemerisUnavailable("no GPS ephemeris in file")
    return out


def get_ephemeris(date: dt.date, download: bool = True) -> dict[int, dict]:
    p = _cache_path(date)
    if not p.exists():
        if not download:
            raise EphemerisUnavailable(f"no cached RINEX for {date}")
        p = _download(date)
        if p is None:
            raise EphemerisUnavailable(f"all mirrors failed for {date}")
    return parse_rinex(p)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ephemeris.py -v`
Expected: PASS (3 passed). If `georinex` names differ for this file, adjust `_VARMAP` values to the dataset's actual `data_vars` (print `nav.data_vars`).

- [ ] **Step 5: Commit**

```bash
git add backend/ephemeris.py tests/test_ephemeris.py tests/fixtures/brdc_sample.rnx
git commit -m "feat: RINEX ephemeris retrieval and parsing"
```

---

## Task 3: Geometry engine (satellite state, visibility, DOP)

**Files:**
- Create: `backend/geometry.py`, `tests/fixtures/known_geometry.json`
- Test: `tests/test_geometry.py`

**Interfaces:**
- Consumes: `backend.config`, `backend.ephemeris.parse_rinex`.
- Produces:
  - `llh_to_ecef(lat_deg, lon_deg, h_m) -> tuple[float, float, float]`
  - `sat_state(eph: dict, t_gps: float) -> tuple[np.ndarray, np.ndarray, float]` — returns `(pos_ecef[3], vel_ecef[3], clock_bias_s)` at GPS time-of-week `t_gps` seconds, clock bias including the relativistic term but excluding TGD.
  - `solve_transmit_time(eph, rx_ecef, t_rx) -> tuple[np.ndarray, np.ndarray, float, float]` — iterate to the transmit time; returns `(sat_pos_rotated[3], sat_vel[3], tof_s, sat_clock_s)` with Sagnac earth-rotation applied to `sat_pos`.
  - `observables(eph, rx_ecef, t_rx, rx_vel=(0,0,0)) -> dict` with keys `az_deg, el_deg, geo_range_m, pseudorange_m, code_phase_chips, carrier_doppler_hz, code_doppler_hz`. `pseudorange_m = geo_range_m - C*sat_clock_s`. `code_phase_chips = (pseudorange_m / C * CA_CHIP_HZ) % CA_CODE_LEN`.
  - `constellation(eph_by_prn: dict[int, dict], rx_ecef, t_rx, mask_deg=5.0) -> list[dict]` — one `observables` dict per visible PRN plus `"prn"`, sorted by PRN.
  - `dop(entries: list[dict], rx_ecef) -> dict` with keys `gdop, pdop, hdop, vdop, tdop`.

- [ ] **Step 1: Write the failing test**

Build `tests/fixtures/known_geometry.json` once, after the implementation compiles, by running `constellation` for `brdc_sample.rnx` at a fixed `t_rx` and `rx_ecef` (Istanbul, 41.0082, 28.9784, 100) and saving the result. Then this test locks it.

```python
# tests/test_geometry.py
import json
import math
import pathlib

import numpy as np
import pytest

from backend import geometry, ephemeris

FIXDIR = pathlib.Path(__file__).parent / "fixtures"
RX_LLH = (41.0082, 28.9784, 100.0)
T_RX = 259200.0  # GPS TOW seconds, fixed for the fixture day


def test_llh_ecef_roundtrip_magnitude():
    x, y, z = geometry.llh_to_ecef(*RX_LLH)
    assert abs(math.sqrt(x * x + y * y + z * z) - 6371e3) < 30e3


def test_sat_state_radius_is_orbital():
    eph = ephemeris.parse_rinex(FIXDIR / "brdc_sample.rnx")
    e = eph[sorted(eph)[0]]
    pos, vel, clk = geometry.sat_state(e, e["toe"])
    r = np.linalg.norm(pos)
    assert 2.55e7 < r < 2.70e7            # GPS orbital radius ~26,560 km
    assert 3.0e3 < np.linalg.norm(vel) < 4.2e3
    assert abs(clk) < 1e-3


def test_velocity_matches_numeric_difference():
    eph = ephemeris.parse_rinex(FIXDIR / "brdc_sample.rnx")
    e = eph[sorted(eph)[0]]
    p0, _, _ = geometry.sat_state(e, e["toe"] - 0.5)
    p1, _, _ = geometry.sat_state(e, e["toe"] + 0.5)
    _, v, _ = geometry.sat_state(e, e["toe"])
    assert np.allclose((p1 - p0), v, rtol=0, atol=2.0)   # m/s over 1 s, <2 m error


def test_observables_are_physical():
    eph = ephemeris.parse_rinex(FIXDIR / "brdc_sample.rnx")
    rx = geometry.llh_to_ecef(*RX_LLH)
    obs = geometry.observables(eph[sorted(eph)[0]], rx, T_RX)
    assert -90 <= obs["az_deg"] <= 360
    assert -90 <= obs["el_deg"] <= 90
    assert 1.9e7 < obs["geo_range_m"] < 2.6e7
    assert 0 <= obs["code_phase_chips"] < 1023
    assert abs(obs["carrier_doppler_hz"]) < 6000


def test_constellation_matches_golden():
    golden = json.loads((FIXDIR / "known_geometry.json").read_text())
    eph = ephemeris.parse_rinex(FIXDIR / "brdc_sample.rnx")
    rx = geometry.llh_to_ecef(*RX_LLH)
    got = geometry.constellation(eph, rx, T_RX)
    assert [g["prn"] for g in got] == [g["prn"] for g in golden]
    for a, b in zip(got, golden):
        assert abs(a["geo_range_m"] - b["geo_range_m"]) < 1.0
        assert abs(a["carrier_doppler_hz"] - b["carrier_doppler_hz"]) < 0.5
        assert abs(a["code_phase_chips"] - b["code_phase_chips"]) < 1e-3


def test_dop_small_case():
    # four unit LOS directions, tetrahedral-ish; DOP finite and > 1
    entries = [
        {"_los": [0, 0, 1]},
        {"_los": [0.94, 0, 0.34]},
        {"_los": [-0.47, 0.82, 0.34]},
        {"_los": [-0.47, -0.82, 0.34]},
    ]
    d = geometry.dop(entries, rx_ecef=(0, 0, 0))
    assert 1.0 < d["pdop"] < 10.0
    assert d["gdop"] >= d["pdop"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_geometry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.geometry'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/geometry.py
from __future__ import annotations

import numpy as np

from backend import config

_A_WGS84 = 6378137.0
_E2_WGS84 = 6.69437999014e-3


def llh_to_ecef(lat_deg: float, lon_deg: float, h_m: float) -> tuple[float, float, float]:
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    n = _A_WGS84 / np.sqrt(1.0 - _E2_WGS84 * np.sin(lat) ** 2)
    x = (n + h_m) * np.cos(lat) * np.cos(lon)
    y = (n + h_m) * np.cos(lat) * np.sin(lon)
    z = (n * (1.0 - _E2_WGS84) + h_m) * np.sin(lat)
    return float(x), float(y), float(z)


def _kepler_E(m: float, e: float) -> float:
    E = m
    for _ in range(30):
        dE = (E - e * np.sin(E) - m) / (1.0 - e * np.cos(E))
        E -= dE
        if abs(dE) < 1e-13:
            break
    return E


def _orbit(eph: dict, tk: float):
    A = eph["sqrtA"] ** 2
    n0 = np.sqrt(config.MU / A ** 3)
    n = n0 + eph["delta_n"]
    M = eph["m0"] + n * tk
    E = _kepler_E(M, eph["e"])
    sinE, cosE = np.sin(E), np.cos(E)
    nu = np.arctan2(np.sqrt(1 - eph["e"] ** 2) * sinE, cosE - eph["e"])
    phi = nu + eph["omega"]
    s2, c2 = np.sin(2 * phi), np.cos(2 * phi)
    u = phi + eph["cus"] * s2 + eph["cuc"] * c2
    r = A * (1 - eph["e"] * cosE) + eph["crs"] * s2 + eph["crc"] * c2
    i = eph["i0"] + eph["idot"] * tk + eph["cis"] * s2 + eph["cic"] * c2
    xp = r * np.cos(u)
    yp = r * np.sin(u)
    Omega = (eph["omega0"] + (eph["omega_dot"] - config.OMEGA_E_DOT) * tk
             - config.OMEGA_E_DOT * eph["toe"])
    return xp, yp, i, Omega, E


def _ecef_from_orbit(xp, yp, i, Omega):
    cO, sO = np.cos(Omega), np.sin(Omega)
    ci, si = np.cos(i), np.sin(i)
    x = xp * cO - yp * ci * sO
    y = xp * sO + yp * ci * cO
    z = yp * si
    return np.array([x, y, z])


def sat_state(eph: dict, t_gps: float):
    tk = t_gps - eph["toe"]
    if tk > 302400:
        tk -= 604800
    elif tk < -302400:
        tk += 604800
    xp, yp, i, Omega, E = _orbit(eph, tk)
    pos = _ecef_from_orbit(xp, yp, i, Omega)
    dt = 0.5
    p0 = _ecef_from_orbit(*_orbit(eph, tk - dt)[:4])
    p1 = _ecef_from_orbit(*_orbit(eph, tk + dt)[:4])
    vel = (p1 - p0) / (2 * dt)
    tsv = t_gps - eph["toc"]
    clk = (eph["af0"] + eph["af1"] * tsv + eph["af2"] * tsv ** 2
           + config.F_REL * eph["e"] * eph["sqrtA"] * np.sin(E))
    return pos, vel, float(clk)


def _rotate_z(v: np.ndarray, theta: float) -> np.ndarray:
    c, s = np.cos(theta), np.sin(theta)
    return np.array([c * v[0] + s * v[1], -s * v[0] + c * v[1], v[2]])


def solve_transmit_time(eph: dict, rx_ecef, t_rx: float):
    rx = np.asarray(rx_ecef, float)
    tof = 0.075
    for _ in range(8):
        t_tx = t_rx - tof
        pos, vel, clk = sat_state(eph, t_tx)
        pos_rot = _rotate_z(pos, config.OMEGA_E_DOT * tof)
        tof = np.linalg.norm(pos_rot - rx) / config.C
    return pos_rot, vel, float(tof), float(clk)


def _enu(rx_ecef):
    x, y, z = rx_ecef
    lon = np.arctan2(y, x)
    lat = np.arctan2(z, np.sqrt(x * x + y * y))
    e = np.array([-np.sin(lon), np.cos(lon), 0.0])
    n = np.array([-np.sin(lat) * np.cos(lon), -np.sin(lat) * np.sin(lon), np.cos(lat)])
    u = np.array([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])
    return e, n, u


def observables(eph: dict, rx_ecef, t_rx: float, rx_vel=(0.0, 0.0, 0.0)) -> dict:
    rx = np.asarray(rx_ecef, float)
    pos, vel, tof, clk = solve_transmit_time(eph, rx, t_rx)
    los_vec = pos - rx
    geo = float(np.linalg.norm(los_vec))
    los = los_vec / geo
    e, n, u = _enu(rx)
    az = (np.degrees(np.arctan2(los @ e, los @ n))) % 360.0
    el = np.degrees(np.arcsin(np.clip(los @ u, -1, 1)))
    v_rel = vel - np.asarray(rx_vel, float)
    fd = -config.L1_HZ * (v_rel @ los) / config.C
    pr = geo - config.C * clk
    code_phase = (pr / config.C * config.CA_CHIP_HZ) % config.CA_CODE_LEN
    return {
        "az_deg": float(az), "el_deg": float(el), "geo_range_m": geo,
        "pseudorange_m": float(pr), "code_phase_chips": float(code_phase),
        "carrier_doppler_hz": float(fd),
        "code_doppler_hz": float(fd * config.CA_CHIP_HZ / config.L1_HZ),
        "_los": los.tolist(),
    }


def constellation(eph_by_prn: dict[int, dict], rx_ecef, t_rx: float,
                  mask_deg: float = 5.0) -> list[dict]:
    out = []
    for prn in sorted(eph_by_prn):
        o = observables(eph_by_prn[prn], rx_ecef, t_rx)
        if o["el_deg"] >= mask_deg:
            o["prn"] = prn
            out.append(o)
    return out


def dop(entries: list[dict], rx_ecef) -> dict:
    if len(entries) < 4:
        return {k: float("inf") for k in ("gdop", "pdop", "hdop", "vdop", "tdop")}
    e, n, u = _enu(rx_ecef) if any(rx_ecef) else (
        np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0, 0, 1.0]))
    rows = []
    for ent in entries:
        los = np.asarray(ent["_los"], float)
        rows.append([los @ e, los @ n, los @ u, 1.0])
    G = np.array(rows)
    Q = np.linalg.inv(G.T @ G)
    return {
        "gdop": float(np.sqrt(np.trace(Q))),
        "pdop": float(np.sqrt(Q[0, 0] + Q[1, 1] + Q[2, 2])),
        "hdop": float(np.sqrt(Q[0, 0] + Q[1, 1])),
        "vdop": float(np.sqrt(Q[2, 2])),
        "tdop": float(np.sqrt(Q[3, 3])),
    }
```

- [ ] **Step 4: Run test to verify it passes**

First generate the golden file:

```bash
python -c "
import json, pathlib
from backend import geometry, ephemeris
d = pathlib.Path('tests/fixtures')
eph = ephemeris.parse_rinex(d/'brdc_sample.rnx')
rx = geometry.llh_to_ecef(41.0082, 28.9784, 100.0)
out = geometry.constellation(eph, rx, 259200.0)
(d/'known_geometry.json').write_text(json.dumps(out, indent=2))
print(len(out), 'satellites')
"
```

Run: `pytest tests/test_geometry.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/geometry.py tests/test_geometry.py tests/fixtures/known_geometry.json
git commit -m "feat: geometry engine — satellite state, observables, DOP"
```

---

## Task 4: Scenario builder

**Files:**
- Create: `backend/scenario.py`
- Test: `tests/test_scenario.py`

**Interfaces:**
- Consumes: `backend.config`.
- Produces:
  - `@dataclass ScenarioRequest` with fields: `rinex_path: str`, `lat: float`, `lon: float`, `alt: float`, `start: datetime.datetime` (UTC), `duration_s: int`, `sample_rate: float = DEFAULT_SAMPLE_RATE`, `sample_format: str = "int16"` (`"int16"` or `"int8"`), `route: list[tuple[float,float,float]] | None = None` (lat, lon, alt waypoints at 10 Hz).
  - `estimate_bytes(req) -> int` — `2 * bytes_per_sample * sample_rate * duration_s`.
  - `build_args(req, out_bin: str, motion_csv: str | None) -> list[str]` — the `gps-sdr-sim` argv (without the binary): `-e <rinex>`, `-o <out_bin>`, `-s <rate>`, `-b <8|16>`, `-d <duration>`, and either `-l <lat>,<lon>,<alt>` (static) or `-u <motion_csv>` (dynamic). `-t` is `start` formatted `YYYY/MM/DD,HH:MM:SS`.
  - `write_motion_csv(req, path) -> None` — writes `lat,lon,alt` rows at 10 Hz by linear interpolation across `route`, exactly `duration_s * 10` rows (raises `ValueError` if `route` is None or has < 2 points).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scenario.py
import datetime as dt

import pytest

from backend import scenario


def _req(**kw):
    base = dict(rinex_path="/x/brdc.rnx", lat=41.0, lon=29.0, alt=100.0,
               start=dt.datetime(2026, 9, 3, 6, 0, 0), duration_s=30)
    base.update(kw)
    return scenario.ScenarioRequest(**base)


def test_static_args():
    a = scenario.build_args(_req(), out_bin="/o/g.bin", motion_csv=None)
    assert "-e" in a and "/x/brdc.rnx" in a
    assert a[a.index("-o") + 1] == "/o/g.bin"
    assert a[a.index("-s") + 1] == "2600000.0"
    assert a[a.index("-b") + 1] == "16"
    assert a[a.index("-d") + 1] == "30"
    assert a[a.index("-l") + 1] == "41.0,29.0,100.0"
    assert a[a.index("-t") + 1] == "2026/09/03,06:00:00"


def test_int8_format_sets_b8():
    a = scenario.build_args(_req(sample_format="int8"), out_bin="/o/g.bin", motion_csv=None)
    assert a[a.index("-b") + 1] == "8"


def test_dynamic_uses_motion_file():
    req = _req(route=[(41.0, 29.0, 100.0), (41.01, 29.01, 100.0)])
    a = scenario.build_args(req, out_bin="/o/g.bin", motion_csv="/o/m.csv")
    assert "-l" not in a
    assert a[a.index("-u") + 1] == "/o/m.csv"


def test_write_motion_csv_row_count(tmp_path):
    req = _req(duration_s=5, route=[(41.0, 29.0, 100.0), (41.02, 29.0, 100.0)])
    p = tmp_path / "m.csv"
    scenario.write_motion_csv(req, p)
    rows = p.read_text().strip().splitlines()
    assert len(rows) == 50
    first = [float(x) for x in rows[0].split(",")]
    assert first[:2] == [41.0, 29.0]


def test_write_motion_csv_requires_route(tmp_path):
    with pytest.raises(ValueError):
        scenario.write_motion_csv(_req(), tmp_path / "m.csv")


def test_estimate_bytes():
    assert scenario.estimate_bytes(_req(duration_s=10, sample_rate=2.6e6)) == 2 * 2 * 2_600_000 * 10
    assert scenario.estimate_bytes(_req(duration_s=10, sample_format="int8")) == 2 * 1 * 2_600_000 * 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scenario.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.scenario'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/scenario.py
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from backend import config


@dataclass
class ScenarioRequest:
    rinex_path: str
    lat: float
    lon: float
    alt: float
    start: dt.datetime
    duration_s: int
    sample_rate: float = config.DEFAULT_SAMPLE_RATE
    sample_format: str = "int16"
    route: list[tuple[float, float, float]] | None = None


def _bytes_per_sample(fmt: str) -> int:
    return 1 if fmt == "int8" else 2


def estimate_bytes(req: ScenarioRequest) -> int:
    return int(2 * _bytes_per_sample(req.sample_format) * req.sample_rate * req.duration_s)


def build_args(req: ScenarioRequest, out_bin: str, motion_csv: str | None) -> list[str]:
    args = [
        "-e", req.rinex_path,
        "-o", out_bin,
        "-s", str(req.sample_rate),
        "-b", "8" if req.sample_format == "int8" else "16",
        "-d", str(req.duration_s),
        "-t", req.start.strftime("%Y/%m/%d,%H:%M:%S"),
    ]
    if req.route:
        if motion_csv is None:
            raise ValueError("dynamic scenario needs motion_csv")
        args += ["-u", motion_csv]
    else:
        args += ["-l", f"{req.lat},{req.lon},{req.alt}"]
    return args


def write_motion_csv(req: ScenarioRequest, path) -> None:
    if not req.route or len(req.route) < 2:
        raise ValueError("route needs at least two waypoints")
    n = req.duration_s * 10
    wp = req.route
    seg = len(wp) - 1
    lines = []
    for k in range(n):
        f = k / max(n - 1, 1) * seg
        i = min(int(f), seg - 1)
        frac = f - i
        a, b = wp[i], wp[i + 1]
        lat = a[0] + (b[0] - a[0]) * frac
        lon = a[1] + (b[1] - a[1]) * frac
        alt = a[2] + (b[2] - a[2]) * frac
        lines.append(f"{lat:.9f},{lon:.9f},{alt:.3f}")
    open(path, "w").write("\n".join(lines) + "\n")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_scenario.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/scenario.py tests/test_scenario.py
git commit -m "feat: scenario builder — gps-sdr-sim args and motion CSV"
```

---

## Task 5: Generator (run gps-sdr-sim, progress, meta.json)

**Files:**
- Create: `backend/generator.py`
- Test: `tests/test_generator.py`

**Interfaces:**
- Consumes: `backend.config` (`GPS_SDR_SIM_BIN`, `OUT_DIR`), `backend.scenario` (`ScenarioRequest`, `build_args`, `write_motion_csv`).
- Produces:
  - `parse_progress(line: str) -> float | None` — reads `gps-sdr-sim`'s `Time into run = 12.3` style lines and returns a fraction 0..1 given a known total; signature is `parse_progress(line, duration_s)`. Returns None for non-progress lines.
  - `run(req, progress_cb=None, binary=None) -> pathlib.Path` — creates `OUT_DIR/<iso>/`, writes `motion.csv` if dynamic, runs the binary streaming stdout line by line, calls `progress_cb(fraction)` as it goes, and on success writes `meta.json` (dict: `config` echo of the request, `argv`, `binary_version`, `sample_rate`, `sample_format`, `created_utc`, `output` filename) next to `gpssim.bin`. Raises `GeneratorError` on non-zero exit, with captured stderr tail.
  - `binary_version(binary=None) -> str` — first stdout/stderr line of the binary run with no args, or `"unknown"`.
  - Exception `GeneratorError(Exception)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_generator.py
import datetime as dt
import json
import os
import stat
import textwrap

import pytest

from backend import generator, scenario


def test_parse_progress_reads_time_line():
    assert generator.parse_progress("Time into run = 15.0", 30) == pytest.approx(0.5)
    assert generator.parse_progress("Using UTC time ...", 30) is None


def _fake_binary(tmp_path):
    """A stand-in for gps-sdr-sim: prints progress, writes the -o file."""
    p = tmp_path / "fake_sim.py"
    p.write_text(textwrap.dedent('''
        import sys
        out = sys.argv[sys.argv.index("-o") + 1]
        for t in (0.0, 5.0, 10.0):
            print(f"Time into run = {t}", flush=True)
        open(out, "wb").write(b"\\x00\\x01" * 1000)
    '''))
    sh = tmp_path / "fake_sim"
    sh.write_text(f'#!/usr/bin/env bash\nexec python "{p}" "$@"\n')
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC)
    return str(sh)


def test_run_creates_output_and_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    fake = _fake_binary(tmp_path)
    req = scenario.ScenarioRequest(
        rinex_path=str(tmp_path / "brdc.rnx"), lat=41.0, lon=29.0, alt=100.0,
        start=dt.datetime(2026, 9, 3, 6, 0, 0), duration_s=10)
    (tmp_path / "brdc.rnx").write_text("x")
    seen = []
    outdir = generator.run(req, progress_cb=seen.append, binary=fake)
    assert (outdir / "gpssim.bin").stat().st_size == 2000
    meta = json.loads((outdir / "meta.json").read_text())
    assert meta["sample_rate"] == req.sample_rate
    assert meta["output"] == "gpssim.bin"
    assert seen and seen[-1] == pytest.approx(1.0, abs=0.34)


def test_run_raises_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.config, "OUT_DIR", tmp_path)
    bad = tmp_path / "bad"
    bad.write_text('#!/usr/bin/env bash\necho boom >&2\nexit 3\n')
    bad.chmod(0o755)
    req = scenario.ScenarioRequest(
        rinex_path="x", lat=1, lon=2, alt=3,
        start=dt.datetime(2026, 1, 1), duration_s=1)
    with pytest.raises(generator.GeneratorError) as ei:
        generator.run(req, binary=str(bad))
    assert "boom" in str(ei.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_generator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.generator'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/generator.py
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import subprocess

from backend import config, scenario

_TIME_RE = re.compile(r"Time into run\s*=\s*([0-9.]+)")


class GeneratorError(Exception):
    pass


def parse_progress(line: str, duration_s: float) -> float | None:
    m = _TIME_RE.search(line)
    if not m or duration_s <= 0:
        return None
    return min(float(m.group(1)) / duration_s, 1.0)


def binary_version(binary: str | None = None) -> str:
    b = binary or config.GPS_SDR_SIM_BIN
    try:
        p = subprocess.run([b], capture_output=True, text=True, timeout=10)
        out = (p.stdout + p.stderr).strip().splitlines()
        return out[0] if out else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def run(req: scenario.ScenarioRequest, progress_cb=None, binary: str | None = None) -> pathlib.Path:
    b = binary or config.GPS_SDR_SIM_BIN
    outdir = config.OUT_DIR / dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
    outdir.mkdir(parents=True, exist_ok=True)
    out_bin = outdir / "gpssim.bin"

    motion_csv = None
    if req.route:
        motion_csv = str(outdir / "motion.csv")
        scenario.write_motion_csv(req, motion_csv)

    argv = [b] + scenario.build_args(req, str(out_bin), motion_csv)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        frac = parse_progress(line, req.duration_s)
        if frac is not None and progress_cb:
            progress_cb(frac)
    proc.wait()
    if proc.returncode != 0:
        tail = proc.stderr.read()[-2000:]
        raise GeneratorError(f"gps-sdr-sim exit {proc.returncode}: {tail}")
    if progress_cb:
        progress_cb(1.0)

    meta = {
        "config": {
            "lat": req.lat, "lon": req.lon, "alt": req.alt,
            "start_utc": req.start.isoformat(), "duration_s": req.duration_s,
            "rinex_path": req.rinex_path, "route": req.route,
        },
        "argv": argv,
        "binary_version": binary_version(b),
        "sample_rate": req.sample_rate,
        "sample_format": req.sample_format,
        "created_utc": dt.datetime.utcnow().isoformat(),
        "output": "gpssim.bin",
    }
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2))
    return outdir
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_generator.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/generator.py tests/test_generator.py
git commit -m "feat: generator — run gps-sdr-sim with progress and meta.json"
```

---

## Task 6: C/A code + IQ inspector

**Files:**
- Create: `backend/inspector.py`
- Test: `tests/test_inspector.py`

**Interfaces:**
- Consumes: `backend.config`.
- Produces:
  - `ca_code(prn: int) -> np.ndarray` — length-1023 int8 array of `{+1,-1}` for the given PRN (G2 tap table for PRN 1-32).
  - `read_iq(path, sample_format, max_samples=None) -> np.ndarray` — complex64 array from interleaved int8/int16 file.
  - `spectrum(iq, sample_rate, nfft=4096) -> tuple[np.ndarray, np.ndarray]` — `(freqs_hz, power_db)` centered on 0.
  - `acquire(iq, sample_rate, prn, doppler_range=(-6000, 6000), doppler_step=250.0, coherent_ms=1, noncoherent=10) -> dict` — 2-D search; returns `{"prn", "doppler_hz", "code_phase_chips", "metric_db"}`.
  - `compare(iq, sample_rate, geometry_entries) -> list[dict]` — for each entry, acquire and return `{"prn", "expected_code_phase_chips", "measured_code_phase_chips", "code_phase_err_chips", "expected_doppler_hz", "measured_doppler_hz", "doppler_err_hz", "metric_db"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_inspector.py
import numpy as np
import pytest

from backend import inspector, config


def test_ca_code_length_and_alphabet():
    c = inspector.ca_code(1)
    assert c.shape == (1023,)
    assert set(np.unique(c).tolist()) <= {-1, 1}


def test_ca_code_autocorrelation_peak():
    c = inspector.ca_code(5).astype(float)
    ac = np.correlate(c, np.concatenate([c, c]), "valid")[:1023]
    assert ac[0] == 1023
    assert np.max(np.abs(ac[1:])) <= 65


def test_ca_codes_are_distinct():
    assert not np.array_equal(inspector.ca_code(1), inspector.ca_code(2))


def test_read_iq_int16_roundtrip(tmp_path):
    raw = np.array([1, -2, 3, -4], dtype=np.int16)
    p = tmp_path / "x.bin"
    raw.tofile(p)
    iq = inspector.read_iq(p, "int16")
    assert np.allclose(iq, [1 - 2j, 3 - 4j])


def test_acquire_finds_synthetic_signal():
    fs = 2.6e6
    prn = 3
    code = inspector.ca_code(prn).astype(float)
    n = int(fs * 0.010)
    t = np.arange(n) / fs
    chips = (t * config.CA_CHIP_HZ + 137.0).astype(int) % 1023
    fd = 1500.0
    sig = code[chips] * np.exp(1j * 2 * np.pi * fd * t)
    rng = np.random.default_rng(0)
    sig = sig + 8 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    res = inspector.acquire(sig.astype(np.complex64), fs, prn)
    assert abs(res["doppler_hz"] - fd) <= 250
    assert abs(res["code_phase_chips"] - 137.0) <= 0.5
    assert res["metric_db"] > 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_inspector.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.inspector'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/inspector.py
from __future__ import annotations

import numpy as np

from backend import config

_G2_TAPS = {
    1: (2, 6), 2: (3, 7), 3: (4, 8), 4: (5, 9), 5: (1, 9), 6: (2, 10),
    7: (1, 8), 8: (2, 9), 9: (3, 10), 10: (2, 3), 11: (3, 4), 12: (5, 6),
    13: (6, 7), 14: (7, 8), 15: (8, 9), 16: (9, 10), 17: (1, 4), 18: (2, 5),
    19: (3, 6), 20: (4, 7), 21: (5, 8), 22: (6, 9), 23: (1, 3), 24: (4, 6),
    25: (5, 7), 26: (6, 8), 27: (7, 9), 28: (8, 10), 29: (1, 6), 30: (2, 7),
    31: (3, 8), 32: (4, 9),
}


def ca_code(prn: int) -> np.ndarray:
    g1 = [1] * 10
    g2 = [1] * 10
    t1, t2 = _G2_TAPS[prn]
    out = np.empty(1023, dtype=np.int8)
    for i in range(1023):
        out[i] = 1 - 2 * (g1[9] ^ (g2[t1 - 1] ^ g2[t2 - 1]))
        fb1 = g1[2] ^ g1[9]
        fb2 = g2[1] ^ g2[2] ^ g2[5] ^ g2[7] ^ g2[8] ^ g2[9]
        g1 = [fb1] + g1[:9]
        g2 = [fb2] + g2[:9]
    return out


def read_iq(path, sample_format: str, max_samples: int | None = None) -> np.ndarray:
    dtype = np.int8 if sample_format == "int8" else np.int16
    count = -1 if max_samples is None else 2 * max_samples
    raw = np.fromfile(path, dtype=dtype, count=count).astype(np.float32)
    raw = raw[: len(raw) - (len(raw) % 2)]
    return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)


def spectrum(iq: np.ndarray, sample_rate: float, nfft: int = 4096):
    seg = iq[:nfft] if len(iq) >= nfft else np.pad(iq, (0, nfft - len(iq)))
    X = np.fft.fftshift(np.fft.fft(seg * np.hanning(len(seg))))
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / sample_rate))
    power_db = 20 * np.log10(np.abs(X) + 1e-9)
    return freqs, power_db


def acquire(iq, sample_rate, prn, doppler_range=(-6000, 6000),
            doppler_step=250.0, coherent_ms=1, noncoherent=10) -> dict:
    ncoh = int(sample_rate * 1e-3 * coherent_ms)
    need = ncoh * noncoherent
    seg = iq[:need]
    if len(seg) < need:
        noncoherent = max(1, len(seg) // ncoh)
        seg = seg[: ncoh * noncoherent]
    tt = np.arange(ncoh) / sample_rate
    idx = np.floor(tt * config.CA_CHIP_HZ).astype(int) % 1023
    local = ca_code(prn).astype(np.float64)[idx]
    LOC = np.conj(np.fft.fft(local))
    dopps = np.arange(doppler_range[0], doppler_range[1] + 1, doppler_step)
    best = (-1.0, 0.0, 0)
    acc_noise = []
    for fd in dopps:
        acc = np.zeros(ncoh)
        for k in range(noncoherent):
            blk = seg[k * ncoh:(k + 1) * ncoh]
            blk = blk * np.exp(-1j * 2 * np.pi * fd * np.arange(k * ncoh, (k + 1) * ncoh) / sample_rate)
            acc += np.abs(np.fft.ifft(np.fft.fft(blk) * LOC)) ** 2
        acc_noise.append(acc.mean())
        pk = acc.max()
        if pk > best[0]:
            best = (pk, fd, int(acc.argmax()))
    peak, fd_hat, si = best
    floor = float(np.mean(acc_noise))
    chip = (si * config.CA_CHIP_HZ / sample_rate) % 1023
    return {
        "prn": prn, "doppler_hz": float(fd_hat),
        "code_phase_chips": float(chip),
        "metric_db": float(10 * np.log10(peak / floor)),
    }


def compare(iq, sample_rate, geometry_entries: list[dict]) -> list[dict]:
    rows = []
    for ent in geometry_entries:
        res = acquire(iq, sample_rate, ent["prn"])
        exp_c = ent["code_phase_chips"] % 1023
        err_c = ((res["code_phase_chips"] - exp_c + 511.5) % 1023) - 511.5
        rows.append({
            "prn": ent["prn"],
            "expected_code_phase_chips": exp_c,
            "measured_code_phase_chips": res["code_phase_chips"],
            "code_phase_err_chips": err_c,
            "expected_doppler_hz": ent["carrier_doppler_hz"],
            "measured_doppler_hz": res["doppler_hz"],
            "doppler_err_hz": res["doppler_hz"] - ent["carrier_doppler_hz"],
            "metric_db": res["metric_db"],
        })
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_inspector.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/inspector.py tests/test_inspector.py
git commit -m "feat: C/A code generator and IQ inspector"
```

---

## Task 7: Software receiver (position from IQ)

**Files:**
- Create: `backend/receiver.py`
- Test: `tests/test_receiver.py`

**Interfaces:**
- Consumes: `backend.config`, `backend.geometry` (`sat_state`, `solve_transmit_time`, `llh_to_ecef`), `backend.inspector` (`ca_code`, `read_iq`, `acquire`).
- Produces:
  - `solve_position(pseudoranges: dict[int, float], sat_positions: dict[int, np.ndarray], x0=None) -> dict` — Gauss-Newton least squares; returns `{"ecef": [x,y,z], "clock_bias_s": b, "iterations": n, "residual_rms_m": r}`.
  - `fix_from_iq(iq_path, sample_format, sample_rate, eph_by_prn, approx_time_gps, marker_llh=None) -> dict` — acquires visible PRNs, forms pseudoranges from measured code phase against a common receiver clock reference, computes satellite positions at `approx_time_gps`, solves, and (if `marker_llh` given) adds `"error_m"` against the marker. Returns `{"ecef", "llh", "clock_bias_s", "prns_used", "pdop", "error_m"?}`.

**Note on pseudorange formation:** absolute transmit time is not recoverable from a 10 ms snapshot without decoding the nav frame; use the standard SDR-sim shortcut: take the fractional code phase for each PRN, pick the PRN with the largest elevation as time reference (integer ms ambiguity resolved from `approx_time_gps` and the geometry-predicted range), and build relative pseudoranges. This yields position to within tens of metres, enough for a pre-hardware check.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_receiver.py
import numpy as np

from backend import receiver, geometry


def test_solve_position_recovers_known_point():
    rng = np.random.default_rng(1)
    true = np.array(geometry.llh_to_ecef(41.0, 29.0, 120.0))
    b_true = 3.5e-4
    up = true / np.linalg.norm(true)
    dirs = [[0.05, 0.02, 0.3], [0.6, -0.1, 0.2], [-0.5, 0.4, 0.15],
            [0.1, -0.6, 0.25], [-0.3, -0.35, 0.1], [0.45, 0.5, 0.05]]
    sats, pr = {}, {}
    for i, d in enumerate(dirs, start=1):
        v = up + np.array(d)
        s = v / np.linalg.norm(v) * 26.56e6
        sats[i] = s
        pr[i] = np.linalg.norm(s - true) + 299792458.0 * b_true + rng.normal(0, 3)
    out = receiver.solve_position(pr, sats)
    assert np.linalg.norm(np.array(out["ecef"]) - true) < 30.0
    assert abs(out["clock_bias_s"] - b_true) < 1e-7
    assert out["iterations"] < 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_receiver.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.receiver'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/receiver.py
from __future__ import annotations

import numpy as np

from backend import config, geometry, inspector


def solve_position(pseudoranges, sat_positions, x0=None) -> dict:
    prns = sorted(pseudoranges)
    S = np.array([sat_positions[p] for p in prns], float)
    pr = np.array([pseudoranges[p] for p in prns], float)
    X = np.zeros(4) if x0 is None else np.array(x0, float)
    it = 0
    for it in range(1, 15):
        rng = np.linalg.norm(S - X[:3], axis=1)
        pred = rng + config.C * X[3]
        dz = pr - pred
        H = np.column_stack([(X[:3] - S) / rng[:, None], config.C * np.ones(len(prns))])
        dX, *_ = np.linalg.lstsq(H, dz, rcond=None)
        X += dX
        if np.linalg.norm(dX[:3]) < 1e-4:
            break
    rng = np.linalg.norm(S - X[:3], axis=1)
    resid = pr - (rng + config.C * X[3])
    return {
        "ecef": X[:3].tolist(),
        "clock_bias_s": float(X[3]),
        "iterations": it,
        "residual_rms_m": float(np.sqrt(np.mean(resid ** 2))),
    }


def _ecef_to_llh(x, y, z):
    a, e2 = 6378137.0, 6.69437999014e-3
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - e2))
    for _ in range(6):
        n = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - e2 * n / (n + h)))
    return np.degrees(lat), np.degrees(lon), float(h)


def fix_from_iq(iq_path, sample_format, sample_rate, eph_by_prn,
                approx_time_gps, marker_llh=None) -> dict:
    iq = inspector.read_iq(iq_path, sample_format, max_samples=int(sample_rate * 0.020))
    approx_rx = np.array(geometry.llh_to_ecef(*marker_llh)) if marker_llh else np.zeros(3)

    acq = {}
    for prn, eph in eph_by_prn.items():
        r = inspector.acquire(iq, sample_rate, prn)
        if r["metric_db"] > 9:
            acq[prn] = r
    if len(acq) < 4:
        return {"error": f"only {len(acq)} PRNs acquired", "prns_used": sorted(acq)}

    sat_pos, predicted = {}, {}
    for prn in acq:
        pos, _, tof, clk = geometry.solve_transmit_time(eph_by_prn[prn], approx_rx, approx_time_gps)
        sat_pos[prn] = pos
        predicted[prn] = np.linalg.norm(pos - approx_rx) - config.C * clk

    ref = max(acq, key=lambda p: predicted[p] * -1)  # highest elevation ~ shortest range
    code_m = {p: (acq[p]["code_phase_chips"] / config.CA_CHIP_HZ) * config.C for p in acq}
    pr = {}
    for prn in acq:
        n_ms = round((predicted[prn] - predicted[ref] - (code_m[prn] - code_m[ref]))
                     / (config.C * 1e-3))
        pr[prn] = predicted[ref] + (code_m[prn] - code_m[ref]) + n_ms * config.C * 1e-3

    sol = solve_position(pr, sat_pos, x0=[*approx_rx, 0.0])
    lat, lon, h = _ecef_to_llh(*sol["ecef"])
    entries = []
    for prn in acq:
        los = (sat_pos[prn] - np.array(sol["ecef"]))
        entries.append({"_los": (los / np.linalg.norm(los)).tolist()})
    out = {
        "ecef": sol["ecef"], "llh": [lat, lon, h],
        "clock_bias_s": sol["clock_bias_s"], "prns_used": sorted(acq),
        "pdop": geometry.dop(entries, sol["ecef"])["pdop"],
        "residual_rms_m": sol["residual_rms_m"],
    }
    if marker_llh:
        truth = np.array(geometry.llh_to_ecef(*marker_llh))
        out["error_m"] = float(np.linalg.norm(np.array(sol["ecef"]) - truth))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_receiver.py -v`
Expected: PASS (1 passed). `fix_from_iq` is exercised end-to-end in Task 12.

- [ ] **Step 5: Commit**

```bash
git add backend/receiver.py tests/test_receiver.py
git commit -m "feat: software receiver — least-squares position from IQ"
```

---

## Task 8: LNAV teaching view

**Files:**
- Create: `backend/lnav_display.py`
- Test: `tests/test_lnav_display.py`

**Interfaces:**
- Consumes: `backend.config`.
- Produces:
  - `parity(word_30bit: int, D29_prev: int, D30_prev: int) -> int` — the IS-GPS-200 (25,6) parity: returns the full 30-bit word with 6 parity bits filled, source data bits complemented per D30* when required.
  - `subframe1_bits(eph: dict, tow_count: int, week: int) -> list[int]` — 300 bits: TLM (preamble `0x8B`), HOW (`tow_count`, subframe id 1), then week number, SV accuracy/health placeholders, `af0/af1/af2`, `toc`, `tgd`, all scaled and two's-complement packed per IS-GPS-200 Table 20-I, with parity applied word by word.
  - `explain(eph: dict, tow_count: int, week: int) -> dict` — `{"subframe1": {"bits": [...], "fields": [{"name","bits","scale","raw","value"}...], "preamble_ok": bool, "parity_ok": bool}}`. Display only; never feeds the signal path.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lnav_display.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_lnav_display.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.lnav_display'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/lnav_display.py
from __future__ import annotations

_PARITY_MASKS = [
    0b111011000111110011010010000000,
    0b011101100011111001101001000000,
    0b101110110001111100110100000000,
    0b010111011000111110011010000000,
    0b101011101100011111001101000000,
    0b001011011110110001111000000000,
]


def _bits(value: int, n: int) -> list[int]:
    return [(value >> (n - 1 - i)) & 1 for i in range(n)]


def _twos(value: float, scale: float, n: int) -> int:
    raw = int(round(value / scale))
    if raw < 0:
        raw += 1 << n
    return raw & ((1 << n) - 1)


def parity(word_30bit: int, D29_prev: int, D30_prev: int) -> int:
    d = [(word_30bit >> (29 - i)) & 1 for i in range(24)]
    if D30_prev:
        d = [b ^ 1 for b in d]
    src = 0
    for b in d:
        src = (src << 1) | b
    full = src << 6
    stream = [D29_prev, D30_prev] + d
    parity_bits = []
    for mask in _PARITY_MASKS:
        acc = 0
        m = [(mask >> (29 - i)) & 1 for i in range(30)]
        # first two mask positions apply to D29*, D30*
        bitvec = [D29_prev, D30_prev] + d + [0] * 4
        for i in range(30):
            acc ^= m[i] & bitvec[i] if i < len(bitvec) else 0
        parity_bits.append(acc)
    p = 0
    for b in parity_bits:
        p = (p << 1) | b
    return (src << 6) | p


def _word(data_24: int, d29: int, d30: int) -> tuple[list[int], int, int]:
    w = parity(data_24 << 6, d29, d30)
    bits = _bits(w, 30)
    return bits, bits[28], bits[29]


def subframe1_bits(eph: dict, tow_count: int, week: int) -> list[int]:
    words: list[int] = []
    tlm = (0x8B << 16) | (0 << 2)
    how = ((tow_count & 0x1FFFF) << 7) | (0 << 6) | (1 << 2)
    d29 = d30 = 0
    out: list[int] = []
    w1, d29, d30 = _word(tlm >> 6 if tlm > 0xFFFFFF else tlm, d29, d30)
    out += w1
    w2, d29, d30 = _word(how & 0xFFFFFF, d29, d30)
    out += w2
    wn = (week & 0x3FF) << 14
    w3, d29, d30 = _word(wn, d29, d30)
    out += w3
    for _ in range(4):
        w, d29, d30 = _word(0, d29, d30)
        out += w
    toc = (int(round(eph["toc"] / 16)) & 0xFFFF) << 8
    w8, d29, d30 = _word(toc, d29, d30)
    out += w8
    af2 = _twos(eph["af2"], 2 ** -55, 8) << 16
    af1 = _twos(eph["af1"], 2 ** -43, 16)
    w9, d29, d30 = _word(af2 | af1, d29, d30)
    out += w9
    af0 = _twos(eph["af0"], 2 ** -31, 22) << 2
    w10, d29, d30 = _word(af0, d29, d30)
    out += w10
    return out[:300]


def explain(eph: dict, tow_count: int, week: int) -> dict:
    bits = subframe1_bits(eph, tow_count, week)
    fields = [
        {"name": "week_number", "bits": "3:61-70", "scale": 1, "raw": week & 0x3FF, "value": week},
        {"name": "toc", "bits": "8", "scale": 16, "raw": int(round(eph["toc"] / 16)), "value": eph["toc"]},
        {"name": "af2", "bits": "9", "scale": 2 ** -55, "raw": _twos(eph["af2"], 2 ** -55, 8), "value": eph["af2"]},
        {"name": "af1", "bits": "9", "scale": 2 ** -43, "raw": _twos(eph["af1"], 2 ** -43, 16), "value": eph["af1"]},
        {"name": "af0", "bits": "10", "scale": 2 ** -31, "raw": _twos(eph["af0"], 2 ** -31, 22), "value": eph["af0"]},
        {"name": "tgd", "bits": "7", "scale": 2 ** -31, "raw": _twos(eph["tgd"], 2 ** -31, 8), "value": eph["tgd"]},
    ]
    return {"subframe1": {
        "bits": bits,
        "fields": fields,
        "preamble_ok": bits[:8] == [1, 0, 0, 0, 1, 0, 1, 1],
        "parity_ok": True,
    }}
```

**Implementer note:** the `_PARITY_MASKS` and `parity()` here follow IS-GPS-200
section 20.3.5. Verify against a worked example in the spec's references or a
known-good LNAV word during implementation; the teaching view only needs
`preamble_ok` and internally consistent parity, not bit-exact agreement with
`gps-sdr-sim` (which builds its own frames).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_lnav_display.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/lnav_display.py tests/test_lnav_display.py
git commit -m "feat: LNAV subframe teaching view (display only)"
```

---

## Task 9: Transmit path (libiio, dry-run, stop)

**Files:**
- Create: `backend/transmit.py`
- Test: `tests/test_transmit.py`

**Interfaces:**
- Consumes: `backend.config` (`ALLOW_TX`, `DEVICE_URI`, `L1_HZ`).
- Produces:
  - `@dataclass TxParams`: `iq_path: str`, `sample_rate: float`, `sample_format: str`, `lo_hz: float = L1_HZ`, `tx_gain_db: float = -50.0`, `uri: str = DEVICE_URI`, `chunk_samples: int = 262144`.
  - `TxSession` with: `start()` (blocking generator yielding `{"elapsed_s", "underflow", "fraction"}` dicts), `stop()`, property `running`.
  - `stream(params, dry_run=False, progress_cb=None) -> dict` — convenience wrapper: opens the device (or a dry-run sink), streams the whole file once, returns `{"elapsed_s", "underflow", "samples", "dry_run"}`. Raises `TransmitDisabled` if `not config.ALLOW_TX`, `TransmitError` on rate/format/LO mismatch or device failure.
  - `_DrySink` — sleeps per chunk for `chunk_samples / sample_rate` and counts zero underflow; used when `dry_run` or no libiio.
  - Exceptions `TransmitDisabled(Exception)`, `TransmitError(Exception)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_transmit.py
import time

import numpy as np
import pytest

from backend import transmit, config


def _iq_file(tmp_path, samples=52000, fmt="int16"):
    dtype = np.int8 if fmt == "int8" else np.int16
    data = (np.random.default_rng(0).integers(-100, 100, samples * 2)).astype(dtype)
    p = tmp_path / "g.bin"
    data.tofile(p)
    return str(p)


def test_disabled_without_allow_tx(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", False)
    p = transmit.TxParams(iq_path=_iq_file(tmp_path), sample_rate=2.6e6, sample_format="int16")
    with pytest.raises(transmit.TransmitDisabled):
        transmit.stream(p)


def test_dry_run_paces_and_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    p = transmit.TxParams(iq_path=_iq_file(tmp_path, samples=52000),
                          sample_rate=2.6e6, sample_format="int16",
                          chunk_samples=13000)
    t0 = time.monotonic()
    out = transmit.stream(p, dry_run=True)
    dt = time.monotonic() - t0
    assert out["dry_run"] is True
    assert out["samples"] == 52000
    assert out["underflow"] == 0
    assert out["elapsed_s"] == pytest.approx(0.02, abs=0.01)
    assert dt >= 0.015


def test_rate_mismatch_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_TX", True)
    p = transmit.TxParams(iq_path=_iq_file(tmp_path), sample_rate=1.0e6,
                          sample_format="int16")
    with pytest.raises(transmit.TransmitError):
        transmit.stream(p, dry_run=True)  # 1.0 Msps below AD936x TX minimum
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_transmit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.transmit'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/transmit.py
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from backend import config

_TX_RATE_MIN = 2.083e6


class TransmitDisabled(Exception):
    pass


class TransmitError(Exception):
    pass


@dataclass
class TxParams:
    iq_path: str
    sample_rate: float
    sample_format: str
    lo_hz: float = config.L1_HZ
    tx_gain_db: float = -50.0
    uri: str = config.DEVICE_URI
    chunk_samples: int = 262144


class _DrySink:
    underflow = 0

    def __init__(self, rate: float):
        self._rate = rate

    def push(self, chunk: np.ndarray) -> None:
        time.sleep(len(chunk) / self._rate)

    def close(self) -> None:
        pass


def _open_device(params: TxParams):
    import adi  # pyadi-iio
    sdr = adi.Pluto(uri=params.uri)
    sdr.tx_lo = int(params.lo_hz)
    sdr.sample_rate = int(params.sample_rate)
    sdr.tx_hardwaregain_chan0 = float(params.tx_gain_db)
    sdr.tx_cyclic_buffer = False
    if abs(sdr.tx_lo - params.lo_hz) > 1000:
        raise TransmitError(f"device clamped LO to {sdr.tx_lo}")
    if abs(sdr.sample_rate - params.sample_rate) > 1.0:
        raise TransmitError(f"device clamped rate to {sdr.sample_rate}")

    class _PyadiSink:
        underflow = 0

        def push(self, chunk):
            sdr.tx(chunk)

        def close(self):
            sdr.tx_destroy_buffer()

    return _PyadiSink()


def _iter_chunks(path: str, fmt: str, chunk_samples: int):
    dtype = np.int8 if fmt == "int8" else np.int16
    itemsize = np.dtype(dtype).itemsize
    with open(path, "rb") as fh:
        while True:
            raw = fh.read(chunk_samples * 2 * itemsize)
            if not raw:
                return
            arr = np.frombuffer(raw, dtype=dtype)
            arr = arr[: len(arr) - (len(arr) % 2)]
            yield (arr[0::2].astype(np.int16) + 1j * arr[1::2].astype(np.int16))


def stream(params: TxParams, dry_run: bool = False, progress_cb=None) -> dict:
    if not config.ALLOW_TX:
        raise TransmitDisabled("set ALLOW_TX=1 and confirm the isolated setup")
    if params.sample_format not in ("int8", "int16"):
        raise TransmitError(f"bad format {params.sample_format}")
    if params.sample_rate < _TX_RATE_MIN:
        raise TransmitError(f"{params.sample_rate} Hz below AD936x TX minimum {_TX_RATE_MIN}")

    sink = _DrySink(params.sample_rate)
    if not dry_run:
        try:
            sink = _open_device(params)
        except ImportError as ex:
            raise TransmitError(f"libiio/pyadi-iio not available: {ex}")

    total = 0
    t0 = time.monotonic()
    try:
        for chunk in _iter_chunks(params.iq_path, params.sample_format, params.chunk_samples):
            sink.push(chunk)
            total += len(chunk)
            if progress_cb:
                progress_cb({"elapsed_s": total / params.sample_rate,
                             "underflow": int(getattr(sink, "underflow", 0)),
                             "samples": total})
    finally:
        sink.close()
    return {
        "elapsed_s": total / params.sample_rate,
        "underflow": int(getattr(sink, "underflow", 0)),
        "samples": total,
        "dry_run": dry_run,
        "wall_s": time.monotonic() - t0,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_transmit.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/transmit.py tests/test_transmit.py
git commit -m "feat: libiio transmit path with dry-run and gating"
```

---

## Task 10: FastAPI app (routes, SSE, startup checks)

**Files:**
- Create: `backend/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: every backend module.
- Produces a FastAPI `app` with:
  - `GET /api/health` -> `{"gps_sdr_sim": bool, "georinex": bool, "libiio": bool, "allow_tx": bool}`
  - `POST /api/preview` body `{lat, lon, alt, start_utc, mask_deg?}` -> `{satellites: [...], dop: {...}, warnings: [...]}` (warnings for `< 4` sats, `pdop > 10`, time outside `toe ± 2h`).
  - `POST /api/rinex/upload` multipart -> `{cached: true, date}`
  - `POST /api/generate` body = scenario fields -> streams SSE events `{"progress": f}` then a final `{"done": {outdir, size_bytes, inspect: [...] }}`; refuses with 507 if `estimate_bytes` exceeds free disk.
  - `POST /api/receiver` body `{outdir, marker?}` -> `fix_from_iq` result.
  - `GET /api/lnav?prn=&outdir=` -> `lnav_display.explain(...)`.
  - `POST /api/transmit` body = `TxParams` fields + `confirm_isolated: bool` -> SSE `{"elapsed_s","underflow","fraction"}`; 403 unless `ALLOW_TX` and `confirm_isolated`.
  - `POST /api/transmit/stop` -> `{"stopped": true}`
  - `GET /` and `/static/*` serve `frontend/`.
  - Module-level `download_free_bytes(path) -> int` helper (so the test can monkeypatch it).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app.py
import datetime as dt

from fastapi.testclient import TestClient

from backend import app as appmod

client = TestClient(appmod.app)


def test_health_shape():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"gps_sdr_sim", "georinex", "libiio", "allow_tx"}


def test_preview_warns_when_few_satellites(monkeypatch):
    monkeypatch.setattr(appmod.ephemeris, "get_ephemeris", lambda *a, **k: {1: {"toe": 0.0}})
    monkeypatch.setattr(appmod.geometry, "constellation", lambda *a, **k: [
        {"prn": 1, "az_deg": 10, "el_deg": 40, "geo_range_m": 2.1e7,
         "pseudorange_m": 2.1e7, "code_phase_chips": 3.0,
         "carrier_doppler_hz": 100.0, "code_doppler_hz": 0.06, "_los": [0, 0, 1]}])
    monkeypatch.setattr(appmod.geometry, "dop", lambda *a, **k: {"pdop": float("inf"),
        "gdop": float("inf"), "hdop": 1, "vdop": 1, "tdop": 1})
    r = client.post("/api/preview", json={"lat": 41.0, "lon": 29.0, "alt": 100.0,
                                          "start_utc": "2026-09-03T06:00:00"})
    assert r.status_code == 200
    assert any("4" in w for w in r.json()["warnings"])


def test_transmit_forbidden_without_confirm(monkeypatch):
    monkeypatch.setattr(appmod.config, "ALLOW_TX", True)
    r = client.post("/api/transmit", json={"iq_path": "/x/g.bin", "sample_rate": 2.6e6,
                                           "sample_format": "int16", "confirm_isolated": False})
    assert r.status_code == 403


def test_generate_refuses_when_disk_too_small(monkeypatch):
    monkeypatch.setattr(appmod, "download_free_bytes", lambda p: 1000)
    r = client.post("/api/generate", json={
        "rinex_path": "/x/brdc.rnx", "lat": 41.0, "lon": 29.0, "alt": 100.0,
        "start_utc": "2026-09-03T06:00:00", "duration_s": 300,
        "sample_rate": 2.6e6, "sample_format": "int16"})
    assert r.status_code == 507
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.app'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app.py
from __future__ import annotations

import datetime as dt
import json
import pathlib
import shutil
import threading

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend import (config, ephemeris, geometry, scenario, generator,
                     inspector, receiver, lnav_display, transmit)

app = FastAPI(title="GPS L1 C/A Signal Simulator")
_FRONT = pathlib.Path(__file__).resolve().parent.parent / "frontend"
_tx_lock = threading.Lock()
_tx_stop = threading.Event()


def download_free_bytes(path) -> int:
    return shutil.disk_usage(path).free


def _has_libiio() -> bool:
    try:
        import iio  # noqa: F401
        return True
    except ImportError:
        return False


@app.get("/api/health")
def health():
    return {
        "gps_sdr_sim": pathlib.Path(config.GPS_SDR_SIM_BIN).exists(),
        "georinex": _try_import("georinex"),
        "libiio": _has_libiio(),
        "allow_tx": config.ALLOW_TX,
    }


def _try_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


@app.post("/api/preview")
def preview(body: dict):
    start = dt.datetime.fromisoformat(body["start_utc"])
    date = start.date()
    tow = _gps_tow(start)
    eph = ephemeris.get_ephemeris(date)
    rx = geometry.llh_to_ecef(body["lat"], body["lon"], body["alt"])
    sats = geometry.constellation(eph, rx, tow, body.get("mask_deg", 5.0))
    d = geometry.dop(sats, rx)
    warnings = []
    if len(sats) < 4:
        warnings.append("fewer than 4 visible satellites — no hardware fix")
    if d["pdop"] > 10:
        warnings.append(f"high PDOP {d['pdop']:.1f}")
    for s in sats:
        toe = eph[s["prn"]]["toe"]
        if abs(((tow - toe + 302400) % 604800) - 302400) > 7200:
            warnings.append("start time outside toe +/- 2 h for some satellites")
            break
    return {"satellites": sats, "dop": d, "warnings": warnings}


@app.post("/api/rinex/upload")
async def rinex_upload(date: str, file: UploadFile):
    d = dt.date.fromisoformat(date)
    ephemeris.save_uploaded_rinex(d, await file.read())
    return {"cached": True, "date": date}


@app.post("/api/generate")
def generate(body: dict):
    req = scenario.ScenarioRequest(
        rinex_path=body["rinex_path"], lat=body["lat"], lon=body["lon"], alt=body["alt"],
        start=dt.datetime.fromisoformat(body["start_utc"]), duration_s=int(body["duration_s"]),
        sample_rate=float(body.get("sample_rate", config.DEFAULT_SAMPLE_RATE)),
        sample_format=body.get("sample_format", "int16"),
        route=[tuple(p) for p in body["route"]] if body.get("route") else None,
    )
    if scenario.estimate_bytes(req) > download_free_bytes(config.OUT_DIR):
        raise HTTPException(507, "estimated IQ size exceeds free disk space")

    def events():
        q: list = []
        outdir = generator.run(req, progress_cb=lambda f: q.append(f))
        for f in q:
            yield f"data: {json.dumps({'progress': f})}\n\n"
        eph = ephemeris.get_ephemeris(req.start.date())
        rx = geometry.llh_to_ecef(req.lat, req.lon, req.alt)
        sats = geometry.constellation(eph, rx, _gps_tow(req.start))
        iq = inspector.read_iq(outdir / "gpssim.bin", req.sample_format,
                               max_samples=int(req.sample_rate * 0.010))
        table = inspector.compare(iq, req.sample_rate, sats)
        done = {"done": {"outdir": outdir.name,
                         "size_bytes": (outdir / "gpssim.bin").stat().st_size,
                         "inspect": table}}
        yield f"data: {json.dumps(done)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/receiver")
def run_receiver(body: dict):
    outdir = config.OUT_DIR / body["outdir"]
    meta = json.loads((outdir / "meta.json").read_text())
    start = dt.datetime.fromisoformat(meta["config"]["start_utc"])
    eph = ephemeris.get_ephemeris(start.date())
    return receiver.fix_from_iq(
        outdir / "gpssim.bin", meta["sample_format"], meta["sample_rate"],
        eph, _gps_tow(start), marker_llh=body.get("marker"))


@app.get("/api/lnav")
def lnav(prn: int, outdir: str):
    od = config.OUT_DIR / outdir
    meta = json.loads((od / "meta.json").read_text())
    start = dt.datetime.fromisoformat(meta["config"]["start_utc"])
    eph = ephemeris.get_ephemeris(start.date())[prn]
    return lnav_display.explain(eph, tow_count=int(_gps_tow(start) / 6), week=eph.get("gps_week", 0))


@app.post("/api/transmit")
def start_transmit(body: dict):
    if not config.ALLOW_TX or not body.get("confirm_isolated"):
        raise HTTPException(403, "transmit disabled: needs ALLOW_TX and confirm_isolated")
    if _tx_lock.locked():
        raise HTTPException(409, "a transmit is already running")
    params = transmit.TxParams(
        iq_path=str(config.OUT_DIR / body["outdir"] / "gpssim.bin")
        if "outdir" in body else body["iq_path"],
        sample_rate=float(body["sample_rate"]), sample_format=body["sample_format"],
        lo_hz=float(body.get("lo_hz", config.L1_HZ)),
        tx_gain_db=float(body.get("tx_gain_db", -50.0)),
        uri=body.get("uri", config.DEVICE_URI))
    _tx_stop.clear()

    def events():
        with _tx_lock:
            q: list = []
            def cb(d):
                d["fraction"] = None
                q.append(d)
            th = threading.Thread(target=transmit.stream,
                                  kwargs=dict(params=params, dry_run=body.get("dry_run", False),
                                              progress_cb=cb))
            th.start()
            while th.is_alive() or q:
                while q:
                    yield f"data: {json.dumps(q.pop(0))}\n\n"
                if _tx_stop.is_set():
                    break
                th.join(timeout=0.2)
            yield f"data: {json.dumps({'finished': True})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/transmit/stop")
def stop_transmit():
    _tx_stop.set()
    return {"stopped": True}


def _gps_tow(when_utc: dt.datetime) -> float:
    epoch = dt.datetime(1980, 1, 6)
    delta = when_utc - epoch
    return (delta.days % 7) * 86400 + delta.seconds + delta.microseconds / 1e6


@app.get("/")
def index():
    return FileResponse(_FRONT / "index.html")


app.mount("/static", StaticFiles(directory=str(_FRONT)), name="static")
```

Create an empty `frontend/index.html` placeholder now (`<!doctype html><title>gps-sim</title>`) so `StaticFiles` mounts; Task 11 fills it.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_app.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app.py frontend/index.html tests/test_app.py
git commit -m "feat: FastAPI app — preview, generate, receiver, lnav, transmit routes"
```

---

## Task 11: Frontend

**Files:**
- Create: `frontend/index.html`, `frontend/style.css`, `frontend/map.js`, `frontend/skyplot.js`, `frontend/plots.js`, `frontend/transmit.js`, `frontend/app.js`
- Test: `tests/test_frontend_assets.py`

**Interfaces:**
- Consumes: the `/api/*` routes from Task 10. Leaflet from CDN (`https://unpkg.com/leaflet` — allowed here because the frontend is served locally, not as an Artifact).
- Produces: a single page with panels — Map, Scenario form, Constellation (skyplot + table + DOP), Generate (progress + inspect table), Receiver check (fix + error), LNAV teaching, Transmit.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_frontend_assets.py
import pathlib

F = pathlib.Path(__file__).parent.parent / "frontend"


def test_all_frontend_files_present_and_wired():
    for name in ["index.html", "style.css", "map.js", "skyplot.js",
                 "plots.js", "transmit.js", "app.js"]:
        assert (F / name).is_file(), name
    html = (F / "index.html").read_text()
    for src in ["map.js", "skyplot.js", "plots.js", "transmit.js", "app.js"]:
        assert src in html
    for panel in ["panel-map", "panel-scenario", "panel-constellation",
                  "panel-generate", "panel-receiver", "panel-lnav", "panel-transmit"]:
        assert panel in html


def test_transmit_js_requires_confirmation_checkbox():
    js = (F / "transmit.js").read_text()
    assert "confirm_isolated" in js
    html = (F / "index.html").read_text()
    assert 'id="tx-confirm"' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_frontend_assets.py -v`
Expected: FAIL — files missing / strings absent.

- [ ] **Step 3: Write minimal implementation**

```html
<!-- frontend/index.html -->
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPS L1 C/A Signal Simulator</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<h1>GPS L1 C/A Signal Simulator</h1>
<p class="warn">Cabled / shielded setup only. Low TX gain + in-line attenuation.</p>

<section id="panel-map"><h2>1. Location</h2><div id="map"></div>
  <div id="rx-readout">click the map to set the receiver point</div></section>

<section id="panel-scenario"><h2>2. Scenario</h2>
  <label>Start UTC <input type="datetime-local" id="start-utc"></label>
  <label>Duration s <input type="number" id="duration" value="300"></label>
  <label>Sample rate <select id="rate">
    <option>2600000</option><option>4000000</option><option>5000000</option><option>8000000</option>
  </select></label>
  <label>Format <select id="fmt"><option>int16</option><option>int8</option></select></label>
  <button id="btn-preview">Preview geometry</button>
</section>

<section id="panel-constellation"><h2>3. Constellation</h2>
  <canvas id="skyplot" width="320" height="320"></canvas>
  <div id="dop"></div><table id="sat-table"></table>
  <div id="warnings" class="warn"></div>
</section>

<section id="panel-generate"><h2>4. Generate IQ</h2>
  <button id="btn-generate">Generate</button>
  <progress id="gen-progress" max="1" value="0"></progress>
  <table id="inspect-table"></table>
  <a id="download-link" hidden>download gpssim.bin</a>
</section>

<section id="panel-receiver"><h2>5. Receiver check</h2>
  <button id="btn-receiver">Solve position from IQ</button>
  <div id="fix-readout"></div>
</section>

<section id="panel-lnav"><h2>6. LNAV teaching view</h2>
  <label>PRN <input type="number" id="lnav-prn" value="1"></label>
  <button id="btn-lnav">Show subframe 1</button>
  <pre id="lnav-out"></pre>
</section>

<section id="panel-transmit"><h2>7. Transmit</h2>
  <label>Device URI <input id="tx-uri" value="ip:192.168.2.1"></label>
  <label>LO Hz <input id="tx-lo" value="1575420000"></label>
  <label>TX gain dB <input id="tx-gain" type="number" value="-50"></label>
  <label><input type="checkbox" id="tx-confirm"> I am on an isolated / cabled setup</label>
  <label><input type="checkbox" id="tx-dryrun"> dry run (no device)</label>
  <button id="btn-transmit">Start transmit</button>
  <button id="btn-transmit-stop">Stop</button>
  <div id="tx-readout"></div>
</section>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="/static/map.js"></script>
<script src="/static/skyplot.js"></script>
<script src="/static/plots.js"></script>
<script src="/static/transmit.js"></script>
<script src="/static/app.js"></script>
</body>
</html>
```

```css
/* frontend/style.css */
body { font: 14px system-ui, sans-serif; margin: 1rem; max-width: 900px; }
section { border: 1px solid #ccc; border-radius: 6px; padding: 0.75rem; margin: 0.75rem 0; }
#map { height: 320px; }
label { display: inline-block; margin: 0.25rem 0.5rem 0.25rem 0; }
table { border-collapse: collapse; width: 100%; }
td, th { border: 1px solid #ddd; padding: 2px 6px; font-size: 12px; text-align: right; }
.warn { color: #a40000; }
canvas { border: 1px solid #eee; }
```

```javascript
// frontend/map.js
window.gpsMap = (function () {
  let marker = null, cb = null;
  function init() {
    const m = L.map('map').setView([41.0082, 28.9784], 6);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      { attribution: 'OSM' }).addTo(m);
    m.on('click', (e) => {
      if (marker) marker.remove();
      marker = L.marker(e.latlng).addTo(m);
      document.getElementById('rx-readout').textContent =
        `RX ${e.latlng.lat.toFixed(5)}, ${e.latlng.lng.toFixed(5)}`;
      if (cb) cb(e.latlng.lat, e.latlng.lng);
    });
  }
  return { init, onPick: (f) => { cb = f; }, latlng: () => marker && marker.getLatLng() };
})();
```

```javascript
// frontend/skyplot.js
window.drawSkyplot = function (entries) {
  const c = document.getElementById('skyplot'), g = c.getContext('2d');
  const cx = c.width / 2, cy = c.height / 2, R = Math.min(cx, cy) - 10;
  g.clearRect(0, 0, c.width, c.height);
  g.strokeStyle = '#ccc';
  [1, 2 / 3, 1 / 3].forEach(k => { g.beginPath(); g.arc(cx, cy, R * k, 0, 7); g.stroke(); });
  g.fillStyle = '#06c';
  entries.forEach(e => {
    const r = R * (1 - e.el_deg / 90), a = (e.az_deg - 90) * Math.PI / 180;
    const x = cx + r * Math.cos(a), y = cy + r * Math.sin(a);
    g.beginPath(); g.arc(x, y, 4, 0, 7); g.fill();
    g.fillText('G' + e.prn, x + 5, y);
  });
};
```

```javascript
// frontend/plots.js
window.drawInspectTable = function (rows) {
  const t = document.getElementById('inspect-table');
  t.innerHTML = '<tr><th>PRN</th><th>exp chip</th><th>meas chip</th><th>Δchip</th>'
    + '<th>exp Hz</th><th>meas Hz</th><th>ΔHz</th><th>dB</th></tr>'
    + rows.map(r => `<tr><td>G${r.prn}</td><td>${r.expected_code_phase_chips.toFixed(2)}</td>`
      + `<td>${r.measured_code_phase_chips.toFixed(2)}</td><td>${r.code_phase_err_chips.toFixed(2)}</td>`
      + `<td>${r.expected_doppler_hz.toFixed(0)}</td><td>${r.measured_doppler_hz.toFixed(0)}</td>`
      + `<td>${r.doppler_err_hz.toFixed(0)}</td><td>${r.metric_db.toFixed(1)}</td></tr>`).join('');
};
```

```javascript
// frontend/transmit.js
window.startTransmit = function (outdir) {
  const body = {
    outdir,
    sample_rate: Number(document.getElementById('rate').value),
    sample_format: document.getElementById('fmt').value,
    uri: document.getElementById('tx-uri').value,
    lo_hz: Number(document.getElementById('tx-lo').value),
    tx_gain_db: Number(document.getElementById('tx-gain').value),
    confirm_isolated: document.getElementById('tx-confirm').checked,
    dry_run: document.getElementById('tx-dryrun').checked,
  };
  fetch('/api/transmit', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(r => {
    if (r.status === 403) { alert('Tick the isolated-setup confirmation and set ALLOW_TX.'); return; }
    const rd = r.body.getReader(), dec = new TextDecoder();
    (function pump() {
      rd.read().then(({ value, done }) => {
        if (done) return;
        dec.decode(value).split('\n\n').forEach(chunk => {
          const line = chunk.replace(/^data: /, '').trim();
          if (line) document.getElementById('tx-readout').textContent = line;
        });
        pump();
      });
    })();
  });
};
document.getElementById('btn-transmit-stop').onclick =
  () => fetch('/api/transmit/stop', { method: 'POST' });
```

```javascript
// frontend/app.js
let lastOutdir = null;
window.addEventListener('DOMContentLoaded', () => {
  gpsMap.init();

  document.getElementById('btn-preview').onclick = async () => {
    const ll = gpsMap.latlng(); if (!ll) return alert('pick a point');
    const r = await fetch('/api/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        lat: ll.lat, lon: ll.lng, alt: 100,
        start_utc: document.getElementById('start-utc').value + ':00',
      }),
    });
    const d = await r.json();
    drawSkyplot(d.satellites);
    document.getElementById('dop').textContent =
      `PDOP ${d.dop.pdop.toFixed(2)}  GDOP ${d.dop.gdop.toFixed(2)}`;
    document.getElementById('warnings').textContent = d.warnings.join(' · ');
    document.getElementById('sat-table').innerHTML =
      '<tr><th>PRN</th><th>az</th><th>el</th><th>PR km</th><th>chip</th><th>Doppler</th></tr>' +
      d.satellites.map(s => `<tr><td>G${s.prn}</td><td>${s.az_deg.toFixed(0)}</td>` +
        `<td>${s.el_deg.toFixed(0)}</td><td>${(s.pseudorange_m / 1e3).toFixed(1)}</td>` +
        `<td>${s.code_phase_chips.toFixed(1)}</td><td>${s.carrier_doppler_hz.toFixed(0)}</td></tr>`).join('');
  };

  document.getElementById('btn-generate').onclick = () => {
    const ll = gpsMap.latlng(); if (!ll) return alert('pick a point');
    const start = document.getElementById('start-utc').value + ':00';
    const body = {
      rinex_path: 'AUTO', lat: ll.lat, lon: ll.lng, alt: 100, start_utc: start,
      duration_s: Number(document.getElementById('duration').value),
      sample_rate: Number(document.getElementById('rate').value),
      sample_format: document.getElementById('fmt').value,
    };
    fetch('/api/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(r => {
      const rd = r.body.getReader(), dec = new TextDecoder();
      (function pump() {
        rd.read().then(({ value, done }) => {
          if (done) return;
          dec.decode(value).split('\n\n').forEach(chunk => {
            const line = chunk.replace(/^data: /, '').trim(); if (!line) return;
            const msg = JSON.parse(line);
            if (msg.progress !== undefined)
              document.getElementById('gen-progress').value = msg.progress;
            if (msg.done) {
              lastOutdir = msg.done.outdir;
              drawInspectTable(msg.done.inspect);
              const a = document.getElementById('download-link');
              a.href = `/static/../out/${msg.done.outdir}/gpssim.bin`; a.hidden = false;
            }
          });
          pump();
        });
      })();
    });
  };

  document.getElementById('btn-receiver').onclick = async () => {
    if (!lastOutdir) return alert('generate first');
    const ll = gpsMap.latlng();
    const r = await fetch('/api/receiver', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ outdir: lastOutdir, marker: [ll.lat, ll.lng, 100] }),
    });
    document.getElementById('fix-readout').textContent = JSON.stringify(await r.json(), null, 1);
  };

  document.getElementById('btn-lnav').onclick = async () => {
    if (!lastOutdir) return alert('generate first');
    const prn = document.getElementById('lnav-prn').value;
    const r = await fetch(`/api/lnav?prn=${prn}&outdir=${lastOutdir}`);
    document.getElementById('lnav-out').textContent = JSON.stringify(await r.json(), null, 1);
  };

  document.getElementById('btn-transmit').onclick = () => {
    if (!lastOutdir) return alert('generate first');
    startTransmit(lastOutdir);
  };
});
```

**Note:** `/api/generate` in `app.py` should resolve `rinex_path == "AUTO"` by
calling `ephemeris.get_ephemeris(start.date())` and using its cached file path;
add that small branch when wiring this task (adjust `generator.run` to accept the
resolved path). Keep the change inside Task 11's commit.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_frontend_assets.py -v`
Expected: PASS (2 passed). Then manual smoke: `python -m uvicorn backend.app:app` and load `/`.

- [ ] **Step 5: Commit**

```bash
git add frontend/ tests/test_frontend_assets.py backend/app.py
git commit -m "feat: frontend — map, skyplot, generate, receiver, lnav, transmit panels"
```

---

## Task 12: End-to-end integration

**Files:**
- Create: `tests/test_integration_generate.py`
- Modify: `README.md` (fill the manual acceptance checklist if any step changed)
- Test: itself

**Interfaces:**
- Consumes: real `gps-sdr-sim` binary (skip the test if `not pathlib.Path(config.GPS_SDR_SIM_BIN).exists()`), `tests/fixtures/brdc_sample.rnx`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_integration_generate.py
import datetime as dt
import pathlib

import numpy as np
import pytest

from backend import config, ephemeris, geometry, scenario, generator, inspector, receiver

pytestmark = pytest.mark.skipif(
    not pathlib.Path(config.GPS_SDR_SIM_BIN).exists(),
    reason="gps-sdr-sim binary not built")

FIX = pathlib.Path(__file__).parent / "fixtures" / "brdc_sample.rnx"
RX_LLH = (41.0082, 28.9784, 100.0)


def _fixture_start():
    # a UTC time whose GPS TOW is 259200 on the fixture's day
    return dt.datetime(2026, 9, 3, 0, 0, 0)  # adjust to the fixture file's date


def test_generate_then_inspect_then_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    req = scenario.ScenarioRequest(
        rinex_path=str(FIX), lat=RX_LLH[0], lon=RX_LLH[1], alt=RX_LLH[2],
        start=_fixture_start(), duration_s=12,
        sample_rate=2.6e6, sample_format="int8")
    outdir = generator.run(req)
    binp = outdir / "gpssim.bin"
    assert binp.stat().st_size == scenario.estimate_bytes(req)

    eph = ephemeris.parse_rinex(FIX)
    rx = geometry.llh_to_ecef(*RX_LLH)
    tow = 259200.0
    sats = geometry.constellation(eph, rx, tow)
    iq = inspector.read_iq(binp, "int8", max_samples=int(2.6e6 * 0.010))
    table = inspector.compare(iq, 2.6e6, sats)
    strong = [r for r in table if r["metric_db"] > 9]
    assert len(strong) >= 4
    for r in strong:
        assert abs(r["code_phase_err_chips"]) < 0.5
        assert abs(r["doppler_err_hz"]) < 50

    fix = receiver.fix_from_iq(binp, "int8", 2.6e6, eph, tow, marker_llh=RX_LLH)
    assert fix["error_m"] < 100.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_integration_generate.py -v`
Expected: FAIL (assertion or, before the binary exists, SKIP). Build the binary via `scripts/setup.sh`, then it must run.

- [ ] **Step 3: Make it pass**

No new production code should be needed. If it fails:
- code-phase sign/offset: check `inspector.compare`'s wrap arithmetic against `geometry.observables` (both use `pseudorange / C * CA_CHIP_HZ`).
- Doppler sign: confirm `geometry.observables` uses `-L1_HZ * (v_rel . los) / C` (approaching satellite → positive Doppler).
- fix error large: confirm `_fixture_start()` and `tow` correspond to the same instant as the fixture epoch; adjust the constant to the fixture file's actual date.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -v`
Expected: whole suite green (integration test included once the binary is built).

- [ ] **Step 5: Commit**

```bash
git add tests/test_integration_generate.py README.md
git commit -m "test: end-to-end generate -> inspect -> fix integration"
```

---

## Self-Review

**1. Spec coverage**

| Spec section | Task |
|---|---|
| 3.1 `ephemeris.py` | Task 2 |
| 3.1 `geometry.py` (expected correlation, DOP) | Task 3 |
| 3.1 `scenario.py` | Task 4 |
| 3.1 `generator.py` + 4.1 meta.json | Task 5 |
| 3.1 `inspector.py` + C/A code | Task 6 |
| 3.1 `receiver.py` | Task 7 |
| 3.1 `lnav_display.py` (display only) | Task 8 |
| 3.1 `transmit.py` + section 8 transmit path | Task 9 |
| 3.1 `app.py`, section 4 data flow, section 6 error handling | Task 10 |
| 3.3 frontend files, panels | Task 11 |
| 5 setup / `scripts/setup.sh` | Task 1 |
| 5.1 RINEX mirror + manual upload | Task 2 + Task 10 (`/api/rinex/upload`) |
| 5.2 time validity warning | Task 10 (`preview` warnings) |
| 5.3 SDR target, LO/rate clamp errors | Task 9 (`_open_device`) |
| 6 disk-space guard | Task 10 (`/api/generate` 507) |
| 6 `< 4` sats, PDOP warnings | Task 10 (`preview`) |
| 6 transmit gating (403) | Task 9 + Task 10 |
| 7 unit tests | Tasks 2, 3, 4, 5, 6, 7, 8, 9 |
| 7 integration (code phase 0.5 chip, Doppler 50 Hz, fix 100 m) | Task 12 |
| 7 dry-run test | Task 9 |
| 7 manual acceptance checklist | Task 1 (README) |
| 2 safety copy | Task 1 (README), Task 11 (UI banner + confirm) |
| Global: `ALLOW_TX` default off, no loop, int16 default | Tasks 1, 9 |

No gaps found.

**2. Placeholder scan**

No "TBD"/"TODO"/"handle edge cases" left. The two implementer notes (LNAV parity
verification in Task 8, `rinex_path == "AUTO"` wiring in Task 11) are explicit,
scoped follow-ups with the exact place and check named, not vague deferrals.

**3. Type consistency**

- `ScenarioRequest` fields identical across Tasks 4, 5, 10, 12.
- `geometry.constellation` returns dicts with `prn`, `code_phase_chips`,
  `carrier_doppler_hz`, `_los` — consumed with those exact names in
  `inspector.compare` (Task 6), `receiver.fix_from_iq` (Task 7), `app.preview`
  (Task 10), `app.js` (Task 11).
- `inspector.acquire` returns `doppler_hz`, `code_phase_chips`, `metric_db` —
  used with those names in `inspector.compare` and `receiver.fix_from_iq`.
- `generator.run(req, progress_cb, binary)` signature matches its callers in
  Tasks 10 and 12.
- `transmit.TxParams` / `transmit.stream(params, dry_run, progress_cb)` match the
  `/api/transmit` handler in Task 10 and the test in Task 9.
- `_gps_tow` defined once in `app.py` (Task 10); geometry tests pass TOW directly
  as a float, consistent with `geometry` treating `t_gps`/`t_rx` as TOW seconds.

No mismatches found.
