"""SDR hardware backends: one module per `kind`, sharing a duck-typed
interface (open_tx, set_gain, write, close, probe_open, probe_info) --
see ad9361.py and bladerf.py. dual_tx.py and device.py both dispatch
through this single registry so the two never drift out of sync on
which `kind` strings are valid."""
from backend.rf.backends import ad9361, ad9361_cs4, ad9361_cs8, ad9361_gnss, bladerf, tezuka

BACKENDS = {"pluto": ad9361, "pluto_cs8": ad9361_cs8,
            "pluto_cs4": ad9361_cs4, "tezuka": tezuka, "bladerf": bladerf,
            "tezuka_gnss": ad9361_gnss}
