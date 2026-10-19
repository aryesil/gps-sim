"""Host clock offset against NTP, so Start Now does not depend on the PC's
clock being synchronised.

Start Now stamps the scenario with the host's UTC. A PC whose clock is off by
d seconds (measured here: 1.6 s on a machine with Windows Time not running)
puts the signal on air d seconds away from real time, and the receiver's time
follows the signal. This module asks a few NTP servers (SNTP, stdlib only, no
admin rights, the system clock is not touched) for the offset
``true UTC - host clock`` and the schedule adds it to every host-clock read.

The host wall clock ticks in 1-15.6 ms steps on Windows, so the wall-clock
reading used in the exchange is rebuilt from ``perf_counter`` and one tick
edge. Of several exchanges the lowest-round-trip ones are averaged (their
asymmetry error is bounded by RTT/2). An unreachable network (an isolated test
rig) gives offset 0 and ``source == "none"``: the host clock is then trusted as
before, and the report says so.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass

from backend import config

_log = logging.getLogger(__name__)

_NTP_EPOCH_OFFSET = 2208988800          # 1900-01-01 -> 1970-01-01, seconds


@dataclass(frozen=True)
class ClockOffset:
    offset_s: float                     # true UTC - host clock
    source: str                         # "ntp:<server>" | "none" | "disabled"
    rtt_s: float | None = None
    samples: int = 0


class _Wall:
    """Host wall clock (unix seconds) with sub-tick resolution: one tick edge
    of ``time.time`` pinned to ``perf_counter``."""

    def __init__(self, max_wait_s: float = 0.05):
        first = time.time()
        end = time.perf_counter() + max_wait_s
        self.t0, self.p0 = first, time.perf_counter()
        while time.perf_counter() < end:
            p = time.perf_counter()
            t = time.time()
            if t != first:
                self.t0, self.p0 = t, p
                break

    def now(self) -> float:
        return self.t0 + (time.perf_counter() - self.p0)


def _ntp_ts(b: bytes) -> float:
    sec, frac = struct.unpack("!II", b)
    return sec - _NTP_EPOCH_OFFSET + frac / 2**32


def query(server: str, wall: _Wall, timeout_s: float) -> tuple[float, float]:
    """One SNTP exchange: ``(offset_s, rtt_s)`` with offset = server - host."""
    pkt = b"\x1b" + 47 * b"\0"                  # LI 0, version 3, mode 3 (client)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout_s)
        s.connect((server, 123))
        t1 = wall.now()
        s.send(pkt)
        data = s.recv(512)
        t4 = wall.now()
    if len(data) < 48 or (data[0] & 0x7) not in (4, 5) or data[1] == 0:
        raise OSError("not an NTP server reply")
    t2, t3 = _ntp_ts(data[32:40]), _ntp_ts(data[40:48])
    return ((t2 - t1) + (t3 - t4)) / 2.0, (t4 - t1) - (t3 - t2)


def measure(servers=None, *, samples: int = 8, keep: int = 3,
            timeout_s: float | None = None) -> ClockOffset:
    """Offset against the first reachable server (lowest-RTT samples averaged)."""
    servers = list(servers if servers is not None else config.LIVE_NTP_SERVERS)
    timeout_s = float(config.LIVE_NTP_TIMEOUT_S if timeout_s is None else timeout_s)
    wall = _Wall()
    for srv in servers:
        got: list[tuple[float, float]] = []
        try:
            for _ in range(samples):
                try:
                    got.append(query(srv, wall, timeout_s))
                except socket.timeout:
                    if not got:
                        break               # silent server: next one
                time.sleep(0.02)
        except OSError as ex:
            _log.debug("NTP %s: %s", srv, ex)
            continue
        if len(got) >= 2:
            best = sorted(got, key=lambda g: g[1])[:max(1, keep)]
            return ClockOffset(sum(o for o, _ in best) / len(best), f"ntp:{srv}",
                               best[0][1], len(got))
    return ClockOffset(0.0, "none")


_lock = threading.Lock()
_cache: tuple[float, ClockOffset] | None = None      # (perf_counter at measure, result)


def current(force: bool = False) -> ClockOffset:
    """Cached offset (refreshed after ``LIVE_NTP_MAX_AGE_S``; a failed lookup
    is retried at the next call). Never raises."""
    global _cache
    if not config.LIVE_CLOCK_SYNC:
        return ClockOffset(0.0, "disabled")
    with _lock:
        now = time.perf_counter()
        if not force and _cache is not None and \
                (now - _cache[0] < config.LIVE_NTP_MAX_AGE_S) and _cache[1].source != "none":
            return _cache[1]
        try:
            res = measure()
        except Exception as ex:             # noqa: BLE001 - best effort
            _log.warning("NTP clock check failed: %s", ex)
            res = ClockOffset(0.0, "none")
        if res.source == "none":
            _log.warning("no NTP server reachable: trusting the host clock "
                         "(Start Now time on air is only as good as that)")
        elif abs(res.offset_s) > 0.05:
            _log.warning("host clock is %.3f s %s of UTC (%s); Start Now corrects for it",
                         abs(res.offset_s), "behind" if res.offset_s > 0 else "ahead of",
                         res.source)
        _cache = (now, res)
        return res
