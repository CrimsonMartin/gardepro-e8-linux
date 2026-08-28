import os
import sys, json, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gardecam as g

def req(path, timeout=25):
    with urllib.request.urlopen(g.BASE + path, timeout=timeout) as r:
        out = r.read()
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except Exception:
        return out.decode("utf-8", "replace")[:200]

def try_path(p):
    try:
        r = req(p)
        s = json.dumps(r, ensure_ascii=False)
        interesting = not (s == '{"code": 0, "data": []}' or '"illegeal para"' in s)
        print(("*** " if interesting else "    ") + f"{p} -> {s[:500]}", flush=True)
        return r if interesting else None
    except Exception as e:
        print(f"    {p} -> ERR {e}", flush=True)
        return None

ka = g.link_up()
hits = []
try:
    print("=== start-index sweep for JPG ===", flush=True)
    for start in (1, 1000, 1001, 1002, 999, 100, 10000):
        r = try_path(f"/list/detail/JPG/{start}/10")
        if r: hits.append(r)

    print("\n=== type token sweep (start 0 and 1001) ===", flush=True)
    for t in ("PIC", "PHOTO", "IMG", "MP4", "VIDEO", "AVI", "ALL", "All", "Jpg", "JPEG", "0", "1"):
        for start in (0, 1001):
            r = try_path(f"/list/detail/{t}/{start}/10")
            if r: hits.append(r)

    print("\n=== count/order variants ===", flush=True)
    for p in ("/list/detail/JPG/0/1", "/list/detail/JPG/0/100", "/list/detail/JPG/0/1000",
              "/list/detail/JPG/1001/1", "/list/detail/JPG/1001/100",
              "/thumb/1001/JPG", "/file/1001/JPG"):
        try:
            with urllib.request.urlopen(g.BASE + p, timeout=25) as r:
                data = r.read()
            if data[:3] == b"\xff\xd8\xff":
                print(f"*** {p} -> JPEG IMAGE, {len(data)} bytes", flush=True)
                hits.append(p)
            else:
                print(f"    {p} -> {data[:200]!r}", flush=True)
        except Exception as e:
            print(f"    {p} -> ERR {e}", flush=True)
finally:
    ka.stop()
print("\nINTERESTING:", json.dumps(hits, ensure_ascii=False)[:1500])
