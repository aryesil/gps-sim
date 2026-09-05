# Precise ephemeris support — design note

## Current system (verified against code, not the README)

```
UTC epoch + lat/lon/alt (+ optional waypoint route)
  |
  v
backend/ephemeris.py   download / upload / cache a *broadcast* RINEX nav file
  |                     parse_rinex(): per PRN, pick the broadcast record whose
  |                     toe is nearest the file's local midday
  v
backend/generator.py   _prepare_nav(): align_epochs() OVERWRITES every
  |                     satellite's toc/toe/gps_week to the requested start,
  |                     then re-serialises to RINEX-2 (to_rinex2_nav)
  v
gps-sdr-sim (C binary)  reads RINEX-2 broadcast Keplerian nav only.
  |                     -t = requested start, -l / -x for position.
  |                     Owns orbit propagation, Doppler, code phase, carrier,
  |                     and the LNAV bitstream. tk = t - toe = 0 at run start
  |                     because align_epochs set toe = start.
  v
gpssim.bin (interleaved int8/int16 IQ) + meta.json
  |
  +--> backend/inspector.py   spectrum, C/A acquisition, correlation, compare
  +--> backend/receiver.py    least-squares position fix from the IQ
  +--> backend/lnav_display.py  LNAV subframe reconstruction
  |        all three build "expected" geometry with backend/geometry.py,
  |        which is an independent broadcast-Kepler propagator (same maths
  |        gps-sdr-sim uses internally)
  v
backend/transmit.py --> PlutoSDR   only when ALLOW_TX and confirm_isolated
```

### Current ephemeris behaviour

| Item | What actually happens |
|------|-----------------------|
| RINEX | georinex loads RINEX-2 or -3, `use="G"`. gps-sdr-sim's bundled parser is RINEX-2 only, so `to_rinex2_nav()` re-serialises. |
| broadcast ephemeris | 15 Keplerian elements + `af0/af1/af2` clock + `tgd`, per PRN. |
| `toe` | broadcast orbit reference time (seconds of week). `parse_rinex` picks the record nearest file midday; `align_epochs` then sets `toe := requested SOW` for *every* satellite. |
| `toc` | clock reference time (distinct field). Also overwritten to the requested SOW by `align_epochs`. |
| requested epoch | passed to gps-sdr-sim as `-t`. Because `toe = toc = start`, `tk = 0` at run start: the orbit is evaluated at the original broadcast set's mean anomaly but relabelled to the new epoch. Error grows with `|requested start - real broadcast epoch|`. |
| satellite state | for the IQ: entirely inside gps-sdr-sim. for verification: `geometry.sat_state` (same broadcast maths). |
| gps-sdr-sim validity check | rejects with "No current set of ephemerides has been found" outside its `toe` window; `align_epochs` guarantees it passes. |

**Conclusion:** the existing `toc`/`toe` realignment is a *broadcast-compatibility mechanism*. It is not precise ephemeris and must never be labelled as such.

## Phase 1 answers (from repository + IS-GPS-200 / IGS product formats)

1. Precise orbit products obtainable? Yes — IGS SP3-c/d (rapid `IGS0OPSRAP`, final `IGS0OPSFIN`) from CDDIS / IGS mirrors. Network-optional.
2. Parseable? Yes — SP3 is fixed-column ASCII; no new dependency.
3. Interpolate position/velocity at an arbitrary epoch? Yes — SP3 carries 15-min ECEF position samples; standard practice is ~10th-order (11-point) Lagrange/Neville interpolation, velocity from the analytic polynomial derivative. Sub-cm interpolation error mid-arc.
4. Precise clock state? SP3 carries a coarse (15-min, us-precision) clock. True precise clock needs a 30 s / 5 min CLK RINEX. **In scope: SP3 clock, linearly interpolated, explicitly flagged coarse.** CLK RINEX is a documented next step.
5. Consumed by the existing generator? **No.** gps-sdr-sim ingests only RINEX-2 broadcast Keplerian nav; it has no satellite-state interface.
6. Does gps-sdr-sim support precise state directly? No.
7. Signal-generation boundary: the RINEX-2 nav file handed to the gps-sdr-sim C binary. Everything past that point is third-party C.
8. Modify gps-sdr-sim? A precise path means replacing its `satpos()` with a table interpolator *and* fabricating a matching LNAV message — large, in C, unverifiable in this environment, and it forks an upstream dependency. Not reasonable here.
9. Separate precise IQ generator? A correct multi-satellite L1 C/A baseband synthesiser with LNAV framing is a multi-week effort; shipping it unvalidated against real hardware is exactly the "fake approximation" this task forbids.
10. Defensible precision claim: precise **satellite state** (position ~cm, velocity ~mm/s, clock coarse) at an **arbitrary epoch within product coverage**, used for **analysis and verification** — quantifying how far the broadcast/aligned ephemeris that actually drives the IQ sits from truth.

