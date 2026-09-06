#!/bin/sh
# macOS counterpart to install-autosync.sh. There is no systemd here, so the
# back-to-back passes are just a loop, meant to be left running in tmux:
#
#   tmux new -d -s gardecam ./autosync-loop.sh
#   tmux attach -t gardecam          # ctrl-b d to leave it running
#   tmux kill-session -t gardecam    # stop
#
# GARDECAM_SYNC_GAP is a plain number of seconds here, not a systemd time
# string: the pause after each pass *ends*, so a long sync never overlaps the
# next one. The camera does not record while its app link is up, so the gap is
# what keeps it watching; 300 leaves it watching about three-quarters of the
# time, 900 or more goes easier on a battery-powered camera.
#
# caffeinate keeps the machine awake for as long as the loop runs. It does NOT
# cover the lid being shut - that needs `sudo pmset -a disablesleep 1`; see the
# macOS section of the README.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)

# launchd hands out a bare PATH, and macOS still ships rsync 2.6.9 in /usr/bin
# which rejects the --info flags wildlife.py passes. Put Homebrew first so the
# modern rsync, tmux and ffmpeg win wherever this is started from.
PATH=/usr/local/bin:/opt/homebrew/bin:$PATH
export PATH
GAP=${GARDECAM_SYNC_GAP:-300}
LOG=${GARDECAM_LOG:-$HERE/autosync.log}

# Prefer the venv bleak is installed into; fall back to whatever python3 is.
PY=${GARDECAM_PYTHON:-$HERE/.venv/bin/python3}
[ -x "$PY" ] || PY=$(command -v python3) || { echo "no python3"; exit 1; }

# -i no idle sleep, -s no system sleep on AC. Re-exec so the assertion is held
# for the whole life of the loop rather than one pass.
if [ "${GARDECAM_CAFFEINATED:-}" != 1 ]; then
    GARDECAM_CAFFEINATED=1
    export GARDECAM_CAFFEINATED
    exec caffeinate -i -s "$0" "$@"
fi

# The log is appended to forever otherwise; a fresh laptop-season is cheap.
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 20000000 ]; then
    mv "$LOG" "$LOG.1"
fi

echo "gardecam autosync loop: python $PY, gap ${GAP}s, log $LOG"
while :; do
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') pass starting ====="
    "$PY" "$HERE/autosync.py" || echo "===== pass exited $? ====="
    echo "===== $(date '+%H:%M:%S') sleeping ${GAP}s ====="
    sleep "$GAP"
done 2>&1 | tee -a "$LOG"
