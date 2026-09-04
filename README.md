# gardecam — GardePro E8 from Linux, no phone app

Pulls photos and videos off the trail camera over its own WiFi hotspot.

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
The script resolves its own location, so it works from any directory — call it by
full path or symlink it onto your `PATH` if you prefer.

Each command takes roughly a minute to establish the link before it does anything.

**If the machine has one WiFi radio, a camera session drops your internet for its
duration.** `sync` reconnects you when it finishes; `disconnect` does it
explicitly if something is interrupted. A cheap USB WiFi dongle removes the
limitation — point the tool at it with `GARDECAM_IFACE=wlan1`.

## Unattended: hourly sync + wildlife alerts on your phone

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
systemctl --user enable --now gardecam-autosync.timer
```

The laptop needs `ssh` + `rsync`, an ssh config entry for the remote host (over
Tailscale works fine), and the usual bluetooth/wifi access for `gardecam.py`.
Docker is only needed on the remote host. A lock file stops two passes from
overlapping; if the camera is out of range the pass still runs the remote scan
for anything already on disk and exits non-zero so the journal shows it.

Notifications go through a self-hosted [ntfy](https://ntfy.sh) server (a
one-container `docker compose` on the GPU host). On the phone install the ntfy
app, add the server URL under settings, and subscribe to the topic named in
`GARDECAM_NTFY_URL` - the topic name is the only secret, so make it long and
random. iOS needs the server's `upstream-base-url` pointed at `https://ntfy.sh`
so Apple push can wake the app; the message itself is still fetched from your
server, so the phone must be able to reach it (Tailscale).

Hourly means the camera is woken over Bluetooth and brings its hotspot up every
hour, which costs battery: fine on mains or solar, noticeable on AA cells. Change
`OnCalendar` in `install-autosync.sh` for a gentler cadence.

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
