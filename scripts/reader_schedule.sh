#!/bin/bash
# Daily unattended reading queue, as a macOS LaunchAgent.
#
#   scripts/reader_schedule.sh install      write ~/Library/LaunchAgents/com.lit-radar.reader.plist and load it
#   scripts/reader_schedule.sh uninstall    unload and remove it
#   scripts/reader_schedule.sh status       is it installed, loaded, and when did it last run?
#   scripts/reader_schedule.sh run          what the agent runs (safe to run by hand)
#   scripts/reader_schedule.sh render       print the plist that `install` would write (no side effects)
#
# At 07:30 local time the agent runs, inside the repository:
#     python3 -m litradar read-queue --limit 20
#     git add reads && git -c commit.gpgsign=false commit -m "reads: <date>" && git pull --rebase && git push
# Nothing is committed when no article was read. The run is skipped entirely while a `litradar login`
# window is open (the browser profile is single-writer) and while another run holds the lock.
#
# The repository path and the Python interpreter are discovered here, never hard-coded: override with
# LITRADAR_READER_PYTHON, LITRADAR_READER_HOUR / _MINUTE, LITRADAR_READER_LIMIT, LITRADAR_READER_DAYS.
set -euo pipefail

LABEL="com.lit-radar.reader"
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
TEMPLATE="$REPO/scripts/com.lit-radar.reader.plist.template"
PLIST_DIR="${LITRADAR_READER_PLIST_DIR:-$HOME/Library/LaunchAgents}"
PLIST="$PLIST_DIR/$LABEL.plist"
LOG="${LITRADAR_READER_LOG:-$REPO/.cache/reader.log}"
LOCK="${LITRADAR_READER_LOCK:-$REPO/.cache/reader.lock}"
HOUR="${LITRADAR_READER_HOUR:-7}"
MINUTE="${LITRADAR_READER_MINUTE:-30}"
LIMIT="${LITRADAR_READER_LIMIT:-20}"
DAYS="${LITRADAR_READER_DAYS:-7}"
PYTHON="${LITRADAR_READER_PYTHON:-$(command -v python3 || echo /usr/bin/python3)}"

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

render() {
  [ -f "$TEMPLATE" ] || { echo "missing template: $TEMPLATE" >&2; exit 1; }
  sed -e "s|@@LABEL@@|$LABEL|g" \
      -e "s|@@SCRIPT@@|$SCRIPT_PATH|g" \
      -e "s|@@REPO@@|$REPO|g" \
      -e "s|@@PYTHON@@|$PYTHON|g" \
      -e "s|@@PATH@@|$(dirname "$PYTHON"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin|g" \
      -e "s|@@HOUR@@|$HOUR|g" \
      -e "s|@@MINUTE@@|$MINUTE|g" \
      -e "s|@@LOG@@|$LOG|g" \
      "$TEMPLATE"
}

do_run() {
  mkdir -p "$(dirname "$LOG")"
  exec >>"$LOG" 2>&1
  if pgrep -f "litradar login" >/dev/null 2>&1; then
    log "skipped: a 'litradar login' window is open"
    exit 0
  fi
  if ! mkdir "$LOCK" 2>/dev/null; then
    log "skipped: another run holds $LOCK"
    exit 0
  fi
  trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM
  cd "$REPO"
  log "read-queue --limit $LIMIT --days $DAYS"
  if ! "$PYTHON" -m litradar read-queue --limit "$LIMIT" --days "$DAYS"; then
    log "read-queue exited non-zero; nothing committed"
    exit 0
  fi
  if [ -z "$(git -C "$REPO" status --porcelain -- reads)" ]; then
    log "no new reads; nothing to commit"
    exit 0
  fi
  git -C "$REPO" add reads
  git -C "$REPO" -c commit.gpgsign=false commit -m "reads: $(date '+%Y-%m-%d')"
  git -C "$REPO" pull --rebase
  git -C "$REPO" push
  log "committed and pushed"
}

case "${1:-}" in
  install)
    [ "$(uname -s)" = "Darwin" ] || { echo "LaunchAgents are macOS only; on Linux use cron or a systemd timer" >&2; exit 1; }
    mkdir -p "$PLIST_DIR" "$(dirname "$LOG")"
    render > "$PLIST"
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "installed $PLIST (daily at $HOUR:$(printf '%02d' "$MINUTE"), logs in $LOG)"
    ;;
  uninstall)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $PLIST"
    ;;
  status)
    if [ -f "$PLIST" ]; then echo "plist:  $PLIST"; else echo "plist:  not installed"; fi
    if launchctl list 2>/dev/null | grep -q "$LABEL"; then echo "loaded: yes"; else echo "loaded: no"; fi
    echo "repo:   $REPO"
    echo "python: $PYTHON"
    echo "log:    $LOG"
    [ -f "$LOG" ] && tail -n 5 "$LOG" || true
    ;;
  run) do_run ;;
  render) render ;;
  *)
    sed -n '2,12p' "$SCRIPT_PATH"
    exit 2
    ;;
esac