## Decision

**DECISION: Strategy D — precise-ephemeris analysis subsystem.**

**WHY:** The signal-generation boundary is a third-party C binary that consumes only broadcast Keplerian nav and owns orbit propagation, Doppler, code phase, carrier, and the LNAV bitstream. No Python can make precise satellite states reach the IQ without forking that C code or writing a new, separately-validated L1 C/A synthesiser — both far outside a safe, testable increment, and both risking a misleading "precise" label over an unvalidated pipeline. A precise-state subsystem slots directly into the existing verification path (`geometry.observables`, `/api/preview`, receiver compare) and delivers real value: an engineer can now quantify the error of the broadcast/aligned ephemeris that drives the IQ against SP3 truth at any epoch in coverage.

**ALTERNATIVES REJECTED:**
- **A** (native precise-state into the generator): the generator is a closed C binary with no state interface.
- **B** (extend gps-sdr-sim): forking an upstream C dependency to swap orbit + LNAV generation; unverifiable here; high regression risk to the working broadcast path.
- **C** (separate precise IQ generator): a correct multi-satellite L1 C/A baseband synthesiser with LNAV is a large project; shipping it unverified is the exact failure mode this task forbids.

**ACCURACY CLAIM:** With an SP3 product loaded, the subsystem returns satellite ECEF position (~cm level: product + ~10th-order Lagrange interpolation), ECEF velocity (~mm/s, analytic polynomial derivative), and satellite clock bias/drift (**coarse** — SP3 15-min clock, linearly interpolated; ~ns-to-sub-us, not CLK-RINEX precise) — valid only for epochs inside the loaded product's time span. **IQ generation is unchanged: still broadcast Keplerian via gps-sdr-sim.**

**KNOWN LIMITATIONS:** no precise IQ; SP3 clock only (no 30 s CLK RINEX); no satellite centre-of-mass to antenna-phase-centre correction (SP3 is CoM, broadcast is APC — documented, ~cm radial); Lagrange interpolation degrades within `order/2` samples of a product-file boundary (raised as an error by default, opt-in reduced order); no automatic multi-day SP3 stitching; the download path is best-effort and disabled unless mirrors are configured.

## Implementation plan

| # | File | Purpose | Tests |
|---|------|---------|-------|
| 1 | `backend/gpstime.py` (new) | GPS time as a first-class type: leap-second table, `utc<->gps`, week/sow, rollover-safe `GPSTime`. | `tests/test_gpstime.py` |
| 2 | `backend/precise.py` (new) | `SatelliteState`, SP3-c/d parser, `PreciseEphemerisProvider` (load / available_epochs / satellites / get_state), Neville interpolation with analytic velocity, strict coverage checks, typed errors. Optional best-effort SP3 download (off by default). | `tests/test_precise.py` |
| 3 | `backend/ephemeris_source.py` (new) | `EphemerisMode`, `state_fn` factory for broadcast and precise, explicit `fallback_to_broadcast` (default false — precise never silently degrades). | `tests/test_ephemeris_source.py` |
| 4 | `backend/geometry.py` (modify) | `observables()` / `constellation()` accept either a broadcast eph dict (unchanged call site) or a `state_fn(t_gps)->(pos,vel,clk)`. | extend `tests/test_geometry.py` |
| 5 | `backend/app.py` (modify) | `GET /api/precise/status`, `POST /api/precise/load` (operator), `POST /api/precise/compare`; `ephemeris_mode` + `fallback_to_broadcast` on `/api/preview`. Invalid combos -> 422, never a silent broadcast substitution. | `tests/test_app_precise.py` |
| 6 | `backend/config.py` (modify) | `PRECISE_DIR`, `PRECISE_SP3_MIRRORS` (empty by default). | `tests/test_config.py` |
| 7 | frontend | `Ephemeris (analysis)` selector on the channel card (Broadcast / Precise, labelled "analysis only — IQ uses broadcast"); a "Precise ephemeris" panel: load SP3 by path, show coverage, run compare, render the per-PRN delta table. Cache-bust `?v=20`. | `tests/test_frontend_assets.py` |
| 8 | `tests/fixtures/igs_sample.sp3` (new) | synthetic SP3-d on a closed-form circular orbit so interpolation error is checkable against truth. | — |
| 9 | `docs/API.md` (new) + `README.md` rewrite | move the exhaustive endpoint table into `docs/API.md`; rewrite README to describe what exists, with an honest Accuracy & Limitations section. | `tests/test_frontend_assets.py` doc checks if any |

