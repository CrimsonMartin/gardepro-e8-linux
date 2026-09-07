# gardecam — GardePro E8 from Linux, no phone app

Pulls photos and videos off the trail camera over its own WiFi hotspot.
Linux is the primary target; macOS works too, with the caveats below.

Tested against a GardePro E8, firmware `V8.2.134 MCU V71`. The E9P is similar but
not identical — see the notes below for where they differ.

## A note on security

There is no authentication here beyond physical proximity. Any device in
Bluetooth range (~30 ft) can wake the camera, and the hotspot ships with the
factory password `1234567890`, which this tool uses by default. That is the
vendor's design, not something this repo introduces, but it is worth knowing if
your camera is somewhere a passer-by could reach it. Changing the WiFi password
on the camera and setting `GARDECAM_WIFI_PASS` accordingly is the only mitigation
available.

Use this on cameras you own.

## Setup

```bash
cp .env.example .env          # then set GARDECAM_BLE_MAC to your camera(s)
pip install bleak
```

Find your camera's Bluetooth MAC with it powered on and nearby:

```bash
bluetoothctl --timeout 20 scan le | grep -i CAM
```

The hotspot name is derived from the MAC, so that one value is all it needs.
`.env` is gitignored because the MAC identifies your specific camera.

## Use

```bash
python3 gardecam.py info        # model, battery, SD usage
python3 gardecam.py list 20     # newest 20 files
python3 gardecam.py sync        # download everything new -> ./media (every camera)
python3 gardecam.py fix         # strip preview track from clips already on disk
python3 gardecam.py setclock    # sync camera clock + timezone to this machine
python3 gardecam.py get 1002 MP4
python3 gardecam.py session 300 # hold the link open for manual curl
python3 gardecam.py disconnect  # back to the normal network
```

Downloads land in `./media` next to the script; `GARDECAM_MEDIA` overrides that.
With several cameras listed in `GARDECAM_BLE_MAC`, files are prefixed `cam1_`,
`cam2_`, ... by their position in that list, so keep the list order stable.
Clips synced before numbering keep their old names and are not re-downloaded.
The script resolves its own location, so it works from any directory — call it by
full path or symlink it onto your `PATH` if you prefer.

Each command takes roughly a minute to establish the link before it does anything.

**If the machine has one WiFi radio, a camera session drops your internet for its
duration.** `sync` reconnects you when it finishes; `disconnect` does it
explicitly if something is interrupted. A cheap USB WiFi dongle removes the
limitation — point the tool at it with `GARDECAM_IFACE=wlan1`.

## Unattended: continuous sync + wildlife alerts on your phone

`autosync.py` chains the whole pipeline for a laptop that sits within Bluetooth
range of the camera: `sync` → `disconnect` → `wildlife.py --remote` on the GPU
host → one push notification per new wildlife clip. The remote host's Immich
watches the `annotated/` folder, so the labelled clips show up there on their
own; raw clips never reach Immich.

```bash
cp .env.example .env            # set GARDECAM_REMOTE, GARDECAM_NTFY_URL, GARDECAM_IMMICH_URL
python3 autosync.py --test-notify        # one sample push with an attachment
python3 autosync.py --skip-camera        # dry pass: remote scan + notify only
./install-autosync.sh                    # systemd user units, timer left off
                                         # (GARDECAM_SYNC_GAP=15min ... for a gentler cadence)
systemctl --user enable --now gardecam-autosync.timer
```

The laptop needs `ssh` + `rsync`, an ssh config entry for the remote host (over
Tailscale works fine), and the usual bluetooth/wifi access for `gardecam.py`.
Docker is only needed on the remote host. A lock file stops two passes from
overlapping; if the camera is out of range the pass still runs the remote scan
for anything already on disk and exits non-zero so the journal shows it.

Each notification carries the annotated best frame as its preview; a tap opens
the annotated clip in the Immich mobile app when `GARDECAM_IMMICH_API_KEY` (a
read-only key) is set, otherwise it just opens the app.

Notifications go through a self-hosted [ntfy](https://ntfy.sh) server (a
one-container `docker compose` on the GPU host). On the phone install the ntfy
app, add the server URL under settings, and subscribe to the topic named in
`GARDECAM_NTFY_URL` - the topic name is the only secret, so make it long and
random. iOS needs the server's `upstream-base-url` pointed at `https://ntfy.sh`
so Apple push can wake the app; the message itself is still fetched from your
server, so the phone must be able to reach it (Tailscale).

### Timing

The timer re-fires five minutes after each pass ends (`GARDECAM_SYNC_GAP` at
install time changes that; `1min` is the fastest sensible setting).
A pass that finds nothing new is about 1.5 min (roughly a minute to wake the
camera and join its hotspot, one listing request, disconnect); a pass with a new
clip adds the download, the remote detection run (model load plus a few seconds
per clip), the Immich import and the push, so a clip is usually on the phone
within 2-4 minutes of the camera finishing it. Notifications are sent only after
`wildlife.py --remote` has returned, so the clip is already on the GPU host and
in Immich by the time the alert arrives.

