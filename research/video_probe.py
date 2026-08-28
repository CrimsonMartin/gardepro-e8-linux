import os
import sys, json, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gardecam as g

def req(path, method="GET", body=None, timeout=30):
    data = None; headers = {}
    if body is not None:
        data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
    r = urllib.request.Request(g.BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        out = resp.read()
    try: return json.loads(out.decode("utf-8", "replace"))
    except Exception: return out.decode("utf-8", "replace")[:200]

def show(label, fn):
    try: print(f"  {label} -> {json.dumps(fn(), ensure_ascii=False)[:400]}", flush=True)
    except Exception as e: print(f"  {label} -> ERR {e}", flush=True)

ka = g.link_up()
try:
    print("=== ALL photos currently indexed (start=999999) ===", flush=True)
    show("/list/detail/JPG/999999/500", lambda: req("/list/detail/JPG/999999/500"))

    print("\n=== storage ===", flush=True)
    show("/cmd/info/3", lambda: req("/cmd/info/3"))
    show("/cmd/info/5", lambda: req("/cmd/info/5"))

    print("\n=== record a short video ===", flush=True)
    show("POST /media/video/start", lambda: req("/media/video/start", "POST", {}))
    time.sleep(12)
    show("POST /media/video/stop", lambda: req("/media/video/stop", "POST", {}))
    time.sleep(6)
    show("/cmd/info/3 after", lambda: req("/cmd/info/3"))

    print("\n=== video listing tokens ===", flush=True)
    for t in ("MP4", "AVI", "MOV", "VID", "VIDEO", "JPG"):
        show(f"/list/detail/{t}/999999/50", lambda t=t: req(f"/list/detail/{t}/999999/50"))
finally:
    ka.stop()
