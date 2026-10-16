#!/bin/sh
# Builds install/Bonsai-x86_64.AppImage: Bonsai with its own Python and
# libraries, as a single file that runs on other Linux machines. Your
# boards aren't in it -- they live in ~/.local/share, so each machine
# starts with a fresh database.
#   ./build-appimage.sh
# Needs internet access (it downloads a Python AppImage and the
# dependencies). The recipe it uses is in packaging/appimage/.
set -e

PYTHON_VERSION="3.12"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$APP_DIR/install"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "Setting up the build tools..."
python3 -m venv "$WORK/tools"
"$WORK/tools/bin/pip" install -q python-appimage build

echo "Building the Bonsai package..."
"$WORK/tools/bin/python" -m build --wheel --outdir "$WORK/dist" "$APP_DIR" > "$WORK/build.log" 2>&1 \
    || { cat "$WORK/build.log"; exit 1; }
rm -rf "$APP_DIR/build" "$APP_DIR"/*.egg-info  # setuptools' leftovers
WHEEL="$(ls "$WORK"/dist/*.whl)"

# The recipe, with this build's wheel as the thing to install.
cp -r "$APP_DIR/packaging/appimage" "$WORK/recipe"
echo "$WHEEL" > "$WORK/recipe/requirements.txt"

echo "Building the AppImage (Python $PYTHON_VERSION)..."
cd "$WORK"
"$WORK/tools/bin/python-appimage" build app -p "$PYTHON_VERSION" -n Bonsai "$WORK/recipe"

mkdir -p "$OUT_DIR"
mv "$WORK/Bonsai-$(uname -m).AppImage" "$OUT_DIR/"
chmod +x "$OUT_DIR/Bonsai-$(uname -m).AppImage"
echo "Done: $OUT_DIR/Bonsai-$(uname -m).AppImage"
