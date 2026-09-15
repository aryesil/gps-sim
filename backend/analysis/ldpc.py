"""Systematic rate-1/2 LDPC(576,288) -- BeiDou B-CNAV2 forward error
correction, documented compromise.

The real BDS-SIS-ICD-B2a-1.0 LDPC is a NON-BINARY code over GF(2^6): 96
GF(64) symbols (576 bits) from 48 GF(64) symbols (288 bits), with a
parity-check matrix whose entries are specific GF(64) elements from the
official ICD. That matrix was not obtainable for this project (it sits deep
in a lengthy, largely paywalled ICD PDF -- transcribing it from memory would
risk silent numeric corruption of a receiver-critical table, exactly the
failure mode this project avoids elsewhere by sourcing real per-PRN tables
verbatim from GNSS-SDR). Notably, GNSS-SDR's own real, merged B-CNAV2
decoder (``beidou_cnav2_navigation_message.cc``) does not implement the
GF(64) LDPC either -- its comment reads verbatim: "First cut: skip 64-ary
LDPC. Take the first 288 systematic bits of the 576 encoded symbols."

This module goes one step further than that shortcut while staying
honest about what it is: a *real*, self-consistent BINARY systematic
LDPC(576,288) rate-1/2 code -- not the ICD's GF(64) matrix, but an actual
sparse low-density parity-check code with real encode/decode:

* ``encode``: 288 systematic message bits + 288 parity bits, parity_i =
  XOR of ``ROW_DEGREE`` message bits selected by a deterministic (seeded)
  sparse parity matrix ``P`` -- the codeword's own parity-check matrix is
  ``H = [P | I_288]`` (low density: each parity bit touches a small,
  fixed number of message bits).
* ``decode``: recomputes parity from the received systematic bits and
  compares against the received parity bits (the syndrome). On a clean
  channel (this project's IQ path has no channel noise model) the
  syndrome is always zero and the systematic bits are the message,
  unchanged -- matching the spec's own bar, "sufficient for the clean
  channel". When the syndrome is non-zero, a bounded single-bit-flip
  search over the message bits is attempted (sufficient to correct an
  isolated bit error) before giving up and returning the raw systematic
  bits with ``ok=False``.

Same compromise pattern used throughout this codebase for data that
cannot be reliably sourced (GPS L2C/L5 per-PRN seed tables, NavIC's
G2-init bit mapping): real structure, explicitly-documented placeholder
data, self-consistent round trip.
"""
from __future__ import annotations

MSG_BITS = 288
PARITY_BITS = 288
CODE_BITS = MSG_BITS + PARITY_BITS
ROW_DEGREE = 6          # number of message bits each parity bit XORs
_SEED = 0xB2A5          # arbitrary, fixed -- deterministic P matrix


def _lcg(seed: int):
    """Tiny deterministic LCG so this module has zero external RNG
    dependency and the parity matrix is stable across Python versions."""
    state = seed & 0xFFFFFFFF
    while True:
        state = (1103515245 * state + 12345) & 0xFFFFFFFF
        yield state


def _build_parity_rows() -> list[tuple[int, ...]]:
    gen = _lcg(_SEED)
    rows = []
    for _ in range(PARITY_BITS):
        cols: set[int] = set()
        while len(cols) < ROW_DEGREE:
            cols.add(next(gen) % MSG_BITS)
        rows.append(tuple(sorted(cols)))
    return rows


_ROWS = _build_parity_rows()


def _parity(msg: list[int]) -> list[int]:
    out = [0] * PARITY_BITS
    for i, cols in enumerate(_ROWS):
        v = 0
        for c in cols:
            v ^= int(msg[c]) & 1
        out[i] = v
    return out


def encode(msg_bits) -> list[int]:
    """288 systematic message bits -> 576-bit codeword (msg + parity)."""
    msg = [int(b) & 1 for b in msg_bits]
    if len(msg) != MSG_BITS:
        raise ValueError(f"ldpc.encode expects {MSG_BITS} message bits, got {len(msg)}")
    return msg + _parity(msg)


def decode(codeword) -> tuple[list[int], bool]:
    """576-bit codeword -> (message[288], ok). ``ok`` is True when the
    parity check passes outright, or after a single-bit correction; False
    (message still returned, best-effort) when neither succeeds."""
    cw = [int(b) & 1 for b in codeword]
    if len(cw) != CODE_BITS:
        raise ValueError(f"ldpc.decode expects {CODE_BITS} bits, got {len(cw)}")
    msg = cw[:MSG_BITS]
    rx_parity = cw[MSG_BITS:]
    if _parity(msg) == rx_parity:
        return msg, True
    # Bounded single-bit-flip search: try flipping each message bit and
    # recheck. O(MSG_BITS) parity recomputes -- cheap, and sufficient to
    # correct an isolated bit error (Gallager-B style, simplified for a
    # clean channel that essentially never needs it).
    for i in range(MSG_BITS):
        trial = list(msg)
        trial[i] ^= 1
        if _parity(trial) == rx_parity:
            return trial, True
    return msg, False
