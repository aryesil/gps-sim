# GPS L1 C/A Signal Simulator

A controlled, observable, and reproducible GNSS signal testing workbench.

Pick a place and time on a map, generate a baseband GPS L1 C/A IQ recording
with [`gps-sdr-sim`](https://github.com/osqzss/gps-sdr-sim), check it with a
built-in software receiver, decode its navigation message, and — optionally,
into a cable and never over the air — replay it to a PlutoSDR-class SDR.
Every RF-relevant action is written to a persistent audit log; a precise
orbit/clock (SP3) subsystem lets you quantify how far the broadcast-ephemeris
signal path sits from a precise reference.

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
- **Precise ephemeris (analysis)** — parse IGS SP3-c/d products, interpolate
  satellite ECEF position/velocity and clock, and compare the precise
  reference against the realigned broadcast column the IQ actually uses.
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
- **Honest about fidelity** — the signal is built from *broadcast* ephemeris
  whose epoch is realigned to the requested start. The precise-ephemeris
  subsystem exists to *measure* the resulting error, not to hide it. It is
  an analysis reference only — SP3 data never drives signal generation.

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
                    v
        broadcast RINEX nav (IGS/BKG mirror or upload)
                    |
          toc/toe realigned to requested start
                    |
                    v
              gps-sdr-sim  ------------->  raw IQ + meta.json
                    |                           |
                    |                  +--------+--------+
                    |                  |                 |
                    v                  v                 v
            software receiver     spectrum /        transmit (opt-in,
            least-squares fix     acquisition /     cabled only) / live
                    |             LNAV decode       segment regeneration
                    v
             fix error vs marker

        precise SP3 orbit/clock  --(analysis only)-->  compare vs the
        (IGS product, interpolated)                    realigned broadcast
                                                       column  ── NEVER
                                                       feeds gps-sdr-sim
```

**The signal is broadcast-only.** `gps-sdr-sim` consumes a RINEX-2
broadcast Keplerian nav file and nothing else — there is no
satellite-state interface into it. The precise subsystem reads the same
epoch and reports per-PRN deltas; it never produces IQ.

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

| Mode | Where it applies | What it means |
|------|------------------|---------------|
| **Broadcast** (default) | signal generation **and** analysis | The daily broadcast RINEX nav file. This is the only thing that reaches `gps-sdr-sim`. |
| **toc/toe realignment** | signal generation | Every satellite's `toc`/`toe`/`gps_week` is overwritten to the requested start so `gps-sdr-sim`'s validity-window check passes. It is a **broadcast-compatibility mechanism, not a new precise ephemeris** — orbit error grows as the requested start moves away from the real broadcast epoch. |
| **Precise (SP3)** | analysis only (`/api/preview`, `/api/precise/*`) | An IGS SP3-c/d orbit/clock product, interpolated (~10th-order Neville for position, analytic derivative for velocity, linear for the coarse SP3 clock). Used as the reference to quantify realignment error. **Never feeds signal generation.** |

Precise mode fails explicitly rather than silently degrading:

- No SP3 product loaded, or the requested epoch is outside the product's
  covered interval → **HTTP 422**, unless the caller passes
  `fallback_to_broadcast: true` (which then returns broadcast data with a
  `FELL BACK` warning).
- A PRN absent from the SP3 product is **omitted and named in `warnings`**,
  never silently replaced with a broadcast value.
- No extrapolation past the product's edge epochs.

`/api/precise/compare` reports, per PRN, the position delta (with
radial/along/cross components), clock delta, range and pseudorange delta,
Doppler delta, and elevation, plus an RMS summary — broadcast(realigned)
minus precise, at one epoch.

---

## The UI

Three pages, switched from the left sidebar.

**Channels** — one card per transmit channel (up to two):

- *Hardware config* — Device URI, LO frequency (default 1575.42 MHz),
  TX gain dB (default −50), *Dry run*, *Auto-stop after (s)*,
  *Record this session*.
- *Simulation config* — map marker, Start UTC, Duration, Sample rate,
  Format (int16 / int8), RINEX (`AUTO` or a path).
- *Ephemeris (analysis)* — Broadcast / Precise (SP3) selector plus a
  collapsible panel to load an SP3 file and run a compare-vs-broadcast.
  The hint states plainly: analysis only — generated IQ always uses
  broadcast.
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

1. Download an IGS SP3 product covering your scenario epoch.
2. In the card's precise panel, load it, set the same lat/lon/time, and
   **Compare vs broadcast**. Read the per-PRN and RMS position/range/Doppler
   deltas — this is how far the generated signal's geometry sits from a
   precise reference.

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
| Generate / inspect | `POST /api/generate` (SSE), `POST /api/receiver`, `GET /api/iqplot`, `GET /api/correlation`, `GET /api/lnav` |
| Transmit *(needs `ALLOW_TX=1`)* | `POST /api/transmit` (SSE, operator), `POST /api/transmit/stop`, `POST /api/live/start` (SSE, operator), `POST /api/live/jog|time_shift` *(operator)*, `POST /api/live/stop` |
| Device *(needs `ALLOW_TX=1`)* | `POST /api/device/connect|disconnect` *(operator)*, `GET /api/device/status` |
| Recording | `GET /api/recording/list`, `GET /api/recording/replay` (SSE) |
| Receiver feedback | `POST /api/receiver/listen|stop_listen|inject` *(operator)*, `GET /api/receiver/fix` |
| Presets | `POST/GET/GET /api/trajectory/save|list|load`, `POST/GET/GET /api/scenario/save|list|load` (saves are *operator*) |
| Observability | `GET /api/audit`, `WS /ws/events` |

Full request/response bodies, roles, and error codes: **[`docs/API.md`](docs/API.md)**.

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
| `PRECISE_SP3_MIRRORS` | `""` | Comma-separated SP3 download URL templates. Empty ⇒ `POST /api/precise/load` with `download` returns 422; local-path loads still work. |
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

**172 passed, 3 xfailed** as of this writing. Coverage spans ephemeris
alignment, GPS-time conversions, the SP3 parser and orbit/clock
interpolation, the broadcast/precise mode selector, geometry, acquisition,
the receiver solve, LNAV decode, the live session, transmit plumbing
(mocked hardware), the device link, audit, RBAC, NMEA parsing,
recording/replay, the scenario library, the WebSocket hub, and the
precise-ephemeris HTTP endpoints. The 3 `xfail` cases need real SDR
hardware. The suite uses fixtures and mocks only — no network downloads.

---

## Accuracy and limitations

- **The generated signal uses broadcast ephemeris, realigned.** Orbit
  error grows the further the requested start is from the real broadcast
  epoch — this is inherent to realigning `gps-sdr-sim`'s ephemeris and is
  exactly what the precise-ephemeris compare is for.
- **Precise (SP3) is analysis only.** It never drives IQ generation. The
  SP3 clock is the product's coarse clock, linearly interpolated; it is a
  reference, not a sub-nanosecond time source.
- **GPS L1 C/A only** — single band, single constellation. No GLONASS /
  Galileo / BeiDou, no L2/L5.
- **Two transmit slots** (`TX1`, `TX2`) maximum.
- **Some endpoints have limited UI wiring** (`/api/receiver`, `/api/lnav`,
  `/api/correlation`, `/api/preview_track`) — call them directly with
  `curl` if a panel isn't exposed on your build.
- The CDDIS RINEX mirror needs NASA Earthdata credentials; without them
  only the BKG mirror is effective. SP3 downloads require
  `PRECISE_SP3_MIRRORS` to be configured; otherwise load SP3 files by path.

---

## License

MIT — see [LICENSE](LICENSE).
