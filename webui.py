#!/usr/bin/env python3
"""
webui - browse gardecam clips grouped by the animals wildlife.py found.

Serves a local gallery of the media directory using the `.wildlife.json`
sidecars: pick a species in the sidebar, get every clip it appears in, newest
first, with the annotated detection frame as the poster. Clips play in place.
Stdlib only; re-reads the sidecars on every page load, so it always reflects
the latest sync + scan without restarting.

Usage:
  webui.py [--dir DIR] [--port PORT] [--host HOST]

  --dir DIR    media directory (default: $GARDECAM_MEDIA or ./media)
  --port PORT  listen port (default 8008)
  --host HOST  bind address (default 127.0.0.1; use 0.0.0.0 for the LAN)
"""

import argparse
import html
import json
import os
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SIDECAR_SUFFIX = ".wildlife.json"
ANNOTATED_SUBDIR = "annotated"

# Real identifications first, taxonomic rollups after, background noise last.
GENERIC = ["cat family", "canis species", "felis species", "bos species",
           "carnivorous mammal", "mammal", "animal"]
LAST = ["human", "vehicle", "unidentified", "nothing detected", "not scanned"]


def _load_env(path=None):
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


def parse_date(name):
    m = re.search(r"_(\d{8})_(\d{6})", name)
    if not m:
        return ""
    d, t = m.group(1), m.group(2)
    return f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}:{t[4:]}"


def collect(mdir):
    """Return {label: [clip, ...]} from the sidecars, clips newest first."""
    groups = {}
    for name in sorted(os.listdir(mdir), reverse=True):
        if not name.lower().endswith((".mp4", ".jpg", ".jpeg")):
            continue
        sc = os.path.join(mdir, name + SIDECAR_SUFFIX)
        clip = {"name": name, "date": parse_date(name),
                "video": name.lower().endswith(".mp4"), "scores": {}}
        if not os.path.exists(sc):
            groups.setdefault("not scanned", []).append(clip)
            continue
        try:
            with open(sc) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            groups.setdefault("not scanned", []).append(clip)
            continue
        species = data.get("species", {})
        present = data.get("present", [])
        for label in present:
            info = species.get(label, {})
            c = dict(clip)
            c["scores"] = {label: info.get("max_score", 0)}
            stem = os.path.splitext(name)[0]
            safe = label.replace(" ", "_").replace("/", "_")
            poster = os.path.join(ANNOTATED_SUBDIR, f"{stem}_{safe}.jpg")
            if os.path.exists(os.path.join(mdir, poster)):
                c["poster"] = poster
            c["others"] = [p for p in present if p != label]
            groups.setdefault(label, []).append(c)
        if not present:
            groups.setdefault("nothing detected", []).append(clip)
    return groups


