#!/usr/bin/env python3
"""
gardecam - talk to a GardePro E8 WiFi trail camera from Linux, no phone app.

How the camera works:
  It sleeps with only Bluetooth LE advertising. Writing an AT command to its
  Nordic UART service wakes the main CPU, which brings up a WPA2 hotspot named
  CAM8Z8_<BLE-MAC>. The camera serves an HTTP API on 192.168.8.1:8080 and drops
  the hotspot again after a short idle timeout unless something keeps polling
  /cmd/standby/reset.

Usage:
  gardecam.py info                 device, battery, storage
  gardecam.py list [N]             list the N most recent files (default 20)
  gardecam.py get ID JPG|MP4       download one file
  gardecam.py sync [DIR]           download everything not already local
  gardecam.py fix [DIR]            strip preview track from clips already on disk
  gardecam.py setclock [TZ]        sync camera clock + timezone to this machine
  gardecam.py session [SECONDS]    hold the link open (default 300)
  gardecam.py disconnect           drop camera wifi, return to normal network
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_env(path=None):
    """Read KEY=value lines from .env. Real environment variables win."""
    path = path or os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env()

# The camera's Bluetooth MAC identifies a specific camera, so it lives in .env
# rather than in the source. See .env.example for how to find yours.
BLE_MAC = os.environ.get("GARDECAM_BLE_MAC", "").strip().upper()
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
# Second writable characteristic on the camera's UART service. Writing the wake
# command here as well as to NUS_RX is what actually brings the hotspot up.
NUS_ALT = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
# The hotspot name is the model prefix plus the MAC with the colons stripped.
SSID = "CAM8Z8_" + BLE_MAC.replace(":", "")
WIFI_PASS = os.environ.get("GARDECAM_WIFI_PASS", "1234567890")
PROFILE = "gardecam"
BASE = "http://192.168.8.1:8080"
IFACE = os.environ.get("GARDECAM_IFACE", "wlp0s20f3")
PHOTO_DIR = os.environ.get("GARDECAM_MEDIA", os.path.join(HERE, "media"))


def require_mac():
    if not BLE_MAC:
        raise SystemExit(
            "No camera configured. Copy .env.example to .env and set GARDECAM_BLE_MAC.\n"
            "Find your camera's MAC with:\n"
            "  bluetoothctl --timeout 20 scan le | grep -i CAM"
        )


def sh(cmd, timeout=60):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------- BLE wake

class BleWaker(threading.Thread):
    """Holds the BLE link open and pulses the wake command.

    A single write is not enough: the camera only raises (and keeps) its hotspot
    while it is being nudged, so this runs for the whole duration of the wifi
    join rather than firing once and disconnecting.
    """

    daemon = True

    def __init__(self, seconds=150):
        super().__init__()
        self.seconds = seconds
        self.stop_flag = threading.Event()
        self.ready = threading.Event()
        self.error = None

    async def _run(self):
        from bleak import BleakClient, BleakScanner

        # The camera stops advertising while anything holds a connection, so
        # clear a stale link before scanning for it.
        sh(f"bluetoothctl disconnect {BLE_MAC}")
        await asyncio.sleep(2)
        dev = await BleakScanner.find_device_by_address(BLE_MAC, timeout=20)
        if dev is None:
            raise RuntimeError(
                f"camera {BLE_MAC} is not advertising - out of Bluetooth range or powered off"
            )
        async with BleakClient(dev, timeout=30) as c:
            self.ready.set()
            end = time.time() + self.seconds
            while time.time() < end and not self.stop_flag.is_set():
                for char in (NUS_RX, NUS_ALT):
                    for cmd in (b"AT+WAKEPULSE=50\r\n", b"AT+WAKEPULSE=10\r\n"):
                        for resp in (False, True):
                            try:
                                await c.write_gatt_char(char, cmd, response=resp)
                            except Exception:
                                pass
                            await asyncio.sleep(0.4)
                await asyncio.sleep(2.0)

    def run(self):
        try:
            asyncio.run(self._run())
        except Exception as e:
            self.error = e
        finally:
            self.ready.set()

    def stop(self):
        self.stop_flag.set()


# ---------------------------------------------------------------- wifi

def hotspot_visible():
    sh("nmcli device wifi rescan", timeout=30)
    out = sh("nmcli -t -f SSID,SIGNAL device wifi list", timeout=30).stdout
    for line in out.splitlines():
        if line.startswith(SSID + ":"):
            return line.rsplit(":", 1)[-1]
    return None


def on_camera_wifi():
    out = sh(f"ip -4 addr show {IFACE}").stdout
    return "192.168.8." in out


def connect_wifi(wait=75):
    if on_camera_wifi():
        return True
    deadline = time.time() + wait
    while time.time() < deadline:
        sig = hotspot_visible()
        if sig:
            print(f"hotspot {SSID} visible (signal {sig}%), joining...")
            sh(f"nmcli connection delete {PROFILE}")
            r = sh(
                f'nmcli --wait 30 device wifi connect "{SSID}" password "{WIFI_PASS}" name {PROFILE}',
                timeout=45,
            )
            # Never let this profile auto-steal the radio later.
            sh(f"nmcli connection modify {PROFILE} connection.autoconnect no")
            if on_camera_wifi():
                print("joined camera network:", ip_addr())
                return True
            print("  join failed:", r.stdout.strip() or r.stderr.strip())
        time.sleep(3)
    return False


def ip_addr():
    for line in sh(f"ip -4 addr show {IFACE}").stdout.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            return line.split()[1]
    return "?"


def disconnect():
    sh(f"nmcli connection delete {PROFILE}")
    sh(f"nmcli device disconnect {IFACE}")
    sh(f"nmcli device connect {IFACE}")
    print("camera wifi dropped; back on normal network")


# ---------------------------------------------------------------- HTTP API

def api(path, timeout=15, raw=False):
    url = BASE + path
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = r.read()
    if raw:
        return data
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return data.decode("utf-8", "replace")


class KeepAlive(threading.Thread):
    """The camera kills its hotspot after a few idle seconds. Poke it."""

    daemon = True

    def __init__(self, every=5):
        super().__init__()
        self.every = every
        self.stop_flag = threading.Event()
        self.failures = 0

    def run(self):
        while not self.stop_flag.is_set():
            try:
                api("/cmd/standby/reset", timeout=8)
                self.failures = 0
            except Exception:
                self.failures += 1
            self.stop_flag.wait(self.every)

    def stop(self):
        self.stop_flag.set()


def link_up(retries=3):
    """Wake, join wifi, and start the keep-alive. Returns the KeepAlive thread."""
    require_mac()
    for attempt in range(1, retries + 1):
        waker = None
        if not on_camera_wifi():
            print("waking camera over Bluetooth (holding link open)...")
            waker = BleWaker()
            waker.start()
            waker.ready.wait(timeout=45)
            if waker.error:
                raise SystemExit(f"bluetooth wake failed: {waker.error}")
            joined = connect_wifi()
            waker.stop()
            if not joined:
                print(f"attempt {attempt}: hotspot never came up, retrying...")
                continue
        ka = KeepAlive()
        ka.start()
        # Confirm the HTTP server is actually answering before handing back.
        for _ in range(12):
            try:
                api("/cmd/standby/reset", timeout=6)
                return ka
            except Exception:
                time.sleep(2)
        ka.stop()
        print(f"attempt {attempt}: wifi joined but HTTP not answering, retrying...")
        sh(f"nmcli connection delete {PROFILE}")
    raise SystemExit("could not establish a link to the camera")


# ---------------------------------------------------------------- commands

def cmd_info():
    ka = link_up()
    try:
        for n, label in ((1, "device"), (2, "power"), (3, "storage"), (4, "clock")):
            try:
                print(f"--- {label} ---")
                print(json.dumps(api(f"/cmd/info/{n}"), indent=2))
            except Exception as e:
                print(f"  ({label} failed: {e})")
    finally:
        ka.stop()


def list_files(count=500):
    """Return the camera's media listing, newest first.

    The path is /list/detail/{type}/{startId}/{count}. Two quirks: startId is an
    exclusive upper bound rather than an offset, so a large value means "newest";
    and the type token is ignored (every listing returns both photos and videos)
    except the literal "MP4", which the firmware rejects outright.
    Each entry carries type 1 for a photo and type 2 for a video.
    """
    return api(f"/list/detail/JPG/999999/{count}", timeout=30)


def list_all_files():
    """Page through the whole listing; firmware caps each response (~40).

    startId is an exclusive upper bound, so after each batch we ask again
    below the lowest id we've seen until nothing new comes back.
    """
    items, seen, start = [], set(), 999999
    while True:
        batch = _entries(api(f"/list/detail/JPG/{start}/500", timeout=30))
        ids = [it.get("id") for it in batch
               if isinstance(it, dict) and isinstance(it.get("id"), int)
               and it.get("id") not in seen]
        if not ids:
            break
        items.extend(it for it in batch
                     if isinstance(it, dict) and it.get("id") in set(ids))
        seen.update(ids)
        start = min(ids)
    return items


def cmd_list(count=20):
    ka = link_up()
    try:
        # A single request tops out around 40 entries; paginate past that.
        if count > 40:
            items = list_all_files()[:count]
        else:
            items = _entries(list_files(max(count, 50)))[:count]
        if not items:
            print("camera reports no media")
            return
        print(f"{'ID':>6}  {'KIND':4}  {'DATE':19}  SIZE")
        for it in items:
            kind = "video" if it.get("type") == 2 else "photo"
            mb = it.get("size", 0) / 1048576
            print(f"{it.get('id'):>6}  {kind:4}  {it.get('date',''):19}  {mb:7.1f} MB")
    finally:
        ka.stop()


def _entries(listing):
    if isinstance(listing, dict):
        for key in ("data", "list", "files", "detail"):
            v = listing.get(key)
            if isinstance(v, list):
                return v
        for v in listing.values():
            if isinstance(v, list):
                return v
    return listing if isinstance(listing, list) else []


def download(fid, kind, outdir=PHOTO_DIR, date=None):
    os.makedirs(outdir, exist_ok=True)
    stamp = ""
    if date:
        stamp = "_" + str(date).replace(":", "").replace("-", "").replace(" ", "_")
    path = os.path.join(outdir, f"{fid}{stamp}.{kind.lower()}")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path, False
    data = api(f"/file/{fid}/{kind}", timeout=300, raw=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path, True


def api_post(path, body, timeout=25):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = r.read()
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return out.decode("utf-8", "replace")


# The camera names zones in the older US/* style rather than the IANA city style.
_TZ_MAP = {
    "America/Chicago": "US/Central",
    "America/New_York": "US/Eastern",
    "America/Denver": "US/Mountain",
    "America/Phoenix": "US/Arizona",
    "America/Los_Angeles": "US/Pacific",
    "America/Anchorage": "US/Alaska",
    "Pacific/Honolulu": "US/Hawaii",
}


def system_tz():
    try:
        with open("/etc/timezone") as f:
            return f.read().strip()
    except Exception:
        return ""


def camera_clock():
    return api("/cmd/info/4").get("data", {})


def cmd_setclock(tz=None):
    """Sync the camera's clock to this machine's wall time.

    Two things have to line up. The camera stamps files with local time derived
    from its own timezone setting, and /cmd/setGmtClock takes UTC, so a wrong
    timezone shows up as a whole-hour error. Set the zone first, then the clock,
    then verify and correct for whatever offset the firmware actually applied.
    """
    ka = link_up()
    try:
        import datetime

        before = camera_clock()
        print(f"camera now: {before.get('clock')}  tz={before.get('tz')}")

        want_tz = tz or _TZ_MAP.get(system_tz(), "")
        if want_tz and before.get("tz") != want_tz:
            print(f"setting timezone to {want_tz} (system is {system_tz()})")
            print("  ->", api_post("/cmd/setSetting", {"data": {"time_zone": want_tz}}))
            time.sleep(2)
            now_tz = api("/cmd/getSetting").get("data", {}).get("time_zone")
            print(f"  camera timezone is now {now_tz}")

        for attempt in (1, 2):
            target = datetime.datetime.now()
            send = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            if attempt == 2:
                # Correct by the error the firmware actually introduced.
                send = send + correction
            stamp = send.strftime("%Y-%m-%d %H:%M:%S")
            print(f"attempt {attempt}: sending {stamp}")
            print("  ->", api_post("/cmd/setGmtClock", {"data": stamp}))
            time.sleep(3)

            shown = camera_clock().get("clock", "")
            try:
                shown_dt = datetime.datetime.strptime(shown, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                print(f"  camera returned an unparseable clock: {shown!r}")
                return
            drift = (shown_dt - datetime.datetime.now()).total_seconds()
            print(f"  camera shows {shown}, local is {target:%Y-%m-%d %H:%M:%S}"
                  f"  (off by {drift/3600:+.2f} h)")
            if abs(drift) < 120:
                print("clock is correct")
                return
            correction = datetime.timedelta(seconds=-drift)
            print(f"  correcting by {-drift/3600:+.2f} h and retrying")
        print("clock still off; the firmware may not accept this zone")
    finally:
        ka.stop()


def _video_stream_count(path):
    r = sh(
        f'ffprobe -v error -select_streams v -show_entries stream=index -of csv=p=0 "{path}"'
    )
    return len([ln for ln in r.stdout.splitlines() if ln.strip()])


def strip_preview_track(path):
    """Drop the camera's second, low-res video track.

    GardePro writes two video streams into every clip: the real footage and a
    320x180 preview. Players that assume a single video track (VLC among them)
    fail with "internal stream error", so keep only the primary video and the
    audio. This is a stream copy, so it is lossless and quick.
    Returns True if the file was rewritten.
    """
    if os.environ.get("GARDECAM_NO_REMUX"):
        return False
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return False
    try:
        if _video_stream_count(path) < 2:
            return False
        tmp = path + ".remux.mp4"
        r = sh(
            f'ffmpeg -v error -y -i "{path}" -map 0:v:0 -map "0:a?" -c copy "{tmp}"',
            timeout=300,
        )
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
            return True
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    return False


def cmd_fix(outdir=None):
    """Strip the preview track from clips already on disk."""
    outdir = outdir or PHOTO_DIR
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is not installed; nothing to do")
    files = sorted(f for f in os.listdir(outdir) if f.lower().endswith(".mp4"))
    if not files:
        print(f"no videos in {outdir}")
        return
    fixed = 0
    for name in files:
        path = os.path.join(outdir, name)
        before = os.path.getsize(path)
        if strip_preview_track(path):
            fixed += 1
            print(f"  fixed {name}  ({before} -> {os.path.getsize(path)} bytes)")
        else:
            print(f"  skipped {name} (already single-track)")
    print(f"done, {fixed} of {len(files)} file(s) rewritten in {outdir}")


def cmd_get(fid, kind):
    ka = link_up()
    try:
        path, fetched = download(fid, kind)
        print(("downloaded " if fetched else "already had ") + path)
    finally:
        ka.stop()


def cmd_sync(outdir=PHOTO_DIR):
    ka = link_up()
    try:
        items = list_all_files()
        if not items:
            print("no files reported by camera; raw response:")
            print(json.dumps(list_files(50), indent=2)[:2000])
            return
        total_mb = sum(i.get("size", 0) for i in items) / 1048576
        print(f"camera reports {len(items)} file(s), {total_mb:.1f} MB; syncing to {outdir}")
        new = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            fid = it.get("id")
            if fid is None:
                continue
            kind = "MP4" if it.get("type") == 2 else "JPG"
            try:
                path, fetched = download(fid, kind, outdir, it.get("date"))
                if fetched:
                    new += 1
                    note = ""
                    if kind == "MP4" and strip_preview_track(path):
                        note = ", preview track stripped"
                    print(
                        f"  + {os.path.basename(path)}  "
                        f"({it.get('size',0)/1048576:.1f} MB{note})"
                    )
            except Exception as e:
                print(f"  ! file {fid} failed: {e}")
        print(f"done, {new} new file(s) in {outdir}")
    finally:
        ka.stop()


def cmd_session(seconds=300):
    ka = link_up()
    print(f"link held open for {seconds}s. Camera API at {BASE}")
    print(f"  e.g. curl {BASE}/cmd/info/3")
    try:
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(5)
            if ka.failures > 6:
                print("lost the camera (it likely slept); re-establishing...")
                ka.stop()
                ka = link_up()
    except KeyboardInterrupt:
        pass
    finally:
        ka.stop()
    print("session ended")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "info":
        cmd_info()
    elif cmd == "list":
        cmd_list(int(args[1]) if len(args) > 1 else 20)
    elif cmd == "get":
        cmd_get(args[1], args[2].upper() if len(args) > 2 else "JPG")
    elif cmd == "sync":
        cmd_sync(args[1] if len(args) > 1 else PHOTO_DIR)
    elif cmd == "fix":
        cmd_fix(args[1] if len(args) > 1 else None)
    elif cmd == "setclock":
        cmd_setclock(args[1] if len(args) > 1 else None)
    elif cmd == "session":
        cmd_session(int(args[1]) if len(args) > 1 else 300)
    elif cmd == "disconnect":
        disconnect()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
