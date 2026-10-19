"""Control and status of the FPGA GNSS engine (``gs_wrap`` register file).

``GnssDevice`` implements the register protocol on top of two primitives,
``read32(offset)`` / ``write32(offset, value)``; the transport behind them is
injected by subclassing:

* ``RemoteDevmem``  -- SSH to the board, ``devmem`` on the AXI-Lite window (or
  the board-side helper ``gnssctl`` for the control verbs, ``GnssctlDevice``).
  The command runner is a plain callable, so tests (and other transports such
  as a local UIO mapping) can replace it.
* ``FakeDevice``    -- in-memory register file with an optional behavioural
  consumer that mimics the counters of ``gs_wrap`` (bit-exact samples via
  ``backend.fpga.model``). For tests only.

Register map (offsets in the 0x1000 window): see ``fpga/rtl/gs_wrap.v``.
"""
from __future__ import annotations

import dataclasses
import re
import shlex
import subprocess
import threading
import time
from typing import Callable

import numpy as np

from backend.fpga import cmdfmt
from backend.fpga import widths as W

REG_ID = 0x00
REG_CTRL = 0x04
REG_STATUS = 0x08
REG_PLAY_LO = 0x10
REG_PLAY_HI = 0x14
REG_UNDERRUNS = 0x18
REG_DROPPED_LATE = 0x1C
REG_CLIPPED = 0x20
REG_CHIP_LATE = 0x24
REG_BAD_WORDS = 0x28
REG_SAMPLE_UNDERFLOWS = 0x2C
REG_SAMPLE_LEVEL = 0x30
REG_CMD_OVERRUNS = 0x34

GNSS_ID = 0x47530001

CTRL_ENABLE = 1 << 0
CTRL_CMD_MODE = 1 << 1
CTRL_SOFT_RESET = 1 << 2
CTRL_CLEAR = 1 << 3

ST_RUNNING = 1 << 0
ST_CMD_FIFO_OK = 1 << 1
ST_CMD_MODE = 1 << 2
ST_PLAYING = 1 << 3
ST_CMD_EMPTY = 1 << 4
ST_SAMPLE_EMPTY = 1 << 5
ST_CMD_FULL_SEEN = 1 << 6

SOFT_RESET_TIMEOUT_S = 1.0
_PLAY_RETRIES = 8


class DeviceError(Exception):
    """The engine cannot be reached or does not behave as expected."""


class IdMismatch(DeviceError):
    pass


@dataclasses.dataclass(frozen=True)
class GnssStatus:
    running: bool
    cmd_fifo_ok: bool
    cmd_mode: bool
    playing: bool
    cmd_fifo_empty: bool
    sample_fifo_empty: bool
    cmd_fifo_full_seen: bool
    queued_words: int
    play_idx: int
    underruns: int
    dropped_late: int
    clipped: int
    chip_late: int
    bad_words: int
    sample_fifo_underflows: int
    sample_fifo_level: int
    cmd_fifo_overruns: int

    @classmethod
    def decode(cls, status: int, play_idx: int, counters: dict) -> "GnssStatus":
        return cls(
            running=bool(status & ST_RUNNING),
            cmd_fifo_ok=bool(status & ST_CMD_FIFO_OK),
            cmd_mode=bool(status & ST_CMD_MODE),
            playing=bool(status & ST_PLAYING),
            cmd_fifo_empty=bool(status & ST_CMD_EMPTY),
            sample_fifo_empty=bool(status & ST_SAMPLE_EMPTY),
            cmd_fifo_full_seen=bool(status & ST_CMD_FULL_SEEN),
            queued_words=(status >> 16) & 0xFFFF,
            play_idx=play_idx, **counters)

    @property
    def play_block(self) -> float:
        return float(self.play_idx)        # the register counts blocks


_COUNTER_REGS = {
    "underruns": REG_UNDERRUNS, "dropped_late": REG_DROPPED_LATE,
    "clipped": REG_CLIPPED, "chip_late": REG_CHIP_LATE,
    "bad_words": REG_BAD_WORDS, "sample_fifo_underflows": REG_SAMPLE_UNDERFLOWS,
    "sample_fifo_level": REG_SAMPLE_LEVEL, "cmd_fifo_overruns": REG_CMD_OVERRUNS,
}


