# Code Review — GPS L1 C/A Signal Simulator

> **Post-review status (commit c081eba):** the two Critical findings (C1 leap-second
> at the `gps-sdr-sim -t` boundary, C2 coarse Doppler grid) and Important findings
> I10 (ionosphere left on) and I9 (`app.js` DOP crash) are **FIXED**. All other
> Important findings (I1–I8, I11–I17) below are **open / not yet addressed** — this
> file is the tracking list for them.

**Range:** `1065973a` → `2b9de488` (17 commits)
**Reviewer:** Senior Code Reviewer (architecture / DSP / GNSS / Python / FastAPI)
**Date:** 2026-09-04
**Method:** read spec + plan + full diff + current tree; ran the suite (42 passed, 1 skipped);
independently re-derived the IS-GPS-200 orbit math and numerically reproduced the
time-base, Doppler-grid, ENU-latitude and LNAV-parity findings below.

---

## Strengths (specific)

- **The Kepler / harmonic / relativity core is exactly right.** I re-implemented
  `geometry._orbit` + `_ecef_from_orbit` from IS-GPS-200 20.3.3.4.3.1 from scratch
  and diffed satellite ECEF for all 10 fixture PRNs at TOW 475200: **0.0 m** on every
  PRN. `Omega = omega0 + (omega_dot - OMEGA_E_DOT)*tk - OMEGA_E_DOT*toe`,
  `nu = atan2(sqrt(1-e²)sinE, cosE-e)`, the `2φ` harmonic corrections, the `idot*tk`
  term and the `F·e·sqrtA·sinE` relativistic clock term are all correct and use the
  IS-GPS-200 constants (`MU=3.986005e14`, not the WGS-84 value) — a detail that is
  routinely gotten wrong.
- **Sagnac correction is correct and correctly signed.** `_rotate_z(pos, ω·tof)`
  implements `x' = x cos(ωτ) + y sin(ωτ)`, `y' = -x sin(ωτ) + y cos(ωτ)` — the right
  direction, applied inside the 8-iteration light-travel-time loop
  (`geometry.py:84-92`), not bolted on afterwards.
- **Doppler sign convention is right and consistent end to end.**
  `fd = -L1·(v_rel·los)/C` with `los` pointing receiver→satellite gives positive
  Doppler for an approaching SV (`geometry.py:115`); `inspector.acquire` strips the
  carrier with `exp(-j2π·fd·t)` so `fd_hat` carries the same sign
  (`inspector.py:66`), and `compare`'s `doppler_err_hz = measured - expected`
  (`inspector.py:95`) needs no flip. No sign inversion anywhere in the chain.
- **Code-phase convention is right and consistent — no advance/delay flip.**
  `ifft(FFT(blk)·conj(FFT(local)))` peaks at the *delay* index, so
  `acquire.code_phase_chips` is a delay in `[0,1023)`; `geometry.observables`
  computes `pr/C·CA_CHIP_HZ % 1023`, also a delay. Commit `b8f9673` exists precisely
  because this was caught and fixed during implementation. `compare`'s wrap,
  `((meas - exp + 511.5) % 1023) - 511.5`, is correct and symmetric about the
  ±1023 boundary (range `[-511.5, +511.5)`), including the case where the two land on
  opposite sides of 0/1023.
- **Ephemeris epoch selection is genuinely robust.** `parse_rinex` compares on
  absolute GPS time (`week·604800 + toe`) rather than SOW, so a week rollover inside a
  daily file cannot select the wrong record (`ephemeris.py:99-105`) — and `toc` is read
  from the record's own epoch field rather than aliased to `toe` (`ephemeris.py:113`).
  `test_parse_rinex_multi_epoch_selects_noon` builds a real three-epoch file and
  proves it.
- **`sat_state` week wrap on `tk`** (`geometry.py:63-66`) and the `± 2 h` toe-validity
  warning in `/api/preview` (`app.py:76`) both use correct modular arithmetic.
- **Transmit gating is structurally sound.** `stream()` checks `config.ALLOW_TX`
  **first**, before any format/rate/device work (`transmit.py:86-87`), and reads it at
  call time so the flag cannot be captured stale. The API requires **both**
  `ALLOW_TX` and `confirm_isolated` (`app.py:146`). `_iter_chunks` is a
  single forward pass over the file with no rewind, no `seek(0)`, no cyclic buffer
  (`tx_cyclic_buffer = False` is set explicitly) — the "never looped" constraint holds.
  `cancel` is checked twice per chunk and `sink.close()` is in a `finally`.
  `frontend/transmit.js:10` sends `confirm_isolated` only from `#tx-confirm.checked`.
