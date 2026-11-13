#!/usr/bin/env python3
"""Log Segway Max G3 rides by reading the odometer whenever the scooter shows up over Bluetooth.

  pair --password HEX   save the Segway app's pairing password (recommended, keeps the app working)
  pair --button         register a new pairing by pressing the scooter's power button
  read                  connect once, print the current values and record them
  watch                 run forever, logging a trip each time the odometer has moved
  trips                 list logged trips
  post                  retry posting trips the website hasn't received yet

Trips are posted to the moving Worker when MOVING_ADMIN_TOKEN is set
(MOVING_API overrides the default https://moving.cailinpitt.com).
"""
import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from bleak import BleakScanner

from ninebot_v2.protocol_v2 import BOARD_VCU, V2_VCU_REGISTERS, NinebotV2Client

KM_PER_MI = 1.609344
DEFAULT_DIR = Path.home() / ".segway-logger"
DEFAULT_API = "https://moving.cailinpitt.com"
RETRY_SECONDS = 600
# posted column: 1 posted, 0 waiting, -1 too short to post, -2 rejected by the website
STATUS = {1: "posted", 0: "pending", -1: "too short", -2: "rejected"}
REGISTERS = {reg.key: reg for reg in V2_VCU_REGISTERS}
WANTED = ("Total mileage", "Riding time", "Battery", "Remaining range")

log = logging.getLogger("segway")


class State:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "state.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    @property
    def password(self):
        hexpw = self.data.get("password")
        return bytes.fromhex(hexpw) if hexpw else None


def matches(dev, adv, target: str) -> bool:
    target = target.lower()
    name = (dev.name or adv.local_name or "").lower()
    return dev.address.lower() == target or name == target


async def find(target: str, timeout: float):
    return await BleakScanner.find_device_by_filter(
        lambda d, a: matches(d, a, target), timeout=timeout
    )


async def read_values(client: NinebotV2Client) -> dict:
    values = {}
    for key in WANTED:
        reg = REGISTERS[key]
        try:
            raw = await client.read_register(BOARD_VCU, reg.index, reg.length)
            values[key] = reg.unpack(raw) * reg.scale
        except Exception as err:
            log.debug("Could not read %s: %s", key, err)
    if "Total mileage" not in values:
        raise RuntimeError("Scooter authenticated but did not return the odometer")
    return values


async def session(state: State, device, password, pair_timeout=0.0, on_button=None) -> dict:
    """Connect, authenticate, read, disconnect. Persists anything the handshake learned."""
    client = NinebotV2Client(
        password=password,
        generation=state.data.get("generation"),
        ble_board=state.data.get("ble_board"),
    )
    try:
        await client.connect(device, pair_timeout=pair_timeout, on_wait_for_button=on_button)
        state.data.update(
            password=client.password.hex(),
            generation=client.generation,
            ble_board=client.ble_board,
        )
        state.save()
        return await read_values(client)
    finally:
        # A pairing confirmed on the scooter must be saved even if a later step fails,
        # or the scooter ends up holding a key nobody has.
        if client.password and client.password != password:
            state.data["password"] = client.password.hex()
            state.save()
        await client.disconnect()


