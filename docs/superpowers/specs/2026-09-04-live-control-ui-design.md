# Live-Control UI & Multi-Channel Backend — Design

**Status:** approved by user, ready for implementation planning.

## Background

The user shared four reference screenshots (`Example-sim/`) from a
commercial GPS simulator (Multi-Channel Operation, Position Spoofing, Time
Spoofing, Trajectory Builder) and asked for a from-scratch, *inspired-by-not-
copied* redesign of this project's UI, plus the backend features those
screens imply. This spec is that design, derived from a section-by-section
walkthrough approved by the user.

**Explicit non-goal:** the UI must not be a pixel copy of the reference
screenshots. Their information architecture (sidebar navigation, tabbed
per-channel panels, a dedicated full-screen trajectory editor) is the
inspiration; the visual design, layout details, and component structure are
new.

## Key hardware fact this design is built on

The PlutoSDR has **two** physical TX outputs (TX1, TX2) — confirmed by
re-reading against the AD936x-based Rev B/C board layout after an earlier,
incorrect claim in this session that only one TX existed. "Multi-channel" in
this project therefore means **up to 2 simultaneously transmitting
channels**, not the unbounded parallel channel count the reference
screenshots show (those target different, multi-chip hardware).

## 1. Frontend Information Architecture

Left sidebar (fixed, icon + label): **Channels** | **Trajectory Builder** |
**Log**. ("Licence" from the reference has no equivalent here and is
dropped.)

**Channels page:** a list of channel cards, at most 2 (TX1/TX2). Each card:

- Header row: name, TX assignment (TX1 / TX2 / — unassigned), Start/Stop,
  status badge (Ready / Generating / On Air).
- Left column — **Hardware Config**: Device URI, LO Hz, TX gain, sample
  rate/format.
- Middle column — **Simulation Config**: RINEX source, start LLA, duration.
- Right column — tabbed panel:
  - **Status** — today's inspect table + IQ/spectrum/correlation plots
    (already built this session).
  - **Satellites** — skyplot + DOP (already built).
  - **Position** — target LLA + jog controls + "load from Trajectory
    Builder" waypoint picker. **Enabled only while the channel is
    transmitting** (live session running) — otherwise disabled with a
    "start transmit first" hint, since jogging has no effect on a channel
    that isn't producing IQ.
  - **Time** — PPS phase shift, GPS ToW shift, satellite clock correction
    offset. Same enablement rule as Position.
- Footer: the existing shared map (reference LLA, route overlay if any).

Page-level: "Add Channel", "Start All".

This reorganizes today's single top-to-bottom flow into up to 2 card-based
parallel channels, matching the real TX1/TX2 hardware limit.

## 2. Backend — Live Session Architecture

New `backend/live.py`:

```python
@dataclass
class LiveState:
    llh: list[float]            # live target LLA -- jog updates this
    time_offset_s: float = 0.0  # GPS ToW shift
    pps_shift_s: float = 0.0
    clock_corr_ns: float = 0.0

class LiveSession:
    def __init__(self, base_req: scenario.ScenarioRequest, tx_params, channel_id):
        self.state = LiveState(llh=[base_req.lat, base_req.lon, base_req.alt])
        self._lock = threading.Lock()   # jog thread vs. generation thread
        self.running = False
        self.consecutive_errors = 0

    def jog(self, direction: str, distance_m: float) -> None:
        """ENU-frame nudge: convert current LLH -> ECEF, add the ENU offset
        for `direction`, convert back (reuses geometry.llh_to_ecef and its
        inverse, same math already used by receiver.py)."""

    def shift_time(self, field: str, delta: float) -> None:
        """field in {"time_offset_s", "pps_shift_s", "clock_corr_ns"}."""

    def _segments(self):
        """Generator: yields ~1s IQ segments. Reads a *snapshot* of self.state
        under the lock at the start of each segment, so a jog/shift landing
        mid-segment applies starting next segment (bounded ~1s lag)."""
        while self.running:
            with self._lock:
                snap = copy.deepcopy(self.state)
            try:
                path = generator.run_segment(self.base_req, snap, duration_s=1.0)
                yield inspector.read_iq(path, self.base_req.sample_format)
                self.consecutive_errors = 0
            except Exception:
                self.consecutive_errors += 1
                if self.consecutive_errors >= 3:
                    self.running = False
                    raise
```

`transmit.stream()` gains a pluggable chunk source: today's file-backed
`_iter_chunks(path, ...)` is one implementation of "an iterable of complex
chunks"; a `LiveSession._segments()` generator is another. `stream()`'s body
(`sink.push`, `progress_cb`, `cancel` handling) is unchanged — only the thing
it iterates changes. This keeps the existing, tested one-shot path
untouched.