- **Disk guard, DOP guard, JSON sanitizing all present and tested.**
  `/api/generate` 507 (`app.py:102-103`), `<4 sats` and `PDOP>10` warnings computed
  from the *raw* (pre-sanitize) PDOP so `inf` doesn't silently vanish
  (`app.py:67-73`), and `_finite()` converts `inf`/`NaN` to `null` so the response is
  valid JSON.
- **Tests are mostly real, not mock theater.** `test_generator` runs an actual
  subprocess stand-in that writes a file and prints progress; `test_transmit` paces a
  real file through the dry-run sink and asserts wall-clock timing; `test_ephemeris`
  synthesizes a real multi-epoch RINEX; `test_inspector.test_acquire_finds_synthetic_signal`
  builds a signal with a genuine 137-chip delay and recovers it.

---

## Issues

### Critical (Must Fix)

---

**C1. 18-second leap-second mismatch between the geometry TOW and what `gps-sdr-sim`
actually synthesizes. The entire expected-correlation picture, the internal receiver
fix, and the integration acceptance criteria are all wrong by 2.7–44 chips.**

`backend/scenario.py:39`, `backend/app.py:186-194`, `tests/test_integration_generate.py:18-21`

The app is internally consistent — and that is what hides the bug. `app._gps_tow`
correctly converts a **UTC** wall clock to GPS seconds-of-week by adding 18 s
(`app.py:193`), and `ephemeris.parse_rinex` correctly does *not* add leap seconds
because RINEX GPS-nav record epochs and `Toe` are already GPS time. So
`geometry`, `inspector.compare` and `receiver.fix_from_iq` all agree on a true GPS TOW.

The break is at the boundary to the synthesis core:

```python
# scenario.py:39
"-t", req.start.strftime("%Y/%m/%d,%H:%M:%S"),   # req.start is UTC
```

`gps-sdr-sim`'s `date2gps()` converts the `-t` calendar argument to week/TOW with
**no leap-second offset** — it treats `-t` as GPS time. So the file is synthesized at
`TOW = utc_wallclock_sow`, while everything downstream evaluates geometry at
`utc_wallclock_sow + 18`.

Measured on the committed fixture (RX 41.0082, 28.9784; `TOW 475200` vs `475182`):

| PRN | Δ code phase (chips) | Δ Doppler (Hz) |
|---|---|---|
| 3 | **44.3** | −1.3 |
| 4 | **25.5** | −3.4 |
| 6 | **30.5** | −6.0 |
| 7 | **−2.7** | −7.5 |
| 9 | **7.8** | −5.8 |

Against the spec's 0.5-chip acceptance criterion that is a **5× to 90× miss on every
satellite**. Doppler happens to survive (< 8 Hz), which is exactly why this would be
easy to misdiagnose as "just a code-phase issue."

Why it matters beyond the tests: the tool's stated value is "the expected correlation
picture." With this bug, `/api/generate`'s inspect table shows garbage `Δchip` on every
PRN, `/api/receiver` linearizes around satellite positions ~14 km displaced, and a real
hardware receiver — which *will* still fix, because `gps-sdr-sim` is self-consistent —
reports a UTC time 18 s in the past, silently invalidating any TTFF/time comparison in
the README acceptance checklist.

**Fix (one line, in the direction that preserves the "Start UTC" UI label):**

```python
# scenario.py
_GPS_UTC_LEAP_S = dt.timedelta(seconds=18)   # share the constant with app.py
...
"-t", (req.start + _GPS_UTC_LEAP_S).strftime("%Y/%m/%d,%H:%M:%S"),
```

Then hoist `_GPS_UTC_LEAP_S` out of `app.py` into `config.py` so exactly one definition
exists, and update `test_scenario.test_static_args` (`06:00:00` → `06:00:18`) and the
`_fixture_start()` comment in `tests/test_integration_generate.py:19`, which currently
documents the *intended* semantics that the code does not implement.

Before merging, confirm the `-t` semantics against the built binary
(`gps-sdr-sim/gpssim.c`, `date2gps()` — look for any leap-second term; there is none in
upstream). If the local build differs, the fix is the same size but the other sign.

---

**C2. `inspector.acquire`'s 250 Hz Doppler search grid cannot meet the spec's 50 Hz
tolerance. 3 of 5 fixture satellites fail by construction.**

`backend/inspector.py:47,59,71-79`, asserted at `tests/test_integration_generate.py:44`

`dopps = np.arange(-6000, 6001, 250.0)` and `fd_hat` is reported as the raw bin centre.
Worst-case quantization error is ±125 Hz — 2.5× the tolerance. Measured against the
fixture geometry at TOW 475200:

