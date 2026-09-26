"""Per-SV land-mobile-satellite channel: configuration and truth-side preview.

The native engine multiplies each satellite's line-of-sight signal by a
complex channel gain from a three-state Loo model (see
``backend/synth/native/fading.hpp``):

* a slow, spatially correlated state process switches the direct path
  between line-of-sight, shadowed and blocked with elevation-dependent
  probabilities for the chosen environment (open / rural / suburban /
  urban);
* within a state the direct path is log-normal (mean and spread per state,
  exponentially correlated shadowing);
* diffuse multipath is complex Gaussian (Rayleigh envelope) with a Jakes
  Doppler spectrum of spread ``speed / wavelength``.

The state and shadowing processes are shared by all bands of one satellite;
the diffuse part is independent per carrier. Random numbers come from
ChaCha20 under a 256-bit key:

* ``seeded`` -- the key is derived from ``seed``, so a run is reproducible
  from the seed (and predictable by anyone who knows or guesses it).
* ``keyed`` -- the key is the caller's, or a fresh one from the OS CSPRNG
  for that run, discarded unless ``record_key`` keeps it in meta.json.

``gain_db`` calls the same C channel process the engine applies, so a
preview matches what the engine bakes into the IQ.
"""
from __future__ import annotations

import ctypes
import hashlib
import math
import secrets
from dataclasses import dataclass

# "lognormal" is the pre-ABI-28 name of the seeded model.
_ALIASES = {"lognormal": "seeded"}
_MODELS = {"off", "seeded", "keyed"}
MODEL_INT = {"off": 0, "seeded": 1, "keyed": 2}
ENVIRONMENTS = {"open": 0, "rural": 1, "suburban": 2, "urban": 3}
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
    environment: str = "suburban"
    speed_mps: float = 0.0
    seed: int = 0
    # keyed model only
    key: bytes | None = None
    record_key: bool = False

    @staticmethod
    def from_dict(d: dict | None) -> "FadingConfig":
        d = d or {}
        model = _ALIASES.get(d.get("model", "off"), d.get("model", "off"))
        if model not in _MODELS:
            raise ValueError(f"fading.model must be one of {sorted(_MODELS)}")
        env = d.get("environment", "suburban")
        if env not in ENVIRONMENTS:
            raise ValueError(
                f"fading.environment must be one of {sorted(ENVIRONMENTS)}")
        speed = float(d.get("speed_mps", 0.0))
        if not (speed >= 0.0 and math.isfinite(speed)):
            raise ValueError("fading.speed_mps must be a finite value >= 0")
        return FadingConfig(model, env, speed, int(d.get("seed", 0)),
                            _parse_key(d.get("key")),
                            bool(d.get("record_key", False)))

    def enabled(self) -> bool:
        return self.model != "off"

    def for_run(self) -> "FadingConfig":
        """The config one synthesis run uses: a keyed model with no key gets a
        fresh 256-bit key from the OS CSPRNG."""
        if self.model == "keyed" and self.key is None:
            return FadingConfig(self.model, self.environment, self.speed_mps,
                                self.seed, secrets.token_bytes(KEY_BYTES),
                                self.record_key)
        return self

    def key_id(self) -> str | None:
        """Short public fingerprint of the key (safe to record)."""
        if self.key is None:
            return None
        return hashlib.sha256(b"gps-sim fading key id" + self.key).hexdigest()[:16]

    def fill(self, c, sysc: str, carrier_hz: float, el_deg: float) -> None:
        """Fill a ctypes ``_lib.FadingCfg`` for one signal of system ``sysc``
        on ``carrier_hz`` from a satellite at ``el_deg``."""
        c.model = MODEL_INT[self.model]
        c.env = ENVIRONMENTS[self.environment]
        c.speed_mps = self.speed_mps
        c.carrier_hz = float(carrier_hz)
        c.el_deg = float(el_deg)
        c.seed = self.seed & 0xFFFFFFFFFFFFFFFF
        c.domain = ord(sysc[0]) if sysc else 0
        if self.model == "keyed":
            if self.key is None:
                raise ValueError("keyed fading needs a key (use for_run())")
            for i, b in enumerate(self.key):
                c.key[i] = b


def _cfg(cfg: FadingConfig, sysc: str, carrier_hz: float, el_deg: float):
    from backend.synth import _lib
    lib = _lib.load_lib()
    _lib.bind_fading(lib)
    c = _lib.FadingCfg()
    cfg.fill(c, sysc, carrier_hz, el_deg)
    return lib, c


def gain(prn: int, t_s: float, cfg: FadingConfig, sysc: str = "G",
         carrier_hz: float = 1575.42e6, el_deg: float = 45.0) -> complex:
    """Complex channel gain the engine applies to this signal at ``t_s``."""
    lib, c = _cfg(cfg, sysc, carrier_hz, el_deg)
    out = (ctypes.c_double * 2)()
    lib.fading_gain_complex(ctypes.byref(c), prn, float(t_s), out)
    return complex(out[0], out[1])


def gain_db(prn: int, t_s: float, cfg: FadingConfig, sysc: str = "G",
            carrier_hz: float = 1575.42e6, el_deg: float = 45.0) -> float:
    """Channel power gain in dB at ``t_s`` (0 dB when fading is off)."""
    if not cfg.enabled():
        return 0.0
    return 20.0 * math.log10(max(abs(gain(prn, t_s, cfg, sysc, carrier_hz,
                                          el_deg)), 1e-9))


def params(cfg: FadingConfig, sysc: str = "G", carrier_hz: float = 1575.42e6,
           el_deg: float = 45.0, prn: int = 1) -> dict:
    """Derived model parameters (Doppler spread, grids, state probabilities)."""
    lib, c = _cfg(cfg, sysc, carrier_hz, el_deg)
    out = (ctypes.c_double * 9)()
    lib.fading_params(ctypes.byref(c), prn, out)
    keys = ("doppler_hz", "dt_state_s", "dt_shadow_s", "dt_diffuse_s",
            "dt_knot_s", "p_los", "p_shadow", "z_los", "z_shadow")
    return dict(zip(keys, list(out)))
