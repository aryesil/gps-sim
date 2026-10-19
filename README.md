# GNSS Signal Simulator

A controlled, observable, and reproducible multi-constellation GNSS signal testing workbench.

Pick a place and time on a map, generate baseband IQ recordings across multiple GNSS bands, check them with a built-in software receiver, decode broadcast navigation messages directly from IQ, and — into a cable and never over the air — replay them live to a PlutoSDR-class SDR, a Tezuka LibreSDR or a Nuand bladeRF. Every RF-relevant action is written to a persistent audit log.

**High-Performance Native C++ Engine:**  
Powered by a built-in C++20 engine that renders **GPS (L1 C/A, L2C, L5), GLONASS (G1, G2), Galileo (E1, E5a), BeiDou (B1I, B2a), QZSS (L1 C/A, L5), SBAS (L1), and NavIC (L5)** from a correlated multi-constellation geometry (synchronized receive epoch so cross-system acquisition lines up). Supports completely arbitrary constellation selection (Galileo-only, BeiDou-only, GPS-free multi-GNSS), per-satellite land-mobile-satellite channel fading (seeded or cryptographically unpredictable ChaCha20), and multi-band RF routing (1575.42 MHz L1 group, 1602 MHz GLONASS FDMA, 1227.60 MHz L2, 1176.45 MHz L5).