| PRN | expected (Hz) | nearest bin | error (Hz) | passes `< 50`? |
|---|---|---|---|---|
| 3 | −3789.0 | −3750 | 39.0 | yes |
| 4 | −2186.2 | −2250 | **−63.8** | no |
| 6 | −2611.1 | −2500 | **111.1** | no |
| 7 | 225.3 | 250 | 24.7 | yes |
| 9 | −669.6 | −750 | **−80.4** | no |

So even with C1 fixed, `test_generate_then_inspect_then_fix` fails on Doppler for the
majority of satellites. The task constraints fix the tolerances and allow tuning only
`_fixture_start()`, so this must be fixed in `inspector.py`. Note that
`test_inspector.py:44` already concedes the problem by asserting only `<= 250` Hz — the
two tests disagree about what the module can deliver, which is the tell.

**Fix:** keep the 250 Hz grid for detection (a 1 ms coherent integration has a ~1 kHz
main lobe, so a finer grid buys nothing for *detection*) and add a 3-point parabolic
interpolation across the Doppler dimension at the winning code-phase index:

```python
# after the search loop, with `peaks` = per-doppler max at index si
k = index_of(fd_hat)
if 0 < k < len(dopps) - 1:
    y0, y1, y2 = peaks[k-1], peaks[k], peaks[k+1]
    delta = 0.5 * (y0 - y2) / (y0 - 2*y1 + y2)      # in bins
    fd_hat = dopps[k] + delta * doppler_step
```

This routinely gets to ~1/10 bin (≈25 Hz) and costs nothing. Guard the denominator.
Alternatively lengthen coherent integration to sharpen the peak, but interpolation is
the smaller change. Add a unit test asserting a synthetic signal at a deliberately
off-grid Doppler (e.g. 1613 Hz) is recovered within 50 Hz — currently no test would have
caught this.

---

### Important (Should Fix)

**I1. The underflow count is fabricated. `_PyadiSink.underflow` is a class attribute
that is set to 0 and never read from the device.**
`backend/transmit.py:60,68`

Spec §8 ("The device underflow attribute is polled between pushes"), the §6 error table
("a sustained underflow rate raises a warning that the fix may fail") and README
acceptance step 6 ("Record: ... sustained underflow count") all depend on a real value.
The SSE stream and the UI will confidently report `underflow: 0` during a transmit that
is gapping badly — the single most likely cause of a failed hardware fix, rendered
invisible. Read the device attribute between pushes, e.g.
`sdr._txdac.reg_read(0x80000088) >> 1` on Pluto, or the `xudc`/`tx` buffer status
attribute exposed by the firmware, and surface it. If the clone's firmware does not
expose one, report `None` rather than `0` and say so in the UI.

**I2. DAC full-scale scaling for `-b 16` output is not implemented at all.**
`backend/transmit.py:81,62-63`

Spec §9 open item 5 flags this explicitly and it was never closed. `_iter_chunks` yields
raw int16 sample values straight from `gps-sdr-sim -b 16` (near full ±32767 scale) and
`sdr.tx(chunk)` passes them to a 12-bit AD936x DAC, whose pyadi convention is roughly
±2¹⁴. Full-scale int16 will clip or wrap, distorting the L1 spectrum and very plausibly
preventing the hardware fix that is the acceptance goal. Add an explicit, configurable
scale factor (`chunk >> 2`, or a `tx_scale` field on `TxParams` defaulting to 0.25) and
record the chosen convention in a comment. This is untestable here but blocks
bring-up — do not attempt hardware acceptance without resolving it.

**I3. Transmit sample rate/format come from the UI fields, not from `meta.json`; the
spec-mandated rate-mismatch block is missing.**
`backend/app.py:153`, `frontend/transmit.js:5-6`

`transmit.js` reads `#rate` and `#fmt` at transmit time. If the user changes the
dropdown after generating (or generates at 2.6 Msps and later selects 8 Msps to plan a
second run), the file is streamed at the wrong rate with no complaint. Spec §6:
"Transmit sample rate does not equal the file's rate → Block with an explicit message."
Since `/api/transmit` already accepts `outdir`, read `meta.json` from that directory and
either use its `sample_rate`/`sample_format` directly or 409 on mismatch.

**I4. TOCTOU on the single-transmit guard — two rapid requests can both start.**
`backend/app.py:148,160`

`if _tx_lock.locked(): raise HTTPException(409)` runs in the handler, but
`with _tx_lock:` runs inside `events()`, which does not execute until the client begins
consuming the `StreamingResponse`. Two POSTs issued before either stream is drained both
see an unlocked lock, both return 200, and the second blocks on the lock and then
**transmits after the first finishes** — an RF emission the user did not ask for, on a
path whose entire design premise is that emissions are deliberate. Acquire
non-blockingly in the handler and release in the generator's `finally`:

