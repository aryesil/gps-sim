import subprocess
import pathlib

import pytest

_NATIVE = pathlib.Path(__file__).parent.parent.parent / "backend" / "synth" / "native"


@pytest.fixture(scope="session", autouse=True)
def _build_native_lib():
    subprocess.run(["make", "-C", str(_NATIVE)], check=True, capture_output=True)
