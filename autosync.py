#!/usr/bin/env python3
"""
autosync - one unattended pass of the whole gardecam pipeline.

  1. gardecam.py sync        pull new clips off the camera (needs BLE range)
  2. gardecam.py disconnect  leave the camera hotspot, back on normal wifi
  3. wildlife.py --remote    push to $GARDECAM_REMOTE, detect, render, pull
  4. ntfy                    one phone notification per new wildlife clip:
                             best frame attached, tap opens the annotated
                             clip in the Immich app

Meant to be fired by a timer (see gardecam-autosync.timer); a lock file keeps
two passes from touching the camera at once. Immich on the remote host picks
the annotated clips up on its own, so nothing here talks to Immich.

Usage:
  autosync.py [--skip-camera] [--test-notify]

  --skip-camera  don't wake the camera; just run the remote scan + notify
                 (useful when the laptop is away from the camera)
  --test-notify  send one sample notification for the newest annotated clip
                 and exit

Settings (from .env or the environment):
  GARDECAM_NTFY_URL   ntfy topic URL, e.g. http://gpu-host:8090/gardecam-<random>
                      (unset = no notifications)
  GARDECAM_IMMICH_URL Immich server URL, used to look the clip up so a tap
                      on the notification deep-links into the Immich app
                      (immich://asset?id=...) - needs the API key below
  GARDECAM_IMMICH_API_KEY  read-only (asset.read) Immich API key; without it
                      a tap just opens the app
  GARDECAM_NOTIFY_MAX max per-clip notifications per pass (default 5); the
                      rest are rolled into one summary message
"""

import argparse
import datetime
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from wildlife import (_load_env, media_files,  # noqa: E402
                      ANNOTATED_SUBDIR, SIDECAR_SUFFIX)

_load_env()
PY = sys.executable
MEDIA = os.path.abspath(os.environ.get("GARDECAM_MEDIA",
                                       os.path.join(HERE, "media")))
LOCK = os.path.join(os.path.expanduser("~"), ".cache", "gardecam-autosync.lock")
NTFY = os.environ.get("GARDECAM_NTFY_URL", "").strip()
IMMICH = os.environ.get("GARDECAM_IMMICH_URL", "").strip()
IMMICH_KEY = os.environ.get("GARDECAM_IMMICH_API_KEY", "").strip()
IMMICH_APP = "immich://"  # the mobile app's URL scheme
NOTIFY_MAX = int(os.environ.get("GARDECAM_NOTIFY_MAX", "5"))
NOT_WILD = ("human", "vehicle")


def log(msg):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def run(*cmd, check=True):
    log("$ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=HERE)
    if check and r.returncode:
        raise RuntimeError(f"{cmd[1] if len(cmd) > 1 else cmd[0]} failed "
                           f"(exit {r.returncode})")
    return r.returncode


def wildlife_clips():
    """{clip name: sidecar dict} for every clip whose sidecar lists wildlife."""
    out = {}
    if not os.path.isdir(MEDIA):
        return out
    for name in media_files(MEDIA):
        sc = os.path.join(MEDIA, name + SIDECAR_SUFFIX)
        try:
            with open(sc) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if [l for l in data.get("present", []) if l not in NOT_WILD]:
            out[name] = data
    return out


def wait_for_remote(tries=24, pause=5):
    """After leaving the camera hotspot it takes a moment to get back online."""
    host = os.environ.get("GARDECAM_REMOTE", "").strip()
    if not host:
        return
    for _ in range(tries):
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=5", "-o",
                            "BatchMode=yes", host, "true"],
                           capture_output=True)
        if r.returncode == 0:
            return
        time.sleep(pause)
    raise RuntimeError(f"{host} not reachable after leaving the camera wifi")


# ------------------------------------------------------------------ ntfy

def notify(title, message, attachment=None, priority=None, tags=None,
           click=None):
    """Publish one message to the ntfy topic; True on success.

    click: URL opened by tapping the notification (default: the Immich app)
    When a file is attached the text travels in the Message header, and HTTP
    headers cannot hold newlines, so multi-line messages are flattened.
    """
    if not NTFY:
        return False
    message = " · ".join(l.strip() for l in message.splitlines() if l.strip())
    headers = {"Title": title.encode("utf-8").decode("latin-1", "replace"),
               "Markdown": "no"}
    headers["Click"] = click or IMMICH_APP
    if priority:
        headers["Priority"] = str(priority)
    if tags:
        headers["Tags"] = ",".join(tags)
    body = message.encode("utf-8")
    if attachment and os.path.exists(attachment):
        # With a file body the text goes in the Message header instead.
        headers["Filename"] = os.path.basename(attachment)
        headers["Message"] = message.encode("utf-8").decode("latin-1", "replace")
        with open(attachment, "rb") as f:
            body = f.read()
    req = urllib.request.Request(NTFY, data=body, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return True
    except Exception as e:  # never let a failed push kill the pass
        log(f"ntfy failed: {e}")
        return False


def clip_when(name):
    """'2026-09-03 21:25' from a camera file name, or '' if it has none."""
    import re
    m = re.search(r"_(\d{8})_(\d{6})", name)
    if not m:
        return ""
    d, t = m.groups()
    return f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}"


