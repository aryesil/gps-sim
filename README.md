# GPS L1 C/A Signal Simulator

An interactive workbench for building, inspecting, verifying, and replaying
GPS L1 C/A signals. Pick a place and time on a map, generate a baseband IQ
recording with [`gps-sdr-sim`](https://github.com/osqzss/gps-sdr-sim), check
it with a built-in software receiver, then optionally stream it to a
PlutoSDR-class SDR — into a cable, never over the air.

The web UI is modelled on commercial multi-channel GNSS constellation
simulators: up to two independent transmit channels, a map-driven trajectory
builder, a persistent audit log, and a live "what the real receiver reports"
feedback panel. Reference screenshots of the layout it targets are in
[`Example-sim/`](Example-sim/).

> ⚠️ **RF safety first.** Transmit is disabled by default. Read
> [Safety](#safety) before enabling it. This tool is for signal generation
> and receiver testing; lawful, authorized operation is entirely the
> operator's responsibility.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Setup](#setup)
- [Running](#running)
- [The UI](#the-ui)
- [Typical workflows](#typical-workflows)
- [Configuration](#configuration)
- [HTTP API reference](#http-api-reference)
- [Access control (RBAC)](#access-control-rbac)
- [Safety](#safety)
- [Testing](#testing)
- [Known limitations](#known-limitations)
- [License](#license)

---

## What it does

### Core signal chain

| Stage | What happens | Module |
|-------|--------------|--------|
| **Ephemeris** | Fetches a daily broadcast RINEX nav file from IGS/BKG mirrors (or accepts an upload), realigns every satellite's `toc`/`toe` to the requested start epoch so `gps-sdr-sim`'s validity-window check passes. | `backend/ephemeris.py` |
| **Generate** | Builds the `gps-sdr-sim` argv (static point or dynamic user-motion route), runs it, streams progress. Output is a raw interleaved IQ file plus `meta.json`. | `backend/generator.py`, `backend/scenario.py` |
| **Inspect** | Reads back the IQ: power spectrum, per-PRN acquisition (Doppler + code phase), code-correlation curve, and a comparison of measured vs. predicted geometry. | `backend/inspector.py` |
| **Verify** | A from-scratch software receiver acquires the generated IQ, solves a least-squares position fix, and reports the error against the map marker. | `backend/receiver.py`, `backend/geometry.py` |
| **Decode** | Reconstructs and explains the LNAV navigation message (subframes, parity, clock/ephemeris parameters) for a chosen PRN. | `backend/lnav_display.py` |
| **Transmit** | Pushes IQ to a PlutoSDR-class device via `pyadi-iio`, with underflow tracking, a cancel event, and an optional per-session fail-safe timeout. | `backend/transmit.py` |

### Live transmit

`backend/live.py` runs an open-ended session that regenerates one short IQ
segment at a time, so position and time can be changed *while transmitting*:

- **Jog** the position N/S/E/W/up/down by a distance step (ENU frame).
- **Time-shift** the GPS time-of-week / clock offset applied to the segment.
- **Timeline scripting** — queue jog / time-shift steps to fire automatically
  at `t + N` seconds into the run.
- **Fail-safe timeout** — `max_duration_s` forces a stop and logs
  `auto_stop_timeout`, so a forgotten transmit can't run indefinitely.
- **Live spectrogram** waterfall and **C/N0 trend** chart streamed over SSE.
- **Session recording** — capture the exact SSE payload sequence to a JSONL
  file and **replay** it later at a chosen speed with no hardware attached.

### Operations support

- **Persistent audit log** (`backend/audit.py`) — every RF-relevant action
  (`transmit_start`, `manual_stop`, `auto_stop_timeout`, `timeline_step`,
  `recording_saved`, …) is appended as one JSON line to `logs/audit.jsonl`.
  Survives a server restart and a closed browser tab.
- **Multi-operator live view** (`backend/ws_hub.py`) — every audit event is
  also broadcast over a `/ws/events` WebSocket, so a second operator's tab
  reflects activity in real time without polling.
- **Real receiver feedback** (`backend/receiver_feed.py`, `backend/nmea.py`)
  — listen to a physical GNSS receiver over UDP or serial, parse its NMEA
  `GGA`/`RMC` sentences, and show "what real hardware reported" next to
  "what the simulator commanded".
- **Scenario library** (`backend/scenario_lib.py`) — save/load a channel's
  whole config by name. An allowlist keeps presets to simulation parameters
  only (never a device URI or an arbitrary file path).
- **Trajectory builder** (`backend/trajectory.py`) — draw a waypoint route on
  a map, save it by name, apply it to a channel as a dynamic scenario.
- **Role-based access control** (`backend/auth.py`) — optional API-key roles
  (`operator` / `viewer`); off entirely when no keys are configured.

---

## Architecture

```
backend/
  app.py            FastAPI app: all HTTP + WebSocket endpoints, SSE streams,
                    the two TX slots (TX1/TX2), request wiring
  config.py         env-var config, constants, data/out/log dir creation
  ephemeris.py      RINEX download, parse, epoch realignment, RINEX-2 writer
  scenario.py       ScenarioRequest dataclass, gps-sdr-sim argv builder
  generator.py      runs gps-sdr-sim (full run + short live segments)
  geometry.py       WGS84 / ECEF / ENU, constellation geometry
  inspector.py      IQ read, spectrum, C/A acquisition, correlation, compare
  receiver.py       least-squares position solve from generated IQ
  lnav_display.py   LNAV subframe reconstruction + human-readable explain
  live.py           LiveSession: segment-at-a-time regeneration, jog, time-shift
  transmit.py       pyadi-iio streaming, underflow tracking, cancel
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
  pages.js          page switching, boot wiring
  channels.js       channel cards: config, start/stop, timeline, scrubber,
                    scenario save/load, replay
  map.js            Leaflet map + marker (per channel)
  trajectory.js     waypoint route editor
  live.js           SSE pump for a running (live) transmit
  plots.js skyplot.js iqplot.js   spectrogram, sky plot, IQ / spectrum plots
  log.js            audit-log merge, /ws/events client, receiver-feed panel
  app.js            API-key prompt, fetch() X-API-Key injection
```

**Transport choices:** long-running generate / transmit / replay operations
stream progress as Server-Sent Events (`text/event-stream`). The multi-
operator shared feed is a WebSocket (`/ws/events`). Everything else is plain
JSON over HTTP.

**Concurrency:** at most two transmit slots, `TX1` and `TX2`. Each start
acquires a slot; the SSE generator releases it in a `finally`. Audit appends
are serialized by a lock so concurrent channels can't tear a line.

**Tech stack:** Python ≥ 3.10, FastAPI + uvicorn, NumPy, `georinex`,
`pyadi-iio` (transmit only), `gps-sdr-sim` (C, built from source). Frontend is
dependency-free vanilla JS plus Leaflet from a CDN.

---

## Setup

```sh
./scripts/setup.sh
```

`setup.sh`:

1. Clones and compiles `gps-sdr-sim` into `gps-sdr-sim/`.
2. Creates `.venv` and installs this package with dev extras (`pip install -e ".[dev]"`).
3. **Transmit path only:** builds `libiio` (v0.25) and `libad9361-iio`
   (v0.4.0) from source *into the venv's own prefix* — no package manager
   ships a usable `libiio`, and nothing is written to a system directory.
   `rm -rf .venv` removes them cleanly.

**Prerequisites** (only needed for the transmit path's native libs): `cmake`,
a C compiler (Xcode Command Line Tools on macOS / `build-essential` on
Debian), and `libusb` (`brew install libusb` / `apt install libusb-1.0-0-dev`).

Without them `setup.sh` still finishes — generate, inspect, and the internal
receiver check all work; only PlutoSDR-class hardware transmit needs `libiio`.

---

## Running

```sh
./scripts/run_server.sh
```

Then open <http://127.0.0.1:8000>.

`run_server.sh` activates `.venv`, points `DYLD_LIBRARY_PATH` /
`LD_LIBRARY_PATH` at the venv's `lib/` (so `pyadi-iio`'s `ctypes` finds the
locally-built libs), and starts uvicorn. Override host/port with `HOST=` /
`PORT=`; extra args pass through to uvicorn (e.g. `--reload`).

To enable transmit endpoints:

```sh
ALLOW_TX=1 ./scripts/run_server.sh
```

---

## The UI

Three pages, switched from the left sidebar.

### Channels

One card per transmit channel (add up to two). Each card has:

- **Hardware config** — Device URI, LO frequency (default 1575.42 MHz),
  TX gain dB (default −50), *Dry run* (exercise the whole path with no RF),
  *Auto-stop after (s)*, *Record this session*.
- **Simulation config** — map marker (click to place), Start UTC, Duration,
  Sample rate, Format (int16 / int8), RINEX (`AUTO` to fetch, or a path).
- **Scenario library** — save the current config under a name, reload it
  later from the dropdown.
- **Timeline** — add `jog` / `time_shift` steps that fire at `t + N` s.
- **Inspect panels** — IQ waveform + spectrum with a **playback scrubber**
  across the whole file, spectrogram waterfall, C/N0 trend, sky plot, and
  an LNAV decode for a selected PRN.
- **Live manipulation** — jog buttons with a distance step, GPS time-of-week
  shift, while a live session is running.
- **Start / Stop**, plus a type-to-confirm gate and the mandatory
  "isolated / cabled setup confirmed" checkbox.

**Start All** starts every configured channel.

### Trajectory Builder

Draw a waypoint route on the map, edit the point table, **Save as…** a named
trajectory, **Open** an existing one, **Apply to channel** to attach it as a
dynamic (moving) scenario for the next generate/live run.

### Log

- **Real Receiver Feedback** — choose UDP or serial, enter `host:port` or
  `/dev/ttyUSB0:9600`, **Start listening**. The readout shows the latest
  parsed fix (lat/lon/alt, sats, HDOP for GGA; status/speed for RMC).
- **Event list** — the persistent audit trail (`/api/audit`) merged with the
  live `/ws/events` WebSocket feed, newest first.

---

## Typical workflows

### Generate and verify (no RF)

1. On a channel card, click the map to set the marker, set Start UTC to now,
   Duration 300 s, 2.6 Msps, int16, RINEX `AUTO`.
2. **Generate.** Watch progress; when done, the inspect table shows measured
   vs. predicted Doppler / code phase per PRN.
3. Scrub the IQ plot, check the spectrogram and sky plot.
4. Run the internal receiver check (see API note below) — expect a fix within
   ~100 m of the marker.

### Cabled hardware replay

1. Start the server with `ALLOW_TX=1`.
2. Generate a scenario as above.
3. Connect the SDR TX port to the receiver antenna port through **≥ 40 dB**
   in-line attenuation. Cabled or shielded only.
4. On the card: set Device URI, LO 1575.42 MHz, rate 2.6 Msps, TX gain
   −50 dB, tick "isolated / cabled setup confirmed", **Start**.
5. Record TTFF, reported position vs. marker, and sustained underflow count.

### Live spoof walk

1. Start a **live** session (position/time editable while running).
2. Use the jog buttons, or pre-load a **Timeline** so the position walks
   automatically at set times.
3. Tick **Record this session**; afterwards, replay it from the dropdown at
   1× / 2× / … with no hardware.

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
| `OUT_DIR` | `./out` | Generated IQ + `meta.json`, recordings. Also served at `/out`. |
| `LOG_DIR` | `./logs` | `audit.jsonl`. |
| `GPS_SDR_SIM_BIN` | `./gps-sdr-sim/gps-sdr-sim` | Path to the built binary. |
| `API_KEYS_JSON` | `""` | JSON `{"<key>": "operator"｜"viewer"}`. Empty ⇒ auth disabled. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | uvicorn bind (via `run_server.sh`). |

---

## HTTP API reference

Base URL `http://127.0.0.1:8000`. SSE endpoints return `text/event-stream`
with `data: {json}\n\n` frames. Endpoints marked �followed by a role require
that role *only when `API_KEYS_JSON` is set*.

### Health & preview

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/health` | libiio availability, gps-sdr-sim version, disk, deps. |
| `POST` | `/api/preview` | Constellation preview for a lat/lon/time (no generation). |
| `POST` | `/api/preview_track` | Acquisition preview for one PRN. |

### Ephemeris

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/api/rinex/upload?date=YYYY-MM-DD` | Upload a nav file instead of fetching. |

### Generate & inspect

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/api/generate` | **SSE.** Body: `lat, lon, alt, start_utc, duration_s, sample_rate, sample_format, rinex_path?, route?`. Emits `progress`, then `done` with `outdir`, `size_bytes`, and the inspect table (or `error`). |
| `POST` | `/api/receiver` | Body: `{outdir, marker?}`. Runs the internal software-receiver solve; returns the fix and error vs. marker. |
| `GET` | `/api/iqplot?outdir=&n=2000&offset=0` | Waveform slice + spectrum; `offset`/`total_samples` drive the scrubber. |
| `GET` | `/api/correlation?outdir=&prn=` | Code-correlation curve (chips vs. amplitude) + Doppler. |
| `GET` | `/api/lnav?outdir=&prn=` | Reconstructed + explained LNAV message. |

### Transmit (needs `ALLOW_TX=1`)  🔑 operator

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/api/transmit` | **SSE.** Replay a generated file. Body: `outdir｜iq_path, sample_rate, sample_format, lo_hz?, tx_gain_db?, uri?, tx_scale?, dry_run?, confirm_isolated`. |
| `POST` | `/api/transmit/stop` | Body `{slot?}`. Any role. Sets the cancel event. |
| `POST` | `/api/live/start` | **SSE.** Open-ended live session. Body adds `max_duration_s?`, `timeline?[]`, `record?`, `track_prn?`. Emits progress, `spectrogram_*`, `cn0_db`, `timeline_step`, `finished`. |
| `POST` | `/api/live/jog` | Body `{slot, direction, distance_m}`. |
| `POST` | `/api/live/time_shift` | Body `{slot, field, delta}`. |
| `POST` | `/api/live/stop` | Body `{slot}`. Any role. |

### Recording & replay

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/recording/list` | Recorded session names. |
| `GET` | `/api/recording/replay?name=&speed=1.0` | **SSE.** Replays recorded payloads with original inter-event timing ÷ `speed` (gap capped at 5 s). |

### Real receiver feedback  🔑 operator

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/api/receiver/listen` | Body `{mode: "udp"｜"serial", …}` (`host,port` or `device,baud`). |
| `POST` | `/api/receiver/stop_listen` | Stop the listener. |
| `GET` | `/api/receiver/fix` | `{listening, fix}` — latest parsed NMEA fix. |
| `POST` | `/api/receiver/inject` | Body `{sentence}` — feed one NMEA line directly (no hardware). |

### Presets

| Method | Path | Notes |
|--------|------|-------|
| `POST` / `GET` / `GET` | `/api/trajectory/save｜list｜load` | Named waypoint routes. Save is 🔑 operator. |
| `POST` / `GET` / `GET` | `/api/scenario/save｜list｜load` | Named config presets (field allowlist). Save is 🔑 operator. |

### Observability

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/audit?limit=200` | Most recent audit records, newest first. |
| `WS` | `/ws/events` | Every audit event, pushed live to all clients. |

---

## Access control (RBAC)

Off by default. Set `API_KEYS_JSON` to switch it on:

```sh
API_KEYS_JSON='{"s3cret-op":"operator","read-only":"viewer"}' ALLOW_TX=1 ./scripts/run_server.sh
```

- **operator** — may start/stop transmit, jog, script timelines, listen for
  receiver feedback, save presets.
- **viewer** — read-only: status, audit, replay. May also *stop* a transmit
  (a stop is safety-positive — any valid key can issue one).

The browser sends the key as the `X-API-Key` header; the UI's 🔑 button
stores it in `localStorage` and `app.js` attaches it to every `fetch`.
When `API_KEYS_JSON` is empty, every check is a no-op and behaviour is
identical to a keyless rig.

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
- Use `max_duration_s` (Auto-stop) so a transmit can't run unattended.

This software is provided for signal-generation and receiver-testing
purposes. Responsibility for lawful, authorized operation rests entirely with
the operator; the authors accept no liability for misuse.

---

## Testing

```sh
.venv/bin/pytest -q
```

~117 tests covering ephemeris alignment, geometry, acquisition, the receiver
solve, LNAV decode, the live session, transmit plumbing (mocked hardware),
audit, RBAC, NMEA parsing, recording/replay, the scenario library, and the
WebSocket hub. A few hardware-dependent cases are `xfail` without a device.

---

## Known limitations

- **`/api/receiver`, `/api/lnav`, `/api/correlation`, `/api/preview_track`**
  are fully implemented and tested but have limited UI wiring — call them
  directly with `curl` if a panel isn't exposed on your build, e.g.
  `curl -s localhost:8000/api/receiver -H 'Content-Type: application/json' -d '{"outdir":"<dir>","marker":[lat,lon,alt]}'`.
- **GPS L1 C/A only** — single band, single constellation. No GLONASS /
  Galileo / BeiDou, no L2/L5.
- **Two transmit slots** (`TX1`, `TX2`) maximum.
- Positional accuracy degrades the further the requested start is from the
  real broadcast epoch (inherent to realigning `gps-sdr-sim` ephemeris).
- The CDDIS RINEX mirror needs NASA Earthdata credentials; without them only
  the BKG mirror is effective.

---

## License

MIT — see [LICENSE](LICENSE).
