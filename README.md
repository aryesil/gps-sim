# GPS L1 C/A Signal Simulator

Interactive scenario workbench: pick a place and time on a map, generate a GPS L1
C/A baseband IQ file with `gps-sdr-sim`, inspect it, verify it with an internal
software receiver, and stream it to a PlutoSDR-class SDR for replay.

## Setup

    ./scripts/setup.sh
    ./scripts/run_server.sh

Open http://127.0.0.1:8000

`setup.sh` creates a `.venv`, installs the Python dependencies into it, and
(for the transmit path only) builds `libiio` and `libad9361-iio` from source
straight into `.venv`'s own prefix -- no system-wide install, no Homebrew
formula (neither ships one). `run_server.sh` activates `.venv` and points the
dynamic linker at its `lib/` before starting uvicorn, so nothing extra needs
sourcing by hand.

**Prerequisites** (only needed to build the transmit path's native deps):
`cmake`, a C compiler (Xcode Command Line Tools on macOS / `build-essential`
on Debian), and `libusb` (`brew install libusb` / `apt install libusb-1.0-0-dev`).
Without these, `setup.sh` still completes -- generation, inspection, and the
internal receiver check all work; only PlutoSDR-class hardware transmit needs
`libiio`.

## Safety

**Do not transmit RF over the air.** Operation outside an appropriately
authorized and controlled test environment may violate applicable spectrum
regulations and can interfere with GNSS receivers.

If RF replay is required, use only an appropriately authorized, fully
shielded or conducted laboratory setup and follow the applicable regulatory
and equipment requirements. Use a cabled or shielded (Faraday) setup only.
When feeding a receiver over cable, use low TX gain (-40 to -60 dB) plus
30-60 dB in-line attenuation. Transmit is disabled unless `ALLOW_TX=1` and
you tick each channel card's "Isolated/cabled setup confirmed" checkbox
(the server rejects a start request without it with HTTP 403).

This software is provided for signal-generation and receiver-testing
purposes. Responsibility for lawful, authorized operation rests entirely
with the person operating it; the authors accept no liability for misuse.

## Manual hardware acceptance checklist

1. `ALLOW_TX=1 python -m uvicorn backend.app:app`
2. Generate a static scenario for your location, current UTC, 300 s, 2.6 Msps int16.
3. Receiver check — expect a fix within 100 m of the marker. **Not currently
   exposed in the UI**: the per-channel redesign dropped the receiver panel,
   so call the endpoint directly, e.g.
   `curl -s localhost:8000/api/receiver -H 'Content-Type: application/json' -d '{"outdir": "<generated dir name>"}'`.
   The same applies to `/api/lnav`, `/api/correlation` and `/api/preview_track`
   — all four still work and are tested, but have no UI wiring yet.
4. Connect SDR TX to the receiver antenna port through >= 40 dB attenuation.
5. On the channel card: Device URI, LO 1575.42 MHz, rate 2.6 Msps, TX gain
   -50 dB, tick "Isolated/cabled setup confirmed", Start.
6. Record: receiver TTFF, reported position vs marker, sustained underflow count.
