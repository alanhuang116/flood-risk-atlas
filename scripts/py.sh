#!/usr/bin/env bash
# Run a Python script inside the floodrisk conda env without needing `conda activate`.
# Usage: scripts/py.sh scripts/01_download_nsi.py --workers 4
ENV_DIR="${FLOODRISK_ENV:-$HOME/.conda/envs/floodrisk}"
export PATH="$ENV_DIR:$ENV_DIR/Library/bin:$ENV_DIR/Scripts:$PATH"
export GDAL_DATA="$ENV_DIR/Library/share/gdal"
export PROJ_DATA="$ENV_DIR/Library/share/proj"
export PROJ_LIB="$PROJ_DATA"
export PYTHONPATH="$(cd "$(dirname "$0")/.." && pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
export PYTHONNOUSERSITE=1   # never mix in packages from %APPDATA%\Python\Python311 (user site)
exec "$ENV_DIR/python.exe" "$@"
