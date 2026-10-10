#!/usr/bin/env bash
# Build a standalone executable (Python runtime bundled): dist/datapulse[.exe]
# Usage: scripts/build_binary.sh      (run from the repository root)
set -euo pipefail
python -m pip install --quiet ".[build]"
python -m PyInstaller --noconfirm --clean --onefile --name datapulse \
  --paths src \
  --collect-all pyarrow \
  --collect-submodules pandas \
  --exclude-module tkinter --exclude-module matplotlib --exclude-module IPython --exclude-module pytest \
  packaging/entry.py
echo "Built: $(ls dist/datapulse*)"