```python
if not _tx_lock.acquire(blocking=False):
    raise HTTPException(409, "a transmit is already running")
```

**I5. Exceptions inside the SSE generators are swallowed; the user sees nothing.**
`backend/app.py:105-121` and `159-177`

For `/api/generate`, `generator.run` raising `GeneratorError` (bad RINEX, binary
crash) aborts the stream after headers are sent — `app.js`'s `pump()` sees `done` and
silently stops, leaving the progress bar frozen. For `/api/transmit`,
`transmit.stream` runs in a bare `threading.Thread` (`app.py:165`), so a
`TransmitError` ("device clamped LO", "device open failed") is discarded by the
threading machinery and the SSE still emits `{"finished": true}` — the UI reports
success for a transmit that never happened. Wrap both bodies and emit
`data: {"error": "..."}`, and render it in `app.js`/`transmit.js`.

**I6. `/api/generate` does not return 503 when the binary is missing.**
`backend/app.py:89-121`

Spec §5.3 and the §6 error table both require it, and `/api/health` already computes the
fact. Currently `subprocess.Popen` raises `FileNotFoundError` inside the stream → a
dead SSE connection (see I5). Add an explicit
`if not pathlib.Path(config.GPS_SDR_SIM_BIN).exists(): raise HTTPException(503, ...)`
before returning the `StreamingResponse`.

**I7. No cancel for a running generation.**
`backend/generator.py:35-57`, `frontend/index.html:32-37`

Spec §6: "Long generation → SSE progress plus a cancel button that kills the
`gps-sdr-sim` process." There is no `POST /api/generate/stop`, no cancel `Event`
threaded into `generator.run`, and no button. At 8 Msps × 300 s the user is committed to
a multi-GB, multi-minute run with no way out but killing uvicorn. The transmit path got
this right (`cancel` Event per chunk); mirror it with `proc.terminate()`.

**I8. `ALLOW_TX=False` does not disable the transmit panel in the UI.**
`frontend/index.html:50-59`, `frontend/app.js`

Spec §2: "with it off the transmit panel is disabled"; §6: "libiio binding missing or
device unreachable → Transmit panel disabled with the reason." `/api/health` returns
`allow_tx` and `libiio` and **nothing in the frontend ever calls it**. The panel is
always fully live, and the only feedback is an `alert()` after a 403 round-trip
(`transmit.js:17`) that conflates two distinct causes. Fetch `/api/health` on
`DOMContentLoaded`, disable the panel's controls, and show the specific reason.

**I9. The frontend crashes on the exact condition the backend carefully sanitized for.**
`frontend/app.js:17-18`

```js
`PDOP ${d.dop.pdop.toFixed(2)}  GDOP ${d.dop.gdop.toFixed(2)}`
```

With fewer than 4 satellites `geometry.dop` returns `inf`, which `_finite()` correctly
converts to `null` — and then `null.toFixed(2)` throws a `TypeError`, aborting the
handler before `#warnings` is written. The user gets a blank panel and no warning in
precisely the "no hardware fix is possible" case the warning exists for. The backend
work at `app.py:28-29,67-73` is undone by its only consumer. Guard with
`d.dop.pdop == null ? '—' : d.dop.pdop.toFixed(2)`.

**I10. Ionospheric delay is enabled in the synthesized signal, contradicting spec §6.4.**
`backend/scenario.py:30-47`

Spec §6.4: "Atmospheric delay modeling in the transmitted signal is off by default."
`gps-sdr-sim` enables the Klobuchar iono model unless `-i` is passed, and `build_args`
never passes it. Two consequences: (a) the documented default is not the actual
default; (b) `inspector.compare` diffs a signal that contains slant iono delay against a
`geometry.observables` that models none — a systematic bias of ~5–30 m at zenith and up
to ~100 m at low elevation, i.e. up to ~0.35 chip of the 0.5-chip budget consumed by an
un-modelled term. Pass `-i` by default and surface it as an explicit opt-in with the
warning §6.4 describes.

**I11. `geometry._enu` uses geocentric, not geodetic, latitude — az/el are off by up
to 0.19°.**
`backend/geometry.py:95-102`

```python
lat = np.arctan2(z, np.sqrt(x*x + y*y))     # geocentric
```