class GnssDevice:
    """Register protocol; subclasses provide ``read32`` / ``write32``."""

    def read32(self, offset: int) -> int:
        raise NotImplementedError

    def write32(self, offset: int, value: int) -> None:
        raise NotImplementedError

    # ---- control ---------------------------------------------------------
    def check_id(self) -> int:
        try:
            v = self.read32(REG_ID) & 0xFFFFFFFF
        except DeviceError:
            raise
        except Exception as ex:
            raise DeviceError(f"GNSS engine not reachable: {ex}") from ex
        if v != GNSS_ID:
            raise IdMismatch(f"GNSS engine ID {v:#010x}, expected {GNSS_ID:#010x} "
                             "(bitstream without the engine, or wrong address)")
        return v

    def _ctrl(self) -> int:
        return self.read32(REG_CTRL) & (CTRL_ENABLE | CTRL_CMD_MODE)

    def _set_ctrl_bit(self, bit: int, on: bool) -> None:
        cur = self._ctrl()
        self.write32(REG_CTRL, (cur | bit) if on else (cur & ~bit))

    def set_cmd_mode(self, on: bool) -> None:
        self._set_ctrl_bit(CTRL_CMD_MODE, on)

    def enable(self) -> None:
        self._set_ctrl_bit(CTRL_ENABLE, True)

    def disable(self) -> None:
        self._set_ctrl_bit(CTRL_ENABLE, False)

    def clear_counters(self) -> None:
        self.write32(REG_CTRL, self._ctrl() | CTRL_CLEAR)

    def arm_enable(self) -> "ArmedEnable":
        """Prepare the CTRL enable write so that it can be issued at a precise
        instant (Start Now scheduling). Default: no preparation, ``fire()`` is
        a plain ``enable()``; transports with a per-access setup cost (ssh)
        override this with a pre-connected command."""
        return ArmedEnable(self.enable)

    def soft_reset(self, timeout_s: float = SOFT_RESET_TIMEOUT_S) -> None:
        """Flush FIFOs and the engine; needed when changing cmd_mode or
        restarting a stream. Waits for the busy bit (CTRL b2) to drop."""
        self.write32(REG_CTRL, self._ctrl() | CTRL_SOFT_RESET)
        deadline = time.monotonic() + timeout_s
        while self.read32(REG_CTRL) & CTRL_SOFT_RESET:
            if time.monotonic() >= deadline:
                raise DeviceError("soft_reset did not complete "
                                  f"within {timeout_s:g} s (clock absent?)")
            time.sleep(0.001)

    # ---- status ----------------------------------------------------------
    def play_idx(self) -> int:
        """64-bit sample index; HI-LO-HI read, retried if HI changed."""
        for _ in range(_PLAY_RETRIES):
            hi = self.read32(REG_PLAY_HI) & 0xFFFFFFFF
            lo = self.read32(REG_PLAY_LO) & 0xFFFFFFFF
            if self.read32(REG_PLAY_HI) & 0xFFFFFFFF == hi:
                return (hi << 32) | lo
        raise DeviceError("PLAY_IDX high word keeps changing")

    def read_many(self, offsets) -> list:
        """Several registers in order; transports with a per-access cost
        (ssh) override this to batch them."""
        return [self.read32(o) for o in offsets]

    def status(self) -> GnssStatus:
        vals = [v & 0xFFFFFFFF for v in self.read_many(
            [REG_STATUS, REG_PLAY_HI, REG_PLAY_LO, REG_PLAY_HI,
             *_COUNTER_REGS.values()])]
        st, hi, lo, hi2 = vals[:4]
        play = (hi << 32) | lo if hi == hi2 else self.play_idx()
        counters = dict(zip(_COUNTER_REGS, vals[4:]))
        return GnssStatus.decode(st, play, counters)


class ArmedEnable:
    """An enable write prepared ahead of time.

    ``fire()`` issues it; it returns None when the write went out at once, or
    the ``time.perf_counter()`` instant a slow fallback write finished (the
    engine started no earlier than that). ``confirm()`` waits until it has
    certainly reached the engine and returns details for the report;
    ``close()`` releases the preparation without enabling (idempotent).
    """

    def __init__(self, enable: Callable[[], None]):
        self._enable = enable
        self.fired = False

    def fire(self) -> float | None:
        self._enable()
        self.fired = True
        return None

    def confirm(self, timeout_s: float = 5.0) -> dict:
        return {"armed": False}

    def close(self) -> None:
        pass


