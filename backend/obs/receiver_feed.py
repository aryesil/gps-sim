# backend/receiver_feed.py
"""Closed-loop receiver feedback: listen for NMEA sentences from a real
GNSS receiver (serial port, or UDP -- most u-blox/UBX-capable receivers
and gpsd can forward plain NMEA over UDP) sitting in front of the
simulator's RF output, and keep the latest parsed fix so the UI can show
"what the simulator commanded" next to "what real hardware reported".

One listener at a time (this rig has one antenna path in front of the
receiver); starting a new one stops whatever was running.
"""
from __future__ import annotations

import socket
import threading

from backend.obs import nmea

_lock = threading.Lock()
_state = {"thread": None, "stop": None, "latest": None, "mode": None}


def inject(sentence: str) -> dict | None:
    """Feed one NMEA sentence directly into the parser/state -- used by
    the real listener loops below, and directly by tests / a manual dev
    endpoint when no physical receiver is attached."""
    parsed = nmea.parse(sentence)
    if parsed is not None:
        with _lock:
            _state["latest"] = parsed
    return parsed


def latest_fix() -> dict | None:
    with _lock:
        return _state["latest"]


def is_listening() -> bool:
    with _lock:
        return _state["thread"] is not None and _state["thread"].is_alive()


def _udp_loop(host: str, port: int, stop: threading.Event):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    try:
        sock.bind((host, port))
        while not stop.is_set():
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            for line in data.decode(errors="ignore").splitlines():
                inject(line)
    finally:
        sock.close()


def _serial_loop(device: str, baud: int, stop: threading.Event):
    import serial  # imported lazily: only needed when serial mode is used
    with serial.Serial(device, baud, timeout=0.5) as ser:
        while not stop.is_set():
            line = ser.readline().decode(errors="ignore")
            if line:
                inject(line)


def start_listen(mode: str, **kwargs) -> None:
    """mode='udp' needs host/port; mode='serial' needs device/baud."""
    stop_listen()
    stop = threading.Event()
    if mode == "udp":
        target = _udp_loop
        args = (kwargs.get("host", "0.0.0.0"), int(kwargs["port"]), stop)
    elif mode == "serial":
        target = _serial_loop
        args = (kwargs["device"], int(kwargs.get("baud", 9600)), stop)
    else:
        raise ValueError(f"unknown receiver feed mode {mode!r}")
    th = threading.Thread(target=target, args=args, daemon=True)
    with _lock:
        _state["thread"] = th
        _state["stop"] = stop
        _state["mode"] = mode
        _state["latest"] = None
    th.start()


def stop_listen() -> None:
    with _lock:
        stop = _state["stop"]
        thread = _state["thread"]
        _state["thread"] = None
        _state["stop"] = None
        _state["mode"] = None
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout=2.0)