The price is battery and recording: every pass wakes the camera over Bluetooth
and holds its hotspot up for the length of the pass, and trail cameras generally
do not trigger on motion while their app link is active. On AA cells this
cadence will drain them quickly; on mains or a solar pack it is fine. The
five-minute gap keeps the camera watching about three-quarters of the time;
`GARDECAM_SYNC_GAP=15min` when running `install-autosync.sh` backs off further. With one wifi radio the laptop is on the camera hotspot most of the
time in this mode; a USB wifi dongle for the camera (`GARDECAM_IFACE`) keeps its
normal connection up.

## Running it from macOS

The camera side works on a Mac, with two differences that are worth knowing
before you rely on it.

**CoreBluetooth never shows you a MAC.** It hands out a UUID that is stable for
one Mac and meaningless on any other, so `GARDECAM_BLE_MAC` cannot be used to
*find* the camera here — only to derive the hotspot name, which it is still
required for. Take the MAC from a Linux box with `bluetoothctl`, or read it off
the `CAM8Z8_<MAC>` hotspot in the wifi menu. Finding the camera then falls back
to its advertised name:

```bash
python3 gardecam.py scan          # list BLE devices; the camera shows as CAM...
```

That is enough for one camera. With several in range, pin each Mac-local UUID
from `scan` in `GARDECAM_BLE_UUID`, since the names are identical.

**Wifi goes through `networksetup`, not NetworkManager.** macOS remembers every
network it joins and would rank the camera above your real one, so the camera
hotspot is removed from the preferred list as soon as the join succeeds, and
`disconnect` bounces the radio to make macOS re-pick the real network. The
interface defaults to `en0`; override with `GARDECAM_IFACE` if yours differs
(`networksetup -listallhardwareports`).

### Setup

```bash
python3 -m venv .venv && .venv/bin/pip install bleak
cp .env.example .env               # GARDECAM_BLE_MAC, and the autosync settings
brew install tmux ffmpeg
```

The first BLE scan makes macOS ask whether the terminal may use Bluetooth. If
it never asks and `scan` finds nothing, grant it under System Settings >
Privacy & Security > Bluetooth. Running under tmux inherits the permission of
whichever terminal started the session.

### The one-time wifi authorization

macOS stores wifi passwords in the *System* keychain, and it puts up an
authorization dialog before writing one. That dialog is fine the first time and
fatal afterwards: nothing answers it in a detached tmux session, so
`networksetup` simply blocks until it times out.

So the hotspot is joined with its password only once. After that it stays in
the preferred networks list - re-pinned to the *last* position on every join,
so it can never outrank real wifi while the camera is asleep - and later joins
pass no password at all, which leaves macOS nothing to authorize.

The first `sync`/`info` therefore raises one dialog; approve it and the rest
are silent. To set a machine up without ever seeing the dialog (over ssh, say),
seed the keychain and register the network by hand first:

```bash
sudo security add-generic-password -U -a "CAM8Z8_<MAC>" -s AirPort \
     -D "AirPort network password" -w "1234567890" -A \
     /Library/Keychains/System.keychain
sudo networksetup -addpreferredwirelessnetworkatindex en0 "CAM8Z8_<MAC>" 999 WPA2
```

The index is clamped to the end of the list, and omitting the password on the
second command is what keeps it from prompting.

Do not run `gardecam.py` by hand while the autosync agent is loaded. The lock
file only stops two autosync passes from overlapping; a manual run competes for
the same Bluetooth radio, and the camera accepts one BLE connection at a time,
so neither side manages to raise the hotspot.

### Unattended

There is no systemd, so `install-autosync.sh` does not apply. `autosync-loop.sh`
runs the same passes back to back and is meant to be left in tmux:

```bash
tmux new -d -s gardecam ./autosync-loop.sh
tmux attach -t gardecam            # ctrl-b d to leave it running
tmux kill-session -t gardecam      # stop
```

`GARDECAM_SYNC_GAP` is a plain number of seconds here (default 300), not a
systemd time string. Output goes to the tmux scrollback and to `autosync.log`.

To have that session come back by itself after a reboot, `install-autosync-macos.sh`
writes a launchd agent that starts it at login:

```bash
./install-autosync-macos.sh --load
launchctl unload ~/Library/LaunchAgents/com.gardecam.autosync.plist   # stop
```

Set the Mac to log in automatically, or the agent never runs. Note that a
launchd agent does not inherit the Bluetooth permission your terminal holds, so
the first pass started this way may find no camera even when `scan` works in a
terminal; approve the prompt, or add the venv python under Privacy & Security >
Bluetooth.

### Keeping the laptop awake

