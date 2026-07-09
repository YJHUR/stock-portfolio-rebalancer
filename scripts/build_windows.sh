#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

python -m pip install --upgrade pip
python -m pip install pyinstaller -r requirements.txt

pyinstaller --clean --noconfirm stockapp.spec

echo "Build completed: dist/stockapp.exe"