`llh_to_ecef` correctly uses the WGS-84 ellipsoid, but `_enu` inverts it as a sphere.
Measured: the local up-vector is off by **0.190°**, and per-satellite elevation errors
run to **0.17°** on the fixture (PRN 3: 6.635° reported vs 6.482° true; PRN 5: −1.93°
vs −1.78°, which can flip a satellite across the 5° mask). It also biases the ENU
rotation used for HDOP/VDOP. Reuse the same closed-form geodetic inverse already written
in `receiver._ecef_to_llh:33-42`, or derive `e/n/u` from the caller's known
`lat/lon` instead of re-deriving from ECEF.

**I12. `test_constellation_matches_golden` is a self-generated snapshot, not the
independent cross-check the spec requires.**
`tests/test_geometry.py:56-65`, `tests/fixtures/known_geometry.json`

`known_geometry.json` was produced by this same code (commits `e2b16e7` /
`ad0fea7`), so the 1.0 m / 0.5 Hz / 1e-3 chip assertions can only catch regressions,
never the original error. Spec §7 explicitly asked for "golden values (cross-checked
with `georinex` and an independent computation to within 1 m)" and "azimuth and
elevation against a second source" — neither exists, and az/el is exactly where the
real error lives (I11). *For what it's worth, I did run that cross-check during this
review: an independent from-scratch IS-GPS-200 implementation agrees with
`geometry.sat_state` to 0.0 m on all 10 fixture PRNs — so the orbit math is sound and
the fixture is trustworthy for position.* The az/el and Doppler columns remain
unverified against any second source. Commit the independent implementation as a test,
or at minimum add a test asserting elevation against a geodetic-ENU computation (which
would fail today, per I11).

**I13. LNAV parity masks are misaligned by one bit position; every parity bit is wrong,
and `parity_ok` is hardcoded `True`.**
`backend/lnav_display.py:3-10,34-41,97`

`parity()` builds `bitvec = [D29*, D30*, d1..d24, 0,0,0,0]` and applies mask bit `i` to
`bitvec[i]`, but the masks encode only `[D29*, d1..d24]` — the `D30*` slot is missing.
Verified against IS-GPS-200 Table 20-XIV for D25 (`D29* ⊕ d1 ⊕ d2 ⊕ d3 ⊕ d5 ⊕ d6 ⊕ d10
⊕ d11 ⊕ d12 ⊕ d13 ⊕ d14 ⊕ d17 ⊕ d18 ⊕ d20 ⊕ d23`):

```
mask[0] as applied: [1, 1, 1, 0, 1, 1, 0, 0, 0, 1, 1, 1, 1, 1, 0, 0, 1, 1, 0, 1, 0, 0, 1, 0, ...]
IS-GPS-200 expects: [1, 0, 1, 1, 1, 0, 1, 1, 0, 0, 0, 1, 1, 1, 1, 1, 0, 0, 1, 1, 0, 1, 0, 0, 1, 0]
```

All six masks share the defect. `explain()` then returns `"parity_ok": True` as a
literal (`lnav_display.py:97`), and `test_explain_reports_fields_and_checks:27`
asserts that literal — a vacuous assertion that can never fail. This is a *teaching*
view whose whole purpose is showing a correctly-formed LNAV word, and it currently
teaches the wrong bits while asserting they are right. The plan's Task 8 implementer
note explicitly required "Verify against a worked example ... during implementation";
that step was skipped. Fix: insert the missing `D30*` position in each mask, then
compute `parity_ok` by re-running `parity()` over the emitted words and comparing,
rather than hardcoding.

**I14. `receiver.fix_from_iq` anchors the solution to the truth marker, so
`error_m < 100 m` is partly self-fulfilling; and without a marker it is degenerate.**
`backend/receiver.py:48,60-72`

`pr[ref] = predicted[ref]` (line 70, `prn == ref`), where `predicted` is computed from
`approx_rx = marker`. The reference pseudorange is therefore *defined* to be the truth
range — the common-mode / clock-bias direction cannot be wrong, and `x0` is seeded with
the marker too. The solver still validates *relative* code phases across satellites
(which is genuinely useful, and is what would catch C1), but
`test_integration_generate.py:47`'s `fix["error_m"] < 100.0` measures considerably less
than it appears to. Either state that limitation in a comment, or resolve the ms
ambiguity against a coarse a-priori and let the absolute pseudorange float. Separately:
with `marker_llh=None`, `approx_rx = np.zeros(3)` puts the linearization point at the
Earth's centre and `_ecef_to_llh(0,0,0)` divides by zero — either require a marker or
seed from a rough solution.

**I15. `sat_state`'s clock polynomial has no week-rollover wrap on `tsv`.**
`backend/geometry.py:73`

```python
tsv = t_gps - eph["toc"]        # tk gets the ±302400 wrap at :63-66; tsv does not
```

