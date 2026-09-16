"""SDR hardware backends: one module per `kind`, sharing a duck-typed
interface (open_tx, set_gain, write, close, probe_open, probe_info) --
see ad9361.py and bladerf.py."""
