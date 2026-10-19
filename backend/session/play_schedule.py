"""Start Now: "schedule then play".

The old Start Now stamped segment 0 with ``now + LIVE_START_LEAD_S`` before any
slow work had run (worker pool spawn, RINEX seed, plan, command packing) and
started transmitting the moment the first segment was ready. Whenever that took
longer than the fixed lead the GNSS time on air was behind real time by the
excess (measured ~10 s on the FPGA path), and a running session can never be
re-stamped without breaking code/carrier continuity.

Here the session commits to a time only after the slow start-up work is done:

1. warm up (worker processes spawned and imported, RINEX cache seeded);
2. ``plan(gen_est_s)`` picks the scenario start ``T_play`` as a wall-clock
   instant ``now + gen_est_s * margin + guard + play_latency``;
3. the first segment is generated for exactly ``T_play``; if it is not ready
   at least ``guard + play_latency`` before ``T_play`` (``on_time()`` false)
   the caller drops it and plans again with the measured generation time --
   nothing was transmitted yet, so there is no continuity to keep;
4. the transmit path prepares everything it can (FPGA: prefill the command
   DMA, arm the enable write) and ``wait_and_fire(fire)`` holds until the
   monotonic clock reaches ``T_play - play_latency`` and then issues the start
   command (FPGA: CTRL enable; PC-IQ: release the first chunk to the pump).

``play_latency_s`` is the time from that start command to the first sample
leaving the DAC. It cannot be measured from the host; it is a config constant
(``GNSS_PLAY_LATENCY_S`` / ``LIVE_PC_PLAY_LATENCY_S``) calibrated on the board
against a receiver (see the config comments). ``lag_s`` -- the residual this
module reports -- is ``(start command time + play_latency) - T_play``: the
host-side scheduling error under that latency assumption (> 0: the time on air
is behind real time).

Wall-clock to monotonic: ``T_play`` is converted to a monotonic deadline once,
when it is chosen, so a wall-clock step during the hold cannot move it. The
host wall clock is assumed to be UTC (NTP-synchronised).
"""
from __future__ import annotations

import datetime as dt
import sys
import threading
import time
from typing import Callable

from backend import config

_FAST_SWITCH_S = 2e-4     # GIL switch interval during the last ~60 ms of a hold
_switch_lock = threading.Lock()
_switch_users = 0
_switch_saved = None