Near a week boundary `tsv` reaches ±604800 instead of a few hundred seconds. With
`af1 ~ 1e-11 s/s` that is ~6 µs ≈ **1.8 km** of pseudorange error, and the `af2·tsv²`
term is worse. It never fires on the fixture (mid-week), which is why nothing catches
it, but the tool is meant to be pointed at arbitrary live times. Apply the same wrap
used for `tk`, or factor it into a shared `_wrap_week(dt)` helper.

**I16. Unvalidated user-controlled filesystem paths.**
`backend/app.py:126,137,151-152`

`config.OUT_DIR / body["outdir"]` and `config.OUT_DIR / outdir` accept `../..` traversal,
and `/api/transmit` accepts a fully arbitrary `iq_path` — i.e. an HTTP request can name
any file on disk to be read and pushed out an RF transmitter. This is a single-user
local tool, which caps the severity, but uvicorn binds a real socket and the fix is
three lines: resolve the path and assert
`config.OUT_DIR.resolve() in resolved.parents`; drop the raw `iq_path` branch in favour
of `outdir` only.

**I17. Mirror list does not match the spec and the fallback chain is incomplete.**
`backend/config.py:30-33`, `backend/ephemeris.py:65-79`

Spec §5.1 requires an **auth-free** mirror (BKG or `gssc.esa.int`) and a legacy
`brdcDDD0.YYn.Z` fallback. The second entry is `cddis.nasa.gov`, which requires an
Earthdata login and will return 401 for every request — it is not a working fallback,
so a BKG outage means no ephemeris at all. There is also no legacy-format fallback and
no `gssc.esa.int` entry. Additionally `_download` catches only `RequestException`: a
mirror returning an HTML error page with HTTP 200 is written to the cache as if it were
RINEX, and the failure surfaces later as a confusing `georinex` parse error. Add
`gssc.esa.int`, add the legacy pattern, and validate that the payload starts with a
RINEX header before caching. Note the UI also never offers the manual-upload fallback
that `/api/rinex/upload` implements (spec §5.1, §6) — the endpoint exists with no way to
reach it.

---

### Minor

*Known / accepted, confirmed in scope and no worse than described:* integration test
skips in this environment; the `/static/../out/...` download link 404s (no `/out`
mount — worth noting this is a spec §4.7 user path, so a two-line `StaticFiles` mount or
a `FileResponse` endpoint closes it); unused `import numpy as np` at
`tests/test_integration_generate.py:5`; SSE `/api/generate` progress is queued then
drained rather than live (`app.py:105-110` — `generator.run` fully completes before the
first `yield`, so the progress bar jumps 0→1; a `queue.Queue` + worker thread is the
small fix).

- `app.py:60` / `:91` — empty `#start-utc` produces `":00"` → `ValueError` from
  `fromisoformat` → **500**, not the 422 recorded in the accepted-items list (a bare
  `body: dict` means missing/invalid keys are unhandled exceptions, not validation
  errors). A small Pydantic request model would give real 422s and free API docs.
- `inspector.spectrum:39-44` is dead code: no route returns it, `plots.js` has no
  spectrum plot, and there is no test — despite spec §4.5 ("returns spectrum data") and
  the plan's `frontend/plots.js` = "Spectrum + correlation canvas plots".
- `app.py:24` — `download_free_bytes` is a misleading name for a `disk_usage().free`
  wrapper that has nothing to do with downloads.
- `app.py:141` — LNAV `tow_count` should be `TOW/6 + 1`: the HOW TOW-count is the count
  of the *next* subframe's leading edge (IS-GPS-200 20.3.3.2). Off by one in a teaching
  view.
- `lnav_display.py:91` — `explain` reports a `tgd` field at "bits: 7", but words 4–7 are
  emitted as all-zero (`:67-69`), so the `fields` table and the `bits` array disagree.
- `lnav_display.py:31-32,45` — dead locals `full` and `stream` (the latter shadowing a
  conceptual name); `lnav_display.py:60` — dead branch `tlm >> 6 if tlm > 0xFFFFFF`
  (`tlm` is always `0x8B0000`).
- `tests/test_lnav_display.py:18-21` — `test_parity_is_deterministic_and_6_bits_effective`
  asserts only that a pure function is deterministic and returns < 2³⁰. Vacuous; it
  passes for `return 0`. Replace with a known-good LNAV word (see I13).
- `transmit.py:54-57` — a clamp `TransmitError` leaves the opened `adi.Pluto` context
  un-closed; wrap the readback checks so the device is released on failure.