def group_order(groups):
    def key(label):
        if label in LAST:
            return (2, LAST.index(label))
        if label in GENERIC:
            return (1, GENERIC.index(label))
        return (0, -len(groups[label]))
    return sorted(groups, key=key)


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gardecam wildlife</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #14171c; color: #dfe5ec;
         font: 14px/1.5 system-ui, sans-serif; }}
  header {{ padding: 14px 20px 6px; }}
  h1 {{ margin: 0; font-size: 18px; font-weight: 600; }}
  h1 small {{ color: #7d8a9a; font-weight: 400; margin-left: 8px; }}
  nav {{ display: flex; flex-wrap: wrap; gap: 8px; padding: 10px 20px 16px;
        position: sticky; top: 0; background: #14171c; z-index: 2;
        border-bottom: 1px solid #232a33; }}
  nav a {{ color: #dfe5ec; text-decoration: none; background: #222933;
          border: 1px solid #2e3947; border-radius: 16px; padding: 4px 12px; }}
  nav a.active {{ background: #2d6a4f; border-color: #40916c; }}
  nav a .n {{ color: #8fa3b8; margin-left: 5px; font-size: 12px; }}
  main {{ padding: 16px 20px 40px; }}
  .grid {{ display: grid; gap: 14px;
          grid-template-columns: repeat(auto-fill, minmax(var(--thumb, 300px), 1fr)); }}
  .size {{ margin-left: auto; display: flex; align-items: center; gap: 6px;
          color: #7d8a9a; font-size: 12px; }}
  .size input {{ width: 140px; accent-color: #40916c; }}
  .card {{ background: #1b2027; border: 1px solid #232a33; border-radius: 10px;
          overflow: hidden; }}
  .card video, .card img.still {{ width: 100%; aspect-ratio: 16/9;
          object-fit: cover; display: block; background: #000; }}
  .meta {{ padding: 8px 12px; display: flex; justify-content: space-between;
          gap: 8px; }}
  .meta .date {{ color: #9fb0c1; }}
  .meta .conf {{ color: #74c69d; }}
  .others {{ padding: 0 12px 10px; color: #7d8a9a; font-size: 12px; }}
  .empty {{ color: #7d8a9a; padding: 40px 0; text-align: center; }}
</style></head><body>
<header><h1>gardecam wildlife<small>{total} clips</small></h1></header>
<nav>{nav}<span class="size">size
<input type="range" id="thumb" min="160" max="640" step="20" value="300">
</span></nav>
<main>{body}</main>
<script>
  const slider = document.getElementById("thumb");
  const apply = v => document.documentElement.style.setProperty("--thumb", v + "px");
  let saved = null;
  try {{ saved = localStorage.getItem("thumb"); }} catch (e) {{}}
  if (saved) {{ slider.value = saved; apply(saved); }}
  slider.addEventListener("input", () => {{
    apply(slider.value);
    try {{ localStorage.setItem("thumb", slider.value); }} catch (e) {{}}
  }});
</script>
</body></html>"""


def render(mdir, selected):
    groups = collect(mdir)
    order = group_order(groups)
    if selected not in groups:
        selected = order[0] if order else ""
    from urllib.parse import quote
    nav = "".join(
        f'<a href="/?g={quote(label)}"'
        f'{" class=active" if label == selected else ""}>'
        f'{html.escape(label)}<span class="n">{len(groups[label])}</span></a>'
        for label in order)
    cards = []
    for c in groups.get(selected, []):
        src = f"/media/{c['name']}"
        poster = f"/media/{c['poster']}" if c.get("poster") else ""
        if c["video"]:
            media = (f'<video controls preload="none"'
                     f'{f" poster={chr(34)}{poster}{chr(34)}" if poster else ""}'
                     f' src="{src}"></video>')
        else:
            media = f'<img class="still" loading="lazy" src="{poster or src}">'
        score = next(iter(c["scores"].values()), 0)
        conf = f'<span class="conf">{score:.2f}</span>' if score else ""
        others = ""
        if c.get("others"):
            others = ('<div class="others">also: '
                      + html.escape(", ".join(c["others"])) + "</div>")
        cards.append(
            f'<div class="card">{media}<div class="meta">'
            f'<span class="date">{c["date"] or html.escape(c["name"])}</span>'
            f'{conf}</div>{others}</div>')
    body = (f'<div class="grid">{"".join(cards)}</div>' if cards
            else '<div class="empty">no clips in this group</div>')
    total = sum(1 for n in os.listdir(mdir)
                if n.lower().endswith((".mp4", ".jpg", ".jpeg")))
    return PAGE.format(total=total, nav=nav, body=body)


class Handler(SimpleHTTPRequestHandler):
    mdir = None

    def do_GET(self):
        if self.path.startswith("/media/"):
            # Serve media files (with Range support for video scrubbing is
            # not in stdlib; browsers cope with full-file responses).
            rel = os.path.normpath(self.path[len("/media/"):]).lstrip("/")
            full = os.path.join(self.mdir, rel)
            if not os.path.abspath(full).startswith(self.mdir + os.sep) \
                    or not os.path.isfile(full):
                self.send_error(404)
                return
            self.serve_file(full)
            return
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        selected = q.get("g", [""])[0]
        page = render(self.mdir, selected).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def serve_file(self, full):
        ctype = self.guess_type(full)
        size = os.path.getsize(full)
        # Minimal HTTP Range support so <video> can seek.
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
        with open(full, "rb") as f:
            f.seek(start)
            length = end - start + 1
            if rng:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.environ.get(
        "GARDECAM_MEDIA", os.path.join(HERE, "media")))
    ap.add_argument("--port", type=int, default=8008)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    Handler.mdir = os.path.abspath(args.dir)
    if not os.path.isdir(Handler.mdir):
        raise SystemExit(f"media directory not found: {Handler.mdir}")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"serving {Handler.mdir} at http://{args.host}:{args.port}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
