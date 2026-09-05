# HTTP / WebSocket API reference

Base URL `http://127.0.0.1:8000`. Request and response bodies are JSON
unless noted. Long-running operations (`/api/generate`, `/api/transmit`,
`/api/live/start`, `/api/recording/replay`) stream **Server-Sent Events**
(`text/event-stream`, `data: {json}\n\n` frames). One endpoint is a
**WebSocket** (`/ws/events`).

**Roles.** When `API_KEYS_JSON` is unset every check is a no-op and all
endpoints are open. When it is set, endpoints marked **operator** require
an `operator` key in the `X-API-Key` header; everything else needs only a
valid key (`viewer` or `operator`). A transmit *stop* is deliberately not
restricted — any valid key may issue one, because stopping is
safety-positive.

---

## Health & constellation preview

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `GET` | `/api/health` | any | `{gps_sdr_sim, georinex, libiio, allow_tx}` — booleans for the built binary, the parser, the transmit native libs, and the master transmit switch. |
| `POST` | `/api/preview` | any | Constellation geometry for a point and time; **no signal is generated**. Body: `{lat, lon, alt, start_utc, rinex_path?, mask_deg?=5, ephemeris_mode?="broadcast", fallback_to_broadcast?=false}`. Returns `{satellites[], dop{}, warnings[]}`. `ephemeris_mode="precise"` runs the geometry against the loaded SP3 product (see [Precise ephemeris](#precise-ephemeris)); it fails **422** if no product is loaded or the epoch is outside coverage, unless `fallback_to_broadcast=true`. |
| `POST` | `/api/preview_track` | any | Satellite-geometry playback: the same geometry as `/api/preview` sampled every `step_s` across `duration_s`, so the UI can scrub az/el over time. Body adds `{duration_s?=300, step_s?=30}`. Returns `{frames[], warnings[]}`. Broadcast ephemeris only. |

---

## Ephemeris (broadcast)

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `POST` | `/api/rinex/upload?date=YYYY-MM-DD` | any | Upload a broadcast RINEX nav file instead of fetching one from a mirror. Multipart `file`. Cached under `DATA_DIR/rinex` for that date. |

The broadcast nav file is what actually drives `gps-sdr-sim`. Its
`toc`/`toe` are realigned to the requested start epoch so the binary's
validity-window check passes — a compatibility mechanism, **not** a
precise-ephemeris upgrade. Accuracy degrades as the requested start moves
away from the real broadcast epoch.

---

## Precise ephemeris

Load and inspect IGS SP3-c/d products. These endpoints are the analysis
surface (compare vs. broadcast); the same loaded product also backs
`ephemeris_mode: "precise"` on `/api/generate` and `/api/live/start`,
where `ephemeris_fit.py` fits it into the broadcast records that drive
`gps-sdr-sim` — see [`precise-ephemeris-design.md`](precise-ephemeris-design.md).

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `GET` | `/api/precise/status` | any | `{loaded}` or `{loaded, source, gps_week, interval_s, satellites[], epochs, coverage_start_utc, coverage_end_utc}`. |
| `POST` | `/api/precise/load` | **operator** | Load an SP3-c/d file. Body: `{path}` to a local file, **or** `{download:{gps_week, dow}}` (only attempted when `PRECISE_SP3_MIRRORS` is configured; otherwise **422**). Returns the same object as `status`. Audited as `precise_load`. |
| `POST` | `/api/precise/compare` | any | Per-PRN broadcast(realigned)-vs-precise state comparison at one epoch. Body: `{lat, lon, alt, start_utc, rinex_path?, mask_deg?=5, fallback_to_broadcast?=false}`. Returns `{epoch_utc, broadcast_source, precise_source, note, warnings[], rows[], summary{}}`. Each row: `prn, pos_delta_m, pos_delta_radial_m, pos_delta_along_m, pos_delta_cross_m, clock_delta_s, range_delta_m, pseudorange_delta_m, doppler_delta_hz, el_deg`. `summary`: `n, pos_delta_rms_m, range_delta_rms_m, doppler_delta_rms_hz`. PRNs absent from the SP3 product are omitted (noted in `warnings`), never silently substituted. **409** if no product is loaded. |

---

## Generate & inspect

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `POST` | `/api/generate` | any | **SSE.** Body: `{lat, lon, alt, start_utc, duration_s, sample_rate, sample_format, rinex_path?, route?, ephemeris_mode?="broadcast", fallback_to_broadcast?=false}`. `route` is a list of timestamped waypoints for a dynamic scenario. `ephemeris_mode="precise"` fits the loaded SP3 product into the broadcast records that drive generation; it fails **422** (before the stream opens) if no product is loaded, the epoch is outside the product's coverage ± 2 h, or a per-PRN fit will not converge below ~2 m — unless `fallback_to_broadcast=true`. Emits `progress` frames, then `done` (`{outdir, size_bytes, inspect{}, ephemeris_mode, warnings[]}}`) or `error`. `meta.json` records `config.ephemeris_mode` and `precise_fit` (per-PRN residual summary). |
| `POST` | `/api/receiver` | any | Body: `{outdir, marker?}`. Runs the from-scratch software receiver over the generated IQ, solves a least-squares fix, returns the fix and its error vs. `marker`. |
| `GET` | `/api/iqplot?outdir=&n=2000&offset=0` | any | Waveform slice + power spectrum. `offset` / `total_samples` drive the playback scrubber. |
| `GET` | `/api/correlation?outdir=&prn=` | any | Code-correlation curve (chips vs. amplitude) and the acquired Doppler for one PRN. |
| `GET` | `/api/lnav?outdir=&prn=` | any | Reconstructed and human-explained LNAV navigation message for one PRN. |

---

## Transmit — requires `ALLOW_TX=1`

Every endpoint here returns **403** unless `ALLOW_TX=1`. RF also requires
the per-request `confirm_isolated: true` flag (the UI's "isolated / cabled
setup confirmed" checkbox).

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `POST` | `/api/transmit` | **operator** | **SSE.** Replay a generated file to the SDR. Body: `{outdir｜iq_path, sample_rate, sample_format, lo_hz?, tx_gain_db?, uri?, tx_scale?, dry_run?, confirm_isolated}`. Emits `progress`/underflow stats then `done`/`error`. Audited. |
| `POST` | `/api/transmit/stop` | any | Body `{slot?}`. Sets the cancel event for that TX slot. |
| `POST` | `/api/live/start` | **operator** | **SSE.** Open-ended live session (segment-at-a-time regeneration). Body adds `{max_duration_s?, timeline?[], record?, track_prn?, ephemeris_mode?, fallback_to_broadcast?}`. `ephemeris_mode="precise"` behaves as on `/api/generate` (one fit covers the whole session; **422** on the same conditions). Emits `progress`, `spectrogram_*`, `cn0_db`, `timeline_step`, and `finished`. `max_duration_s` forces a stop and logs `auto_stop_timeout`. Audited. |
| `POST` | `/api/live/jog` | **operator** | Body `{slot, direction, distance_m}`. Shift the live position in the ENU frame. |
| `POST` | `/api/live/time_shift` | **operator** | Body `{slot, field, delta}`. Shift the live GPS time-of-week / clock offset. |
| `POST` | `/api/live/stop` | any | Body `{slot}`. Stop the live session. |

---

## Device (standby control link)

Opens a control connection to the SDR **without emitting RF** — RF starts
only when a transmit does. Requires `ALLOW_TX=1`.

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `POST` | `/api/device/connect` | **operator** | Body `{uri?}` (defaults to `DEVICE_URI`). Returns the device entry with `info`. **502** on connection failure. Audited as `device_connect`. |
| `POST` | `/api/device/disconnect` | **operator** | Body `{uri?}`. Audited as `device_disconnect`. |
| `GET` | `/api/device/status` | any | `{devices: [...]}` — currently connected control links. |

---

## Recording & replay

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `GET` | `/api/recording/list` | any | `{names[]}` of saved live-session recordings. |
| `GET` | `/api/recording/replay?name=&speed=1.0` | any | **SSE.** Replays the recorded SSE payloads with their original inter-event timing divided by `speed` (each gap capped at 5 s). No hardware involved. |

---

## Real receiver feedback

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `POST` | `/api/receiver/listen` | **operator** | Body `{mode:"udp"｜"serial", …}` — `{host, port}` for UDP or `{device, baud}` for serial. Starts the NMEA listener. |
| `POST` | `/api/receiver/stop_listen` | **operator** | Stop the listener. |
| `GET` | `/api/receiver/fix` | any | `{listening, fix}` — latest parsed GGA/RMC fix. |
| `POST` | `/api/receiver/inject` | **operator** | Body `{sentence}` — feed one NMEA line directly, no hardware. |

---

## Presets

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `POST` | `/api/trajectory/save` | **operator** | Body `{name, waypoints[]}`. Named waypoint route. |
| `GET` | `/api/trajectory/list` | any | `{names[]}`. |
| `GET` | `/api/trajectory/load?name=` | any | The saved route. |
| `POST` | `/api/scenario/save` | **operator** | Body `{name, config{}}`. An allowlist restricts saved fields to simulation parameters (never a device URI or arbitrary path). |
| `GET` | `/api/scenario/list` | any | `{names[]}`. |
| `GET` | `/api/scenario/load?name=` | any | The saved config. |

---

## Observability

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `GET` | `/api/audit?limit=200` | any | `{events[]}` — most recent audit records, newest first. |
| `WS` | `/ws/events` | any | Every audit event, pushed live to all connected clients. |
