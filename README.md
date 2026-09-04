# GPS L1 C/A Signal Simulator

Interactive scenario workbench: pick a place and time on a map, generate a GPS L1
C/A baseband IQ file with `gps-sdr-sim`, inspect it, verify it with an internal
software receiver, and stream it to a PlutoSDR-class SDR for replay.

## Setup

    ./scripts/setup.sh
    python -m uvicorn backend.app:app --reload

Open http://127.0.0.1:8000

## Safety

Transmitting in the GPS band over the air is illegal and disrupts real receivers.
Use a cabled or shielded (Faraday) setup only. When feeding a receiver over
cable, use low TX gain (-40 to -60 dB) plus 30-60 dB in-line attenuation.
Transmit is disabled unless `ALLOW_TX=1` and you confirm the isolated setup in
the UI.

## Manual hardware acceptance checklist

1. `ALLOW_TX=1 python -m uvicorn backend.app:app`
2. Generate a static scenario for your location, current UTC, 300 s, 2.6 Msps int16.
3. Run "Receiver check" — expect a fix within 100 m of the marker.
4. Connect SDR TX to the receiver antenna port through >= 40 dB attenuation.
5. Transmit panel: URI, LO 1575.42 MHz, rate 2.6 Msps, TX gain -50 dB, tick the
   isolated-setup confirmation, Start.
6. Record: receiver TTFF, reported position vs marker, sustained underflow count.
