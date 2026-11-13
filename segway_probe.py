#!/usr/bin/env python3
"""Probe a Segway-Ninebot scooter over BLE to see if it speaks the plain (unencrypted) protocol.

Usage:
  python segway_probe.py                 # scan and list nearby BLE devices
  python segway_probe.py --name G3       # connect to the first device whose name contains "G3"
  python segway_probe.py --address <id>  # connect by address (on macOS this is a UUID, not a MAC)
"""
import argparse
import asyncio
import struct

from bleak import BleakClient, BleakScanner

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

ESC = 0x20
# Different models expect different "app" source addresses.
SOURCES = (0x3E, 0x3D)

REGISTERS = {
    "firmware": (0x1A, 2),
    "battery_pct": (0x22, 2),
    "total_mileage": (0x29, 4),
    "trip_mileage (unverified)": (0xB9, 2),
}


def checksum(body: bytes) -> bytes:
    return struct.pack("<H", (sum(body) ^ 0xFFFF) & 0xFFFF)


def build_read(src: int, dst: int, reg: int, nbytes: int) -> bytes:
    body = bytes([1, src, dst, 0x01, reg, nbytes])
    return b"\x5a\xa5" + body + checksum(body)


class FrameParser:
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf += data
        frames = []
        while True:
            start = self.buf.find(b"\x5a\xa5")
            if start < 0:
                self.buf.clear()
                return frames
            del self.buf[:start]
            if len(self.buf) < 3:
                return frames
            total = self.buf[2] + 9
            if len(self.buf) < total:
                return frames
            raw = bytes(self.buf[:total])
            del self.buf[:total]
            body, chk = raw[2:-2], raw[-2:]
            frames.append({
                "raw": raw,
                "valid": checksum(body) == chk,
                "src": raw[3],
                "dst": raw[4],
                "cmd": raw[5],
                "arg": raw[6],
                "payload": raw[7:-2],
            })


def decode(name: str, payload: bytes) -> str:
    if name == "firmware" and len(payload) >= 2:
        v = struct.unpack("<H", payload[:2])[0]
        return f"{v >> 8:x}.{(v >> 4) & 0xF:x}.{v & 0xF:x}"
    if name == "battery_pct" and len(payload) >= 2:
        return f"{struct.unpack('<H', payload[:2])[0]}%"
    if name == "total_mileage" and len(payload) >= 4:
        m = struct.unpack("<I", payload[:4])[0]
        return f"{m} m  ({m / 1000:.2f} km / {m / 1609.344:.2f} mi)"
    if len(payload) >= 2:
        return f"raw uint16 = {struct.unpack('<H', payload[:2])[0]}"
    return payload.hex(" ")


async def scan(timeout: float):
    print(f"Scanning for {timeout:.0f}s...\n")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    rows = sorted(found.values(), key=lambda da: da[1].rssi, reverse=True)
    for dev, adv in rows:
        nus = NUS_SERVICE in [u.lower() for u in adv.service_uuids]
        name = dev.name or adv.local_name or "?"
        flag = "  <-- Nordic UART (likely scooter)" if nus else ""
        print(f"{adv.rssi:>4} dBm  {dev.address}  {name}{flag}")
    print("\nRe-run with --name <part of name> or --address <address> to probe one.")


async def find(args):
    if args.address:
        return await BleakScanner.find_device_by_address(args.address, timeout=args.timeout)
    needle = args.name.lower()
    return await BleakScanner.find_device_by_filter(
        lambda d, adv: needle in (d.name or adv.local_name or "").lower(),
        timeout=args.timeout,
    )


async def probe(args):
    device = await find(args)
    if not device:
        print("Scooter not found. Is it powered on and the Segway app closed?")
        return

    print(f"Connecting to {device.name} ({device.address})...")
    async with BleakClient(device) as client:
        print("Connected.\n\nServices:")
        for svc in client.services:
            print(f"  {svc.uuid}  {svc.description}")
            for ch in svc.characteristics:
                print(f"      {ch.uuid}  [{', '.join(ch.properties)}]")

        if not client.services.get_service(NUS_SERVICE):
            print("\nNo Nordic UART service found; this scooter uses a different BLE layout.")
            return

        parser = FrameParser()
        pending: dict[int, asyncio.Future] = {}
        stats = {"notifications": 0, "valid": 0, "invalid": 0}

        def on_notify(_, data: bytearray):
            stats["notifications"] += 1
            print(f"    <- {bytes(data).hex(' ')}")
            for f in parser.feed(bytes(data)):
                stats["valid" if f["valid"] else "invalid"] += 1
                fut = pending.get(f["arg"])
                if f["valid"] and fut and not fut.done():
                    fut.set_result(f)

        await client.start_notify(NUS_RX, on_notify)

        results = {}
        for src in SOURCES:
            print(f"\nTrying source address 0x{src:02X}:")
            for name, (reg, n) in REGISTERS.items():
                frame = build_read(src, ESC, reg, n)
                fut = asyncio.get_running_loop().create_future()
                pending[reg] = fut
                print(f"  {name}: -> {frame.hex(' ')}")
                await client.write_gatt_char(NUS_TX, frame, response=False)
                try:
                    f = await asyncio.wait_for(fut, timeout=2.0)
                    results[name] = decode(name, f["payload"])
                    print(f"    = {results[name]}")
                except asyncio.TimeoutError:
                    print("    (no valid response)")
                finally:
                    pending.pop(reg, None)
            if results:
                break

        await client.stop_notify(NUS_RX)

    print("\n=== Verdict ===")
    if results:
        print("Plain protocol works. A home-server mileage logger is straightforward.")
        for k, v in results.items():
            print(f"  {k}: {v}")
    elif stats["invalid"]:
        print("Got frames but checksums failed: the scooter is probably using the encrypted protocol.")
    elif stats["notifications"]:
        print("Got data that isn't plain Ninebot frames: probably the encrypted protocol.")
    else:
        print("No reply at all. Either encrypted firmware ignoring plain commands, or another")
        print("device (your phone) is still connected. Close the Segway app and retry.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--name", help="substring of the scooter's BLE name")
    g.add_argument("--address", help="BLE address (UUID on macOS)")
    p.add_argument("--timeout", type=float, default=10.0, help="scan timeout in seconds")
    args = p.parse_args()
    asyncio.run(probe(args) if (args.name or args.address) else scan(args.timeout))


if __name__ == "__main__":
    main()