def _fast_switch(on: bool) -> None:
    """Reference-counted short GIL switch interval (two slots may hold at once)."""
    global _switch_users, _switch_saved
    with _switch_lock:
        if on:
            if _switch_users == 0:
                _switch_saved = sys.getswitchinterval()
                sys.setswitchinterval(_FAST_SWITCH_S)
            _switch_users += 1
        else:
            _switch_users = max(0, _switch_users - 1)
            if _switch_users == 0 and _switch_saved is not None:
                sys.setswitchinterval(_switch_saved)
                _switch_saved = None


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class PlaySchedule:
    """One Start Now's play time and its measurements (thread-safe enough for
    one producer thread and one reader: fields are written before the
    ``played`` event is set)."""

    def __init__(self, *, play_latency_s: float, gen_est_s: float | None = None,
                 guard_s: float | None = None, margin: float | None = None,
                 max_attempts: int | None = None,
                 utcnow: Callable[[], dt.datetime] = _utcnow,
                 monotonic: Callable[[], float] = time.perf_counter,
                 sleep: Callable[[float], None] = time.sleep,
                 edge_align: bool | None = None):
        # perf_counter, not time.monotonic: on Windows the latter is
        # GetTickCount64 (15.6 ms steps), which alone would misplace the start.
        # edge_align: read the wall clock at one of its tick edges (its own
        # resolution is 1-15.6 ms on Windows); on by default with real clocks.
        self._edge_align = (monotonic is time.perf_counter) if edge_align is None \
            else bool(edge_align)
        self.play_latency_s = max(0.0, float(play_latency_s))
        self.initial_gen_est_s = float(config.LIVE_SCHED_GEN_EST_S
                                       if gen_est_s is None else gen_est_s)
        self.guard_s = float(config.LIVE_SCHED_GUARD_S if guard_s is None else guard_s)
        self.margin = float(config.LIVE_SCHED_GEN_MARGIN if margin is None else margin)
        self.max_attempts = max(1, int(config.LIVE_SCHED_MAX_ATTEMPTS
                                       if max_attempts is None else max_attempts))
        self._utcnow, self._mono, self._sleep = utcnow, monotonic, sleep
        self.t_play: dt.datetime | None = None
        self._play_mono: float | None = None
        self.lead_s: float | None = None      # T_play - decision instant (last plan)
        self.attempts = 0
        self.gen_s: float | None = None       # first-segment generation, last attempt
        self.gen_history: list[float] = []
        self.warmup_s: float | None = None
        self.fire_mono: float | None = None
        self.lag_s: float | None = None
        self.extra: dict = {}                 # transport details (armed enable rtt, ...)
        self.played = threading.Event()

    def now_mono(self) -> float:
        """The schedule's monotonic clock (injectable for tests)."""
        return self._mono()

    # ---- planning --------------------------------------------------------
    def next_gen_est_s(self) -> float:
        """Generation time to plan the next attempt with: the configured or
        historical estimate first, then the slowest measured attempt."""
        if self.gen_history:
            return max(self.gen_history)
        return self.initial_gen_est_s

    def plan(self, gen_est_s: float | None = None) -> dt.datetime:
        """Choose ``T_play`` for a first segment that takes about
        ``gen_est_s`` to generate from now. Returns it (naive UTC, whole
        microseconds)."""
        if gen_est_s is None:
            gen_est_s = self.next_gen_est_s()
        lead = max(0.0, gen_est_s) * self.margin + self.guard_s + self.play_latency_s
        now_utc, now_m = self._now_pair()
        t = now_utc + dt.timedelta(microseconds=round(lead * 1e6))
        self.t_play = t
        self._play_mono = now_m + (t - now_utc).total_seconds()
        self.lead_s = (t - now_utc).total_seconds()
        self.attempts += 1
        return t

    def _now_pair(self, max_wait_s: float = 0.04) -> tuple[dt.datetime, float]:
        """(UTC now, monotonic now) read at the same instant. With edge
        alignment the wall clock is polled until its value changes, which
        happens exactly at one of its ticks, so the pair is good to the
        monotonic clock's resolution instead of the wall clock's."""
        if not self._edge_align:
            return self._utcnow(), self._mono()
        first = self._utcnow()
        t_end = time.perf_counter() + max_wait_s
        while time.perf_counter() < t_end:
            m = self._mono()
            u = self._utcnow()
            if u != first:
                return u, m       # the tick fell between the previous read and m
        return self._utcnow(), self._mono()

    def record_gen(self, gen_s: float) -> None:
        self.gen_s = float(gen_s)
        self.gen_history.append(float(gen_s))

    def remaining_s(self) -> float:
        """Seconds from now to ``T_play`` (negative: past)."""
        if self._play_mono is None:
            raise RuntimeError("no play time planned")
        return self._play_mono - self._mono()

    def on_time(self) -> bool:
        """True when the transmit path still has ``guard + play_latency``
        before ``T_play``: enough to prefill / arm and fire on time."""
        return self.remaining_s() >= self.guard_s + self.play_latency_s

    @property
    def last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts

    # ---- playing ---------------------------------------------------------
    def wait_and_fire(self, fire: Callable[[], object] | None = None,
                      cancel: threading.Event | None = None,
                      spin_s: float = 0.002, publish: bool = True) -> float | None:
        """Hold until ``T_play - play_latency`` and call ``fire()``. Returns the
        residual lag in seconds (> 0: late), or None when ``cancel`` was set
        during the hold (``fire`` is then not called).

        ``fire`` may return the monotonic instant the start command was really
        issued (e.g. after a fallback that took a while); otherwise the instant
        just before the call is used. ``publish=False`` leaves ``played``
        unset: the caller confirms the start and calls ``publish()``."""
        if self._play_mono is None:
            raise RuntimeError("no play time planned")
        target = self._play_mono - self.play_latency_s
        # Other threads of this process (the pool's result thread unpickling
        # later segments, the SSE loop) can hold the GIL for a whole switch
        # interval (5 ms) when the hold ends; shorten it for the last moments.
        fast = False
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    return None
                rem = target - self._mono()
                if rem <= 0.0:
                    break
                if rem < 0.06 and not fast:
                    _fast_switch(True)
                    fast = True
                if rem > spin_s:
                    self._sleep(min(rem - spin_s, 0.05))
                else:
                    self._sleep(0)        # spin the last couple of ms
            t_fire = self._mono()
            ret = fire() if fire is not None else None
        finally:
            if fast:
                _fast_switch(False)
        if isinstance(ret, (int, float)) and not isinstance(ret, bool):
            t_fire = float(ret)
        self.fire_mono = t_fire
        self.lag_s = t_fire - target
        if publish:
            self.played.set()
        return self.lag_s

    def publish(self, fire_mono: float | None = None) -> None:
        """Make the result visible (``played``); ``fire_mono`` corrects the
        start-command instant (e.g. the armed write failed and a later direct
        write started the stream)."""
        if fire_mono is not None and self._play_mono is not None:
            self.fire_mono = float(fire_mono)
            self.lag_s = self.fire_mono - (self._play_mono - self.play_latency_s)
        self.played.set()

    def report(self, ready_s: float | None = None) -> dict:
        """The ``lead_report`` payload of a scheduled Start Now."""
        rep = {
            "lead_s": None if self.lead_s is None else round(self.lead_s, 3),
            "ready_s": None if ready_s is None else round(float(ready_s), 3),
            "tx_buffer_s": round(self.play_latency_s, 4),
            "lag_s": None if self.lag_s is None else round(self.lag_s, 4),
            "auto": False,
            "scheduled": True,
            "attempts": self.attempts,
            "gen_s": None if self.gen_s is None else round(self.gen_s, 3),
            "warmup_s": None if self.warmup_s is None else round(self.warmup_s, 3),
            "t_play_utc": None if self.t_play is None else self.t_play.isoformat(),
        }
        rep.update(self.extra)
        return rep
