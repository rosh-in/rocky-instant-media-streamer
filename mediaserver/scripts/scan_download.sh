#!/bin/sh
# scan_download.sh — Post-download malware scanner
# Called by qBittorrent AutoRun on torrent completion.
# Usage: scan_download.sh "<content_path>" "<torrent_name>"
#
# Args:
#   $1 = %R  — Root path of the completed content
#   $2 = %N  — Name of the torrent
#
# The ClamAV daemon (clamd) runs in the "clamav" container on the
# media_net Docker network. We send a SCAN command over TCP so no
# shared socket file is needed.

CONTENT_PATH="${1:-}"
TORRENT_NAME="${2:-unknown}"
CLAMAV_HOST="clamav"
CLAMAV_PORT=3310
LOG_FILE="/data/torrents/scan.log"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

# Wait for ClamAV daemon to be ready (up to 60 seconds)
log "Waiting for ClamAV daemon at ${CLAMAV_HOST}:${CLAMAV_PORT}..."
TRIES=0
MAX_TRIES=12
while [ $TRIES -lt $MAX_TRIES ]; do
  if echo "PING" | nc -w 2 "$CLAMAV_HOST" "$CLAMAV_PORT" 2>/dev/null | grep -q "PONG"; then
    log "ClamAV daemon is ready."
    break
  fi
  TRIES=$((TRIES + 1))
  log "ClamAV not ready (attempt ${TRIES}/${MAX_TRIES}), retrying in 5s..."
  sleep 5
done

if [ $TRIES -ge $MAX_TRIES ]; then
  log "ERROR: ClamAV daemon not reachable after ${MAX_TRIES} attempts. Skipping scan for '${TORRENT_NAME}'."
  exit 1
fi

# Run the scan via clamd TCP protocol
log "Scanning '${TORRENT_NAME}' at '${CONTENT_PATH}'..."
SCAN_OUTPUT=$(echo "SCAN ${CONTENT_PATH}" | nc -w 300 "$CLAMAV_HOST" "$CLAMAV_PORT" 2>&1)
SCAN_EXIT=$?

log "Scan result for '${TORRENT_NAME}':"
echo "$SCAN_OUTPUT" >> "$LOG_FILE"

# Check if any threats were found
if echo "$SCAN_OUTPUT" | grep -q "FOUND"; then
  log "WARNING: THREAT DETECTED in '${TORRENT_NAME}'!"
  log "Review and remove the file manually: ${CONTENT_PATH}"
  # Future: optionally delete or quarantine the file here
elif echo "$SCAN_OUTPUT" | grep -q "OK"; then
  log "Clean — no threats in '${TORRENT_NAME}'."
else
  log "Scan completed with unknown status for '${TORRENT_NAME}'. Check log above."
fi

exit 0