def db_open(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    con = sqlite3.connect(data_dir / "trips.db")
    con.execute(
        """CREATE TABLE IF NOT EXISTS readings (
            ts TEXT NOT NULL,
            odometer_km REAL NOT NULL,
            riding_s INTEGER,
            battery_pct INTEGER,
            range_km REAL
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS trips (
            id TEXT PRIMARY KEY,
            started_at INTEGER NOT NULL,
            ended_at INTEGER NOT NULL,
            distance_km REAL NOT NULL,
            riding_s INTEGER,
            battery_pct INTEGER,
            posted INTEGER NOT NULL DEFAULT 0
        )"""
    )
    return con


def record(con: sqlite3.Connection, values: dict, min_km: float):
    """Store a reading; return the trip if the odometer moved since the last one."""
    now = int(time.time())
    odo = round(values["Total mileage"], 1)
    riding = values.get("Riding time")
    riding_s = round(riding * 3600) if riding is not None else None
    battery = round(values["Battery"]) if "Battery" in values else None
    prev = con.execute(
        "SELECT odometer_km, riding_s FROM readings ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    con.execute(
        "INSERT INTO readings VALUES (?, ?, ?, ?, ?)",
        (
            datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            odo,
            riding_s,
            battery,
            values.get("Remaining range"),
        ),
    )
    trip = None
    if prev and odo - prev[0] > 0.05:
        distance = round(odo - prev[0], 1)
        dt = riding_s - prev[1] if riding_s is not None and prev[1] is not None else None
        stamp = now
        while con.execute("SELECT 1 FROM trips WHERE id = ?", (f"scooter-{stamp}",)).fetchone():
            stamp += 1
        # The scooter keeps no trip start time; wheel time back from arrival is the best estimate.
        trip = {
            "id": f"scooter-{stamp}",
            "started_at": now - (dt or 0),
            "ended_at": now,
            "distance_km": distance,
            "riding_s": dt,
            "battery_pct": battery,
            "posted": 0 if distance >= min_km else -1,
        }
        con.execute(
            "INSERT INTO trips VALUES (:id, :started_at, :ended_at, :distance_km, :riding_s, :battery_pct, :posted)",
            trip,
        )
    con.commit()
    return trip


def post_pending(con: sqlite3.Connection):
    """Send every trip the website hasn't received. Safe to repeat: the Worker upserts by id."""
    rows = con.execute(
        "SELECT id, started_at, distance_km, riding_s FROM trips WHERE posted = 0 ORDER BY ended_at"
    ).fetchall()
    if not rows:
        return
    token = os.environ.get("MOVING_ADMIN_TOKEN")
    if not token:
        log.info("%d trip(s) waiting to post; set MOVING_ADMIN_TOKEN to publish them", len(rows))
        return
    api = os.environ.get("MOVING_API", DEFAULT_API).rstrip("/")

    for trip_id, started_at, km, riding_s in rows:
        body = {
            "id": trip_id,
            "startedAt": started_at,
            "startDate": datetime.fromtimestamp(started_at).date().isoformat(),
            "distanceMi": round(km / KM_PER_MI, 2),
            "movingTime": riding_s or 0,
            "elapsedTime": riding_s or 0,
        }
        request = urllib.request.Request(
            f"{api}/activities",
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
                "user-agent": "segway-logger/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:200]
            if err.code == 400:
                con.execute("UPDATE trips SET posted = -2 WHERE id = ?", (trip_id,))
                con.commit()
                log.warning("Website rejected %s: %s", trip_id, detail)
                continue
            log.warning("Posting %s failed (HTTP %s: %s); will retry", trip_id, err.code, detail)
            return
        except OSError as err:
            log.warning("Posting %s failed (%s); will retry", trip_id, err)
            return
        con.execute("UPDATE trips SET posted = 1 WHERE id = ?", (trip_id,))
        con.commit()
        log.info("Posted %s to the website", trip_id)


def fmt_km(km: float) -> str:
    return f"{km / KM_PER_MI:.1f} mi ({km:.1f} km)"


def fmt_trip(km: float, riding_s) -> str:
    text = fmt_km(km)
    if riding_s:
        text += f" in {riding_s // 60} min, avg {km / KM_PER_MI / (riding_s / 3600):.1f} mph"
    return text


def print_values(values: dict):
    print(f"Odometer:        {fmt_km(values['Total mileage'])}")
    if "Battery" in values:
        print(f"Battery:         {values['Battery']:.0f}%")
    if "Remaining range" in values:
        print(f"Remaining range: {fmt_km(values['Remaining range'])}")
    if "Riding time" in values:
        print(f"Riding time:     {values['Riding time']:.1f} h (lifetime)")


async def cmd_pair(args, state: State):
    password = None
    if args.password:
        try:
            password = bytes.fromhex(args.password.strip())
        except ValueError:
            sys.exit("Password must be hex.")
        if len(password) != 16:
            sys.exit(f"Password must be 16 bytes (32 hex characters), got {len(password)}.")
    else:
        print(
            "Button pairing registers this computer with the scooter. If the Segway app\n"
            "is paired, the scooter may refuse, or the app may have to pair again afterwards."
        )

    device = await find(args.target, args.timeout)
    if not device:
        sys.exit(f"{args.target} not found. Is it powered on and the Segway app closed?")
    state.data["target"] = args.target
    values = await session(
        state,
        device,
        password,
        pair_timeout=0.0 if password else 120.0,
        on_button=lambda: print("Press the scooter's power button once now..."),
    )
    print(f"Paired and authenticated. Saved to {state.path}\n")
    print_values(values)


def require_paired(state: State):
    if not state.password or not state.data.get("target"):
        sys.exit("Not paired yet. Run: scooter_logger.py pair <SERIAL> --password <HEX>")


async def cmd_read(args, state: State, con):
    require_paired(state)
    device = await find(state.data["target"], args.timeout)
    if not device:
        sys.exit("Scooter not found. Is it powered on and the Segway app closed?")
    values = await session(state, device, state.password)
    print_values(values)
    trip = record(con, values, args.min_km)
    if trip:
        print(f"\nSince last reading: {fmt_trip(trip['distance_km'], trip['riding_s'])}")
    post_pending(con)


async def cmd_watch(args, state: State, con):
    require_paired(state)
    target = state.data["target"]
    seen = asyncio.Event()
    found = {}

    def on_adv(dev, adv):
        if matches(dev, adv, target):
            found["device"] = dev
            seen.set()

    log.info("Watching for %s", target)
    last_read = None
    while True:
        if last_read is not None:
            wait = args.cooldown * 60 - (time.monotonic() - last_read)
            if wait > 0:
                await asyncio.sleep(wait)
        post_pending(con)
        seen.clear()
        async with BleakScanner(detection_callback=on_adv):
            try:
                await asyncio.wait_for(seen.wait(), timeout=RETRY_SECONDS)
            except asyncio.TimeoutError:
                continue
        log.info("Scooter seen, reading")
        try:
            values = await session(state, found["device"], state.password)
        except Exception as err:
            log.warning("Read failed: %s", err)
            await asyncio.sleep(20)
            continue
        last_read = time.monotonic()
        trip = record(con, values, args.min_km)
        battery = values.get("Battery")
        if trip:
            log.info("Trip: %s, battery %s%%", fmt_trip(trip["distance_km"], trip["riding_s"]), battery)
            if trip["posted"] == -1:
                log.info("Too short to post (under %.2f mi)", args.min_miles)
        else:
            log.info("No new distance (odometer %s, battery %s%%)", fmt_km(values["Total mileage"]), battery)


def cmd_trips(args, con):
    rows = con.execute(
        "SELECT ended_at, distance_km, riding_s, battery_pct, posted FROM trips ORDER BY ended_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No trips logged yet.")
    for ended_at, km, riding_s, battery, posted in rows:
        when = datetime.fromtimestamp(ended_at).strftime("%Y-%m-%d %H:%M")
        print(f"{when}  {fmt_trip(km, riding_s)}  (battery {battery}%)  [{STATUS.get(posted, posted)}]")


def cmd_post(con):
    post_pending(con)
    waiting = con.execute("SELECT COUNT(*) FROM trips WHERE posted = 0").fetchone()[0]
    print(f"{waiting} trip(s) still waiting to post." if waiting else "Nothing waiting to post.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DIR)
    p.add_argument("--timeout", type=float, default=15.0, help="scan timeout in seconds")
    p.add_argument("-v", "--verbose", action="store_true", help="log the BLE handshake")
    p.add_argument("--min-miles", type=float, default=0.25, help="shorter trips are logged but not posted")
    sub = p.add_subparsers(dest="cmd", required=True)

    pair = sub.add_parser("pair", help="pair with the scooter and save the key")
    pair.add_argument("target", help="the scooter's Bluetooth name (its serial) or address")
    how = pair.add_mutually_exclusive_group(required=True)
    how.add_argument("--password", help="the Segway app's 16-byte pairing password, in hex")
    how.add_argument("--button", action="store_true", help="pair by pressing the power button")

    sub.add_parser("read", help="read and record the current values once")

    watch = sub.add_parser("watch", help="log trips whenever the scooter is seen")
    watch.add_argument("--cooldown", type=float, default=5.0, help="minutes between reads (default 5)")

    trips = sub.add_parser("trips", help="list logged trips")
    trips.add_argument("--limit", type=int, default=20)

    sub.add_parser("post", help="retry posting trips the website hasn't received")

    args = p.parse_args()
    args.min_km = args.min_miles * KM_PER_MI
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    if args.verbose:
        logging.getLogger("ninebot_v2").setLevel(logging.DEBUG)

    state = State(args.data_dir)
    try:
        if args.cmd == "pair":
            asyncio.run(cmd_pair(args, state))
            return
        con = db_open(args.data_dir)
        if args.cmd == "trips":
            cmd_trips(args, con)
        elif args.cmd == "post":
            cmd_post(con)
        elif args.cmd == "read":
            asyncio.run(cmd_read(args, state, con))
        else:
            asyncio.run(cmd_watch(args, state, con))
    except KeyboardInterrupt:
        pass
    except TimeoutError as err:
        sys.exit(f"Error: {err}")


if __name__ == "__main__":
    main()
