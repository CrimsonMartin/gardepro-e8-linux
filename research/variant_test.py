import os
import sys
import asyncio, subprocess
from bleak import BleakClient, BleakScanner
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gardecam as _g
MAC=_g.BLE_MAC
RX="6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # 0x001f write/wnr
N4="6e400004-b5a3-f393-e0a9-e50e24dcca9e"   # 0x0022 write/wnr/notify
TX="6e400003-b5a3-f393-e0a9-e50e24dcca9e"

notifs=[]
def mk(tag):
    def cb(_c,d):
        notifs.append((tag,d.hex()))
        print(f"  <NOTIFY {tag}> {d.hex()} {d.decode('utf-8','replace')!r}",flush=True)
    return cb

BASE=set()
def scan():
    subprocess.run("nmcli device wifi rescan",shell=True,capture_output=True)
    out=subprocess.run("nmcli -t -f BSSID,SSID,SIGNAL device wifi list",shell=True,capture_output=True,text=True).stdout
    rows=[]
    for line in out.splitlines():
        p=line.replace("\\:","-").split(":")
        if len(p)>=3:
            rows.append((p[0],p[1],p[-1]))
    return rows

def report(label):
    rows=scan()
    new=[r for r in rows if r[0] not in BASE]
    cam=[r for r in rows if any(k in (r[1] or "").upper() for k in ("CAM8","GARDE","NONAME","_E8"))]
    strongnew=[r for r in new if r[2].isdigit() and int(r[2])>=55]
    print(f"  [{label}] total={len(rows)} new={len(new)} strong_new={strongnew} cam={cam}",flush=True)
    return cam or strongnew

async def variant(c,char,payload,resp,label):
    print(f"== {label} ==",flush=True)
    try:
        await c.write_gatt_char(char,payload,response=resp)
        print("  write ok",flush=True)
    except Exception as e:
        print("  writeerr",e,flush=True); return None
    await asyncio.sleep(9)
    return report(label)

async def main():
    global BASE
    BASE={r[0] for r in scan()}
    print("baseline APs:",len(BASE),flush=True)
    print("discovering camera over BLE...",flush=True)
    dev=await BleakScanner.find_device_by_address(MAC,timeout=25)
    print("found:",dev,flush=True)
    async with BleakClient(dev or MAC,timeout=25) as c:
        print("connected",flush=True)
        for u,t in ((TX,"TX3"),(N4,"N4")):
            try: await c.start_notify(u,mk(t)); print("sub",t,flush=True)
            except Exception as e: print("subfail",t,e,flush=True)
        await asyncio.sleep(1)
        WP=b"AT+WAKEPULSE=10\r\n"
        for args in ((RX,WP,False,"RX write-NO-response"),
                     (N4,WP,False,"CHAR4 write-NO-response"),
                     (N4,WP,True, "CHAR4 write-request"),
                     (RX,b"AT+WAKEPULSE=50\r\n",False,"RX wnr WAKEPULSE=50")):
            hit=await variant(c,*args)
            if hit: print("  *** CANDIDATE AP FOUND ***",hit,flush=True)
        print("== burst 6x RX wnr ==",flush=True)
        for _ in range(6):
            try: await c.write_gatt_char(RX,WP,response=False)
            except Exception as e: print("  err",e,flush=True)
            await asyncio.sleep(0.4)
        await asyncio.sleep(14)
        report("after-burst")
        print("total notifications:",len(notifs),flush=True)
asyncio.run(main())
