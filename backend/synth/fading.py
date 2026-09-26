"""Per-SV fading configuration and truth-side preview.

Two models share one gain process shape (log-normal in dB, smoothstep
between knots):

* ``lognormal`` -- seeded and reproducible: knots on a fixed
  ``coherence_s`` grid, values from a splitmix64 hash of (seed, prn, knot).
  Anyone who knows the seed (or brute-forces a small one) can predict it.
* ``keyed`` -- cryptographically unpredictable: knot values, knot jitter and
  each SV's grid phase/spacing come from ChaCha20 under a 256-bit key.
  Without the key, no amount of observed IQ predicts the next value. When
  the request carries no key, a fresh one is drawn from the OS CSPRNG for
  that run and discarded unless ``record_key`` asks for it to be kept.

The fading *gain model* is a published model shared with synthesis on purpose:
``predicted_metric_db`` calls the same C ``fading_gain_linear`` the native
engine applies, so the preview matches what the engine bakes into the IQ.
Acquisition parity still measures the generated IQ independently.
"""
from __future__ import annotations

import hashlib
import math
import secrets
from dataclasses import dataclass

_MODELS = {"off", "lognormal", "keyed"}
MODEL_INT = {"off": 0, "lognormal": 1, "keyed": 2}
KEY_BYTES = 32


def _parse_key(v) -> bytes | None:
    if v is None or v == "":
        return None
    try:
        key = bytes.fromhex(str(v).strip())
    except ValueError:
        raise ValueError("fading.key must be hex") from None
    if len(key) != KEY_BYTES:
        raise ValueError(f"fading.key must be {KEY_BYTES} bytes "
                         f"({2 * KEY_BYTES} hex digits)")
    return key


@dataclass(frozen=True)
class FadingConfig:
    model: str = "off"
    sigma_db: float = 0.0
    coherence_s: float = 1.0
    seed: int = 0
    # keyed model only
    key: bytes | None = None
    record_key: bool = False

    @staticmethod
    def from_dict(d: dict | None) -> "FadingConfig":
        d = d or {}
        model = d.get("model", "off")
        if model not in _MODELS:
            raise ValueError(f"fading.model must be one of {_MODELS}")
        sigma = float(d.get("sigma_db", 0.0))
        coh = float(d.get("coherence_s", 1.0))
        if sigma < 0:
            raise ValueError("fading.sigma_db must be >= 0")
        if coh <= 0:
            raise ValueError("fading.coherence_s must be > 0")
        return FadingConfig(model, sigma, coh, int(d.get("seed", 0)),
                            _parse_key(d.get("key")),
                            bool(d.get("record_key", False)))

    def enabled(self) -> bool:
        return self.model != "off" and self.sigma_db > 0.0

    def for_run(self) -> "FadingConfig":
        """The config one synthesis run uses: a keyed model with no key gets a
        fresh 256-bit key from the OS CSPRNG."""
        if self.model == "keyed" and self.key is None:
            return FadingConfig(self.model, self.sigma_db, self.coherence_s,
                                self.seed, secrets.token_bytes(KEY_BYTES),
                                self.record_key)
        return self

    def key_id(self) -> str | None:
        """Short public fingerprint of the key (safe to record)."""
        if self.key is None:
            return None
        return hashlib.sha256(b"gps-sim fading key id" + self.key).hexdigest()[:16]

    def fill(self, c, sysc: str) -> None:
        """Fill a ctypes ``_lib.FadingCfg`` for one SV of system ``sysc``."""
        c.model = MODEL_INT[self.model]
        c.sigma_db = self.sigma_db
        c.coherence_s = self.coherence_s
        c.seed = self.seed
        c.domain = ord(sysc[0]) if sysc else 0
        if self.model == "keyed":
            if self.key is None:
                raise ValueError("keyed fading needs a key (use for_run())")
            for i, b in enumerate(self.key):
                c.key[i] = b


def predicted_metric_db(prn: int, t_s: float, cfg: FadingConfig,
                        base_metric_db: float, sysc: str = "G") -> float:
    """Shift a nominal acquisition metric by the same per-SV fading gain the
    synthesis applies at ``t_s``, so the preview tracks generation. Returns
    ``base_metric_db`` unchanged when fading is disabled."""
    if not cfg.enabled():
        return base_metric_db
    return base_metric_db + _gain_db(prn, t_s, cfg, sysc)


def _gain_db(prn: int, t_s: float, cfg: FadingConfig, sysc: str = "G") -> float:
    import ctypes

    from backend.synth import _lib
    lib = _lib.load_lib()
    _lib.bind_fading(lib)
    c = _lib.FadingCfg()
    cfg.fill(c, sysc)
    g = float(lib.fading_gain_linear(ctypes.byref(c), prn, float(t_s)))
    return 20.0 * math.log10(max(g, 1e-9))