Two ways to produce the transmitted IQ: the **host (PC) path** renders it in software and streams it over libiio, and the **FPGA path** (`kind: tezuka_gnss`) only sends compact command words to a Tezuka LibreSDR whose programmable logic generates GPS L1 C/A, Galileo E1, BeiDou B1I and SBAS L1 samples itself, at up to 61.44 Msps. See [Transmit paths](#transmit-paths).

Signal generation runs from broadcast ephemeris by default; in **precise mode** an IGS SP3 orbit/clock product drives satellite propagation directly at runtime across all constellations (GPS, GLONASS, Galileo, BeiDou, QZSS from an MGEX SP3 product), or fits into broadcast records with sub-metre residual error.

> ⚠️ **RF safety first.** Transmit is disabled by default. Read
> [Safety](#safety) before enabling it. Lawful, authorized operation is
> entirely the operator's responsibility.

---

## Table of contents

- [Features](#features)
- [Why this project](#why-this-project)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Transmit paths](#transmit-paths)
- [FPGA GNSS engine](#fpga-gnss-engine)
- [Timing and frequency accuracy](#timing-and-frequency-accuracy)
- [Ephemeris and accuracy modes](#ephemeris-and-accuracy-modes)
- [The UI](#the-ui)
- [Typical workflows](#typical-workflows)
- [API](#api)
- [Configuration](#configuration)
- [Access control](#access-control)
- [Safety](#safety)
- [Testing](#testing)
- [Scripts](#scripts)
- [Accuracy and limitations](#accuracy-and-limitations)
- [Known limitations / validation](#known-limitations--validation)
- [License](#license)

---

## Features

- **Generate** — static point or dynamic waypoint route → native C++20 engine (`libgnsssynth`) → raw interleaved IQ + `meta.json`, with streamed progress.
  - **Flexible Constellations** — arbitrary constellation selection without mandatory GPS (run pure Galileo E1, BeiDou B1I, GLONASS, QZSS, SBAS, or any combination).
  - **Multi-Band & Quantisation** — supports L1 (1575.42 MHz / 1602 MHz GLONASS), L2 (1227.60 MHz), and L5 (1176.45 MHz) with `int16`, `int12`, and `int8` complex sample formats.
  - **Land-Mobile-Satellite Channel** — three-state (line-of-sight / shadowed / blocked) Loo model: elevation-dependent state probabilities (`open`, `rural`, `suburban`, `urban`), log-normal shadowing of the direct path, and Rayleigh diffuse multipath with a Jakes Doppler spread ($v / \lambda$). Advances with distance travelled along waypoint routes (or static drift). Blockage is shared across satellite bands; diffuse fading is carrier-independent. Randomness via ChaCha20: `seeded` (reproducible) or `keyed` (256-bit CSPRNG key).
- **Inspect** — power spectrum (every RF band overlaid on an absolute-Hz axis), per-PRN acquisition (Doppler + code phase), per-satellite signal-power panel across **all** constellations (elevation taper + channel gain, re-evaluated as the IQ scrubber moves), code-correlation curves, and measured-vs-predicted geometry. Status panels render at display pixel density.
- **Verify** — a from-scratch software receiver acquires the generated composite IQ, solves a multi-GNSS least-squares fix (LS / WLS), and reports the position error against ground truth.
- **Standalone Blind Nav Decode** — reconstructs broadcast navigation messages directly from IQ without external ephemeris files:
  - **GPS L1 C/A:** LNAV subframes (50 bps), parity checks, ephemeris/clock parameters.
  - **Galileo E1:** I/NAV pages (250 sym/s, rate-1/2 FEC, 10-word reconstructor) compliant with Galileo OS SIS ICD Issue 2.0/2.1.
  - **BeiDou B1I:** D1 navigation frames (50 bps) with BCH (15,11,1) decoding, deinterleaving, and NH20 secondary code wipe-off.
  - **SBAS L1:** RTCA DO-229 / ICAO Annex 10 compliant 500 sym/s stream, rate-1/2 K=7 convolutional FEC, direct-form CRC-24Q, Type 9 GEO navigation messages.
- **Transmit (opt-in)** — push IQ to a PlutoSDR-class device (`pyadi-iio`), a Tezuka LibreSDR or a Nuand bladeRF (`libbladeRF`), selectable per channel (`kind: pluto`, `tezuka`, `tezuka_gnss`, `pluto_cs8`, `pluto_cs4` or `bladerf`), with underflow tracking, thread cancellation, and fail-safe auto-stop timeouts. An RF frontend layer plans LO-offsets (`DISABLED` / `MANUAL` / `AUTO`) between the AD9361 LO and target frequency, auto-triggering TX quadrature calibration.
  - **Tezuka automatic sample format** — `kind: tezuka` picks the TX format from the sample rate: 16-bit complex up to 10 Msps, 8-bit complex (cs8, needs the Tezuka image built with `CS8TX=1`) from 10 up to 27.5 Msps, and refuses anything above. The libiio network link of a LibreSDR sustains roughly 55-60 MB/s, which measured about 14 Msps for 16-bit and 27-30 Msps for 8-bit samples; the defaults leave headroom below that. 8-bit mode drives TX1 only. `pluto_cs8` forces 8-bit at any rate. Above 27.5 Msps `tezuka` can use 4-bit samples (cs4, up to 40 Msps) only with `TX_CS4_ENABLED=1` and a Tezuka FPGA image built with `CS4TX=1`; any other image plays cs4 back as noise. `pluto_cs4` forces cs4.
  - **Tezuka FPGA GNSS engine** — `kind: tezuka_gnss` does not stream IQ at all; see [Transmit paths](#transmit-paths).
  - **Thermal management** — the AD9361 runs in TDD mode while transmitting (ENSM `tx` state, RX path off), which measured 26-40 °C cooler than FDD on a LibreSDR without heatsink, and a die-temperature guard stops the stream above `TX_MAX_TEMP_C`.
- **Live Session (`Start Now`)** — direct in-memory live streaming to SDR with zero disk wait:
  - **Strict Time Continuity** — GPS transmit time advances strictly forward ($t = k \times 1.0\text{ s}$ for $k=0, 1, 2, \dots$); time never rewinds or replays.
  - **Live Controls & Scripting** — jog receiver coordinates and shift GPS time on-the-fly while transmitting; scheduled timeline scripting (`jog` and `time_shift` at $t + N\text{ s}$).
  - **Real-Time Diagnostics** — live spectrogram waterfall and C/N0 trend streamed via Server-Sent Events (SSE). Record SSE streams to disk and replay without SDR hardware.
  - **Live GPS Time Sync** — `Live: now` schedules the start: it warms up the generator, picks the start time from the measured first-segment generation time and holds the transmitter until that instant, so the GPS time on air matches real-time GPS for smartphone and A-GPS testing. The start time uses the host clock corrected by an SNTP offset (no admin rights, the system clock is not changed), so a PC clock that is off by seconds is handled; see [Timing and frequency accuracy](#timing-and-frequency-accuracy).
- **Precise Ephemeris** — parse IGS SP3-c/d products with multi-GNSS Lagrange/Neville orbit and clock interpolation:
  - *Analysis:* compare precise ground truth against realigned broadcast orbits (`/api/precise/compare`).
  - *Generation:* MGEX SP3-driven generation on the native engine, or least-squares Keplerian fit (4 h arc, sub-metre residuals) into broadcast records. Auto-downloads free IGS/MGEX rapid products for scenario epochs.
- **Operations Support** — persistent JSONL audit trail, multi-operator `/ws/events` WebSocket feed, real-receiver NMEA feedback (UDP/serial), named presets, and optional API-key RBAC.

---

## Why this project

Many signal generators emit raw samples without self-validation tools. This workbench wraps generation with everything a test rig needs:

- **Observable** — every stage is inspectable (spectrum, acquisition, receiver fix, decoded nav frames), and every RF transmission is audited.
- **Reproducible** — scenarios and trajectories are saved by name; a live session's exact event stream can be recorded and replayed bit-for-bit.
- **Honest about fidelity** — broadcast mode realigns ephemeris epochs and reports the resulting orbit drift against precise products rather than hiding it. Precise mode propagates satellites directly from MGEX SP3 products or records per-PRN fit residuals in `meta.json`.
- **Standards-Compliant** — signal coding, navigation structures, and FEC polynomials strictly adhere to official ICDs (IS-GPS-200, Galileo OS SIS ICD, BeiDou ICD, RTCA DO-229 / ICAO Annex 10).

---

## Quick start

```sh
./scripts/setup.sh          # sets up .venv, builds native SDR libraries (Pluto/bladeRF)
./scripts/run_server.sh      # serves http://127.0.0.1:8000
```

On Windows, `run_server.bat` starts the same server.

`setup.sh` creates `.venv`, runs `pip install -e ".[dev]"`, and builds native SDR libraries into the virtual environment prefix (`libiio` + `libad9361-iio` for Pluto, `libbladeRF` for bladeRF). Specify `SDR_KIND=pluto|bladerf|both` to skip interactive prompts.

`run_server.sh` activates `.venv`, sets dynamic library paths, and launches Uvicorn. Pass `ALLOW_TX=1 ./scripts/run_server.sh` to enable hardware transmission.

---

## Architecture

```
                 map marker / trajectory route + time
                                  |
       +--------------------------+--------------------------+
       | ephemeris_mode = broadcast                          | ephemeris_mode = precise
       v                                                     v
  broadcast RINEX nav                                   IGS MGEX SP3 orbit/clock
  (IGS/BKG mirror or upload)                            (auto-downloaded rapid/final)
       |                                                     |
  realigned / real broadcast epoch                           | direct state propagation
  (toe/toc grid, IODnav preserved)                           | or 4 h Keplerian fit
       +--------------------------+--------------------------+
                                  v
                       Native C++20 Signal Engine
                     (libgnsssynth via ctypes ABI)
                                  |
           +----------------------+----------------------+
           |                                             |
           v                                             v
     File Generation                               Live Session (Start Now)
  (multi-band IQ + meta.json)                   (in-memory ring buffer, 1 s chunks)
           |                                             |
     +-----+-----+                                 +-----+-----+
     v           v                                 v           v
  Inspect     Software Receiver                 SDR Transmit  SSE Diagnostics
(spectrum,   (LS/WLS fix, blind                 (Pluto/bladeRF, (waterfall, C/N0,
 acquisition, nav decode: LNAV,                  cabled only)    timeline steps)
 correlation) I/NAV, B1I, SBAS)

 Live session, FPGA path (kind = tezuka_gnss):
  engine.plan (per-channel parameters + nav bits) -> backend/fpga/cmdgen.py (command words)
    -> libiio DMA -> gs_wrap on the Zynq PL (12 pipes, ~250 MHz) -> AD9361 DAC
```

### Repository Structure

```
backend/
  app.py            FastAPI application: HTTP, WebSocket, SSE streams, TX slot management
  config.py         Configuration constants, directory management, environment variables
  scenario.py       ScenarioRequest dataclass, route interpolators, engine parameters
  geometry.py       WGS84 ellipsoid normals, ECEF / ENU conversions, transmit-time solver
  gpstime.py        GPSTime representation, leap-second tables, UTC <-> GPS conversions
  signals.py        Per-system signal parameters (carriers, chip rates, code lengths, BOC, secondary codes)
  inspector.py      IQ reader, FFT spectrum analyzer, multi-system acquisition and correlation
  auth.py           API-key role-based access control (RBAC)
  ephem/            Ephemeris handling:
                    ephemeris.py (RINEX parse, realign, IODnav derivation, real-epoch preservation)
                    ephemeris_source.py (broadcast vs precise selector, state evaluators)
                    ephemeris_fit.py (SP3 track -> Keplerian broadcast record fitting)
                    precise.py (SP3-c/d parser, Neville orbit and clock interpolation)
  synth/            Native C++ synthesis engine:
                    engine.py (Python orchestration, geometry -> SV specs -> multi-band planning)
                    bands.py (band registry: L1 1575.42 MHz, GLONASS G1 1602 MHz, L2, L5)
                    signal_engine.py (top-level run dispatcher)
                    _lib.py (ctypes bindings for libgnsssynth)
                    fading.py (Loo channel models, Jakes Doppler, ChaCha20 PRNG)
                    sbas.py, glonass.py, fs_policy.py (helpers for FDMA and sample rates)
                    native/ (C++20 source: PRN generators, NCO mixers, quantizers, LMS fading)
  analysis/         Receiver verification and navigation message decoders:
                    receiver.py (least-squares multi-GNSS solver, pseudorange extraction)
                    inav_encode.py, inav_decode.py (Galileo E1 I/NAV FEC, framing, CRC-24Q)
                    fnav_encode.py, fnav_decode.py (Galileo E5a F/NAV framing and SISA mapping)
                    sbas_encode.py, sbas_decode.py (RTCA DO-229 SBAS L1 framing, MT 1-26, CRC-24Q)
                    lnav_encode.py (GPS LNAV), cnav_encode.py, bds_d1_encode.py, bcnav2_encode.py,
                    glo_str_encode.py, navic_encode.py and the matching *_decode.py; utc_params.py (GPS-UTC block)
                    nav_encoders.py (unified navigation message dispatch)
                    _viterbi.py, _crc.py, _bit_utils.py (convolutional FEC and CRC routines)
  models/           Atmosphere (Klobuchar, Saastamoinen), receiver clock, multipath, RF impairments
  rf/               Hardware transmission:
                    transmit.py (SDR streaming, buffer management, underflow tracking)
                    device.py (control links), dual_tx.py (TX1/TX2 arbitration)
                    backends/ (ad9361.py via pyadi-iio, ad9361_cs8.py 8-bit TX for Tezuka,
                              tezuka.py picks 16/8-bit from the sample rate, bladerf.py via libbladeRF)
                    frontend/ (LO-offset planner, automatic TX quadrature calibration)
  fpga/             Host side of the FPGA GNSS engine (kind tezuka_gnss):
                    cmdfmt.py (frozen command-word format), cmdgen.py (BandPlan -> per-block channel records),
                    select.py (elevation-based channel selection for the engine capacity), model.py
                    (bit-exact fixed-point reference model), roms.py (code ROM images), stream.py (DMA buffers),
                    device.py (register access over ssh + devmem), tx.py (GnssTx: prefill, enable, poll counters)
  session/          live.py (LiveSession: in-memory streaming, time progression, jog, timeline),
                    play_schedule.py (schedule-then-play Start Now), clock_sync.py (SNTP host clock offset),
                    lead_calib.py (measured generation / play latencies)
  rf/air.py         Scales, saturates and packs 8-/4-bit TX samples in the generator worker processes

fpga/               GNSS engine RTL and tooling (Verilog):
  rtl/              gs_wrap (AXI-Lite, async FIFOs), gs_top, gs_pipe, gs_loader, gs_seq, gs_scan, gs_reduce, ...
  tb/, sim/         Testbenches and fpga/sim/run_xsim.sh (Vivado xsim)
  synth/            ooc.sh (out-of-context synthesis, xc7z020), check_init.sh, gui_show.tcl
  mem/              Code ROM and sin/cos table images (generated by scripts/fpga_vectors.py)
  stub/             gs_wrap_stub.v (synthesis stub)

scripts/            setup.sh, run_server.sh / run_server.bat, FPGA bring-up and live-pipeline check tools
                    (see Scripts below)

frontend/           Vanilla JavaScript and CSS (no build step required):
  index.html        Single-page application (Channels, Trajectory Builder, Log)
  js/channels.js    Channel configuration, generation, SDR streaming, advanced sub-tabs
  js/compare.js     Interactive broadcast vs precise ephemeris visualizer
  js/skyplot.js     Multi-constellation skyplot and satellite selection
  js/iqplot.js      Waveform scrubber, multi-band spectrum, and acquisition waterfall
```

---

## Transmit paths

Both paths are started with `POST /api/live/start` (the UI's **Start Now**). The SDR kind is the `kind` field.

| | `kind: "tezuka_gnss"` (FPGA engine) | `kind: "tezuka"` (host IQ over libiio) |
|---|---|---|
| Who generates the samples | The Zynq PL on the LibreSDR (`gs_wrap`); the host sends command words through the DMA | The PC (native C++ engine); IQ is sent over the network as cs16 / cs8 / cs4 |
| Signals | GPS L1 C/A, Galileo E1 (B and C), BeiDou B1I, SBAS L1 (PRN 120..150, through the C/A ROM) | Everything the software engine supports (`backend/synth/bands.py`, `signals.py`): GPS L1/L2C/L5, GLONASS G1/G2, Galileo E1/E5a, BeiDou B1I/B2a, QZSS L1/L5, SBAS L1, NavIC L5 |
| Sample rates | Tested at 20, 30, 30.72, 40, 50 and 61.44 Msps (the UI lists up to 61.44) | Limited by the libiio link, see [Accuracy and limitations](#accuracy-and-limitations) |
| Fading (`fading` field) | Not supported: `backend/fpga/cmdgen.py` raises `NotImplementedError` | Supported |
| Needs on the board | A Tezuka image with the GNSS bitstream (`libre_gnss`, see the [Tezuka fork](https://github.com/aryesil/ZYNQsdr)) and ssh access as `root` for the `devmem` register path | Any supported image; cs8 / cs4 need the matching FPGA image |

Practical notes:

- **FPGA path gain.** Per-satellite amplitude falls as 1/sqrt(channels), so compensate with TX gain. The rule that worked on the bench is `-40 + 10*log10(nch/10)` dB (-40 dB at 10 channels, about -34 dB at 45), never above -30 dB; the level window is narrow. It is an empirical rule of thumb, not enforced by the code. The AGC reading is not an indicator at GNSS levels; judge by the receiver's C/N0 and PVT.
- **PC path sample rate.** Use a rate the AD9361 reaches exactly, e.g. `3.84e6`. `2.6e6` is clamped by the chip to 2599998 and the start fails with an error.
- **Board services.** On the stock Dashboard image `api_controller.sh`, `mqtt_pubd` and `pluto_stream` use a large share of the dual Cortex-A9 and lower the libiio ceiling; stop them before link-rate tests (the Dashboard stops working while they are down). Measured with them stopped: cs4 about 42.6 Msps and cs8 about 28.7 Msps; with them running about 28 and 22 Msps.
- **ssh access.** The FPGA path needs key-based ssh to the board (`GNSS_SSH_USER`, default `root`). The authorization is lost when the RAM-disk root filesystem is reflashed; run `device_persistent_keys` on the board after adding the key to keep it.
- **Duplex mode.** The backend puts the AD9361 in TDD while transmitting (`TX_TDD_MODE=1`). A board that boots in TDD stays in TDD afterwards; the FDD digital-loopback self-tests only run at 10 Msps or less.

## FPGA GNSS engine

`fpga/` holds the Verilog of the `gs_wrap` block (AXI-Lite window at `0x43C10000`, ID register `0x47530001`). The engine clock is the Zynq `FCLK2` at 250 MHz. 12 pipes each serve one channel per clock, so a sample takes `T = max(4, ceil(nch / 12))` clocks and `nch <= 12 * floor(250e6 / fs)` (capped at 64 channels). When a scenario has more channels than the rate allows, `backend/fpga/select.py` drops the lowest-elevation satellites first and keeps a floor per constellation where it can; the kept and dropped counts are reported in the live stream.

Verification flow (Vivado 2023.2 on the PC; set `VIVADO_BIN` if it is not in the default path of the scripts):

```sh
fpga/sim/run_xsim.sh tb_top                            # RTL testbenches: tb_loader, tb_pipe, tb_reduce, tb_scan, tb_seq, tb_sincos, tb_top, tb_wrap
PYTHONPATH=. python scripts/fpga_vectors.py case all   # regenerate golden vectors
PYTHONPATH=. python scripts/fpga_compare.py --systems GCE --fs 30e6 --blocks 2   # C++ mixer vs fixed-point model
fpga/synth/ooc.sh 4.0                                  # out-of-context synthesis at 250 MHz, reports in fpga/synth/reports/
fpga/synth/check_init.sh                               # post-synthesis BRAM init check
```

The bitstream itself is built from the separate `maia-sdr` project (`build_gnss.sh`, not part of this repository) and packaged into the Tezuka firmware. After an RTL change, check that the `gs_wrap` run log does not say `IPCACHE: Exporting cached entry`; the ADI IP cache can reuse a stale synthesis of a module reference.

Results on the board (LibreSDR, Zynq-7020): FDD digital loopback matches the host model at about -44 dB NRMSE (1 SV) and -53 dB (11 SV) with all counters 0; GPS+Galileo+BeiDou at 20, 30.72, 40, 50 and 61.44 Msps tracked 34-38 satellites on a Septentrio receiver and gave a fix. At 30.72 Msps and above a constant 16-block (one DMA group) engine underrun still appears at a random point early in a run; its cause is open. Check a rate with `scripts/fpga_bringup.py --tdd-counters` before an RF test.

## Timing and frequency accuracy

**Time.** `start_utc: "now"` stamps the scenario with the host UTC corrected by an SNTP offset (`LIVE_CLOCK_SYNC`, servers in `LIVE_NTP_SERVERS`; the PC clock is read, never set). A PC clock that is off by seconds is handled; without network access the offset is 0 and the stream reports `clock_source: none`. The live stream's `lead_report` carries `lag_s` (target about 0), `clock_offset_s` and `clock_source`. The FPGA start latency is learned from the round trip of the armed enable write (`GNSS_PLAY_LATENCY_S` is only the starting value). On the bench, receiver GPS time was within about 10 ms of UTC after this was added.

**Frequency.** The API field `xo_ppm` (also `DEVICE_XO_PPM`; positive = the oscillator runs fast) is written to the AD9361 `xo_correction` before the sample rate and LO are set, so it corrects both the sample clock and the carrier. The measured uncalibrated TX crystal error of the test board is about +3.5 ppm. With an external 10 MHz reference connected, the board's `gpsdo_boot.sh` disciplines the VCTCXO at boot and `xo_ppm` must be 0.

---

## Ephemeris and accuracy modes

Set via `ephemeris_mode` on `/api/generate`, `/api/live/start`, and `/api/preview` (`"broadcast"` default, or `"precise"`).

| Mode | Where it applies | Description |
|------|------------------|-------------|
| **Broadcast** (default) | Generation & analysis | Daily broadcast RINEX nav file. For epochs within ±4 hours of broadcast records, true ephemerides and original IODs are preserved. Outside that window, epochs are cleanly aligned to the start time on standard grid steps with derived IODnav parameters. |
| **Precise (SP3), analysis** | `/api/preview`, `/api/precise/compare` | Interpolated IGS SP3-c/d orbit/clock reference (~10th-order Neville polynomial for position, analytic derivative for velocity). Evaluates per-PRN radial/along/cross position deltas, clock offsets, and pseudorange errors. |
| **Precise (SP3), generation** | `/api/generate`, `/api/live/start` | Propagates satellite states directly from MGEX SP3 products at runtime across GPS, GLONASS, Galileo, BeiDou, and QZSS. When fitting into broadcast format, `ephemeris_fit.py` executes a least-squares solve over a 4 h arc with sub-metre residuals. |

When precise mode is enabled, the server auto-downloads the best available SP3 product for the scenario epoch across configured mirrors (`PRECISE_SP3_MIRRORS`: GFZ MGEX rapid `GBM0MGXRAP`, ESA, IGN, IGS). If no daily product is published yet (e.g. current day), the newest ultra-rapid product (`PRECISE_SP3_ULTRA_MIRRORS`) is used.

---

## The UI

The frontend is organized into three main views accessible from the sidebar:

### 1. Channels
Each channel card controls an independent transmission channel (up to two slots: TX1 and TX2):
- **Hardware Config:** Device URI, SDR Kind (Pluto, Tezuka auto 16/8-bit, Tezuka 8-bit only, Tezuka FPGA synth (default), or bladeRF), TX Slot, LO Frequency (with Auto resolution), TX Gain (−50 dB default; −30 dB is the highest value used in the bench tests), Dry Run, Auto-Stop timeout, Session Recording.
- **Simulation Config:** Map reference position, Start UTC, Duration, Sample Rate (`auto` resolves the lowest rate for the chosen constellations; manual presets are 2.6, 4, 5, 8, 10, 15, 20, 25 and 27.5 Msps, plus 30 to 61.44 Msps for the FPGA synth kind only), Format (`int16` / `int8`), RINEX path (`AUTO` or manual).
- **Constellation & Band Selector:** Freely toggle GPS, GLONASS, Galileo, BeiDou, QZSS, SBAS, and NavIC, and select RF bands. L1, L2 and L5 are independent checkboxes, none ticked by default: with none ticked every selected system uses its own native band (GPS L1, GLONASS G1, BeiDou B1I, ...), with any ticked only those bands are generated (a GLONASS selection then needs no L1 box, but L1 alone does not include G1). GPS is not mandatory.
- **Advanced Sub-Tabs:**
  - **SP3:** Manual SP3 loading and interactive broadcast-vs-precise comparison charts.
  - **RF Impairments:** CFO, sample clock drift, phase noise, IQ imbalance, DC offset, AWGN.
  - **Propagation:** Atmospheric models (Klobuchar / Saastamoinen), receiver clock dynamics, specular multipath.
  - **Signal Engine:** Sample rate overrides, quantization bits, land-mobile-satellite channel fading (environment, speed, seed/key).
- **Inspection & Diagnostics:** Constellation skyplot, multi-band RF spectrum, acquisition correlation peak, IQ waveform scrubber, LNAV/INAV decode table, live spectrogram, and C/N0 trends.
- **Transmission Controls:**
  - `Generate`: Pre-computes full scenario IQ files to disk.
  - `Start`: Replays pre-computed scenario files to SDR hardware.
  - `Start Now`: Initiates direct in-memory live streaming without disk I/O.
  - `Stop`: Immediately terminates active transmission.

### 2. Trajectory Builder
Draw multi-waypoint routes on the map, configure velocities, view elevation profiles, and apply dynamic trajectories directly to channel simulations.

### 3. Log
Displays the persistent JSONL audit trail merged with the live `/ws/events` WebSocket stream, alongside real-time NMEA receiver feedback (UDP/serial).

---

## Typical workflows

### Generate and verify (no RF hardware required)
1. Select reference coordinates on the map, set Duration to 300 s, Sample Rate to `auto`, and select desired constellations (e.g., GPS + Galileo + SBAS).
2. Click **Generate**. Progress is streamed live via SSE.
3. Inspect the resulting spectrum and skyplot, scrub through the IQ waveform, and examine acquisition metrics.
4. Run the internal receiver solve (`POST /api/receiver`) to verify position fixes within nominal tolerances of the marker.

### Real-time live transmission (`Start Now`)
1. Connect SDR TX to the receiver RF input via **≥ 40 dB in-line attenuation** (cabled setup only).
2. Set Device URI (e.g., `ip:192.168.3.1`) and select SDR kind (`pluto`, `tezuka`, `tezuka_gnss` or `bladerf`). With `tezuka` choose a sample rate of up to 27.5 Msps (the 16-bit/8-bit switch happens by itself at 10 Msps; use `3.84e6` rather than `2.6e6`). With `tezuka_gnss` the engine on the board generates the signal at up to 61.44 Msps.
3. Check `Live: now` to synchronize with current real-world GPS time (the PC clock is corrected by NTP) (essential for smartphones and A-GPS receivers).
4. Uncheck `Auto-stop after duration` to allow continuous streaming indefinitely, or specify an auto-stop duration.
5. Confirm the isolated setup prompt and click **Start Now**. The signal is synthesized directly in memory and streamed via DMA without disk bottlenecks.
6. Use the **Live Manipulation** tab to jog coordinates or shift GPS time on-the-fly.

### Ephemeris error analysis
1. Perform a **Preview** for a specific epoch to auto-download the MGEX SP3 product.
2. Click **Compare vs broadcast** to open the full-width comparison dashboard, visualizing radial/along-track/cross-track errors, satellite clock offsets, and pseudorange deltas.

---

## API

Base URL: `http://127.0.0.1:8000`. Long operations stream Server-Sent Events (SSE); `/ws/events` provides a WebSocket event feed; all other endpoints consume and return JSON.

| Group | Endpoints |
|-------|-----------|
| **Health & Preview** | `GET /api/health`, `POST /api/preview`, `POST /api/preview_track` |
| **Ephemeris** | `POST /api/rinex/upload`, `GET /api/precise/status`, `POST /api/precise/load`, `POST /api/precise/compare` |
| **Synthesis & Inspection** | `POST /api/generate` (SSE), `POST /api/receiver`, `GET /api/iqplot`, `GET /api/correlation`, `GET /api/lnav` |
| **Hardware Transmission** | `POST /api/transmit` (SSE), `POST /api/transmit/stop`, `POST /api/live/start` (SSE), `POST /api/live/stop`, `POST /api/live/jog`, `POST /api/live/time_shift` |
| **Device Control** | `POST /api/device/connect`, `POST /api/device/disconnect`, `GET /api/device/status` |
| **Session & Presets** | `GET /api/recording/list`, `GET /api/recording/replay` (SSE), `POST/GET /api/trajectory/*`, `POST/GET /api/scenario/*` |
| **Observability** | `GET /api/audit`, `WS /ws/events`, `POST /api/receiver/listen`, `GET /api/receiver/fix` |

`POST /api/live/start` takes, among the scenario fields: `kind` (`pluto` | `tezuka` | `tezuka_gnss` | `pluto_cs8` | `pluto_cs4` | `bladerf`), `start_utc` (`"now"` or an ISO time), `sample_rate`, `tx_gain_db` (default -50), `xo_ppm` (positive = oscillator fast), `fading` (PC path only), `max_duration_s`, `dry_run` and `confirm_isolated`. Transmit endpoints return 403 unless the server runs with `ALLOW_TX=1` and the body has `confirm_isolated: true`.

---

## Configuration

Configured via environment variables (see `backend/config.py`):

| Variable | Default | Description |
|----------|---------|-------------|
| `ALLOW_TX` | `0` | Master switch for RF transmission. Set `1` to enable transmit endpoints. |
| `LIVE_START_SCHEDULED` | `1` | `Live: now` "schedule then play": warm the worker pool, plan the start time T_play = now + first-segment generation estimate x `LIVE_SCHED_GEN_MARGIN` (`1.3`) + `LIVE_SCHED_GUARD_S` (`0.5`) + play latency, generate segment 0 for T_play (re-planned up to `LIVE_SCHED_MAX_ATTEMPTS` (`4`) times if it is late) and hold the start command until T_play - play latency. The first estimate is `LIVE_SCHED_GEN_EST_S` (`3.0`), later ones the measured time (`DATA_DIR/live_lead.json`). `lead_report.lag_s` is the measured residual (target ~0). `0` = the fixed lead below. |
| `GNSS_PLAY_LATENCY_S` | `0.03` | FPGA path: start command (pre-connected ssh `devmem` CTRL enable) to first DAC sample. Only the starting value: after the first run the app uses half the median armed-enable round trip (`enable_rtt_s`, stored in `live_lead.json`), bounded by `GNSS_PLAY_LATENCY_MIN_S` / `_MAX_S`. |
| `LIVE_CLOCK_SYNC` | `1` | Start Now asks NTP (`LIVE_NTP_SERVERS`, `LIVE_NTP_TIMEOUT_S`, cached `LIVE_NTP_MAX_AGE_S`) for the host clock offset and adds it to every host-clock read; no admin rights, the system clock is not changed. No network: offset 0, reported as `clock_source: none`. `lead_report` carries `clock_offset_s` / `clock_source`. |
| `LIVE_PC_PLAY_LATENCY_S` | `0.08` | PC-IQ path: first chunk released to the TX pump to first DAC sample (calibrate the same way). |
| `LIVE_START_LEAD_S` | `2.5` | `Live: now` lead (s) with `LIVE_START_SCHEDULED=0`: how far ahead of the wall clock segment 0 is stamped. |
| `LIVE_START_LEAD_AUTO` | `0` | Fixed-lead mode only (`LIVE_START_SCHEDULED=0`). Opt-in. When `1`, each session measures its first-segment latency (stored in `DATA_DIR/live_lead.json`) and the next `Live: now` for the same device/rate/systems/band uses median + TX buffer + `LIVE_START_LEAD_MARGIN_S` (default `0.3`). Every session reports `lead_report` (`lag_s` > 0 = on-air GPS time behind real time) in the SSE stream either way. |
| `DEVICE_XO_PPM` | `0` | TX reference-oscillator error in ppm, applied as AD9361 `xo_correction`; a request's `xo_ppm` overrides it. Leave 0 with an external 10 MHz reference. |
| `GNSS_BASE_ADDR` / `GNSS_SSH_USER` | `0x43C10000` / `root` | FPGA path: `gs_wrap` AXI-Lite base address and the ssh user used for `devmem` register access. |
| `GNSS_PREFILL_S` | `0.5` | FPGA path: seconds of command blocks queued before the engine is enabled. |
| `GNSS_POLL_S` | `1.0` | FPGA path: engine status and counter poll period. |
| `LIVE_GEN_WORKERS` | `min(6, max(2, cpu_count - 2))` | Worker processes that generate and pack live segments. |
| `TX_CS4_ENABLED` / `TX_CS4_MAX_RATE_HZ` | `0` / `40000000` | Allow 4-bit samples for `tezuka` above the cs8 limit (needs a `CS4TX=1` FPGA image). |
| `DEVICE_URI` | `ip:192.168.3.1` | Default SDR URI (`pyadi-iio` URI for PlutoSDR, bladeRF identifier for bladeRF). |
| `TX_CS16_MAX_RATE_HZ` | `10000000` | `tezuka` kind: highest sample rate sent as 16-bit complex. |
| `TX_CS8_MAX_RATE_HZ` | `27500000` | `tezuka` kind: highest sample rate sent as 8-bit complex; higher rates are rejected. |
| `TX_TDD_MODE` | `1` | Run the AD9361 in TDD (`tx` state) while transmitting; `0` keeps FDD, e.g. for loopback self-tests that also receive. |
| `TX_MAX_TEMP_C` | `85` | AD9361 die temperature above which a running stream is stopped; `0` disables the guard. |
| `RF_FRONTEND_ENABLED` | `0` | Enables LO-offset planning and automatic TX quadrature calibration. |
| `DEFAULT_SAMPLE_RATE` | `2600000` | Default fallback sample rate in Hz. |
| `DEFAULT_FORMAT` | `int16` | Default complex sample quantization (`int16`, `int12`, `int8`). |
| `DATA_DIR` | `./data` | Cache directory for RINEX files, trajectories, and presets. |
| `OUT_DIR` | `./out` | Storage directory for generated IQ files and metadata. |
| `LOG_DIR` | `./logs` | Storage directory for `audit.jsonl`. |
| `PRECISE_DIR` | `./data/precise` | Cache directory for downloaded IGS/MGEX SP3 products. |
| `PRECISE_SP3_MIRRORS` | GFZ, ESA, IGN, IGS mirrors | Comma-separated URL templates for downloading SP3 products. |
| `PRECISE_SP3_ULTRA_MIRRORS` | GFZ, ESA ultra-rapid | Fallback templates for near-real-time ultra-rapid SP3 orbits. |
| `API_KEYS_JSON` | `""` | JSON map for RBAC: `{"<key>": "operator"｜"viewer"}`. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Uvicorn server bind address and port. |

---

## Access control

RBAC is disabled by default. Enable by defining `API_KEYS_JSON`:

```sh
API_KEYS_JSON='{"secret-op":"operator","viewer-key":"viewer"}' ALLOW_TX=1 ./scripts/run_server.sh
```

- **operator** — authorized to start/stop transmissions, jog live sessions, script timelines, connect hardware, and load SP3 products.
- **viewer** — read-only access (status, preview, audit logs, replays). May issue transmission *stop* commands for safety.

---

## Safety

> ⚠️ **Do not transmit RF over the air.**

Unlicensed over-the-air transmission on GNSS frequencies violates spectrum regulations and creates severe hazards for safety-critical aviation, maritime, and telecommunications infrastructure.

- **Conducted Testing Only:** Always connect the SDR output directly to the receiver antenna port via coaxial cable.
- **Mandatory In-Line Attenuation:** Insert **30 to 60 dB** of fixed RF attenuation between SDR TX and receiver input, and keep software TX gain low (−40 to −60 dB). The bench tests were run at −50 dB and later relaxed to at most −30 dB (needed when the same power is spread over a wider sample rate or many FPGA channels); do not go above that.
- **Safety Interlocks:** Transmissions require `ALLOW_TX=1` and explicit confirmation of an isolated setup (`confirm_isolated: true` in the request body, set by the UI modal).
- **Fail-Safe Timeouts:** Configure `max_duration_s` so unattended transmissions terminate automatically.

---

## Testing

The automated tests (unit, integration and validation layers) live in `tests/`, which is gitignored by design and is not part of a clone. If you have them locally:

```sh
pip install -e ".[dev]"      # pytest, httpx
pytest -q                    # testpaths = tests (pyproject.toml)
```

`pyproject.toml` sets `--basetemp=D:/codes/gps-sim/.pytest_tmp`; change it if your checkout is elsewhere. They cover the native engine bindings, signal codes, FEC and CRC routines, ephemeris handling, the software receiver and blind nav decoders, live streaming, the FPGA host model and command generator, and the SDR transmit backends with mocked hardware.

---

## Scripts

Developer tools, run from the repo root. Anything that touches an SDR needs `ALLOW_TX=1` and a cabled setup.

| Script | Purpose |
|---|---|
| `scripts/setup.sh`, `run_server.sh`, `run_server.bat` | Environment setup and server start |
| `scripts/fpga_bringup.py` | Bring-up of the FPGA engine host path: FDD digital loopback (<= 10 Msps, NRMSE against the bit-exact model) or `--tdd-counters`; `--prns 120 126 131` tests SBAS PRNs; `--fake` runs without a board |
| `scripts/fpga_vectors.py`, `fpga_loader_vectors.py`, `fpga_compare.py`, `fpga_check_init.py` | Golden vectors and memory images for the RTL, C++ mixer vs model comparison, post-synthesis BRAM check |
| `scripts/live_e2e.py`, `live_soak.py`, `live_stuck_probe.py` | Digital-loopback check, soak test and DAC-starvation probe of the live pipeline (PC path) at any rate |
| `scripts/cs8_tx_selftest.py`, `cs8_e2e.py`, `cs4_*.py`, `loopback_dc.py`, `loopback_integrity.py` | cs8 / cs4 format checks over the AD9361 digital loopback |

---

## Accuracy and limitations

- **Continuous Live Progression:** In live in-memory mode (`Start Now`), time advances linearly forward indefinitely ($t = k \times 1.0\text{ s}$). The session does not loop, rewind, or wrap. Ephemeris blocks are refreshed seamlessly every 15 minutes to keep orbit parameters valid for receivers.
- **Standards Compliance:**
  - **SBAS L1:** Implements RTCA DO-229 / ICAO Annex 10 standards: direct-form CRC-24Q, rate-1/2 FEC, 1-second preamble sync (0x53, 0x9A, 0xC6), MT 1/2/7/9/10/18/24/25/26 framing.
  - **Galileo E1:** Implements Galileo OS SIS ICD Issue 2.0/2.1: time-reversed polynomial FEC (G1=0o117, G2=0o155 with inverted G2), BPSK symbol polarity (bit 0 $\to$ +1, bit 1 $\to$ −1), and structured I/NAV word mapping. The 250-symbol half-page interleaver is an 8-row × 30-column block (column-write / row-read, `out[j*30+i] = c[i*8+j]`). An I/NAV page lasts 2 s (1 s even + 1 s odd part). `af0..af2` are referenced to the E1-E5b combination in I/NAV and to E1-E5a in F/NAV (IF_a = IF_b + Δ, Δ = BGD(E1,E5a) − BGD(E1,E5b)); RINEX records carry a `clk_ref` tag (from `DataSrc`) and the generator/encoders apply Δ so dual-frequency E1/E5a receivers see a consistent clock. I/NAV word 5 `ai0..ai2` are 0 on purpose: no ionospheric delay is injected into Galileo signals.
  - **BeiDou B1I:** Implements BeiDou SIS ICD: D1 navigation framing, BCH (15,11,1) error correction, and NH20 secondary code modulation. BDT runs 14 s behind GPST. Inside the pipeline BeiDou `toe`/`toc` stay on the GPS scale, the nav encoders broadcast them minus 14 s, and the Omega earth-rotation term uses `toe - 14 s` so a receiver evaluating the broadcast message gets the same satellite position as the simulator.
- **Multi-GNSS Software Receiver Fix:** Built-in receiver performs code-phase pseudorange extraction, least-squares navigation positioning, and geodetic coordinate transformations on the WGS-84 ellipsoid normal.
- **SDR Hardware Channels:** Up to two concurrent physical TX slots (`TX1`, `TX2`) supported on dual-channel SDR platforms. The Tezuka 8-bit path drives TX1 only.
- **Network throughput ceiling:** over libiio on a 1 Gbit link, 16-bit streaming holds about 14 Msps and 8-bit about 27-30 Msps (measured, LibreSDR). Multi-constellation, multi-band scenarios that need a higher rate cannot be streamed live over that link.

---

## Known limitations / validation

Validated on a LibreSDR (Zynq-7020 + AD9361) transmitting through a cable and attenuation into a Septentrio mosaic-X5 receiver, judged by SBF `MeasEpoch` C/N0 and `PVTGeodetic`:

- PC path (`tezuka`, cs4) at 30 Msps, GPS only, -50 dB: stand-alone PVT with 10 SVs at 43-44 dB-Hz and the exact position. Earlier failures at that rate were host CPU/GIL starvation, fixed by generating and packing segments in worker processes.
- FPGA path (`tezuka_gnss`) GPS+Galileo+BeiDou at 20 to 61.44 Msps: 34-38 SVs tracked and a fix within about 2 m. At 61.44 Msps the first fix took about 195 s, so use a TX window of 400 s or more at 40 Msps and above. SBAS PRNs 123 and 136 were tracked and gave an SBAS-mode PVT.
- **Receiver lock time.** After `exeResetReceiver Hard none` the first lock takes 60-130 s; a window of 80 s can look like "no lock". Use windows of 200 s or more and reset the receiver before every run.
- **GPS LNAV content** (commit `125d709`): the broadcast now carries a complete UTC block (SF4 page 18), almanac for all GPS records propagated to one common `toa`, SF4/SF5 page 25, reserved SF4 pages with data ID 01, the page SVID and alternating fill, the HOW anti-spoof bit set to 1, and the standard 25-page cycle.
- **Spoofing flag (open).** The mosaic-X5 raises its built-in "Spoofing detected" flag (SBF `RFStatus` 4092, flags bit 0) about 20-39 s after the first fix whenever GPS L1 C/A is in the broadcast. It does not fire for Galileo-only or BeiDou-only broadcasts, and it is not latched (it cleared by itself about 75 s after the fix was lost). Eliminated as the cause (the flag persisted): UTC and ionosphere parameters, almanac, SF4/SF5 page 25, the anti-spoof HOW bit, reserved pages, the 25-page cycle, TX oscillator drift, restricting the receiver to L1 C/A, signal power, fading, and FPGA versus PC path. The bit is set before the receiver has decoded UTC or almanac, so navigation pages cannot be the trigger. Remaining suspects: a GPS broadcast without P(Y)/L2/L5 energy, the absolute-time check, and a receiver-internal heuristic.
- Fading is not available on the FPGA path; an engine start-up underrun of one DMA group at 30.72 Msps and above is still open (see [FPGA GNSS engine](#fpga-gnss-engine)).

---

## License

MIT — see [LICENSE](LICENSE).
