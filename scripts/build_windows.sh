#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

python -m pip install --upgrade pip
python -m pip install pyinstaller -r requirements.txt

if [ ! -f db.sqlite3 ]; then
  python manage.py migrate --noinput
fi

pyinstaller --clean --noconfirm stockapp.spec

echo "Build completed: dist/stockapp/stockapp.exe"
