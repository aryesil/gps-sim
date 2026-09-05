"""Provenance helpers: content hashes and a stable scenario fingerprint.

A recording must be reproducible from its ``meta.json`` alone -- never from
a filename. These helpers give ``generator.run`` the hashes and the
scenario fingerprint it records.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def sha256_file(path: str | pathlib.Path) -> str | None:
    p = pathlib.Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(obj) -> str:
    """Order-independent hash of a JSON-serialisable object."""
    return sha256_text(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                  default=str))


def scenario_hash(req) -> str:
    """Stable fingerprint of the *physical* scenario -- the inputs that
    determine the signal, not incidental fields (output paths, binary
    location). Two requests with the same fingerprint should produce the
    same IQ up to documented numerical limits.
    """
    fields = {
        "lat": req.lat, "lon": req.lon, "alt": req.alt,
        "start_utc": req.start.isoformat(),
        "duration_s": req.duration_s,
        "sample_rate": req.sample_rate,
        "sample_format": req.sample_format,
        "route": req.route,
        "ionosphere": getattr(req, "ionosphere", False),
        "ephemeris_mode": "precise" if getattr(req, "nav_override", None) is not None
                          else "broadcast",
        "atmosphere": getattr(req, "atmosphere", None),
        "impairments": getattr(req, "impairments", None),
        "receiver_clock": getattr(req, "receiver_clock", None),
        "random_seed": getattr(req, "random_seed", None),
    }
    return sha256_json(fields)


def git_revision() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5)
        rev = out.stdout.strip()
        if rev:
            dirty = subprocess.run(
                ["git", "-C", str(_ROOT), "status", "--porcelain"],
                capture_output=True, text=True, timeout=5).stdout.strip()
            return f"git:{rev}{'+dirty' if dirty else ''}"
    except (OSError, subprocess.SubprocessError):
        pass
    return "git:unknown"
