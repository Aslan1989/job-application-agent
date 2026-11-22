#!/usr/bin/env bash
#
# Safe daily runner for the skill-based discovery pipeline.
#
# What it does:   discovers jobs, scores them with the configured LLM, writes
#                 Found/Review/Skip rows to Google Sheets.
# What it never   submits an application, overwrites a status or approval you
# does:           set yourself, or runs twice at once.
#
# Run `job-agent daily --ai` interactively at least once before scheduling this.
# Rehearsal switches:
#   DRY_RUN=1   run everything but make no Google Sheets write
#   USE_AI=0    skip all model calls (no Gemini tokens spent)

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

LOG_DIR="${LOG_DIR:-logs}"
LOG_FILE="$LOG_DIR/daily-$(date +%Y-%m).log"
LOCK_FILE="${LOCK_FILE:-$LOG_DIR/daily.lock}"
MAX_SEARCHES="${MAX_SEARCHES:-33}"
PER_SEARCH="${PER_SEARCH:-10}"
MAX_NEW="${MAX_NEW:-12}"
MAX_MATCHES="${MAX_MATCHES:-6}"
MAX_DETAIL="${MAX_DETAIL:-20}"

mkdir -p "$LOG_DIR"

# The Sheets values API has no compare-and-swap, so the pipeline is a single
# writer by design. flock makes an overlapping run fail fast instead of
# interleaving writes. -n: do not queue, just exit.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date -Is) another daily run holds $LOCK_FILE; exiting" >>"$LOG_FILE"
  exit 0
fi

ARGS=(daily
      --max-searches "$MAX_SEARCHES"
      --per-search "$PER_SEARCH"
      --max-new "$MAX_NEW"
      --max-matches "$MAX_MATCHES"
      --max-detail "$MAX_DETAIL"
      --verbose)

if [[ "${USE_AI:-1}" != "0" ]]; then
  ARGS+=(--ai)
fi

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "$(date -Is) DRY RUN: no Google Sheets writes" >>"$LOG_FILE"
else
  ARGS+=(--write)
fi

{
  echo "=== $(date -Is) starting job-agent ${ARGS[*]} ==="
  # uv resolves the project environment; no global Python state is relied on.
  uv run job-agent "${ARGS[@]}"
  echo "=== $(date -Is) finished rc=0 ==="
} >>"$LOG_FILE" 2>&1 || {
  status=$?
  echo "=== $(date -Is) FAILED rc=$status (see above) ===" >>"$LOG_FILE"
  exit "$status"
}

# Keep the log directory small; monthly files, a year of history.
find "$LOG_DIR" -name 'daily-*.log' -type f -mtime +365 -delete 2>/dev/null || true
