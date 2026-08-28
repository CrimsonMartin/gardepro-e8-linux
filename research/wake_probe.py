import sys
import asyncio, sys
from bleak import BleakClient, BleakScanner

MAC = "<set GARDECAM_BLE_MAC in .env>"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # write
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # notify
NUS_4  = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"  # notify/indicate

def show(tag, data):
    try:
        txt = data.decode("utf-8", "replace")
    except Exception:
        txt = ""
    print(f"[NOTIFY {tag}] hex={data.hex()} ascii={txt!r}", flush=True)

async def main():
    print("scanning for camera...", flush=True)
    dev = await BleakScanner.find_device_by_address(MAC, timeout=20)
    if not dev:
        print("device not found in scan; trying direct connect anyway", flush=True)
    async with BleakClient(dev or MAC, timeout=30) as c:
        print("connected:", c.is_connected, flush=True)
        # subscribe to both notify chars
        for u, tag in ((NUS_TX, "TX3"), (NUS_4, "N4")):
            try:
                await c.start_notify(u, lambda _cf, d, t=tag: show(t, d))
                print("subscribed", tag, flush=True)
            except Exception as e:
                print("subscribe fail", tag, e, flush=True)
        await asyncio.sleep(1)
        for cmd in (b"AT+WAKEPULSE=10\r\n", b"AT+WAKEPULSE=10\r\n"):
            print("writing:", cmd, flush=True)
            try:
                await c.write_gatt_char(NUS_RX, cmd, response=True)
            except Exception as e:
                print("write err (response):", e, flush=True)
                await c.write_gatt_char(NUS_RX, cmd, response=False)
            await asyncio.sleep(8)
        print("listening 20s more for late notifications...", flush=True)
        await asyncio.sleep(20)
    print("done", flush=True)

asyncio.run(main())