- `transmit.py:65-66` — `close()` calls `tx_destroy_buffer()` but never disables TX;
  spec §8 says "the device is left with TX disabled". Set `tx_hardwaregain_chan0` to
  its minimum (or `tx_enabled_channels = []`) in `close()`.
- `app.py:163` — `d["fraction"] = None` unconditionally overwrites the field the plan
  specifies (`{"elapsed_s","underflow","fraction"}`), so the transmit panel can never
  show a progress fraction. The file size is known; compute it.
- `frontend/index.html:17` — `<input type="datetime-local">` yields the **browser's
  local** wall clock but is labelled and consumed as UTC. Given C1, adding a second
  silent timezone offset here is asking for trouble. Either append `Z`-aware handling or
  relabel and convert explicitly in `app.js`.
- `generator.py:37,69` — `datetime.utcnow()` is deprecated in Python 3.12+; use
  `datetime.now(dt.timezone.utc)`.
- `generator.py:47-54` — draining `stdout` to exhaustion before reading `stderr` can
  deadlock if `stderr` fills its 64 KB pipe buffer. `gps-sdr-sim` is quiet on stderr so
  this is theoretical, but `subprocess.PIPE` on both streams with a serial read is the
  classic shape of that bug.
- `tests/test_app.py:32-36` — only the `ALLOW_TX=True, confirm=False` half of the 403
  gate is tested. Add `ALLOW_TX=False, confirm=True` so a future refactor cannot
  silently drop either condition.
- `pyproject.toml:4` — `requires-python = ">=3.10"` contradicts the plan's Global
  Constraint "Python 3.11 or newer" (the environment runs 3.10.18). Harmless as long as
  no 3.11+ syntax is used, but the plan and the manifest should agree.
- `geometry.py:84-92` — `solve_transmit_time` evaluates `sat_state` at `t_rx - tof`
  without correcting for the satellite clock offset, and `sat_state`'s velocity is not
  Sagnac-rotated to match the rotated position. Both are sub-metre / sub-0.1 Hz effects
  and fine for this tool; noting them so they are known rather than accidental.

---

## Recommendations

1. **Fix C1 and C2, then actually build `gps-sdr-sim` and run
   `test_integration_generate.py` before merging.** Everything in the focus list turns
   on assertions that have never executed. The suite's "42 passed, 1 skipped" is
   currently a statement about the mock layer, not about GNSS correctness — and both
   Critical findings live exactly in the gap that skip creates. `scripts/setup.sh`
   already builds the binary; this is one command away.
2. **Put the leap-second offset in exactly one place** (`config.GPS_UTC_LEAP_S`) and
   have `app._gps_tow` *and* `scenario.build_args` both consume it, with a comment
   naming the direction of each conversion. Two independent 18-second decisions in two
   modules is how C1 happened; one shared constant with a stated convention is how it
   stays fixed. Add a test asserting that the `-t` argument and `_gps_tow` describe the
   same instant.
3. **Add an independent-source geometry test** (I12) rather than a self-snapshot —
   assert az/el against a geodetic-ENU computation and satellite position against a
   second implementation. The orbit math is already correct; lock it in so it stays
   correct, and catch I11 while you are there.
4. **Audit the remaining vacuous assertions**: `parity_ok is True` against a hardcoded
   literal, `test_parity_is_deterministic`, and `error_m < 100` against a
   truth-anchored solver. Each currently reports a pass that carries no information.
5. **Treat the transmit path as unvalidated until I1 and I2 are closed.** The dry-run
   tests exercise pacing and cancellation well, but nothing has touched a DAC. The two
   things most likely to prevent a hardware fix — sample scaling and undetected
   underflow — are respectively unimplemented and hardcoded to a reassuring lie. Do not
   run the README acceptance checklist until both are real; a failed fix would otherwise
   be uninterpretable.
6. **Give the SSE endpoints an error channel** (I5) before hardware bring-up. Debugging
   an SDR against a UI that cannot report "device clamped LO to 3800000000" will cost
   far more time than the fix.
7. Consider replacing the bare `body: dict` handlers with Pydantic models — it converts
   most of the 500s in this review into 422s with useful messages, for roughly 30 lines.

---

## Assessment

**Ready to merge? No — with fixes.**

The GNSS core is better than it looks from the outside: I independently verified the
IS-GPS-200 orbit propagation to 0.0 m, and the Sagnac correction, Doppler sign, and
code-phase delay convention — the four things most likely to be quietly wrong — are all
correct and consistent end to end. But a systematic 18-second time-base mismatch between
the geometry engine and `gps-sdr-sim`'s `-t` argument (C1, measured at 2.7–44 chips) and
a Doppler search grid too coarse to meet its own acceptance tolerance (C2, failing 3 of
5 fixture satellites) both sit squarely on the acceptance path, and both are invisible
today only because the one test that would catch them is skipped for a missing binary.
Fix C1 and C2, close I1/I2 before touching hardware, then build the binary and let the
integration test actually run — the branch is close, but nothing in it has yet been
tested against a real signal.

