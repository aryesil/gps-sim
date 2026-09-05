# GPS L1 C/A Signal Simulator

A controlled, observable, and reproducible GNSS signal testing workbench.

Pick a place and time on a map, generate a baseband GPS L1 C/A IQ recording
with [`gps-sdr-sim`](https://github.com/osqzss/gps-sdr-sim), check it with a
built-in software receiver, decode its navigation message, and — optionally,
into a cable and never over the air — replay it to a PlutoSDR-class SDR.
Every RF-relevant action is written to a persistent audit log. Signal
generation runs from broadcast ephemeris by default; in **precise mode** an
IGS SP3 orbit/clock product is fitted into the broadcast record set that
drives `gps-sdr-sim`, so the satellite states behind the IQ match the
precise product to well under a metre.

> ⚠️ **RF safety first.** Transmit is disabled by default. Read
> [Safety](#safety) before enabling it. Lawful, authorized operation is
> entirely the operator's responsibility.

---

## Table of contents

- [Features](#features)
- [Why this project](#why-this-project)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Ephemeris and accuracy modes](#ephemeris-and-accuracy-modes)
- [The UI](#the-ui)
- [Typical workflows](#typical-workflows)
- [API](#api)
- [Configuration](#configuration)
- [Access control](#access-control)
- [Safety](#safety)
- [Testing](#testing)
- [Accuracy and limitations](#accuracy-and-limitations)
- [License](#license)

---

## Features

- **Generate** — static point or dynamic waypoint route → `gps-sdr-sim` →
  raw interleaved IQ + `meta.json`, with streamed progress.
- **Inspect** — power spectrum, per-PRN acquisition (Doppler + code phase),
  code-correlation curves, measured-vs-predicted geometry.
- **Verify** — a from-scratch software receiver acquires the generated IQ,
  solves a least-squares fix, and reports the error against the map marker.
- **Decode** — reconstruct and explain the LNAV navigation message
  (subframes, parity, clock/ephemeris parameters) for a chosen PRN.
- **Transmit** (opt-in) — push IQ to a PlutoSDR-class device via `pyadi-iio`
  with underflow tracking, a cancel event, and an optional fail-safe timeout.
- **Live session** — regenerate one short IQ segment at a time so position
  and GPS time can be jogged *while transmitting*; timeline scripting;
  live spectrogram + C/N0 trend over SSE; record the SSE stream and replay
  it later with no hardware attached.
- **Precise ephemeris** — parse IGS SP3-c/d products and interpolate
  satellite ECEF position/velocity and clock. Two uses: (1) *analysis* —
  compare the precise reference against the realigned broadcast column;
  (2) *generation* — least-squares fit the SP3 track into a broadcast
  record set (solved M0 + perturbations, a 4 h arc) so `gps-sdr-sim`
  propagates satellite states that match the precise product to
  sub-metre, instead of the epoch-stamped `toc`/`toe` approximation.
- **Operations support** — persistent JSONL audit log, multi-operator
  `/ws/events` WebSocket mirror, real-receiver NMEA feedback (UDP/serial),
  named trajectory and scenario presets, optional API-key RBAC.

---

## Why this project

`gps-sdr-sim` produces a signal; it does not tell you how good that signal
is. This workbench wraps generation with the parts a test rig needs:

- **Observable** — every stage is inspectable (spectrum, acquisition,
  receiver fix, decoded LNAV), and every RF action is logged.
- **Reproducible** — scenarios and trajectories are saved by name; a live
  session's exact event stream can be recorded and replayed.
- **Honest about fidelity** — by default the signal is built from
  *broadcast* ephemeris whose epoch is only realigned (`toc`/`toe`
  stamped) to the requested start, and the precise subsystem *measures*
  the resulting error rather than hiding it. When you need the geometry to
  be right, precise mode fits the SP3 track into the broadcast records so
  the IQ is genuinely precise-driven — and the README says exactly which
  path produced a given recording (`meta.json` records `ephemeris_mode`
  and the per-PRN fit residual).

---

## Quick start

```sh
./scripts/setup.sh          # builds gps-sdr-sim, creates .venv, installs deps
./scripts/run_server.sh      # serves http://127.0.0.1:8000
```

`setup.sh` clones and compiles `gps-sdr-sim`, creates `.venv`, and runs
`pip install -e ".[dev]"`. For the transmit path only it also builds
`libiio` (v0.25) and `libad9361-iio` (v0.4.0) from source into the venv's
own prefix — nothing touches a system directory, and `rm -rf .venv` removes
them cleanly. Prerequisites for that path: `cmake`, a C compiler, and
`libusb`. Without them everything except hardware transmit still works.

`run_server.sh` activates `.venv`, points `DYLD_LIBRARY_PATH` /
`LD_LIBRARY_PATH` at the venv's `lib/`, and starts uvicorn. Override with
`HOST=` / `PORT=`; extra args pass through (e.g. `--reload`). Enable
transmit endpoints with `ALLOW_TX=1 ./scripts/run_server.sh`.

Run the test suite:

```sh
.venv/bin/pytest -q
```

---

## Architecture

```
                map + time
                    |
       +------------+--------------------------------+
       | ephemeris_mode = broadcast (default)        | ephemeris_mode = precise
       v                                             v
  broadcast RINEX nav                    IGS SP3 orbit/clock (interpolated)
  (IGS/BKG mirror or upload)                          |
       |                                  least-squares fit -> broadcast
  toc/toe realigned to start              records (solved M0 + perturb.,
       |                                  4 h arc, sub-metre residual)
       +------------------+--------------------------+
                          v
                    RINEX-2 nav file
                          |
                          v
                    gps-sdr-sim  ------------->  raw IQ + meta.json
                          |                           |
                          |                  +--------+--------+
                          v                  v                 v
                  software receiver     spectrum /        transmit (opt-in,
                  least-squares fix     acquisition /     cabled only) / live
                          |             LNAV decode       segment regeneration
                          v
                   fix error vs marker

  precise SP3 orbit/clock  --(analysis)-->  /api/preview, /api/precise/compare
  (also drives generation                   per-PRN delta vs realigned broadcast
   in precise mode, above)
```

**`gps-sdr-sim` always consumes a RINEX-2 broadcast Keplerian nav file** —
there is no satellite-state interface into it. In broadcast mode that file
is the realigned daily BRDC; in precise mode it is a record set whose
parameters were *solved* against the SP3 track, so the states gps-sdr-sim
propagates match the precise product. The analysis endpoints
(`/api/preview` precise, `/api/precise/compare`) still report the
broadcast-vs-precise delta so the realignment error stays visible.

```
backend/
  app.py            FastAPI app: all HTTP + WS endpoints, SSE streams, TX slots
  config.py         env-var config, constants, data/out/log/precise dir creation
  ephemeris.py      RINEX download, parse, epoch realignment, RINEX-2 writer
  scenario.py       ScenarioRequest, gps-sdr-sim argv builder
  generator.py      runs gps-sdr-sim (full run + short live segments)
  geometry.py       WGS84 / ECEF / ENU, constellation geometry, transmit-time solve
  gpstime.py        GPSTime dataclass, leap-second table, UTC<->GPS
  precise.py        SP3-c/d parser, Neville position/velocity interp, clock interp
  ephemeris_source.py  broadcast|precise mode selection, per-PRN state functions
  ephemeris_fit.py  least-squares fit of SP3 track -> broadcast record set (precise generation)
  inspector.py      IQ read, spectrum, C/A acquisition, correlation, compare
  receiver.py       least-squares position solve from generated IQ
  lnav_display.py   LNAV subframe reconstruction + human-readable explain
  live.py           LiveSession: segment regeneration, jog, time-shift
  transmit.py       pyadi-iio streaming, underflow tracking, cancel
  device.py         standby SDR control link (no RF)
  audit.py          append-only JSONL audit trail
  ws_hub.py         thread-safe WebSocket broadcast of audit events
  auth.py           API-key role checks
  receiver_feed.py  UDP/serial NMEA listener, latest-fix state
  nmea.py           pure NMEA 0183 GGA/RMC parser
  trajectory.py     named waypoint-route storage
  scenario_lib.py   named scenario-preset storage (field allowlist)
  recording.py      SSE-payload recording + replay

frontend/           vanilla JS, no build step; served static by FastAPI
  index.html        three pages: Channels, Trajectory Builder, Log
  channels.js       channel cards: config, start/stop, timeline, scrubber,
                    scenario save/load, replay, precise-ephemeris panel
  map.js trajectory.js live.js pages.js app.js
  plots.js skyplot.js iqplot.js   spectrogram, sky plot, IQ / spectrum plots
  log.js            audit-log merge, /ws/events client, receiver-feed panel
```

**Transport:** long-running generate / transmit / replay stream Server-Sent
Events; the multi-operator feed is a WebSocket; everything else is JSON over
HTTP. **Concurrency:** at most two transmit slots (`TX1`, `TX2`); audit
appends are lock-serialized. **Tech stack:** Python ≥ 3.10, FastAPI +
uvicorn, NumPy, `georinex`, `pyadi-iio` (transmit only), `gps-sdr-sim` (C,
built from source); dependency-free vanilla JS plus Leaflet from a CDN.

---

## Ephemeris and accuracy modes

Set with `ephemeris_mode` on `/api/generate`, `/api/live/start`, and
`/api/preview` (`"broadcast"` default, or `"precise"`).

| Mode | Where it applies | What it means |
|------|------------------|---------------|
| **Broadcast** (default) | generation + analysis | The daily broadcast RINEX nav file, with every satellite's `toc`/`toe`/`gps_week` **stamped** to the requested start so `gps-sdr-sim`'s validity-window check passes. M0 and the perturbations are left untouched — a broadcast-compatibility mechanism, not a precise ephemeris — so orbit error grows as the requested start moves away from the real broadcast epoch. |
| **Precise (SP3), analysis** | `/api/preview`, `/api/precise/compare` | An IGS SP3-c/d orbit/clock product, interpolated (~10th-order Neville for position, analytic derivative for velocity, linear for the coarse SP3 clock). Reports the per-PRN broadcast-vs-precise delta. |
| **Precise (SP3), generation** | `/api/generate`, `/api/live/start` | `ephemeris_fit.py` least-squares fits the interpolated SP3 track (position over a 4 h arc, `toe` ± 2 h) into a broadcast record set — solving M0, `delta_n`, the rate terms and the six harmonic corrections, plus `af0/af1/af2` against the SP3 clock. The fitted records go to `gps-sdr-sim` with **no realignment**, so the satellite states behind the IQ match the SP3 product to sub-metre. Fit residuals land in `meta.json` (`precise_fit`); a fit that cannot get under ~2 m is an error, not a silent bad nav file. |

When precise mode is requested and no loaded SP3 product covers the start
time, the server auto-downloads the best free IGS product for that GPS day
*and the day either side* (`PRECISE_SP3_MIRRORS`: rapid → final →
ultra-rapid), merges them, and loads the result — no file to place, no
button to press. Ultra-rapid (IGU) is only reached for epochs too recent
for rapid/final; its second day is *predicted*, so a warning is added.

The three-day merge matters because a single one-day SP3 file
cannot supply a centred ~11-point interpolation window when the start
sits within ~3 h of its 00:00/24:00 edge (the fit arc is `toe ± 2 h`). If
a neighbour day is not published yet the run still proceeds, with a
warning, on an off-centre window. A manually loaded product covering the
epoch is reused as-is. Set `PRECISE_SP3_MIRRORS=""` to disable downloads.

Precise mode fails explicitly rather than silently degrading:

- No SP3 product could be obtained for the epoch (auto-download disabled or
  failed, none loaded), the epoch is outside a manually loaded product's
  interval, or a fit that will not converge below tolerance → **HTTP 422**,
  unless the caller passes `fallback_to_broadcast: true` (which then uses
  broadcast with a warning that names what failed).
- A PRN absent from the SP3 product is **omitted and named in `warnings`**,
  never silently replaced with a broadcast value.
- No extrapolation past the product's edge epochs.

`/api/precise/compare` reports, per PRN, the position delta (with
radial/along/cross components), clock delta, range and pseudorange delta,
Doppler delta, and elevation, plus an RMS summary — broadcast(realigned)
minus precise. With `sweep_s`/`step_s` in the body it repeats the
comparison across the scenario duration and returns a per-PRN `series`.
The browser panel sends the sweep automatically and renders it with
`frontend/compare.js` in a dedicated full-width region below the config
row (not squeezed into the narrow sim column): a plain-language verdict
("broadcast ranges sit ~N m off precise, ≈ X% of one GPS code chip") plus
a colour-graded recommendation, summary cards, and — in a responsive grid
— a per-PRN radial/along/cross bar chart, a per-PRN clock-offset bar
chart, and an interactive time-sweep line chart (per-PRN toggles, metric
selector, hover readout). Charts are drawn at the display's pixel density
and stacked full width. No RMS jargon and no text to scroll. The region
has a Hide button, and pressing Compare again with the position, time,
RINEX path and sweep unchanged re-opens the last result from memory
instead of recomputing.

---

## The UI

Three pages, switched from the left sidebar.

**Channels** — one card per transmit channel (up to two):

- *Hardware config* — Device URI, LO frequency (default 1575.42 MHz),
  TX gain dB (default −50), *Dry run*, *Auto-stop after (s)*,
  *Record this session*.
- *Simulation config* — map marker, Start UTC, Duration, Sample rate,
  Format (int16 / int8), RINEX (`AUTO` or a path).
- *Ephemeris* — Broadcast (realigned) / Precise (SP3-fitted) selector,
  applied to Generate and live runs, plus a collapsible panel to load an
  SP3 file and run a compare-vs-broadcast.
- *Scenario library* — save/reload the card's config by name.
- *Timeline* — `jog` / `time_shift` steps that fire at `t + N` s.
- *Inspect panels* — IQ waveform + spectrum with a playback scrubber,
  spectrogram waterfall, C/N0 trend, sky plot, LNAV decode for a PRN.
- *Live manipulation* — jog buttons and GPS time-of-week shift while a live
  session runs.
- *Start / Stop*, with a type-to-confirm gate and the mandatory
  "isolated / cabled setup confirmed" checkbox. **Start All** starts every
  configured channel.

**Trajectory Builder** — draw a waypoint route on the map, edit the point
table, **Save as…**, **Open**, **Apply to channel** as a dynamic scenario.

**Log** — Real Receiver Feedback (UDP or serial NMEA, latest parsed fix)
and the Event list: the persistent audit trail merged with the live
`/ws/events` feed, newest first.

---

## Typical workflows

**Generate and verify (no RF)**

1. Click the map to set the marker, Start UTC = now, Duration 300 s,
   2.6 Msps, int16, RINEX `AUTO`.
2. **Generate.** The inspect table shows measured vs. predicted Doppler /
   code phase per PRN.
3. Scrub the IQ plot; check spectrogram and sky plot.
4. Run the internal receiver check (`POST /api/receiver`) — expect a fix
   within ~100 m of the marker.

**Quantify ephemeris error**

1. Run a precise **Preview** for your lat/lon/time — this auto-downloads
   the SP3 product for that epoch. Then **Compare vs broadcast** in the
   precise panel — the result is charted (radial/along/cross bars, clock
   bars, and a time-sweep line chart with per-PRN toggles), showing how
   far a *broadcast*-mode recording's geometry sits from the precise
   reference. (Load a specific SP3 by path first only to pin one.)

**Precise-geometry generation**

1. Set the card's Ephemeris selector to **Precise (SP3-fitted)** and
   **Generate**. The SP3 product for the scenario epoch is downloaded and
   loaded automatically (no file to place); a specific local SP3 loaded by
   path is used instead if present.
2. The `done` event and `meta.json` (`precise_fit`) report the per-PRN fit
   residual; an epoch with no obtainable product, or a fit that will not
   converge, fails with HTTP 422 instead of falling back silently.

**Cabled hardware replay**

1. Start with `ALLOW_TX=1`. Generate a scenario.
2. Connect SDR TX to the receiver antenna port through **≥ 40 dB** in-line
   attenuation. Cabled or shielded only.
3. Set Device URI, LO 1575.42 MHz, 2.6 Msps, TX gain −50 dB, tick
   "isolated / cabled setup confirmed", **Start**.
4. Record TTFF, reported position vs. marker, sustained underflow count.

**Live walk**

1. Start a **live** session (position/time editable while running).
2. Use jog buttons or pre-load a **Timeline**.
3. Tick **Record this session**; replay it later at 1× / 2× with no hardware.

---

## API

Base URL `http://127.0.0.1:8000`. Long operations stream Server-Sent Events;
`/ws/events` is a WebSocket; everything else is JSON over HTTP. When
`API_KEYS_JSON` is set, endpoints below marked *operator* need an operator
key in `X-API-Key`; a transmit *stop* never does.

| Group | Endpoints |
|-------|-----------|
| Health / preview | `GET /api/health`, `POST /api/preview` (accepts `ephemeris_mode`, `fallback_to_broadcast`), `POST /api/preview_track` |
| Ephemeris | `POST /api/rinex/upload` |
| Precise (analysis) | `GET /api/precise/status`, `POST /api/precise/load` *(operator)*, `POST /api/precise/compare` |
| Generate / inspect | `POST /api/generate` (SSE; accepts `ephemeris_mode`, `fallback_to_broadcast`), `POST /api/receiver`, `GET /api/iqplot`, `GET /api/correlation`, `GET /api/lnav` |
| Transmit *(needs `ALLOW_TX=1`)* | `POST /api/transmit` (SSE, operator), `POST /api/transmit/stop`, `POST /api/live/start` (SSE, operator), `POST /api/live/jog|time_shift` *(operator)*, `POST /api/live/stop` |
| Device *(needs `ALLOW_TX=1`)* | `POST /api/device/connect|disconnect` *(operator)*, `GET /api/device/status` |
| Recording | `GET /api/recording/list`, `GET /api/recording/replay` (SSE) |
| Receiver feedback | `POST /api/receiver/listen|stop_listen|inject` *(operator)*, `GET /api/receiver/fix` |
| Presets | `POST/GET/GET /api/trajectory/save|list|load`, `POST/GET/GET /api/scenario/save|list|load` (saves are *operator*) |
| Observability | `GET /api/audit`, `WS /ws/events` |

Full request/response bodies, roles, and error codes are documented inline
in `backend/app.py` route handlers.

---

## Configuration

All via environment variables (see `backend/config.py`).

| Variable | Default | Meaning |
|----------|---------|---------|
| `ALLOW_TX` | `0` | Master switch for every transmit endpoint. Off ⇒ HTTP 403. |
| `DEVICE_URI` | `ip:192.168.2.1` | Default SDR URI for `pyadi-iio`. |
| `DEFAULT_SAMPLE_RATE` | `2600000` | Default IQ sample rate (Hz). |
| `DEFAULT_FORMAT` | `int16` | Default sample format (`int16` / `int8`). |
| `DATA_DIR` | `./data` | RINEX cache, saved trajectories & scenarios. |
| `OUT_DIR` | `./out` | Generated IQ + `meta.json`, recordings. Served at `/out`. |
| `LOG_DIR` | `./logs` | `audit.jsonl`. |
| `PRECISE_DIR` | `./data/precise` | SP3 product cache / load directory. |
| `PRECISE_SP3_MIRRORS` | free BKG + IGN rapid → final → ultra-rapid SP3 (anonymous) | Comma-separated SP3 URL templates (`{gpsweek}`/`{gps_week}`/`{dow}`/`{yyyy}`/`{doy}`/`{wwwwd}`/`{hh}`). Tried in order, first hit wins: rapid (final-grade orbits, ~17 h latency) → final (best, ~12 d) → ultra-rapid (IGU, ~3–9 h, 2-day file whose second half is *predicted*) as the last resort for very recent epochs. Used by an explicit `POST /api/precise/load` with `download` and by the auto-fetch on the precise `/api/preview` and `/api/generate` paths. Cached per day and tier (`IGS_wwww_d_{RAP,FIN,ULT}.sp3`); delete the file to force a refresh. Set to `""` to disable all downloads. |
| `GPS_SDR_SIM_BIN` | `./gps-sdr-sim/gps-sdr-sim` | Path to the built binary. |
| `API_KEYS_JSON` | `""` | JSON `{"<key>": "operator"｜"viewer"}`. Empty ⇒ auth disabled. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | uvicorn bind (via `run_server.sh`). |

---

## Access control

Off by default. Set `API_KEYS_JSON` to switch it on:

```sh
API_KEYS_JSON='{"s3cret-op":"operator","read-only":"viewer"}' ALLOW_TX=1 ./scripts/run_server.sh
```

- **operator** — start/stop transmit, jog, script timelines, connect the
  device link, listen for receiver feedback, load SP3 products, save presets.
- **viewer** — read-only: status, audit, replay, preview, compare. May also
  *stop* a transmit (safety-positive — any valid key can issue one).

The browser sends the key as `X-API-Key`; the UI's 🔑 button stores it in
`localStorage`. When `API_KEYS_JSON` is empty, every check is a no-op.

---

## Safety

**Do not transmit RF over the air.** Operation outside an authorized,
controlled test environment may violate spectrum regulations and can
interfere with GNSS receivers that safety-critical systems depend on.

If RF replay is required:

- Use only a fully shielded (Faraday) or conducted / cabled setup.
- Feed a receiver over cable with **low TX gain (−40 to −60 dB)** plus
  **30–60 dB in-line attenuation**.
- Transmit is refused (HTTP 403) unless **both** `ALLOW_TX=1` **and** the
  per-channel "isolated / cabled setup confirmed" checkbox are set.
- `POST /api/device/connect` opens a control link only — RF begins solely
  when a transmit starts.
- Use `max_duration_s` (Auto-stop) so a transmit can't run unattended.

Responsibility for lawful, authorized operation rests entirely with the
operator; the authors accept no liability for misuse.

---

## Testing

```sh
.venv/bin/pytest -q
```

**406 passed, 3 xfailed** as of this writing. Coverage spans ephemeris
alignment, GPS-time conversions, the SP3 parser and orbit/clock
interpolation, the broadcast/precise mode selector, the SP3→broadcast
fit (pure-Kepler recovery to millimetres, SP3-fixture fit, RINEX-2
serialisation), precise generation wiring, geometry, acquisition, the
receiver solve, LNAV decode, the live session, transmit plumbing
(mocked hardware), the device link, audit, RBAC, NMEA parsing,
recording/replay, the scenario library, the WebSocket hub, and the
precise-ephemeris HTTP endpoints. The 3 `xfail` cases need real SDR
hardware. The suite uses fixtures and mocks only — no network downloads.

`tests/` is organised into flat integration tests plus `unit/`,
`validation/` (independent cross-checks — see below), and `regression/`
(deterministic-output and provenance guards).

### Self-consistency / validation layer

A set of software-only checks that do not need hardware or the real
`gps-sdr-sim` binary:

- **Independent geometry reference** (`backend/reference.py`) — a
  from-scratch IS-GPS-200 broadcast propagator (different anomaly solver,
  analytic velocity, rotation-matrix ECEF, own Sagnac loop) and a
  least-squares-polynomial SP3 interpolator. The validation tests compare
  the production path against this, never against itself.
- **Canonical truth model** (`backend/truth.py`) — one conversion path
  from (lat/lon/alt, UTC start, duration) to ECEF, GPS week/sow, and
  truth observables, shared by the generator and the validator.
- **`scripts/validate_scenario.py`** — chains ephemeris → geometry vs
  reference → generation → IQ integrity → acquisition → weighted receiver
  fix → nominal error budget and prints a human or `--json` PASS/FAIL
  report. `--no-generate` runs the geometry/budget stages only.

---

## Accuracy and limitations

- **Broadcast mode realigns, it does not propagate.** `toc`/`toe` are
  stamped to the requested start; M0 and the perturbations are not, so
  orbit error grows the further the start is from the real broadcast
  epoch. Use the precise compare to quantify it, or precise generation to
  avoid it.
- **Precise generation is orbit-accurate, not a time standard.** The
  fitted records reproduce the SP3 *position* track to sub-metre over the
  fit arc, but the clock comes from the SP3 product's coarse clock
  (linearly interpolated) fitted to `af0/af1/af2` — good to a few ns, not
  a sub-nanosecond source. Iono/tropo/multipath are not part of the fit;
  they reach the IQ only through the opt-in channel models (ionosphere via
  `gps-sdr-sim`'s `-i`, receiver-clock + multipath via the quasi-static
  `_apply_channel` post-process, troposphere not at all).
- **The fit arc is 4 h.** Precise generation needs an SP3 product whose
  epochs cover the scenario start ± 2 h; outside that it returns HTTP 422.
- **GPS L1 C/A only** — single band, single constellation. No GLONASS /
  Galileo / BeiDou, no L2/L5.
- **Two transmit slots** (`TX1`, `TX2`) maximum.
- **Some endpoints have limited UI wiring** (`/api/receiver`, `/api/lnav`,
  `/api/correlation`, `/api/preview_track`) — call them directly with
  `curl` if a panel isn't exposed on your build.
- The CDDIS RINEX mirror needs NASA Earthdata credentials; without them
  only the BKG mirror is effective. SP3 products download from the free
  anonymous BKG/IGN mirrors in `PRECISE_SP3_MIRRORS` (rapid tried before
  final) on an explicit `download` request, or load by local path. Very
  recent epochs may have no rapid product published yet. The bundled
  `tests/fixtures/igs_sample.sp3` is a synthetic Kepler+bias file, so the
  offline test-suite does not exercise real non-Keplerian orbit dynamics.

### Optional models (all default-off, all deterministic)

These are library modules with unit + consistency tests. Every sub-model
defaults to `off`. When a request enables one it **always** shapes the
`/api/preview` and truth observables (never the geometric range or the
motion-derived Doppler); whether it also alters the generated **IQ** is a
separate opt-in — see *Channel models in the IQ* below.

| Module | Model | Notes / assumptions |
|---|---|---|
| `backend/atmosphere.py` | Klobuchar iono (L1), Saastamoinen tropo | Broadcast-grade. Saastamoinen uses a `1/sin(el)` mapping, good to a few cm above ~15° — not Niell/VMF1. |
| `backend/receiver_clock.py` | Receiver clock offset: bias + drift + drift-rate polynomial, optional sawtooth | Distinct from satellite clock and propagation delay; adds a common `c·offset` to simultaneous pseudoranges and a common `−f_L1·drift` carrier offset. No RNG. |
| `backend/multipath.py` | Specular: direct + N reflections (delay, amplitude<1, phase, Doppler) | `channel_taps()` for convolving clean IQ; `tracking_bias()` is a closed-form narrow-correlator DLL/Costas approximation, **not** a substitute for filtering the IQ. |
| `backend/channel_models.py` | Glue: parses the three request dicts, applies them to the preview/truth observables, produces the UI summary | The three model modules stay standalone; this is the only place that binds them to a request. |
| `backend/impairments.py` | RF impairment layer over complex IQ: CFO, sample-clock ppm, phase noise, I/Q imbalance, DC offset, AWGN (SNR or noise power), clipping, requantisation | All randomness from one seeded `default_rng`; `(config, seed, input) → output` is bit-for-bit reproducible. Wired into `generator.run` via `ScenarioRequest.impairments` (default `None`); the clean file is kept as `gpssim.clean.bin`. Reachable from the browser UI via the per-channel *RF impairments (advanced)* panel (collapsed and opt-in; an untouched panel leaves the `/api/generate` body unchanged). |
| `backend/wls.py` | Elevation-weighted least-squares fix + GDOP/PDOP/HDOP/VDOP/TDOP + formal covariance | Standalone; the legacy unweighted `receiver.solve_position` is unchanged. |
| `backend/error_budget.py` | Per-PRN 1-σ range error budget, RSS to a UERE | Nominal figures are **documentation-grade, not a calibration** of this simulator. |

All four sub-models are reachable from the browser: the per-channel
**Propagation & receiver models (advanced)** panel (collapsed, opt-in)
feeds `atmosphere` / `receiver_clock` / `multipath` and a `models_to_iq`
flag into `/api/preview` and `/api/generate`. An untouched panel adds
nothing to either request. `/api/preview` returns a `channel_models`
summary (per-epoch iono/tropo metres, receiver-clock offset, multipath
bias) that the panel renders under the controls.

#### Channel models in the IQ

By default the models are **truth-only** — the generated `gpssim.bin` is
byte-identical to a run without them. Ticking *Also apply these models to
the generated IQ* (`models_to_iq=true`) turns on:

- **Ionosphere** — `atmosphere.ionosphere == "klobuchar"` keeps
  gps-sdr-sim's own broadcast Klobuchar enabled (drops its `-i`
  suppression flag), so the delay is in the IQ via the generator, not a
  post-process.
- **Receiver clock + multipath** — `generator._apply_channel` runs a
  *quasi-static* post-process on the composite signal (evaluated once at
  mid-scenario): a common receiver-clock time/carrier offset and a
  specular-multipath FIR. It runs before the RF-impairment stage; the
  pre-channel file is kept as `gpssim.prechannel.bin` and a report lands
  in `meta.json`'s `provenance.channel_models`. A large clock drift or a
  non-zero reflection Doppler is only approximated, and **tropospheric
  delay is never injected into the IQ** — the report flags this.

### Reproducibility

`generator.run` writes a `provenance` block into `meta.json`: a
`scenario_hash` over the physical inputs, the git revision, the
`gps-sdr-sim` version string, SHA-256 of the RINEX and nav files, the
random seed, and (for precise mode) the fit method and worst dense
post-fit residual. It also always records an `iq_integrity` report over
the first 2M samples of the output. Identical `(request, seed)` inputs
with a given `gps-sdr-sim` build reproduce the same `gpssim.bin`; the
impairment layer is separately bit-reproducible.

### What is **not** claimed

- Not "precise" in the geodetic sense: broadcast mode realigns rather
  than propagates, and precise mode is orbit-accurate over a 4 h arc with
  a few-ns clock.
- Not a calibrated error model: the atmospheric, multipath and
  receiver-clock models are physically shaped but not tuned to match any
  reference receiver or real recording.
- Not a full software receiver: acquisition + a single-epoch LS/WLS fix,
  no tracking loops, no carrier-phase / RTK processing.
- No real-SDR or hardware-in-the-loop guarantees from the test suite; the
  `xfail` cases are the only hardware-touching tests and are not run by
  default.

---

## License

MIT — see [LICENSE](LICENSE).
