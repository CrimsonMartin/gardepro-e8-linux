import os
import sys, json, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gardecam as g

BASE = g.BASE

def req(path, method="GET", body=None, timeout=30, raw=False):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        out = resp.read()
    if raw:
        return out
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except Exception:
        return out.decode("utf-8", "replace")[:300]

def show(label, fn):
    try:
        print(f"{label}: {json.dumps(fn(), ensure_ascii=False)[:600]}", flush=True)
    except Exception as e:
        print(f"{label}: ERR {e}", flush=True)

ka = g.link_up()
try:
    print("=== storage BEFORE ===", flush=True)
    show("info/3", lambda: req("/cmd/info/3"))

    print("\n=== take a picture ===", flush=True)
    show("POST /media/pic/take", lambda: req("/media/pic/take", "POST", {}))
    time.sleep(6)
    show("POST /media/pic/result", lambda: req("/media/pic/result", "POST", {}))
    show("GET  /media/pic/result", lambda: req("/media/pic/result"))
    time.sleep(3)

    print("\n=== storage AFTER ===", flush=True)
    show("info/3", lambda: req("/cmd/info/3"))

    print("\n=== listing attempts ===", flush=True)
    for t in (0, 1, 2):
        for cnt in (1, 5, 20):
            show(f"/list/detail/{t}/0/{cnt}", lambda t=t, c=cnt: req(f"/list/detail/{t}/0/{c}"))
    for path in ("/list/detail/1/1/1", "/list/detail/0/1/20",
                 "/list/detail/JPG/0/10", "/list/detail/jpg/0/10",
                 "/list/detail/all/0/10", "/list/detail/1/0/10/",
                 "/list/detail/-1/0/10", "/list/detail/1/0/0"):
        show(path, lambda p=path: req(p))
finally:
    ka.stop()
