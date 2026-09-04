# GPS L1 C/A Signal Simulator — Design

**Date:** 2026-09-04
**Status:** Approved for planning
**Location:** `/Users/arda/codes/gps-sim/`

## 1. Purpose

An interactive workbench that turns a user-chosen location and start time into a
GPS L1 C/A baseband IQ file suitable for RF replay through an SDR in an isolated
(cabled or shielded) setup, where a real hardware GNSS receiver with an external
antenna must acquire, decode LNAV, and compute a position fix.

The tool is a scenario and teaching layer around a proven RF synthesis core
(`gps-sdr-sim`). It does not re-implement IQ synthesis. Its value is everything
around that core: live ephemeris retrieval, a map UI, constellation geometry and
the expected correlation picture, inspection of the generated file, an internal
software receiver for pre-hardware validation, a read-only LNAV/ephemeris
teaching view, and streaming the generated IQ to an SDR over libiio for replay in
an isolated setup.

### Non-goals

- Re-implementing L1 C/A RF synthesis in Python (the `gps-sdr-sim` binary owns this).
- Multi-constellation or multi-frequency support. GPS L1 C/A only.
- Atmospheric modeling in the transmitted signal by default (see 6.4).

Transmit is in scope but constrained: it is limited to an isolated (cabled or
shielded) setup, is gated behind an `ALLOW_TX` config flag and an explicit UI
confirmation, and streams the whole file once as a continuous non-cyclic flow (no
short-buffer looping, which would make the receiver see time jumps).

## 2. Users and safety context

Single local user. The generated IQ is broadcast only in an isolated environment
(RF cage or direct cable). Transmitting in the GPS band over the air is a
regulatory violation and disrupts real receivers; the README states this and the
UI shows the reminder on both the download panel and the transmit panel.

The tool can drive an SDR directly (see 3.1 `transmit.py` and section 8), but
only when `ALLOW_TX` is set in `backend/config.py` and the user ticks an explicit
"I am on an isolated / cabled setup" confirmation. `ALLOW_TX` defaults to off;
with it off the transmit panel is disabled and the tool only writes files. When
transmitting into a receiver over cable, a low TX hardware gain (-40 to -60 dB)
plus 30-60 dB of in-line attenuation is required so the receiver front end is not
saturated; this is stated in the README and the transmit panel.

## 3. Architecture

Two processes:

- **Browser UI** — static HTML/JS with Leaflet. Talks to the backend over REST
  plus Server-Sent Events for generation progress.
- **Python backend** — FastAPI. Orchestrates data retrieval, geometry, the
  `gps-sdr-sim` subprocess, inspection, and the internal receiver.

The backend shells out to the `gps-sdr-sim` binary (built once from source).

### 3.1 Backend modules

| Module | Input | Output | Side effects |
|---|---|---|---|
| `ephemeris.py` | date | `{PRN: eph_dict}` | network fetch, disk cache under `data/rinex/` |
| `geometry.py` | eph, RX ECEF, time window | per-sat table (PRN, az, el, geometric range, pseudorange, code delay in chips, carrier Doppler Hz, code Doppler), DOP, visibility mask | none (pure) |
| `scenario.py` | RX position or route, start time, duration, sampling, format | `gps-sdr-sim` argument set; user-motion CSV for dynamic scenarios | writes motion CSV |
| `generator.py` | scenario args | `out/<iso-timestamp>/gpssim.bin`, `meta.json` | runs subprocess, parses stdout for progress |
| `inspector.py` | `.bin` path, geometry table | measured vs expected comparison (code phase, Doppler) per visible PRN, spectrum data | reads file (mmap) |
| `receiver.py` | `.bin` path | solved ECEF position, error vs marker, PDOP | reads file |
| `lnav_display.py` | eph, PRN, time | LNAV subframe bytes, parity bits, decoded ephemeris fields | none (pure, display only) |
| `transmit.py` | `.bin` path, LO freq, sample rate, TX gain, context URI | elapsed time, underflow count, stop | opens SDR via libiio, streams IQ to hardware |
| `app.py` | — | FastAPI routes, SSE, static file serving | process lifecycle |