`generator.py` gains `run_segment(base_req, state, duration_s)`: a short-
duration, static-LLA (no motion CSV) variant of `run()`. To avoid
duplicating the gps-sdr-sim argv/subprocess logic, `run()`'s internals are
refactored so both entry points share one `_build_and_exec(req, out_bin)`
helper — `run()` keeps its full-duration behavior, `run_segment` calls the
same helper with a 1-second request built from `base_req` + `state`
(`state.llh` as the static position, `state.time_offset_s` added to the
nav's toc/toe before `ephemeris.align_epochs`, so the PPS/ToW/clock shifts
are real changes to the transmitted subframes, not cosmetic).

**New endpoints** (SSE progress reuses today's `/api/transmit` pattern):

- `POST /api/live/start` — body: channel_id, base scenario fields, tx
  params. Picks a free TX slot (see below) or 409s.
- `POST /api/live/jog` — `{channel_id, direction, distance_m}`.
- `POST /api/live/time_shift` — `{channel_id, field, delta}`.
- `POST /api/live/stop` — `{channel_id}`.

**Open risk, first implementation task:** whether gps-sdr-sim can produce a
1-second segment inside 1 wall-clock second on the target machine is
unverified. The first plan task must measure this before building the rest
of the live loop; if it can't keep up, the segment duration grows (with a
small look-ahead buffer) rather than the architecture changing.

## 3. Trajectory Builder

Its own sidebar page, full-screen Leaflet map:

- Click the map → append a numbered waypoint (marker + red line from the
  previous point), matching the reference screenshot's interaction.
- Markers are draggable (Leaflet `draggable: true`) to reposition.
- A table below lists editable rows: `# | lat | lon | alt | speed m/s |
  accel m/s²`; editing a cell updates the map marker, and vice versa.
- Toolbar: **New** (clear) | **Save as...** (prompts a name, POSTs JSON) |
  **Open** (lists saved trajectories, loads one) | **Apply to channel**
  (writes the waypoint list into the target channel's `route` field).

**Backend:**

- `POST /api/trajectory/save` — `{name, waypoints}` →
  `data/trajectories/<name>.json`.
- `GET /api/trajectory/list` — saved names.
- `GET /api/trajectory/load?name=` → waypoints.
- No change needed to accept the route at generation time —
  `scenario.ScenarioRequest.route` and `write_motion_csv` already exist and
  already work; they were simply never wired to any UI control.

`write_motion_csv` already rejects fewer than 2 waypoints — the "Apply"
button is disabled client-side below that count as well, so the rejection
is never hit in normal use.

## 4. Concurrency, TX-Slot Assignment, Error Handling

**TX slot assignment:** replace the single `_tx_lock` with a 2-slot map
`_tx_slots = {"TX1": None, "TX2": None}` (holds a channel_id or `None`).
Starting a channel looks for a free slot; both full → 409. This is the
direct backend expression of "at most 2 real simultaneous channels" from
the hardware-scope decision above.

**Jog/time-shift vs. generation race:** `LiveState` mutations happen under
`LiveSession._lock`; `_segments()` takes one lock-protected snapshot per
segment. A jog landing mid-segment is visible starting the next segment —
bounded ~1s lag, accepted as fine for this use case (this is stated
explicitly so a future reviewer doesn't mistake it for a bug).

**Error handling in the live loop:**
- A single segment's generation failure doesn't kill the session — it's
  skipped (reported via `progress_cb` as `{"error": ...}`) up to 3
  consecutive failures, after which the session stops itself.
- If segment generation can't keep up with real time (see the open risk
  above), `progress_cb` carries `underrun: true`, surfaced by the existing
  `tx-progress` canvas bar (already built this session) rather than a new
  UI element.

## 5. Test Plan

- **Segment-generation performance test** (do this first, it's the
  project's biggest unknown): assert a 1-second segment is produced in
  under 1 wall-clock second on this machine; if not, this spec's "grows the
  segment duration" fallback is what the plan implements instead of the
  1-second default.
- `test_live.py`: `LiveSession.jog()` mutates state correctly (ENU math
  reused from `geometry.py`); `_segments()` yields consecutive segments from
  a mocked `generator.run_segment`; no race between a jog and a snapshot
  read (threading test, e.g. hammer `jog()` from one thread while
  `_segments()` runs, assert no torn reads).
- `test_transmit.py` addition: `stream()` still works when its chunk source
  is a plain generator (not just `_iter_chunks(file)`) — existing tests
  must keep passing unchanged.
- `test_trajectory.py`: save/load JSON round-trip; reject/disable below 2
  waypoints.
- Frontend: `node --check` syntax validation (existing convention this
  session) plus manual browser verification; no JS test framework is being
  added — out of scope.

## Explicitly out of scope for this pass

- True 3D/globe visualization, route-animation-on-map (raised earlier as
  visualization ideas, not part of this redesign).
- Any hardware beyond a single PlutoSDR (TX1/TX2 only).