Backward compatibility gate: `.venv/bin/pytest -q` baseline is **124 passed, 3 xfailed**; must stay green after every step. Broadcast generate/inspect/receiver/lnav/transmit/RBAC/audit paths unchanged.

---

## Addendum — Strategy B for signal generation (`ephemeris_fit.py`)

The Strategy D subsystem above left IQ generation on broadcast Keplerian
nav with only `toc`/`toe` stamped (`ephemeris.align_epochs`). That is
self-consistent between the simulated LNAV and a receiver decoding it, so
most of the absolute orbit error cancels in the navigation solution — but
the sky geometry (elevation, azimuth, Doppler) and the inter-satellite
error are wrong, and the error grows with distance from the real
broadcast epoch.

This addendum makes precise satellite states actually drive generation,
which is what "precise IQ" requires.

### Approach

`gps-sdr-sim` still consumes a RINEX-2 broadcast record and owns orbit
propagation, Doppler, code phase, carrier, and the LNAV bitstream — none
of that C code is touched. Instead, for `ephemeris_mode="precise"`:

1. Sample the interpolated SP3 ECEF position over a 4 h arc (`toe` ± 2 h,
   97 points) — the same interval the GPS control segment fits a
   broadcast record over.
2. Least-squares fit (damped Gauss-Newton, normalised parameters, a
   coarse 2-D grid seed on M0 / node longitude) the 15 broadcast
   parameters — `m0, e, sqrtA, delta_n, i0, idot, omega0, omega_dot,
   omega, cuc, cus, crc, crs, cic, cis` — so the standard broadcast
   propagation (`geometry._orbit`, identical to gps-sdr-sim's `eph2pos`)
   reproduces the SP3 track. `af0/af1/af2` are fitted to the SP3 clock
   with the relativistic-eccentricity term removed.
3. Serialise the fitted records with `ephemeris.to_rinex2_nav` and hand
   them to `gps-sdr-sim` with **no realignment** — they already carry a
   real `toe` valid across the whole run (one fit covers every live
   segment).

Over a ±1 h arc the harmonic terms are unobservable and the fit stalls
hundreds of metres out; the 4 h arc makes them observable and a
pure-Keplerian synthetic recovers to millimetres. A real SP3 fits to
well under a metre; a fit that cannot get under ~2 m is returned as
HTTP 422, never written to a nav file.

### What still is not precise

- **Clock:** SP3 coarse clock (15-min, linearly interpolated) fitted to a
  quadratic — good to a few ns, not a 30 s CLK-RINEX source.
- **Iono / tropo / multipath:** absent — that is `gps-sdr-sim`, unchanged.
- **CoM vs APC:** the ~cm radial SP3 centre-of-mass / antenna-phase-centre
  offset is still not applied.

### Files

| File | Change |
|------|--------|
| `backend/ephemeris_fit.py` (new) | `fit_satellite`, `build_precise_broadcast`, `EphemerisFitError`; vectorised broadcast forward model for the fit's inner loop. |
| `backend/scenario.py` | `ScenarioRequest.nav_override` — a parsed-eph dict written straight to the nav file, no realignment. |
| `backend/generator.py` | `_prepare_nav` honours `nav_override`; `meta.json` records `ephemeris_mode` and `precise_fit`. |
| `backend/app.py` | `_precise_nav_override` helper; `ephemeris_mode` / `fallback_to_broadcast` on `/api/generate` and `/api/live/start`; 422 on unavailable precise. |
| frontend | Ephemeris selector now applies to Generate/live; hint reworded; cache-bust `?v=21`. |
| `tests/test_ephemeris_fit.py`, `tests/test_app_precise_generate.py` (new) | fit recovery + wiring. |