class SshArmedEnable(ArmedEnable):
    """Enable through an ssh session opened in advance.

    The remote shell prints ``armed`` once connected, then blocks in ``read``;
    ``fire()`` only writes a newline to the local pipe, so the register write
    follows within one network hop plus a devmem exec instead of a whole ssh
    connect (hundreds of ms, jittery). If the session is not usable at fire
    time, or exits with an error, the write falls back to the ordinary (slow)
    ``enable`` so the stream still runs (reported as ``enable_fallback``).
    """

    READY = "armed"

    def __init__(self, proc, fallback: Callable[[], None],
                 clock: Callable[[], float] = time.perf_counter):
        super().__init__(fallback)
        self._proc, self._clock = proc, clock
        self.ready = threading.Event()
        self.t_fire: float | None = None
        self.fallback_used = False
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        try:
            for line in self._proc.stdout:
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                if line.strip() == self.READY:
                    self.ready.set()
        except Exception:
            pass

    def wait_ready(self, timeout_s: float) -> bool:
        return self.ready.wait(timeout_s)

    def fire(self) -> float | None:
        self.fired = True
        if self._proc.poll() is None:
            t = self._clock()
            try:
                self._proc.stdin.write(b"go\n")
                self._proc.stdin.flush()
                self.t_fire = t
                return None
            except (OSError, ValueError):
                pass
        # session died before fire: ordinary enable (late, but the stream runs)
        self.fallback_used = True
        self._enable()
        self.t_fire = self._clock()
        return self.t_fire

    def confirm(self, timeout_s: float = 5.0) -> dict:
        info = {"armed": True, "armed_ready": self.ready.is_set(),
                "enable_fallback": self.fallback_used}
        if self.fallback_used or self.t_fire is None:
            return info
        try:
            rc = self._proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            rc = None
        info["enable_rtt_s"] = round(self._clock() - self.t_fire, 4)
        if rc != 0:
            # the armed write did not happen (or not provably): do it now
            self.close()
            self.fallback_used = True
            info["enable_fallback"] = True
            self._enable()
            info["fire_mono"] = self._clock()    # the engine started no earlier
        return info

    def close(self) -> None:
        p = self._proc
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
        for f in (p.stdin, p.stdout, p.stderr):
            try:
                if f is not None:
                    f.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Remote board
# ---------------------------------------------------------------------------
Runner = Callable[[list], str]   # runs argv on the board, returns stdout


class SshRunner:
    """``ssh [-o ...] user@host <argv>``; raises DeviceError on failure."""

    def __init__(self, host: str, user: str = "root", port: int | None = None,
                 timeout_s: float = 5.0, ssh: str = "ssh"):
        self.host, self.user, self.port = host, user, port
        self.timeout_s, self.ssh = timeout_s, ssh

    @classmethod
    def from_uri(cls, uri: str, **kw) -> "SshRunner":
        """``ip:192.168.3.1`` (libiio network URI) -> runner for that host."""
        m = re.match(r"^(?:ip:)?([^:/]+)", uri or "")
        if not m:
            raise DeviceError(f"cannot derive an SSH host from URI {uri!r}")
        return cls(m.group(1), **kw)

    def _cmd(self, argv: list) -> list:
        cmd = [self.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=3"]
        if self.port:
            cmd += ["-p", str(self.port)]
        cmd += [f"{self.user}@{self.host}", " ".join(shlex.quote(str(a)) for a in argv)]
        return cmd

    def spawn(self, argv: list) -> subprocess.Popen:
        """Start ``argv`` on the board and return the running ssh process with
        binary stdin/stdout/stderr pipes (for a pre-connected command)."""
        try:
            return subprocess.Popen(self._cmd(argv), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    bufsize=0)
        except OSError as ex:
            raise DeviceError(f"ssh to {self.host} failed: {ex}") from ex

    def __call__(self, argv: list) -> str:
        cmd = self._cmd(argv)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout_s)
        except (OSError, subprocess.TimeoutExpired) as ex:
            raise DeviceError(f"ssh to {self.host} failed: {ex}") from ex
        if r.returncode != 0:
            raise DeviceError(f"`{' '.join(map(str, argv))}` on {self.host} "
                              f"exited {r.returncode}: {r.stderr.strip()}")
        return r.stdout


