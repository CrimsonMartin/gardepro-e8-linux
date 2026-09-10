#!/bin/sh
# macOS counterpart to install-autosync.sh. launchd stands in for systemd: the
# agent starts a detached tmux session at login running autosync-loop.sh, so
# the laptop picks the passes back up after a reboot with nobody at the
# keyboard. That only means anything if the Mac is set to log in
# automatically - check System Settings > Users & Groups > Automatic login.
#
#   ./install-autosync-macos.sh          install the agent, leave it stopped
#   ./install-autosync-macos.sh --load   install and start it now
#
# Afterwards:
#   tmux attach -t gardecam     # watch it; ctrl-b d leaves it running
#   launchctl unload ~/Library/LaunchAgents/com.gardecam.autosync.plist
#
# A launchd agent does not inherit the Bluetooth permission your terminal was
# granted, so the first pass started this way may find no camera even though
# `gardecam.py scan` works in a terminal. Approve the prompt if one appears, or
# add the venv python by hand under Privacy & Security > Bluetooth.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
GAP=${GARDECAM_SYNC_GAP:-300}
TMUX=$(command -v tmux) || { echo "tmux is not installed"; exit 1; }
PLIST="$HOME/Library/LaunchAgents/com.gardecam.autosync.plist"
mkdir -p "$(dirname "$PLIST")"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.gardecam.autosync</string>
  <key>ProgramArguments</key>
  <array>
    <string>$TMUX</string>
    <string>new-session</string>
    <string>-d</string>
    <string>-s</string>
    <string>gardecam</string>
    <string>$HERE/autosync-loop.sh</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>GARDECAM_SYNC_GAP</key><string>$GAP</string>
    <!-- launchd's default PATH would find macOS's rsync 2.6.9, which does not
         understand the --info flags wildlife.py passes to it. -->
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <!-- The loop pipes into tee, so python would block-buffer its progress
         output and a running pass would look hung. -->
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <!-- tmux -d returns as soon as the session exists, so there is nothing here
       for launchd to keep alive; the loop's own restarts live inside tmux. -->
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>$HERE/launchd.log</string>
  <key>StandardErrorPath</key><string>$HERE/launchd.log</string>
</dict>
</plist>
PLISTEOF

echo "installed $PLIST (gap ${GAP}s, tmux $TMUX)"
if [ "${1:-}" = "--load" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "loaded; watch it with: tmux attach -t gardecam"
else
    echo "not loaded; run: launchctl load $PLIST"
fi
