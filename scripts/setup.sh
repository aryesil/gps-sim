#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x gps-sdr-sim/gps-sdr-sim ]; then
  [ -d gps-sdr-sim ] || git clone https://github.com/osqzss/gps-sdr-sim
  gcc -O3 gps-sdr-sim/gpssim.c -lm -o gps-sdr-sim/gps-sdr-sim -DUSER_MOTION_SIZE=4000
fi
echo "gps-sdr-sim built: $(gps-sdr-sim/gps-sdr-sim 2>&1 | head -1 || true)"

python -m pip install -e ".[dev]"

echo "System libs for transmit (install if you will use the SDR path):"
echo "  macOS:  brew install libiio libad9361"
echo "  Debian: apt install libiio-dev libad9361-dev"
