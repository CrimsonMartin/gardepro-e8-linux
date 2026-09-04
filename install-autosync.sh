#!/bin/sh
# Install (but do not enable) the autosync timer for this clone as systemd
# *user* units, so it runs as you with your ssh keys, .env and bluetooth/wifi
# permissions. The timer re-fires GAP after each pass ends (default 1min), so
# passes run back-to-back and a new clip reaches the phone within a few
# minutes of being recorded. GARDECAM_SYNC_GAP=15min ./install-autosync.sh
# for something gentler on the camera battery.
#
#   ./install-autosync.sh            install units, leave the timer off
#   ./install-autosync.sh --enable   install and start the timer
#
# Afterwards:
#   systemctl --user start gardecam-autosync.service   # one pass, right now
#   journalctl --user -u gardecam-autosync -f           # watch it
#   systemctl --user enable --now gardecam-autosync.timer
#   systemctl --user disable --now gardecam-autosync.timer
#
# If the laptop should keep running passes while nobody is logged in:
#   sudo loginctl enable-linger "$USER"
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
PY=$(command -v python3)
GAP=${GARDECAM_SYNC_GAP:-1min}
UNITS="$HOME/.config/systemd/user"
mkdir -p "$UNITS"

cat > "$UNITS/gardecam-autosync.service" <<UNIT
[Unit]
Description=gardecam: sync camera, detect wildlife on the remote GPU host, notify phone
# Fired by gardecam-autosync.timer; start by hand for a one-off pass.

[Service]
Type=oneshot
WorkingDirectory=$HERE
ExecStart=$PY $HERE/autosync.py
# A first full sync or a big scan can take a while; don't let systemd kill it.
TimeoutStartSec=3h
Nice=10
UNIT

cat > "$UNITS/gardecam-autosync.timer" <<UNIT
[Unit]
Description=gardecam camera sync + wildlife detection, back-to-back passes

[Timer]
# First pass shortly after login/boot, then GAP after each pass *ends*
# (not after it starts), so a long sync never overlaps the next one.
OnBootSec=1min
OnUnitInactiveSec=$GAP
AccuracySec=15s

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
echo "installed $UNITS/gardecam-autosync.{service,timer} for $HERE"
if [ "$1" = "--enable" ]; then
    systemctl --user enable --now gardecam-autosync.timer
    systemctl --user list-timers --no-legend gardecam-autosync.timer
else
    echo "timer NOT enabled (gap $GAP); run: systemctl --user enable --now gardecam-autosync.timer"
fi