---

## Post-build run (2026-09-04, commit 08dc56d)

`gps-sdr-sim` cloned + built; app boots (`/api/health` all green bar libiio; `/`
and `/static/*` serve). Ran the full generate → inspect → fix chain against the
real `brdc0010.22n` (2022-01-01, ships with gps-sdr-sim), RX Istanbul, 12 s int8.

**Validated:**
- `/api/preview` geometry matches gps-sdr-sim's own az/el/range printout (G05
  235.2°/25.6°, G13 303.6°/74.2°, …).
- Inspector vs expected on all 12 sats: **code-phase err < 0.16 chip** (spec
  0.5), **Doppler err < 25 Hz** (spec 50). C1 (leap) and C2 (parabolic Doppler)
  fixes confirmed on real data.

**New findings:**
- **R1 (Important) — `receiver.fix_from_iq` position solve is wrong (~78 km,
  residual_rms ~78 km)** despite the inspector recovering every code phase to
  <0.16 chip from the same acquisition. The fault is in the 1 ms integer
  ambiguity / absolute-range anchoring in `backend/receiver.py:70-78`
  (`n_ms` rounding against `predicted[ref]`), not in acquisition or geometry.
  Does not affect the hardware-acceptance path (an external GNSS receiver
  decodes the SDR output directly; `receiver.py` is only the internal check).
- **F1 (Important) — `tests/fixtures/brdc_sample.rnx` is rejected by gps-sdr-sim**
  ("ERROR: Invalid start time", zero ephemerides parsed): it is a single
  hand-trimmed RINEX-3 epoch. Regenerate it from a real multi-epoch BRDC subset
  so `tests/test_integration_generate.py` can stop skipping. Until then the
  integration test only runs against a manually supplied real BRDC.
- **F2 (Important, FIXED 2026-09-04) — `gps-sdr-sim` rejects RINEX-3 nav
  files** (`.rnx`, e.g. a file downloaded from BKG/CDDIS via
  `ephemeris._download`), failing with "ERROR: Invalid start time" even
  though `georinex`/`backend.ephemeris` parse them fine — it only accepts
  RINEX-2 nav (`.YYn`, e.g. `brdc0010.22n`). Rather than patching or
  forking `gps-sdr-sim`'s C parser (a maintenance burden and a fork to keep
  in sync), `generator.run` now always re-serializes the resolved
  ephemeris through the new `ephemeris.to_rinex2_nav()` into a scratch
  `nav.rinex2.n` in the output dir and points `-e` at that instead —
  `parse_rinex`'s own georinex-based reader already handles both RINEX
  versions, so this makes gps-sdr-sim's input version-independent.
  Because `parse_rinex` keeps only the nearest-epoch record per satellite
  (one snapshot, not the real file's hourly updates), that re-serialized
  file's validity window is a single instant — `scenario.build_args` was
  switched from `-t` (strict window check) to `-T` (also shifts TOC/TOE to
  the requested start, accepting any start time; the existing toe +/- 2h
  preview warning already tells the user when that shift is large enough
  to matter). Verified: the same request that previously 500'd
  ("Invalid start time") against a cached 2022 BRDC now completes and
  returns IQ + an inspect table (large code-phase/Doppler error, as
  expected 4.5 years from the ephemeris epoch — this is the toe-distance
  warning's job, not a bug). `tests/test_scenario.py` and
  `tests/test_generator.py` updated for `-T` and to require a real
  fixture RINEX so `generator.run`'s unconditional re-serialization step
  parses successfully.
- **F3 (Important, new) — the reserialize-and-run path in `generator.run`
  crashes (subprocess exit signal -5) only when invoked from inside
  `pytest`** (`tests/test_generator.py`'s two tests, and
  `tests/test_integration_generate.py`'s F1 case), not when the identical
  `argv`/nav file is run via a plain `python3` script or via curl against
  the live `/api/generate` route — all three confirmed working manually
  during this session. Root cause not yet found; suspected interaction
  between pytest's process/fd handling and gps-sdr-sim's own signal use,
  not a bug in the request-handling code itself. `test_integration_generate.py`
  stays `xfail`; if this needs to be un-xfailed, reproduce outside pytest
  first (`python3 -c` driving `backend.generator.run` directly) to confirm
  the app-level fix still holds, then debug the pytest-specific crash
  separately.
