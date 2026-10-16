#!/usr/bin/env bash
# Start this project's API with an identifiable absolute script path.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
PIDFILE="$ROOT/logs/run.pid"
LOG="$ROOT/logs/run.log"
exec 9>"$ROOT/logs/run.lock"
flock -x 9

# Match an exact argument, including when the pidfile was lost.
for cmdline in /proc/[0-9]*/cmdline; do
  [[ -r "$cmdline" ]] || continue
  while IFS= read -r -d '' arg; do
    if [[ "$arg" == "$ROOT/run.py" ]]; then
      pid="${cmdline%/cmdline}"
      pid="${pid##*/}"
      echo "$pid" >"$PIDFILE"
      echo "already running pid=$pid"
      exit 0
    fi
  done <"$cmdline" 2>/dev/null || true
done

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "Missing $ROOT/.venv/bin/python; create .venv and install requirements.txt first." >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$ROOT/.venv/bin/activate"
nohup "$ROOT/.venv/bin/python" -u "$ROOT/run.py" >>"$LOG" 2>&1 9>&- &
pid=$!
echo "$pid" >"$PIDFILE"
sleep 1
if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PIDFILE"
  echo "Startup failed; see $LOG" >&2
  exit 1
fi
echo "started pid=$pid port=7998 log=$LOG"
