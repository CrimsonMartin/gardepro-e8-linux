#!/usr/bin/env python3
"""
gardecam - talk to a GardePro E8 WiFi trail camera from Linux or macOS,
no phone app.

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
  gardecam.py sync [DIR] [JOBS]    download everything not already local from
                                   every camera in GARDECAM_BLE_MAC
                                   (JOBS parallel downloads, default 4)
  gardecam.py fix [DIR]            strip preview track from clips already on disk
  gardecam.py setclock [TZ]        sync camera clock + timezone to this machine
  gardecam.py session [SECONDS]    hold the link open (default 300)
  gardecam.py disconnect           drop camera wifi, return to normal network
  gardecam.py scan [SECONDS]       list nearby BLE devices (find your camera)

Platforms:
  Linux drives wifi through NetworkManager (nmcli) and addresses the camera
  by its Bluetooth MAC. macOS has neither: wifi goes through networksetup
  and CoreBluetooth hides MACs behind a per-host UUID, so the camera is
  matched by its advertised name (or GARDECAM_BLE_UUID). GARDECAM_BLE_MAC is
  still required on both - the hotspot name is derived from it.
"""

import asyncio
import json
import os
import re
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
# rather than in the source. See .env.example for how to find yours. Several
# cameras can be listed (comma or space separated); `sync` visits each in turn
# and everything lands in the same media directory, while the other commands
# talk to the first one listed.
CAMERAS = [m.upper() for m in
           re.split(r"[\s,;]+", os.environ.get("GARDECAM_BLE_MAC", "").strip())
           if m]
BLE_MAC = CAMERAS[0] if CAMERAS else ""
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
# Second writable characteristic on the camera's UART service. Writing the wake
# command here as well as to NUS_RX is what actually brings the hotspot up.
NUS_ALT = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
# The hotspot name is the model prefix plus the MAC with the colons stripped.
SSID = "CAM8Z8_" + BLE_MAC.replace(":", "")


# 1-based position of the active camera in CAMERAS; downloads are prefixed
# cam<N>_ so clips from several cameras can share one directory and still say
# where they came from.
CAM_INDEX = 1


def select_camera(mac):
    """Point the module-level camera identity at another listed camera."""
    global BLE_MAC, SSID, CAM_INDEX
    BLE_MAC = mac
    SSID = "CAM8Z8_" + mac.replace(":", "")
    CAM_INDEX = CAMERAS.index(mac) + 1 if mac in CAMERAS else 1
WIFI_PASS = os.environ.get("GARDECAM_WIFI_PASS", "1234567890")
PROFILE = "gardecam"
BASE = "http://192.168.8.1:8080"
# macOS names its wifi radio en0 and has no NetworkManager; everything
# platform-specific in this file branches on IS_MAC.
IS_MAC = sys.platform == "darwin"
IFACE = os.environ.get("GARDECAM_IFACE", "en0" if IS_MAC else "wlp0s20f3")
AIRPORT = ("/System/Library/PrivateFrameworks/Apple80211.framework"
           "/Versions/Current/Resources/airport")
# CoreBluetooth never reveals a peripheral's MAC, it invents a UUID per
# host, so BLE_MAC cannot address the camera on macOS. Pin the UUID here
# (see `gardecam.py scan`) or leave it unset to match on advertised name.
BLE_UUID = os.environ.get("GARDECAM_BLE_UUID", "").strip()
BLE_NAME = os.environ.get("GARDECAM_BLE_NAME", "CAM").strip()
PHOTO_DIR = os.environ.get("GARDECAM_MEDIA", os.path.join(HERE, "media"))
# Raw clips are the bulk of the archive and grow about a gigabyte a day on a
# busy camera, so sync drops the ones older than this. 0 keeps everything.
KEEP_DAYS = int(os.environ.get("GARDECAM_KEEP_DAYS", "14") or 0)


def require_mac():
    if not BLE_MAC:
        raise SystemExit(
            "No camera configured. Copy .env.example to .env and set GARDECAM_BLE_MAC.\n"
            "Find your camera's MAC with:\n"
            "  bluetoothctl --timeout 20 scan le | grep -i CAM   (Linux)\n"
            "macOS cannot read the MAC over Bluetooth at all - take it from a\n"
            "Linux box, or read it off the CAM8Z8_<MAC> hotspot name."
        )


