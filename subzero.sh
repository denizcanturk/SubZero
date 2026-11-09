#!/usr/bin/env bash
# SubZero başlatıcı — tüm argümanları oyuna iletir (ör. ./subzero.sh --sound --seed 42)
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
    echo "Hata: python3 bulunamadı." >&2
    exit 1
fi

if ! "$PY" -c "import curses" 2>/dev/null; then
    echo "Hata: Python 'curses' modülü yok (Debian/Ubuntu: sudo apt install python3-curses)." >&2
    exit 1
fi

COLS="$(tput cols 2>/dev/null || echo 0)"
ROWS="$(tput lines 2>/dev/null || echo 0)"
if [ -t 0 ] && [ -t 1 ] && [ "$COLS" -gt 0 ] && { [ "$COLS" -lt 100 ] || [ "$ROWS" -lt 30 ]; }; then
    echo "Uyarı: terminal ${COLS}x${ROWS}; en az 100x30 gerekli (150x45 önerilir)." >&2
    echo "Pencereyi büyütüp Enter'a basın, ya da Ctrl+C ile çıkın." >&2
    read -r _
fi

export TERM="${TERM:-xterm-256color}"
exec "$PY" -m subzero "$@"
