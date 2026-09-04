#!/bin/sh
# Install (but do not enable) the hourly autosync timer for this clone as
# systemd *user* units, so it runs as you with your ssh keys, .env and
# bluetooth/wifi permissions.
#
#   ./install-autosync.sh            install units, leave the timer off
#   ./install-autosync.sh --enable   install and start the hourly timer
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
Description=Hourly gardecam camera sync + wildlife detection

[Timer]
OnCalendar=hourly
# Catch up if the laptop was asleep when the hour ticked over.
Persistent=true
# Spread the start a little so it never lands exactly on the hour.
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
echo "installed $UNITS/gardecam-autosync.{service,timer} for $HERE"
if [ "$1" = "--enable" ]; then
    systemctl --user enable --now gardecam-autosync.timer
    systemctl --user list-timers --no-legend gardecam-autosync.timer
else
    echo "timer NOT enabled; run: systemctl --user enable --now gardecam-autosync.timer"
fi
