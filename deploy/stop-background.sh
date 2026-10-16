#!/usr/bin/env bash
# Stop only processes whose arguments contain this project's absolute run.py path.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/logs"
PIDFILE="$ROOT/logs/run.pid"
exec 9>"$ROOT/logs/run.lock"
flock -x 9

pids=()
for cmdline in /proc/[0-9]*/cmdline; do
  [[ -r "$cmdline" ]] || continue
  while IFS= read -r -d '' arg; do
    if [[ "$arg" == "$ROOT/run.py" ]]; then
      pid="${cmdline%/cmdline}"
      pids+=("${pid##*/}")
      break
    fi
  done <"$cmdline" 2>/dev/null || true
done

if (( ${#pids[@]} == 0 )); then
  rm -f "$PIDFILE"
  echo "not running"
  exit 0
fi

kill -TERM "${pids[@]}"
for pid in "${pids[@]}"; do
  # Allow uvicorn and the optional Duplug subprocess to shut down gracefully.
  for ((attempt=0; attempt<15; attempt++)); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "pid=$pid still shutting down; retained $PIDFILE, check logs/run.log" >&2
    exit 1
  fi
  echo "stopped pid=$pid"
done
rm -f "$PIDFILE"
