#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x gps-sdr-sim/gps-sdr-sim ]; then
  [ -d gps-sdr-sim ] || git clone https://github.com/osqzss/gps-sdr-sim
  gcc -O3 gps-sdr-sim/gpssim.c -lm -o gps-sdr-sim/gps-sdr-sim -DUSER_MOTION_SIZE=4000
fi
echo "gps-sdr-sim built: $(gps-sdr-sim/gps-sdr-sim 2>&1 | head -1 || true)"

VENV=.venv
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -e ".[dev]"

# libiio/libad9361 are the native libraries pyadi-iio needs to talk to a
# PlutoSDR-class device (the transmit path only). No package manager ships a
# prebuilt libiio (removed from homebrew-core, no official tap) so this
# builds both from source and installs them INTO the venv's own prefix
# (never system /usr/local) -- `pip uninstall`/`rm -rf .venv` removes them
# cleanly, and nothing here touches the host.
LIBEXT=so
[ "$(uname)" = "Darwin" ] && LIBEXT=dylib
if [ ! -f "$VENV/lib/libiio.$LIBEXT" ]; then
  echo "Building libiio (native transmit dependency) into $VENV ..."
  tmp=$(mktemp -d)
  # Pinned to the last pre-v1 API release: libad9361-iio's latest tag
  # (v0.4.0) still calls the old iio_buffer_refill()-style API, which
  # libiio's current main branch (v1.x, iio_block_*) removed.
  git clone --depth 1 --branch v0.25 https://github.com/analogdevicesinc/libiio "$tmp/libiio"
  cmake -S "$tmp/libiio" -B "$tmp/libiio/build" \
    -DCMAKE_INSTALL_PREFIX="$PWD/$VENV" -DCMAKE_BUILD_TYPE=Release \
    -DWITH_TESTS=OFF -DWITH_DOC=OFF -DPYTHON_BINDINGS=OFF -DWITH_MAN=OFF \
    -DOSX_FRAMEWORK=OFF -DCPP_BINDINGS=OFF
  cmake --build "$tmp/libiio/build" --parallel
  cmake --install "$tmp/libiio/build"

  export PKG_CONFIG_PATH="$PWD/$VENV/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
  git clone --depth 1 --branch v0.4.0 https://github.com/analogdevicesinc/libad9361-iio "$tmp/libad9361"
  cmake -S "$tmp/libad9361" -B "$tmp/libad9361/build" \
    -DCMAKE_INSTALL_PREFIX="$PWD/$VENV" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH="$PWD/$VENV" -DOSX_FRAMEWORK=OFF -DOSX_PACKAGE=OFF \
    -DBUILD_TESTS=OFF -DWITH_DOC=OFF \
    -DLIBIIO_INCLUDEDIR="$PWD/$VENV/include" -DLIBIIO_LIBRARIES="$PWD/$VENV/lib/libiio.$LIBEXT"
  # Only the "ad9361" target -- its test programs (need a real device) and
  # the macOS .pkg packaging target aren't needed and don't build cleanly
  # from a bare venv build.
  cmake --build "$tmp/libad9361/build" --target ad9361 --parallel
  cmake --install "$tmp/libad9361/build"
  rm -rf "$tmp"
fi
"$VENV/bin/pip" install -q pylibiio pyadi-iio

echo
echo "Setup complete. Start the server with:"
echo "  ./scripts/run_server.sh"