The loop holds a `caffeinate` assertion, which covers idle sleep but **not the
lid being shut**. For a Mac sitting closed next to the camera:

```bash
sudo pmset -a disablesleep 1       # never sleep, lid open or closed
sudo pmset -a sleep 0 standby 0 autopoweroff 0 hibernatemode 0
sudo pmset -c autorestart 1        # come back up after a power cut
sudo pmset -b sleep 0              # a brief unplug must not put it under
```

`sudo pmset -a disablesleep 0` puts it back. Two things to know: a closed
laptop running flat out has no good way to shed heat, and with sleep disabled a
real power loss drains the battery to empty rather than sleeping at a low
threshold.

## Keeping the disk in check

Raw clips are the bulk of what `sync` writes - roughly a gigabyte a day on a
camera with something to look at - so `sync` deletes the ones older than
`GARDECAM_KEEP_DAYS` (default 14) when it has finished. Only the top level of
the media directory is touched: annotated clips, their stills and the sidecars
stay, so Immich and the phone notifications are unaffected. `GARDECAM_KEEP_DAYS=0`
turns it off.

Age comes from the capture stamp in the file name rather than mtime, which on a
copy made by rsync says when the file was transferred rather than when it was
recorded.

This works because `sync` also keeps a ledger of every id it has downloaded, in
`.gardecam-synced` beside the media. A listing entry counts as new precisely
because it is not on disk, so without the ledger every pruned clip would be
downloaded again on the next pass - the camera holds its own copies until the
SD card rotates. The ledger seeds itself from whatever is already on disk, so an
archive that predates it is safe.

Note this prunes wherever `sync` runs. A remote host that receives the media
over rsync keeps its own copies, since the push has no `--delete`.

## How it works

The camera sleeps with only Bluetooth LE advertising (`CAM8Z8_NoName_G_E8`).
Waking it and getting to the files takes three steps, each with a non-obvious
catch:

1. **Bluetooth wake.** Write `AT+WAKEPULSE=10\r\n` to the Nordic UART service.
   The characteristic *value handle is 0x001f* — the widely-copied `0x001e` is the
   declaration handle and always fails. The command must also go to characteristic
   `6e400004`, and the BLE link has to stay open and keep pulsing for the whole
   join; a single write followed by a disconnect never raises the hotspot.
2. **WiFi.** SSID is `CAM8Z8_<BLE MAC without colons>`, WPA2, password `1234567890`.
   Camera is `192.168.8.1`, client gets `192.168.8.30`.
3. **HTTP API** on port 8080. The hotspot dies within seconds unless something
   polls `/cmd/standby/reset`, which is what the keep-alive thread does.

### The two-video-track problem

Every clip the camera records contains **two** video streams: the real footage
(2304x1296 h264) and a 320x180 preview track, plus AAC audio. Players that assume
a single video track fail on this — VLC reports `internal stream error` — even
though the file is perfectly valid. You get the same file from the SD card, so it
is not a download artifact.

`sync` now strips the preview track automatically after each video download, and
`fix` does the same for clips already on disk. It is an `ffmpeg` stream copy, so
it is lossless and costs about a second per clip; the file shrinks by roughly 1%.
If `ffmpeg` is not installed the step is skipped silently and the clips still
play in mpv. Set `GARDECAM_NO_REMUX=1` to turn it off.

### Setting the clock

`/cmd/setGmtClock` takes **UTC**, and the camera renders local time from its own
`time_zone` setting, so a wrong zone shows up as a whole-hour error on every file
stamp. Fix the zone first (`/cmd/setSetting` with `{"data": {"time_zone": "US/Central"}}`
— the camera uses the old `US/*` names, not `America/Chicago`), then send the time.
`setclock` does both, then reads the clock back and corrects for any remaining
offset rather than trusting the write.

### API notes

| Endpoint | Notes |
|---|---|
| `/cmd/info/1..5` | device, power, storage, clock, versions |
| `/cmd/getSetting` | all camera settings |
| `/list/detail/{type}/{startId}/{count}` | see quirks below |
| `/file/{id}/{JPG\|MP4}` | full-resolution download |
| `/thumb/{id}/{JPG\|MP4}` | thumbnail |
| `/media/pic/take`, `/media/video/start\|stop` | remote capture (POST) |
| `/cmd/delete/{id}/{JPG\|MP4}` | delete one file |

Listing quirks, all found the hard way:

- `startId` is an **exclusive upper bound**, not an offset. `999999` means "newest".
- The `type` token is **ignored** — any listing returns both photos and videos —
  except the literal `MP4`, which the firmware rejects with `illegeal para`.
  Numeric types are rejected too; `JPG` is the safe token.
- Entries carry `type: 1` for a photo and `type: 2` for a video.
- An empty `data` array with `code: 0` means the camera genuinely has no media
  indexed, which is normal before it has been triggered. File ids start at 1001.