def sh(cmd, timeout=60):
    """Run a shell command and return the CompletedProcess.

    A timeout comes back as a failed result rather than an exception. Every
    caller here treats a non-zero result as "that did not work", and on macOS
    networksetup can block indefinitely behind an authorization dialog that
    nothing is going to answer in an unattended run - which used to take the
    whole sync down with an uncaught TimeoutExpired.
    """
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(
            cmd, 124, e.stdout or "",
            (e.stderr or "") + f"(timed out after {timeout}s)")


# ---------------------------------------------------------------- BLE wake

async def find_camera(BleakScanner, timeout=20):
    """Resolve the camera to a bleak device.

    Linux addresses it by MAC. On macOS CoreBluetooth exposes only a UUID,
    so an explicitly pinned GARDECAM_BLE_UUID is used when set and the
    advertised name otherwise - which means one camera per machine unless
    the UUIDs are pinned.
    """
    if not IS_MAC:
        return await BleakScanner.find_device_by_address(BLE_MAC, timeout=timeout)
    if BLE_UUID:
        return await BleakScanner.find_device_by_address(BLE_UUID, timeout=timeout)
    for d in await BleakScanner.discover(timeout=timeout):
        if (d.name or "").upper().startswith(BLE_NAME.upper()):
            return d
    return None


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
        if not IS_MAC:
            sh(f"bluetoothctl disconnect {BLE_MAC}")
        await asyncio.sleep(2)
        dev = await find_camera(BleakScanner)
        if dev is None:
            who = (BLE_UUID or BLE_NAME + "*") if IS_MAC else BLE_MAC
            hint = " (or Bluetooth is not allowed for this terminal:"\
                   " Privacy & Security > Bluetooth)" if IS_MAC else ""
            raise RuntimeError(
                f"camera {who} is not advertising - out of Bluetooth range"
                f" or powered off{hint}"
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

def register_hotspot():
    """Remember the camera hotspot, ranked below every real network (macOS).

    Storing the password up front is what keeps macOS from raising an
    authorization dialog on each join - one that blocks networksetup until it
    times out, which no unattended run can answer. Joining a network promotes
    it to the top of the preferred list, so this re-pins it to the bottom every
    time; otherwise the camera would outrank the house wifi the moment the
    hotspot is up.
    """
    sh(f'networksetup -removepreferredwirelessnetwork {IFACE} "{SSID}"')
    last = len(preferred_networks())
    return sh(f'networksetup -addpreferredwirelessnetworkatindex '
              f'{IFACE} "{SSID}" {last} WPA2 "{WIFI_PASS}"')


def preferred_networks():
    """Remembered wifi networks, best-ranked first (macOS only)."""
    out = sh(f"networksetup -listpreferredwirelessnetworks {IFACE}").stdout
    return [ln.strip() for ln in out.splitlines()[1:] if ln.strip()]


def hotspot_visible():
    """Signal strength of the camera hotspot as a percentage, or None."""
    if IS_MAC:
        # airport -s rescans on every call. It reports RSSI in dBm; map it onto
        # the same rough 0-100 scale nmcli gives so callers stay platform-blind.
        # The BSSID column is blank without location permission, so the RSSI is
        # found by scanning for the first negative number after the SSID rather
        # than by a fixed column index.
        out = sh(f"{AIRPORT} -s", timeout=40).stdout
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith(SSID + " "):
                continue
            for tok in line[len(SSID):].split():
                if tok.startswith("-") and tok[1:].isdigit():
                    return str(max(0, min(100, 2 * (int(tok) + 100))))
            return "?"
        return None
    sh("nmcli device wifi rescan", timeout=30)
    out = sh("nmcli -t -f SSID,SIGNAL device wifi list", timeout=30).stdout
    for line in out.splitlines():
        if line.startswith(SSID + ":"):
            return line.rsplit(":", 1)[-1]
    return None


def on_camera_wifi():
    if IS_MAC:
        return sh(f"ipconfig getifaddr {IFACE}").stdout.strip().startswith("192.168.8.")
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
            if IS_MAC:
                # Registering the network with its password *before* joining is
                # what stops macOS putting up an authorization dialog on every
                # join. Forgetting it after each pass (which is what this used
                # to do) makes every join look like a brand new network, and
                # the dialog blocks networksetup until it times out. It goes in
                # at the bottom of the preferred list instead, so it can never
                # outrank the real network while the camera is asleep.
                # The password goes on every join. The SecurityAgent dialog that
                # makes unattended joins hang comes from *creating* the keychain
                # entry, never from joining with a password that matches the
                # one already stored - so registering once (below) is the only
                # step that can prompt. Joining without the password instead,
                # to lean on the stored credential, looked equivalent and is
                # not: macOS caches a derived key per access point, and after
                # the camera rebooted on a sagging battery every such join
                # failed with kCWInvalidPMKErr until the password was given
                # again and the key re-derived.
                if SSID not in preferred_networks():
                    register_hotspot()
                r = sh(
                    f'networksetup -setairportnetwork {IFACE} "{SSID}" "{WIFI_PASS}"',
                    timeout=45,
                )
                # networksetup returns as soon as it associates, before DHCP.
                for _ in range(10):
                    if on_camera_wifi():
                        break
                    time.sleep(1)
            else:
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
    if IS_MAC:
        return sh(f"ipconfig getifaddr {IFACE}").stdout.strip() or "?"
    for line in sh(f"ip -4 addr show {IFACE}").stdout.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            return line.split()[1]
    return "?"


def disconnect():
    if IS_MAC:
        # The hotspot deliberately stays in the preferred list (ranked last by
        # connect_wifi) - removing it here is what used to make the next join
        # pop an authorization dialog.
        if not on_camera_wifi():
            print("not on the camera network; nothing to drop")
            return
        # macOS has no "leave this network" verb, so the radio gets bounced and
        # macOS re-picks the best remembered network - the real one, now that
        # the camera has been forgotten. Skipped above when we were never on
        # the camera: autosync calls this after every pass, including the ones
        # where it never came in range, and a needless bounce takes ssh and
        # Tailscale down with it.
        sh(f"networksetup -setairportpower {IFACE} off")
        time.sleep(2)
        sh(f"networksetup -setairportpower {IFACE} on")
        # Reassociation takes a few seconds and the caller goes straight on to
        # talk to the remote host, so don't hand back a dead network.
        for _ in range(20):
            if ip_addr() != "?":
                break
            time.sleep(1)
    else:
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
                # The camera advertises in bursts to save its battery, and not
                # at all while anything still holds a BLE link to it, so one
                # discovery window misses it often. Scanning is passive on our
                # side and costs the camera nothing; a miss used to abort the
                # whole pass before the retry loop below ever got a look.
                print(f"attempt {attempt}: bluetooth wake failed: {waker.error}")
                if attempt == retries:
                    raise SystemExit(f"bluetooth wake failed: {waker.error}")
                time.sleep(5)
                continue
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
        if not IS_MAC:
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


LEDGER_NAME = ".gardecam-synced"


def _ledger_path(outdir):
    return os.path.join(outdir, LEDGER_NAME)


def _read_ledger(outdir):
    try:
        with open(_ledger_path(outdir)) as f:
            return {ln.strip() for ln in f if ln.strip()}
    except OSError:
        return set()


def _append_ledger(outdir, keys):
    if not keys:
        return
    try:
        os.makedirs(outdir, exist_ok=True)
        with open(_ledger_path(outdir), "a") as f:
            for k in sorted(keys):
                f.write(k + "\n")
    except OSError as e:
        print(f"  (could not update the sync ledger: {e})")


def _disk_keys(outdir):
    """{'<id>_<stamp>'} for every clip currently on disk (any camera prefix)."""
    keys = set()
    if not os.path.isdir(outdir):
        return keys
    for n in os.listdir(outdir):
        m = re.match(r"^(?:cam\d+_)?(\d+_\d{8}_\d{6})\.(?:mp4|jpg)$", n, re.I)
        if m and os.path.getsize(os.path.join(outdir, n)) > 0:
            keys.add(m.group(1))
    return keys


def _local_keys(outdir):
    """Ids already fetched: what is on disk now, plus everything ever fetched.

    Old clips get pruned so the disk does not fill, but the camera keeps them
    on its SD card until it rotates, and a listing entry counts as new purely
    because it is not local - so without this ledger every pruned clip would be
    downloaded again on the next pass. Whatever is on disk is folded into the
    ledger as a side effect, which is what seeds it for an archive that predates
    it.
    """
    disk = _disk_keys(outdir)
    ledger = _read_ledger(outdir)
    _append_ledger(outdir, disk - ledger)
    return disk | ledger


def _key(it):
    stamp = str(it.get("date", "")).replace(":", "").replace("-", "").replace(" ", "_")
    return f"{it.get('id')}_{stamp}"


def prune_old(outdir, days=None):
    """Delete raw clips older than `days` from the top level of outdir.

    annotated/ is never walked: those clips are what Immich shows and they are
    a small fraction of the size. Sidecars stay too - a few KB each, and they
    are the record of what was seen.

    Age comes from the capture stamp in the file name rather than mtime, which
    on a copy made by rsync says when the file was transferred rather than when
    the animal walked past.

    This is only safe because _local_keys consults the ledger as well as the
    disk: a listing entry counts as new precisely because it is not local, so
    without that every pruned clip would be downloaded again on the next pass.
    """
    days = KEEP_DAYS if days is None else days
    if days <= 0 or not os.path.isdir(outdir):
        return
    cutoff = time.time() - days * 86400
    freed = gone = 0
    for name in os.listdir(outdir):
        m = re.match(r"^(?:cam\d+_)?\d+_(\d{8})_(\d{6})\.mp4$", name, re.I)
        if not m:
            continue
        try:
            when = time.mktime(time.strptime(m.group(1) + m.group(2),
                                             "%Y%m%d%H%M%S"))
        except ValueError:
            continue
        if when >= cutoff:
            continue
        path = os.path.join(outdir, name)
        try:
            size = os.path.getsize(path)
            os.remove(path)
        except OSError as e:
            print(f"  (could not prune {name}: {e})")
            continue
        freed += size
        gone += 1
    if gone:
        print(f"pruned {gone} raw clip(s) older than {days}d "
              f"({freed / 1073741824:.2f} GB freed)")


def list_new_files(outdir):
    """Like list_all_files, but stop paging as soon as a whole page is
    already on disk. The camera lists newest first, so an unattended sync
    that finds nothing new costs one request instead of one per ~40 files.
    """
    local = _local_keys(outdir)
    items, seen, start = [], set(), 999999
    while True:
        batch = _entries(api(f"/list/detail/JPG/{start}/500", timeout=30))
        fresh = [it for it in batch
                 if isinstance(it, dict) and isinstance(it.get("id"), int)
                 and it.get("id") not in seen]
        if not fresh:
            break
        seen.update(it["id"] for it in fresh)
        items.extend(fresh)
        if all(_key(it) in local for it in fresh):
            break
        start = min(it["id"] for it in fresh)
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
    name = f"{fid}{stamp}.{kind.lower()}"
    # Files synced before cameras were numbered have no prefix; treat them as
    # already downloaded rather than fetching a second copy under the new name.
    legacy = os.path.join(outdir, name)
    if os.path.exists(legacy) and os.path.getsize(legacy) > 0:
        return legacy, False
    path = os.path.join(outdir, f"cam{CAM_INDEX}_{name}")
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
            f'ffmpeg -nostdin -v error -y -i "{path}" -map 0:v:0 -map "0:a?" -c copy "{tmp}"',
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


def _sync_one(it, outdir):
    """Download one listing entry. Returns (fetched, message-or-None)."""
    kind = "MP4" if it.get("type") == 2 else "JPG"
    path, fetched = download(it["id"], kind, outdir, it.get("date"))
    if not fetched:
        return False, None
    note = ""
    if kind == "MP4" and strip_preview_track(path):
        note = ", preview track stripped"
    return True, (f"  + {os.path.basename(path)}  "
                  f"({it.get('size',0)/1048576:.1f} MB{note})")


def cmd_sync(outdir=PHOTO_DIR, jobs=4):
    """Sync every configured camera into outdir, one after another.

    A camera that is out of range (or never wakes) is reported and skipped so
    the rest still get synced; the exit status is non-zero if any failed.
    """
    require_mac()
    failed = []
    for i, mac in enumerate(CAMERAS, 1):
        select_camera(mac)
        if len(CAMERAS) > 1:
            print(f"--- camera {i}/{len(CAMERAS)}: {mac}")
        try:
            _sync_camera(outdir, jobs)
        except SystemExit as e:
            print(f"camera {mac}: {e}")
            failed.append(mac)
        except Exception as e:
            print(f"camera {mac}: sync failed: {e}")
            failed.append(mac)
    # After the cameras, not per-camera: one pass over the directory, and it
    # still runs when a camera was unreachable.
    prune_old(outdir)
    if failed:
        raise SystemExit(f"{len(failed)} of {len(CAMERAS)} camera(s) failed: "
                         + ", ".join(failed))


def _sync_camera(outdir, jobs):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ka = link_up()
    try:
        items = list_new_files(outdir)
        if not items:
            print("no files reported by camera; raw response:")
            print(json.dumps(list_files(50), indent=2)[:2000])
            return
        items = [it for it in items
                 if isinstance(it, dict) and it.get("id") is not None]
        total_mb = sum(i.get("size", 0) for i in items) / 1048576
        print(f"camera reports {len(items)} file(s), {total_mb:.1f} MB; "
              f"syncing to {outdir} with {jobs} worker(s)")
        new, failed = 0, []
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            futs = {pool.submit(_sync_one, it, outdir): it for it in items}
            for fut in as_completed(futs):
                it = futs[fut]
                try:
                    fetched, msg = fut.result()
                except Exception as e:
                    print(f"  ! file {it['id']} failed: {e} (will retry)")
                    failed.append(it)
                    continue
                if fetched:
                    new += 1
                    print(msg)
        # Whatever failed under concurrency gets one calm serial retry: if
        # the firmware chokes on parallel connections, this is the fallback.
        for it in failed:
            try:
                fetched, msg = _sync_one(it, outdir)
                if fetched:
                    new += 1
                    print(msg)
            except Exception as e:
                print(f"  ! file {it['id']} failed again: {e}")
        print(f"done, {new} new file(s) in {outdir}")
    finally:
        ka.stop()
        # Drop the camera hotspot straight away rather than waiting for the
        # camera to time out, so the machine is back on its normal network
        # (and internet) as soon as the downloads finish.
        disconnect()


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


def cmd_scan(seconds=20):
    """List nearby BLE devices so the camera can be identified.

    Linux prints the MAC that goes in GARDECAM_BLE_MAC. macOS prints the
    CoreBluetooth UUID instead, which is what GARDECAM_BLE_UUID takes; the
    MAC still has to come from elsewhere because the hotspot name needs it.
    """
    from bleak import BleakScanner

    print(f"scanning {seconds}s...")
    found = asyncio.run(BleakScanner.discover(timeout=seconds))
    for d in sorted(found, key=lambda d: (d.name or "~")):
        name = d.name or "?"
        mark = "  <- camera?" if name.upper().startswith("CAM") else ""
        print(f"  {d.address}  {name}{mark}")
    if not found:
        print("  nothing advertising"
              + (" (check Bluetooth permission for this terminal)" if IS_MAC else ""))


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
        # sync [DIR] [JOBS] in either order; a bare integer means jobs.
        rest = args[1:]
        jobs = next((int(a) for a in rest if a.isdigit()), 4)
        outdir = next((a for a in rest if not a.isdigit()), PHOTO_DIR)
        cmd_sync(outdir, jobs)
    elif cmd == "fix":
        cmd_fix(args[1] if len(args) > 1 else None)
    elif cmd == "setclock":
        cmd_setclock(args[1] if len(args) > 1 else None)
    elif cmd == "session":
        cmd_session(int(args[1]) if len(args) > 1 else 300)
    elif cmd == "disconnect":
        disconnect()
    elif cmd == "scan":
        cmd_scan(int(args[1]) if len(args) > 1 else 20)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
