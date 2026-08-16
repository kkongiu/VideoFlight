#!/usr/bin/env bash
# Avvia la GUI della telemetria con un Python dotato di Tkinter funzionante.
# Si può lanciare dal Terminale oppure con doppio clic (Finder) .command
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUI="$DIR/gui.py"

# Se esiste il virtualenv di progetto, è la scelta preferita (ha Tk + Pillow)
if [[ -x "$DIR/.venv/bin/python" ]]; then
  ver=$("$DIR/.venv/bin/python" -c "import _tkinter as t; print(t.TK_VERSION)" 2>/dev/null) || ver=""
  if [[ "$ver" == 9.* || "$ver" == 8.6* ]]; then
    echo "Uso: $DIR/.venv (Tk $ver, Pillow)"
    "$DIR/.venv/bin/python" "$GUI"
    exit 0
  fi
fi

# Candidati in ordine di preferenza (Homebrew -> python.org -> di sistema -> PATH)
CANDIDATES=(
  /opt/homebrew/bin/python3
  /opt/homebrew/bin/python3.13
  /opt/homebrew/bin/python3.14
  /usr/local/bin/python3
  /usr/local/bin/python3.13
  /usr/bin/python3
  python3
  python3.13
  python3.12
  python3.11
)

for p in "${CANDIDATES[@]}"; do
  command -v "$p" >/dev/null 2>&1 || continue
  ver=$("$p" -c "import _tkinter as t; print(t.TK_VERSION)" 2>/dev/null) || continue
  case "$ver" in
    9.*|8.6*)
      echo "Uso: $p (Tk $ver)"
      "$p" "$GUI"
      exit 0
      ;;
  esac
done

echo "ERRORE: nessun Python con Tkinter 8.6+ trovato." >&2
echo "Installa uno di questi e riprova:" >&2
echo "  brew install python-tk@3.13   (poi: /opt/homebrew/bin/python3.13 gui.py)" >&2
exit 1