class RemoteDevmem(GnssDevice):
    """Registers through busybox ``devmem <addr> 32 [value]`` on the board.

    ``base`` is the physical address of the engine's AXI-Lite window.
    """

    def __init__(self, runner: Runner, base: int, tool: str = "devmem"):
        self._run, self.base, self.tool = runner, int(base), tool

    def read32(self, offset: int) -> int:
        out = self._run([self.tool, f"{self.base + offset:#x}", "32"]).strip()
        try:
            return int(out.split()[-1], 0) & 0xFFFFFFFF
        except (ValueError, IndexError) as ex:
            raise DeviceError(f"unparsable devmem output {out!r}") from ex

    def read_many(self, offsets) -> list:
        """One ssh round trip for all offsets (devmem in a board-side loop)."""
        addrs = " ".join(f"{self.base + o:#x}" for o in offsets)
        out = self._run(["sh", "-c",
                         f"for a in {addrs}; do {self.tool} $a 32; done"])
        try:
            vals = [int(t, 0) & 0xFFFFFFFF for t in out.split()]
        except ValueError as ex:
            raise DeviceError(f"unparsable devmem output {out!r}") from ex
        if len(vals) != len(offsets):
            raise DeviceError(f"devmem batch returned {len(vals)} of "
                              f"{len(offsets)} values")
        return vals

    def write32(self, offset: int, value: int) -> None:
        self._run([self.tool, f"{self.base + offset:#x}", "32",
                   f"{value & 0xFFFFFFFF:#x}"])

    def _armed_enable_cmd(self) -> str:
        # Armed only in the command-mode start sequence (CTRL = CMD_MODE), so
        # the enable read-modify-write result is known: ENABLE | CMD_MODE.
        return (f"{self.tool} {self.base + REG_CTRL:#x} 32 "
                f"{CTRL_ENABLE | CTRL_CMD_MODE:#x}")

    def arm_enable(self) -> ArmedEnable:
        spawn = getattr(self._run, "spawn", None)
        if spawn is None:
            return super().arm_enable()
        script = f"echo {SshArmedEnable.READY}; read _go && {self._armed_enable_cmd()}"
        try:
            proc = spawn(["sh", "-c", script])
        except DeviceError:
            return super().arm_enable()
        return SshArmedEnable(proc, self.enable)


class GnssctlDevice(RemoteDevmem):
    """Like RemoteDevmem, but the control verbs go through the board-side
    helper ``gnssctl`` (id / status / enable / disable / cmdmode on|off /
    soft_reset / clear). Reads of single registers still use devmem."""

    def __init__(self, runner: Runner, base: int, ctl: str = "gnssctl",
                 tool: str = "devmem"):
        super().__init__(runner, base, tool)
        self.ctl = ctl

    def _ctl(self, *args) -> str:
        return self._run([self.ctl, *args])

    def check_id(self) -> int:
        out = self._ctl("id").strip()
        m = re.search(r"0x[0-9a-fA-F]+|\b\d+\b", out)
        v = int(m.group(0), 0) if m else -1
        if v != GNSS_ID:
            raise IdMismatch(f"GNSS engine ID {out!r}, expected {GNSS_ID:#010x}")
        return v

    def set_cmd_mode(self, on: bool) -> None:
        self._ctl("cmdmode", "on" if on else "off")

    def _armed_enable_cmd(self) -> str:
        return f"{shlex.quote(self.ctl)} enable"

    def enable(self) -> None:
        self._ctl("enable")

    def disable(self) -> None:
        self._ctl("disable")

    def clear_counters(self) -> None:
        self._ctl("clear")

    def soft_reset(self, timeout_s: float = SOFT_RESET_TIMEOUT_S) -> None:
        self._ctl("soft_reset")


# ---------------------------------------------------------------------------
# Fake (tests)
# ---------------------------------------------------------------------------
class FakeEngine:
    """Behavioural consumer mimicking gs_wrap/gs_top: parses command words,
    runs the bit-exact model per block record and keeps the counters.

    Words are consumed only in command mode; blocks are played (processed)
    only while enabled (``play`` is called from ``FakeDevice.enable`` and on
    every push when enabled). Output samples are kept in ``samples``.
    """

    def __init__(self, nch_max: int = W.MAX_CH, keep_samples: bool = True):
        self.nch_max = nch_max
        self.keep_samples = keep_samples
        self.reset()

    def reset(self) -> None:
        from backend.fpga.model import EngineModel
        self.model = None
        self._EngineModel = EngineModel
        self.pending = bytearray()
        self.words_seen = 0
        self.bad_words = 0
        self.records = []          # decoded items, in order
        self.samples = []          # int16 (n, 2) arrays, in play order
        self.play_idx = 0
        self.cfg = None

    def push(self, data: bytes) -> None:
        self.pending += data

    def play(self) -> None:
        """Decode and play everything queued so far."""
        n = len(self.pending) // 4
        if not n:
            return
        data = bytes(self.pending[:n * 4])
        del self.pending[:n * 4]
        self.words_seen += n
        items = cmdfmt.unpack_stream(data)
        used = sum(cmdfmt.CFG_WORDS if isinstance(i, cmdfmt.CfgCommand)
                   else cmdfmt.BLOCK_HDR_WORDS + cmdfmt.CH_WORDS * len(i[1])
                   for i in items)
        self.bad_words += n - used
        for it in items:
            self.records.append(it)
            if isinstance(it, cmdfmt.CfgCommand):
                self.cfg = it
                self.model = self._EngineModel(float(it.fs_hz), self.nch_max)
                self.model.start(it.start_block)
                continue
            if self.model is None:
                self.bad_words += 1
                continue
            idx, chans = it
            out = self.model.feed_block(idx, chans)
            self.play_idx += len(out) // W.BLOCK   # the register counts blocks
            if self.keep_samples and len(out):
                self.samples.append(out)

    @property
    def underruns(self) -> int:
        return self.model.underruns if self.model else 0

    @property
    def dropped_late(self) -> int:
        return self.model.dropped_late if self.model else 0

    @property
    def clipped(self) -> int:
        return self.model.clipped if self.model else 0


