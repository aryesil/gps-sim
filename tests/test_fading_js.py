"""frontend/js/fading.js must reproduce the native channel gain, so the
scrubbed per-SV power bars show what the IQ actually carries."""
import json
import pathlib
import shutil
import subprocess

import pytest

from backend.synth import fading

_JS = pathlib.Path(__file__).parent.parent / "frontend" / "js" / "fading.js"
_KEY = bytes(range(7, 39)).hex()

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")


def _node(expr: str):
    src = _JS.read_text() + f"\nconsole.log(JSON.stringify({expr}));\n"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True,
                         check=True)
    return json.loads(out.stdout)


def test_js_chacha20_rfc8439_block_vector():
    words = _node("globalThis._fadingChachaBlock("
                  "[0x03020100,0x07060504,0x0b0a0908,0x0f0e0d0c,"
                  "0x13121110,0x17161514,0x1b1a1918,0x1f1e1d1c],"
                  "1, 0x09000000, 0x4a000000, 0)")
    got = b"".join(int(w).to_bytes(4, "little") for w in words).hex()
    assert got.startswith("10f1e7e4d13b5915500fdd1fa32071c4")


# (sys, prn, t, environment, speed, carrier, elevation)
_CASES = [("G", 5, 0.1, "urban", 10.0, 1575.42e6, 15.0),
          ("E", 5, 0.1, "urban", 10.0, 1575.42e6, 15.0),
          ("C", 30, 12.34, "suburban", 1.4, 1176.45e6, 40.0),
          ("R", 3, 99.9, "rural", 0.0, 1602.5625e6, 70.0),
          ("G", 17, 250.0, "open", 25.0, 1227.60e6, 5.0),
          ("J", 2, 0.0, "urban", 3.0, 1575.42e6, 88.0)]


@pytest.mark.parametrize("model", ["seeded", "keyed"])
def test_js_matches_native(model):
    svs, want = [], []
    for s, p, t, env, v, car, el in _CASES:
        d = {"model": model, "environment": env, "speed_mps": v, "seed": 11,
             "key": _KEY}
        cfg = fading.FadingConfig.from_dict(d)
        want.append(fading.gain(p, t, cfg, s, car, el))
        svs.append({"sys": s, "prn": p, "fading_model": fading.MODEL_INT[model],
                    "fading_env": env, "fading_speed_mps": v,
                    "fading_carrier_hz": car, "fading_el_deg": el,
                    "fading_seed": 11, "fading_key": _KEY})
    js = _node("[" + ",".join(
        f"globalThis.fadingGain({json.dumps(sv)}, {c[2]})"
        for sv, c in zip(svs, _CASES)) + "]")
    for c, got, w in zip(_CASES, js, want):
        assert complex(*got) == pytest.approx(w, abs=1e-9), c


def test_js_keyed_without_recorded_key_is_flat():
    sv = {"sys": "G", "prn": 5, "fading_model": 2, "fading_env": "urban",
          "fading_speed_mps": 3.0, "fading_carrier_hz": 1575.42e6,
          "fading_el_deg": 30.0}
    assert _node(f"globalThis.fadingGainDb({json.dumps(sv)}, 4.2)") == 0