`lnav_display.py` is a parallel re-derivation for the teaching view only. It never
feeds the signal path; `gps-sdr-sim` builds the actual transmitted LNAV
internally.

`transmit.py` uses pyadi-iio (`adi.Pluto`) for device setup (context URI, LO
frequency, sample rate, TX hardware gain) and a raw `iio.Buffer` loop for the
actual continuous flow: a large non-cyclic buffer filled from the file in chunks,
pushed back to back at the sample rate, with an underflow counter read from the
device. The file is streamed once from start to end; it is never looped. Sample
format is int16 interleaved I/Q at the file's sample rate, matching
`gps-sdr-sim -b 16`, so no conversion pass is needed.

### 3.2 Module boundaries

`geometry.py` is the heart of the "expected correlation" picture and is a pure
function of ephemeris, receiver position, and time. It is independently testable
against golden values. `ephemeris.py` isolates all network and cache concerns
behind a single `get_ephemeris(date) -> dict` call. `generator.py` is the only
module that knows the `gps-sdr-sim` CLI contract. `inspector.py` and
`receiver.py` reuse the numpy acquisition/tracking/least-squares code already
prototyped (see the session's earlier `gps_sim.py`).

### 3.3 File layout

```
gps-sim/
  README.md
  scripts/setup.sh          # clone + build gps-sdr-sim, pip install deps
  pyproject.toml
  backend/
    app.py
    config.py               # binary path, mirror URLs, defaults
    ephemeris.py
    geometry.py
    scenario.py
    generator.py
    inspector.py
    receiver.py
    lnav_display.py
    transmit.py
  frontend/
    index.html
    app.js
    map.js
    skyplot.js
    plots.js
    transmit.js
    style.css
  tests/
    test_geometry.py
    test_rinex.py
    test_dop.py
    test_integration_generate.py
    fixtures/brdc_sample.rnx  # small committed BRDC file
  data/rinex/                # gitignored cache
  out/                       # gitignored output
  docs/superpowers/specs/    # this document
```

## 4. Data flow

1. User sets RX position (map click), start UTC, duration, sampling rate, and
   sample format, then requests a preview: `POST /api/preview`.
2. Backend ensures a RINEX navigation file for that date exists (cache or
   download), computes geometry, and returns the satellite table, skyplot data,
   and DOP. No IQ is generated yet.
3. User reviews visibility and DOP, then requests generation: `POST /api/generate`.
4. Backend writes a motion CSV for dynamic scenarios, runs `gps-sdr-sim`, streams
   progress percentage over SSE (parsed from stdout), and on completion writes
   `out/<iso-timestamp>/gpssim.bin` and `meta.json`.
5. Backend automatically runs `inspector.py` and returns spectrum data plus, for
   each visible PRN, measured code phase and Doppler alongside the expected
   values from the geometry table.
6. On request (`POST /api/receiver`), the internal software receiver solves a
   position from the IQ and reports the error against the marker and the PDOP.
7. User downloads `gpssim.bin` and `meta.json` for external replay, or
8. with `ALLOW_TX` enabled and the isolated-setup confirmation ticked, starts a
   transmit: `POST /api/transmit` streams the file through the SDR at the LO
   frequency and sample rate from `meta.json`, with SSE reporting elapsed time
   and underflow count; `POST /api/transmit/stop` ends it. A dry-run mode paces
   through the file with no device attached and reports timing only.

### 4.1 Reproducibility

Each `/api/generate` call creates a fresh `out/<iso-timestamp>/` directory.
`meta.json` records the full configuration, the geometry table, the
`gps-sdr-sim` version and commit hash, and the sampling rate and format. This is
sufficient to reproduce a run.

## 5. External dependencies and setup

`scripts/setup.sh` performs:

1. `git clone https://github.com/osqzss/gps-sdr-sim`, then build with
   `gcc -O3 gpssim.c -lm -o gps-sdr-sim -DUSER_MOTION_SIZE=4000` (the larger
   motion buffer supports longer dynamic scenarios). The resulting binary path is
   written into `backend/config.py`.
2. `pip install fastapi uvicorn numpy requests georinex python-dateutil pyadi-iio`.
   `georinex` handles RINEX parsing; no custom parser is needed. `pyadi-iio`
   pulls the libiio Python bindings and needs system `libiio` and `libad9361`
   (`brew install libiio libad9361` on the macOS dev machine).
3. On startup `app.py` verifies the binary exists and `georinex` imports; if
   either is missing the UI shows a specific setup message and `/api/generate`
   returns 503. It also tries `import iio` / `import adi` and, if `ALLOW_TX` is
   on, opens the configured device context; a missing binding or unreachable
   device disables the transmit panel but leaves generation and inspection
   working.

### 5.1 RINEX source

Daily broadcast ephemeris (BRDC) is fetched from an auth-free mirror (BKG at
`igs.bkg.bund.de`, or `gssc.esa.int`), preferring the RINEX 3 product
`BRDC00IGS_R_YYYYDDD0000_01D_GN.rnx.gz` and falling back to the legacy
`brdcDDD0.YYn.Z`. `ephemeris.py` checks the cache first, otherwise downloads,
decompresses, and stores under `data/rinex/`. If download fails the UI offers a
manual RINEX upload (`POST /api/rinex/upload`).

### 5.2 Time validity

The chosen start time must fall within `toe ± 2 hours` of the ephemeris. Outside
that window the UI warns and suggests the nearest valid hour, but still allows
generation at the user's discretion.

### 5.3 SDR target

The transmit target is a PlutoSDR-class device: a ZYNQSDR clone with an AD9363
(AD9361-modified, so the full 70 MHz - 6 GHz tuning range is available and
L1 at 1575.42 MHz is reachable) on a Xilinx XC7Z020. It is reached over libiio by
context URI (`ip:192.168.2.1` or `usb:`), set in `backend/config.py` and
overridable in the transmit panel. Minimum AD936x TX sample rate is about
2.083 Msps, so the 2.6 Msps default is valid; the transmit sample rate must equal
the generated file's rate. If the device firmware still enforces the stock AD9363
325-3800 MHz limit, setting the L1 LO fails with a clear error.

## 6. Error handling

| Condition | Behavior |
|---|---|
| RINEX download fails | Clear message plus manual upload fallback |
| `gps-sdr-sim` binary missing | Detected at startup; UI shows setup steps; `/api/generate` returns 503 |
| Start time outside `toe ± 2 h` | Warn, suggest nearest valid hour, allow anyway |
| Fewer than 4 visible satellites | Warn that a hardware fix is not possible; allow generation |
| PDOP above 10 | Yellow warning, no block |
| Estimated file size exceeds free disk (e.g. 8 Msps int16 for 300 s is about 9.6 GB) | Show size estimate before generation; block if it exceeds free space |
| Long generation | SSE progress plus a cancel button that kills the `gps-sdr-sim` process |
| Dynamic route longer than the RINEX span | Warn and clip the duration |
| `ALLOW_TX` off or confirmation not ticked | Transmit panel disabled; `/api/transmit` returns 403 |
| libiio binding missing or device unreachable | Transmit panel disabled with the reason; generation and inspection unaffected |
| Requested LO frequency rejected by device | Clear error naming the device's tuning limit |
| Transmit sample rate does not equal the file's rate | Block with an explicit message |
| Buffer underflow during transmit | Count shown live over SSE; a sustained underflow rate raises a warning that the fix may fail |
| Transmit running when a new one is requested | Reject until the current one is stopped |

### 6.4 Atmosphere

Atmospheric delay modeling in the transmitted signal is off by default. If
ionospheric delay is added to the signal without broadcasting matching Klobuchar
coefficients, the hardware receiver's correction diverges and the fix degrades.
An AWGN / C/N0 control and an elevation-based power taper remain available;
atmospheric delay stays an explicit opt-in with a warning.

## 7. Testing

- **Unit `test_geometry.py`** — satellite ECEF from a committed RINEX fixture at a
  fixed epoch against golden values (cross-checked with `georinex` and an
  independent computation to within 1 m); azimuth and elevation against a second
  source; light-travel-time iteration including Sagnac (earth-rotation) correction.
- **Unit `test_rinex.py`** — mocked mirror download, `.gz` decompression, and
  parse, asserting the expected PRN count and field presence.
- **Unit `test_dop.py`** — hand-computed four-satellite `(GᵀG)⁻¹` against the module.
- **Integration `test_integration_generate.py`** — fixture RINEX plus a fixed
  location and time, generate 10 s of static IQ at 2.6 Msps int8, then assert the
  inspector matches each visible PRN's code phase within 0.5 chip and Doppler
  within 50 Hz of the geometry table.
- **Integration** — the same IQ through `receiver.py` yields a position within
  100 m of the marker with a consistent solved clock bias.
- **Unit `test_transmit.py`** — `transmit.py` in dry-run mode paces through a
  fixture IQ file with no device attached and reports the expected total duration
  (samples / rate) within a small tolerance and a zero nominal underflow count;
  a rate/format mismatch raises before any device call.
- **Manual acceptance** (outside automation, README checklist) — isolated
  replay (external SDR tool and the built-in `transmit.py` path), hardware
  receiver fix, with TTFF, position error, and observed underflow count recorded.
- **Fixture** — a small BRDC file committed under `tests/fixtures/` for
  deterministic snapshots.

## 8. Transmit path

`transmit.py` streams a generated IQ file to the SDR for replay in the isolated
setup. It is only reachable with `ALLOW_TX` set and the isolated-setup
confirmation ticked.

- **Device setup (pyadi-iio).** `adi.Pluto(uri)` opens the context; the module
  sets `tx_lo` (default 1575.42e6), `sample_rate` (from `meta.json`, must equal
  the file rate), and `tx_hardwaregain_chan0` (default low, -50 dB). It reads
  back the actual values and fails if the LO or rate was clamped.
- **Streaming core (raw libiio).** A single large non-cyclic `iio.Buffer` on the
  TX channels is filled from the file in fixed-size chunks (int16 interleaved
  I/Q, scaled to the DAC range) and pushed back to back. The file is played once,
  start to end, never looped, so nav-message time keeps advancing as a real
  receiver expects. The device underflow attribute is polled between pushes; the
  count and elapsed time are sent over SSE.
- **Dry-run.** With no device or `TRANSMIT_DRYRUN=1`, the same chunk loop runs
  against a sink that sleeps for each chunk's real duration and tallies nominal
  timing, so the pacing path is exercised in tests and demos.
- **Stop.** `POST /api/transmit/stop` sets a flag the chunk loop checks; the
  buffer is destroyed and the device is left with TX disabled.
- **Concurrency.** One transmit at a time; a second request is rejected until the
  running one stops or finishes.

## 9. Open items for the implementation plan

- Exact `gps-sdr-sim` stdout progress format to parse (verify against the current
  upstream source during implementation).
- Whether the internal receiver runs on a decimated copy of the IQ for speed.
- Skyplot and spectrum rendering: lightweight canvas drawing versus a small
  charting dependency.
- Optimal `iio.Buffer` size and chunk count for gap-free 2.6 Msps TX over the
  clone's USB and Ethernet links (tune during hardware bring-up).
- DAC full-scale scaling for `gps-sdr-sim -b 16` output (12-bit vs 16-bit
  left-justified) so TX amplitude is right without clipping.