class FakeDevice(GnssDevice):
    """In-memory register file. With ``engine`` (a FakeEngine) the counters,
    play index and status bits follow the words pushed via ``push_words``."""

    def __init__(self, engine: FakeEngine | None = None, dev_id: int = GNSS_ID,
                 absent: bool = False):
        self.engine = engine
        self.regs = {REG_ID: dev_id}
        self.ctrl = 0
        self.absent = absent
        self.log = []                 # (op, ...) history of control writes
        self._base = {k: 0 for k in _COUNTER_REGS}
        self.play_hi_glitch = 0       # >0: HI changes between the next reads
        self.soft_reset_stuck = False
        self.inject = {}              # counter name -> extra count

    # words coming from the (fake) DAC DMA
    def push_words(self, data: bytes) -> None:
        if self.engine is None or not (self.ctrl & CTRL_CMD_MODE):
            return
        self.engine.push(data)
        if self.ctrl & CTRL_ENABLE:
            self.engine.play()

    def read32(self, offset: int) -> int:
        if self.absent:
            raise DeviceError("device absent (no response)")
        if offset == REG_CTRL:
            return self.ctrl | (CTRL_SOFT_RESET if self.soft_reset_stuck else 0)
        if offset == REG_STATUS:
            return self._status()
        if offset in (REG_PLAY_LO, REG_PLAY_HI):
            idx = self.engine.play_idx if self.engine else self.regs.get(REG_PLAY_LO, 0)
            if offset == REG_PLAY_HI:
                if self.play_hi_glitch > 0:
                    self.play_hi_glitch -= 1
                    return ((idx >> 32) + self.play_hi_glitch + 1) & 0xFFFFFFFF
                return (idx >> 32) & 0xFFFFFFFF
            return idx & 0xFFFFFFFF
        for name, r in _COUNTER_REGS.items():
            if r == offset:
                return self._counter(name)
        return self.regs.get(offset, 0)

    def write32(self, offset: int, value: int) -> None:
        if self.absent:
            raise DeviceError("device absent (no response)")
        value &= 0xFFFFFFFF
        if offset != REG_CTRL:
            self.regs[offset] = value
            return
        self.log.append(("ctrl", value))
        self.ctrl = value & (CTRL_ENABLE | CTRL_CMD_MODE)
        if value & CTRL_SOFT_RESET:
            if self.engine:
                self.engine.reset()
            self._base = {k: 0 for k in _COUNTER_REGS}
            self.inject = {}
        if value & CTRL_CLEAR:
            self._base = {k: self._raw(k) for k in _COUNTER_REGS}
        if self.engine and (self.ctrl & CTRL_ENABLE) and (self.ctrl & CTRL_CMD_MODE):
            self.engine.play()

    def _raw(self, name: str) -> int:
        e = self.engine
        raw = {"underruns": e.underruns if e else 0,
               "dropped_late": e.dropped_late if e else 0,
               "clipped": e.clipped if e else 0,
               "chip_late": 0,
               "bad_words": e.bad_words if e else 0,
               "sample_fifo_underflows": 0, "sample_fifo_level": 0,
               "cmd_fifo_overruns": 0}[name]
        return raw + self.inject.get(name, 0)

    def _counter(self, name: str) -> int:
        if name == "sample_fifo_level":
            return self._raw(name)
        return (self._raw(name) - self._base[name]) & 0xFFFFFFFF

    def _status(self) -> int:
        en = bool(self.ctrl & CTRL_ENABLE)
        cm = bool(self.ctrl & CTRL_CMD_MODE)
        e = self.engine
        queued = (len(e.pending) // 4) if e else 0
        s = ST_CMD_FIFO_OK
        if en and cm:
            s |= ST_RUNNING
            if e and e.play_idx > 0:
                s |= ST_PLAYING
        if cm:
            s |= ST_CMD_MODE
        if not queued:
            s |= ST_CMD_EMPTY
        return s | ((min(queued, 0xFFFF)) << 16)
