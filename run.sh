#!/bin/bash
# Generic mail-store bridge: pulls one IMAP mailbox and pushes new messages
# to an HTTP endpoint, on a loop, so a scheduled trigger's jitter only costs
# one cycle instead of the whole gap between triggers.
#
# No message content is ever written to this repo. State (just UID
# numbers + content hashes, no subjects/bodies/addresses) lives in
# actions/cache between runs, restored/saved by the caller - this script
# only reads/writes ./state locally.
#
# All identifying values (host, mailbox, destination) come from env vars /
# secrets, never hardcoded here.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

: "${TARGET_HOST:?}"
: "${MAILBOX:?}"
: "${DEST_BASE:?}"
: "${AUTH_TOKEN:?}"
: "${MASTER_PASS:?}"
: "${RUN_SECS:=355}"
: "${CYCLE_SECS:=30}"

STATE_DIR="$SCRIPT_DIR/state"
mkdir -p "$STATE_DIR"

PASS_FILE="$(mktemp)"
trap 'rm -f "$PASS_FILE"' EXIT
printf '%s' "$MASTER_PASS" > "$PASS_FILE"

export IMAP_HOST="$TARGET_HOST"
export UPLOAD_TOKEN="$AUTH_TOKEN"
export IMAP_MASTER_PASS_FILE="$PASS_FILE"
LOCAL="${MAILBOX%%@*}"
export MAIL_PREFIX="$LOCAL"
export ACCOUNT_ID="${LOCAL}_bridge"

if [ ! -s "$STATE_DIR/imap_state.json" ]; then
  echo "=== no state yet - seeding from current UIDs (no content fetched)"
  python3 "$SCRIPT_DIR/seed.py" "$MAILBOX" "$STATE_DIR"
fi

one_cycle() {
  python3 "$SCRIPT_DIR/fetch.py" "$MAILBOX" "$STATE_DIR" "$DEST_BASE" && \
  python3 "$SCRIPT_DIR/publish.py" "$STATE_DIR" "$DEST_BASE"
}

deadline=$(( $(date +%s) + RUN_SECS ))
cycles=0
ok=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  cycles=$((cycles + 1))
  echo "=== cycle $cycles: $(date -u +%H:%M:%S)"
  if one_cycle; then
    ok=$((ok + 1))
  else
    echo "  cycle $cycles FAILED - will retry next cycle" >&2
  fi
  remaining=$(( deadline - $(date +%s) ))
  [ "$remaining" -le "$CYCLE_SECS" ] && break
  sleep "$CYCLE_SECS"
done

echo "=== done $(date -u +%H:%M:%S) - $ok/$cycles cycles ok"
[ "$ok" -gt 0 ] || exit 1