def immich_asset_id(filename, tries=6, pause=5):
    """Immich asset id for a file the external library has imported, or None.

    Immich picks new annotated clips up via its folder watch, which can lag
    the end of the scan by a few seconds, so retry briefly.
    """
    if not (IMMICH and IMMICH_KEY):
        return None
    url = IMMICH.rstrip("/") + "/api/search/metadata"
    body = json.dumps({"originalFileName": filename, "size": 1}).encode()
    for attempt in range(tries):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"x-api-key": IMMICH_KEY,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                items = json.load(r).get("assets", {}).get("items", [])
            if items:
                return items[0]["id"]
        except Exception as e:
            log(f"immich lookup failed: {e}")
            return None
        if attempt < tries - 1:
            time.sleep(pause)
    return None


def clip_link(annotated_video):
    """Tap target for a clip: the annotated video inside the Immich app when
    it can be looked up, else just the app."""
    if annotated_video:
        asset = immich_asset_id(os.path.basename(annotated_video))
        if asset:
            return f"{IMMICH_APP}asset?id={asset}"
    return IMMICH_APP


def notify_new(new):
    """One message per new wildlife clip (with its best frame), then a
    summary if there were more than NOTIFY_MAX."""
    items = sorted(new.items())
    for name, data in items[:NOTIFY_MAX]:
        wild = [l for l in data["present"] if l not in NOT_WILD]
        scores = data.get("species", {})
        desc = ", ".join(
            f"{l} ({scores.get(l, {}).get('max_score', 0):.2f})" for l in wild)
        primary = max(wild, key=lambda l: scores.get(l, {}).get("frames", 0))
        safe = primary.replace(" ", "_").replace("/", "_")
        stem = os.path.splitext(name)[0]
        jpg = os.path.join(MEDIA, ANNOTATED_SUBDIR, f"{stem}_{safe}.jpg")
        when = clip_when(name)
        notify(f"gardecam: {primary}",
               f"{desc}\n{name}" + (f"  {when}" if when else ""),
               attachment=jpg if os.path.exists(jpg) else None,
               click=clip_link(data.get("annotated_video")),
               tags=["paw_prints"])
    rest = items[NOTIFY_MAX:]
    if rest:
        counts = {}
        for _, data in rest:
            for l in data["present"]:
                if l not in NOT_WILD:
                    counts[l] = counts.get(l, 0) + 1
        summary = ", ".join(f"{n} {l}" for l, n in
                            sorted(counts.items(), key=lambda kv: -kv[1]))
        notify(f"gardecam: {len(rest)} more clip(s)", summary,
               tags=["paw_prints"])


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--skip-camera", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    args = ap.parse_args()

    if args.test_notify:
        if not NTFY:
            raise SystemExit("GARDECAM_NTFY_URL is not set")
        # Newest clip that has an annotated video: frame attached, tap plays.
        newest = max(((clip_when(n), n, d) for n, d in wildlife_clips().items()
                      if d.get("annotated_video")), default=None)
        sample, clip = None, None
        if newest:
            _, name, data = newest
            vid = os.path.basename(data["annotated_video"])
            sample = os.path.join(MEDIA, ANNOTATED_SUBDIR,
                                  os.path.splitext(vid)[0] + ".jpg")
            clip = clip_link(data["annotated_video"])
        ok = notify("gardecam: test", "notifications are wired up"
                    + (f"\n{name}" if newest else ""),
                    attachment=sample if sample and os.path.exists(sample)
                    else None, click=clip, tags=["white_check_mark"])
        log(f"test notification {'sent to' if ok else 'FAILED for'} {NTFY}")
        raise SystemExit(0 if ok else 1)

    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another autosync pass is still running; skipping")
        return

    before = wildlife_clips()
    camera_ok = True
    try:
        if not args.skip_camera:
            try:
                run(PY, "gardecam.py", "sync", MEDIA)
            except RuntimeError as e:
                # Out of range or the camera never woke: still worth pushing
                # anything already on disk through the scan.
                camera_ok = False
                log(f"camera sync failed: {e}")
            finally:
                run(PY, "gardecam.py", "disconnect", check=False)
            wait_for_remote()
        run(PY, "wildlife.py", "--remote", "--dir", MEDIA)
    except Exception as e:
        log(f"FAILED: {e}")
        notify("gardecam: autosync failed", str(e), priority=2,
               tags=["warning"])
        raise SystemExit(1)

    after = wildlife_clips()
    new = {n: d for n, d in after.items() if n not in before}
    log(f"{len(new)} new wildlife clip(s)"
        + ("" if camera_ok else " (camera sync failed this pass)"))
    if new:
        notify_new(new)
    if not camera_ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
