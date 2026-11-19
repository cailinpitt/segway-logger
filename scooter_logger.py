#!/usr/bin/env python3
"""Log Segway Max G3 rides by reading the odometer whenever the scooter shows up over Bluetooth.

  pair --password HEX   save the Segway app's pairing password (recommended, keeps the app working)
  pair --button         register a new pairing by pressing the scooter's power button
  read                  connect once, print the current values and record them
  watch                 run forever, reading the scooter after each ride and once a night
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
import warnings
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bleak import BleakScanner

from ninebot_v2.protocol_v2 import (
    BOARD_VCU,
    V2_BMS_BOARDS,
    V2_BMS_REGISTERS,
    V2_VCU_REGISTERS,
    NinebotV2Client,
)

KM_PER_MI = 1.609344
DEFAULT_DIR = Path.home() / ".segway-logger"
DEFAULT_API = "https://moving.cailinpitt.com"
RETRY_SECONDS = 600
# Both batteries are 13 cells in series; watt-hours are taken at their nominal voltage.
NOMINAL_V = 13 * 3.6
# Energy between two readings counts as the ride's only if the scooter spent at most
# this long awake but not riding (stops mid-ride count). Charging, and the external
# battery refilling the internal one, keep it awake for hours and drain it.
MAX_IDLE_AWAKE_H = 1.5
# The add-on external battery, found by decoding the Segway app's traffic; the internal pack is 0x07.
EXTERNAL_BOARD = 0x06
# posted column: 1 posted, 0 waiting, -1 too short to post, -2 rejected by the website
STATUS = {1: "posted", 0: "pending", -1: "too short", -2: "rejected"}
REGISTERS = {reg.key: reg for reg in V2_VCU_REGISTERS}
WANTED = ("Total mileage", "Riding time", "Battery", "Remaining range", "Charger connected", "Time since charger change")

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


async def read_register(client, board, reg, tries=3) -> bytes:
    """Reads of a parked scooter sometimes go unanswered, so retry a couple of times."""
    for attempt in range(tries):
        try:
            return await client.read_register(board, reg.index, reg.length)
        except Exception:
            if attempt == tries - 1:
                raise
            await asyncio.sleep(0.5)


async def read_values(client: NinebotV2Client) -> dict:
    values = {}
    for key in WANTED:
        reg = REGISTERS[key]
        try:
            raw = await read_register(client, BOARD_VCU, reg)
            values[key] = reg.unpack(raw) * reg.scale
        except Exception as err:
            log.debug("Could not read %s: %s", key, err)
    if "Total mileage" not in values:
        raise RuntimeError("Scooter authenticated but did not return the odometer")
    return values


def scaled(reg, raw: bytes):
    value = reg.unpack(raw)
    if isinstance(value, (int, float)) and reg.scale != 1:
        value = round(value * reg.scale, 4)
    return value


async def read_board(client, board, registers, snapshot, prefix="") -> int:
    """Read what answers into snapshot; return how many registers didn't."""
    missed = 0
    for reg in registers:
        try:
            snapshot[prefix + reg.key] = scaled(reg, await read_register(client, board, reg))
        except Exception as err:
            missed += 1
            log.debug("No %s%s: %s", prefix, reg.key, err)
    return missed


async def find_bms(client, state: State):
    """The battery pack's board number varies by model, so try each candidate once and remember it."""
    if state.data.get("bms_board"):
        return state.data["bms_board"]
    probe = V2_BMS_REGISTERS[0]
    for board in (b for b in V2_BMS_BOARDS if b != EXTERNAL_BOARD):
        try:
            await client.read_register(board, probe.index, probe.length)
        except Exception:
            continue
        state.data["bms_board"] = board
        state.save()
        return board
    return None


# What the battery figures need, read before anything else in case the link doesn't last.
PACK_FIRST = ("Remaining capacity", "Full capacity", "State of charge")


async def read_snapshot(client, state: State) -> dict:
    """Every register the protocol knows, saved raw so later stats can be built from real readings."""
    snapshot = {}
    internal = await find_bms(client, state)
    packs = [("Pack ", internal)] if internal is not None else []
    packs.append(("External ", EXTERNAL_BOARD))
    first = [r for r in V2_BMS_REGISTERS if r.key in PACK_FIRST]
    rest = [r for r in V2_BMS_REGISTERS if r.key not in PACK_FIRST]
    missed = 0
    for prefix, board in packs:
        missed += await read_board(client, board, first, snapshot, prefix)
    missed += await read_board(client, BOARD_VCU, V2_VCU_REGISTERS, snapshot)
    for prefix, board in packs:
        missed += await read_board(client, board, rest, snapshot, prefix)
    if missed:
        log.info("%d register(s) didn't answer%s", missed, "" if charge(snapshot) else ", including battery capacity")
    return snapshot


def fill_known(state: State, values: dict):
    """Fill gaps with values that can't have changed or are read elsewhere.

    Full capacity barely moves, so the last one read stands in. The vehicle's own
    battery percent is the internal pack's state of charge.
    """
    snapshot = values.setdefault("snapshot", {})
    known = state.data.setdefault("full_capacity", {})
    for pack in ("Pack", "External"):
        key = f"{pack} Full capacity"
        if snapshot.get(key):
            if known.get(pack) != snapshot[key]:
                known[pack] = snapshot[key]
                state.save()
        elif known.get(pack):
            snapshot[key] = known[pack]
    if snapshot.get("Pack State of charge") is None and values.get("Battery") is not None:
        snapshot["Pack State of charge"] = round(values["Battery"])


def merge(first: dict, second: dict) -> dict:
    """Two reads of the parked scooter, combined: the newer one wins where both answered."""
    merged = {**first, **second}
    merged["snapshot"] = {**first.get("snapshot", {}), **second.get("snapshot", {})}
    return merged


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
        values = await read_values(client)
        try:
            values["snapshot"] = await read_snapshot(client, state)
        except Exception as err:
            log.warning("Could not read the full register set: %s", err)
        fill_known(state, values)
        return values
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
    add_column(con, "readings", "charger INTEGER")
    add_column(con, "readings", "since_charger_s INTEGER")
    add_column(con, "readings", "registers TEXT")
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
    add_column(con, "trips", "battery_start INTEGER")
    add_column(con, "trips", "battery_end INTEGER")
    add_column(con, "trips", "energy_wh INTEGER")
    add_column(con, "trips", "elapsed_s INTEGER")
    add_column(con, "readings", "combined_pct INTEGER")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return con


def add_column(con, table, column):
    if column.split()[0] not in {row[1] for row in con.execute(f"PRAGMA table_info({table})")}:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column}")


def record(con: sqlite3.Connection, values: dict, min_km: float, outing=None):
    """Store a reading; return the trip if the odometer moved since the last one.

    outing is (departed_at, arrived_at) when the scooter was seen leaving and coming
    back, which gives the trip real times instead of an estimate.
    """
    now = int(time.time())
    odo = round(values["Total mileage"], 1)
    riding = values.get("Riding time")
    riding_s = round(riding * 3600) if riding is not None else None
    battery = round(values["Battery"]) if "Battery" in values else None
    charger = values.get("Charger connected")
    since_charger = values.get("Time since charger change")
    prev = con.execute(
        "SELECT odometer_km, riding_s, registers, ts FROM readings ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    combined = combined_pct(values.get("snapshot"))
    con.execute(
        "INSERT INTO readings (ts, odometer_km, riding_s, battery_pct, range_km, charger, since_charger_s, registers,"
        " combined_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            odo,
            riding_s,
            battery,
            values.get("Remaining range"),
            None if charger is None else int(bool(charger)),
            None if since_charger is None else round(since_charger),
            json.dumps(values["snapshot"]) if values.get("snapshot") else None,
            combined,
        ),
    )
    trip = None
    if prev and odo - prev[0] > 0.05:
        distance = round(odo - prev[0], 1)
        dt = riding_s - prev[1] if riding_s is not None and prev[1] is not None else None
        # The scooter keeps no trip start time; wheel time back from arrival is the best estimate.
        start, end, energy = None, combined, None
        before, after = charge(json.loads(prev[2]) if prev[2] else None), charge(values.get("snapshot"))
        if before and after:
            # A charge between readings is always to full on both batteries, so the ride started at 100%.
            if after[0] > before[0]:
                before = (after[1], after[1])
            start = round(100 * before[0] / before[1])
            idle = idle_awake(json.loads(prev[2]) if prev[2] else None, values.get("snapshot"))
            if idle is not None and idle <= MAX_IDLE_AWAKE_H:
                energy = round((before[0] - after[0]) / 1000 * NOMINAL_V) or None
        started_at, ended_at, elapsed = now - (dt or 0), now, None
        # Only an outing that began after the last reading describes this distance.
        if outing and outing[0] >= datetime.fromisoformat(prev[3]).timestamp():
            started_at, ended_at = outing
            elapsed = ended_at - started_at
        trip = insert_trip(
            con, started_at, ended_at, distance, dt, battery, distance >= min_km, start, end, energy, elapsed
        )
    con.commit()
    return trip


def idle_awake(before, after) -> float | None:
    """Hours the scooter was awake but not riding between two readings, if both have the counters."""
    keys = ("Awake time", "Riding time")
    if not before or not after or any(before.get(k) is None or after.get(k) is None for k in keys):
        return None
    return (after["Awake time"] - before["Awake time"]) - (after["Riding time"] - before["Riding time"])


def charge(snapshot) -> tuple[int, int] | None:
    """(remaining, full) mAh across the internal and external batteries, or None unless both are known.

    The external battery recharges the internal one, so only the two together say
    what a ride used. A missing remaining capacity falls back to state of charge.
    """
    left = full = 0
    for pack in ("Pack", "External"):
        snap = snapshot or {}
        capacity = snap.get(f"{pack} Full capacity")
        remaining = snap.get(f"{pack} Remaining capacity")
        if remaining is None and capacity and snap.get(f"{pack} State of charge") is not None:
            remaining = round(snap[f"{pack} State of charge"] / 100 * capacity)
        if remaining is None or not capacity:
            return None
        left += remaining
        full += capacity
    return left, full


def combined_pct(snapshot) -> int | None:
    both = charge(snapshot)
    return round(100 * both[0] / both[1]) if both else None


def insert_trip(
    con, started_at, ended_at, distance_km, riding_s, battery, postable,
    battery_start=None, battery_end=None, energy_wh=None, elapsed_s=None,
) -> dict:
    stamp = ended_at
    while con.execute("SELECT 1 FROM trips WHERE id = ?", (f"scooter-{stamp}",)).fetchone():
        stamp += 1
    trip = {
        "id": f"scooter-{stamp}",
        "started_at": started_at,
        "ended_at": ended_at,
        "distance_km": distance_km,
        "riding_s": riding_s,
        "battery_pct": battery,
        "posted": 0 if postable else -1,
        "battery_start": battery_start,
        "battery_end": battery_end,
        "energy_wh": energy_wh,
        "elapsed_s": elapsed_s,
    }
    con.execute(
        "INSERT INTO trips (id, started_at, ended_at, distance_km, riding_s, battery_pct, posted, battery_start,"
        " battery_end, energy_wh, elapsed_s) VALUES (:id, :started_at, :ended_at, :distance_km, :riding_s,"
        " :battery_pct, :posted, :battery_start, :battery_end, :energy_wh, :elapsed_s)",
        trip,
    )
    return trip


class PostFailed(Exception):
    pass


def post_json(path: str, body: dict, token: str):
    """POST to the moving Worker. Raises PostFailed to retry later, HTTPError 400 for a rejection."""
    api = os.environ.get("MOVING_API", DEFAULT_API).rstrip("/")
    request = urllib.request.Request(
        f"{api}{path}",
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
            raise ValueError(detail) from err
        raise PostFailed(f"HTTP {err.code}: {detail}") from err
    except OSError as err:
        raise PostFailed(str(err)) from err


def post_pending(con: sqlite3.Connection):
    """Send every trip the website hasn't received, then the odometer. Safe to repeat: the Worker upserts."""
    rows = con.execute(
        "SELECT id, started_at, distance_km, riding_s, battery_start,"
        " COALESCE(battery_end, CASE WHEN battery_start IS NOT NULL THEN battery_pct END), energy_wh, elapsed_s"
        " FROM trips"
        " WHERE posted = 0 ORDER BY ended_at"
    ).fetchall()
    token = os.environ.get("MOVING_ADMIN_TOKEN")
    if not token:
        if rows:
            log.info("%d trip(s) waiting to post; set MOVING_ADMIN_TOKEN to publish them", len(rows))
        return

    for trip_id, started_at, km, riding_s, battery_start, battery_end, energy_wh, elapsed_s in rows:
        body = {
            "id": trip_id,
            "startedAt": started_at,
            "startDate": datetime.fromtimestamp(started_at).date().isoformat(),
            "distanceMi": round(km / KM_PER_MI, 2),
            "movingTime": riding_s or 0,
            "elapsedTime": max(elapsed_s or 0, riding_s or 0),
            "batteryStart": battery_start,
            "batteryEnd": battery_end,
            "energyWh": energy_wh,
        }
        try:
            post_json("/activities", body, token)
        except ValueError as err:
            con.execute("UPDATE trips SET posted = -2 WHERE id = ?", (trip_id,))
            con.commit()
            log.warning("Website rejected %s: %s", trip_id, err)
            continue
        except PostFailed as err:
            log.warning("Posting %s failed (%s); will retry", trip_id, err)
            return
        con.execute("UPDATE trips SET posted = 1 WHERE id = ?", (trip_id,))
        con.commit()
        log.info("Posted %s to the website", trip_id)

    post_odometer(con, token)


def post_odometer(con: sqlite3.Connection, token: str):
    latest = con.execute(
        "SELECT ts, odometer_km, riding_s, registers FROM readings ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if not latest:
        return
    ts, km, riding_s, registers = latest
    both = charge(json.loads(registers) if registers else None)
    body = {
        "odometerMi": round(km / KM_PER_MI, 1),
        "ridingTime": riding_s or 0,
        "readAt": int(datetime.fromisoformat(ts).timestamp()),
        "fullWh": round(both[1] / 1000 * NOMINAL_V) if both else None,
    }
    key = json.dumps([km, body["fullWh"]])
    sent = con.execute("SELECT value FROM meta WHERE key = 'posted_scooter'").fetchone()
    if sent and sent[0] == key:
        return
    try:
        post_json("/scooter", body, token)
    except (ValueError, PostFailed) as err:
        log.warning("Posting the odometer failed (%s); will retry", err)
        return
    con.execute("INSERT OR REPLACE INTO meta VALUES ('posted_scooter', ?)", (key,))
    con.commit()
    log.info("Posted odometer %s to the website", fmt_km(km))


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
    if values.get("snapshot"):
        print("\nAll registers:")
        for key, value in values["snapshot"].items():
            print(f"  {key + ':':27} {value}")


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


def window(day, args, tz):
    start = datetime.combine(day, args.at, tzinfo=tz)
    return start, start + timedelta(hours=args.hours)


def read_since(con, start: datetime) -> bool:
    row = con.execute("SELECT ts FROM readings ORDER BY rowid DESC LIMIT 1").fetchone()
    return bool(row) and datetime.fromisoformat(row[0]).astimezone() >= start


# Reads of a parked scooter can break off partway, so a read missing the battery
# figures is retried and the attempts are merged.
ATTEMPTS_FOR_BATTERY = 3

# Presence, from the advertisements the scooter sends even while off. Seconds.
TICK = 5
AWAY_AFTER = 180  # silence that means it was rolled out, not a dropped advert
SETTLE = 120  # back this long before reading, so it's parked rather than passing by
SEEN_RECENTLY = 30
ARRIVAL_RETRY = 120
ARRIVAL_DEADLINE = 30 * 60


class Presence:
    """Tracks the scooter leaving range and coming back, without connecting to it."""

    def __init__(self, target: str):
        self.target = target
        self.started = time.time()
        self.device = None
        self.last_seen = None
        self.away = False
        self.departed_at = None
        self.back_since = None

    def saw(self, dev, adv):
        if matches(dev, adv, self.target):
            self.device = dev
            self.last_seen = time.time()

    def update(self, now: float) -> str | None:
        """Advance on the latest adverts; returns "left" or "back" when that changes."""
        if self.last_seen is None:
            if not self.away and now - self.started > AWAY_AFTER:
                self.away = True  # away since before we started; departure time unknown
            return None
        silent = now - self.last_seen
        if not self.away and silent > AWAY_AFTER:
            self.away, self.departed_at, self.back_since = True, self.last_seen, None
            return "left"
        if self.away and silent < SEEN_RECENTLY and self.back_since is None:
            self.back_since = self.last_seen
            return "back"
        if self.away and self.back_since is not None and silent > AWAY_AFTER:
            self.back_since = None  # passed by without stopping
        return None

    def settled(self, now: float) -> bool:
        return (
            self.away
            and self.back_since is not None
            and now - self.back_since >= SETTLE
            and self.recent(now)
        )

    def recent(self, now: float) -> bool:
        return self.last_seen is not None and now - self.last_seen < SEEN_RECENTLY

    def home(self):
        self.away, self.departed_at, self.back_since = False, None, None


class ReadJob:
    """One reading, retried until the battery figures are complete or time runs out."""

    def __init__(self, kind: str, retry: float, deadline: float, outing=None):
        self.kind, self.retry, self.deadline, self.outing = kind, retry, deadline, outing
        self.values, self.attempts, self.next_try = None, 0, 0.0

    def due(self, now: float) -> bool:
        return now >= self.next_try

    def add(self, fresh: dict, now: float) -> bool:
        """Merge a read in; True when it's good enough to record."""
        self.attempts += 1
        self.values = merge(self.values, fresh) if self.values else fresh
        self.next_try = now + self.retry
        return bool(charge(self.values["snapshot"])) or self.attempts >= ATTEMPTS_FOR_BATTERY


def nightly_window(args, tz, now: datetime):
    """Tonight's window if it's open now."""
    for day in (now.date() - timedelta(days=1), now.date()):
        start, end = window(day, args, tz)
        if start <= now < end:
            return start, end
    return None


async def cmd_watch(args, state: State, con):
    """Read the scooter once each time it comes back from a ride, plus once a night.

    Connecting wakes the scooter, so it's only read right after it was ridden (it's
    awake anyway) and in the nightly window, which catches anything missed.
    """
    require_paired(state)
    tz = ZoneInfo(args.tz)
    presence = Presence(state.data["target"])
    scanner = BleakScanner(detection_callback=presence.saw)
    scanning = False
    job = None
    last_post = 0.0
    log.info("Watching for %s; nightly read at %s %s", presence.target, args.at.strftime("%H:%M"), args.tz)

    while True:
        if not scanning:
            try:
                await scanner.start()
                scanning = True
            except Exception as err:
                log.warning("Could not start scanning: %s", err)
        await asyncio.sleep(TICK)
        now = time.time()

        change = presence.update(now)
        if change == "left":
            log.info("Scooter left at %s", datetime.fromtimestamp(presence.departed_at).strftime("%H:%M"))
        elif change == "back":
            log.info("Scooter back at %s", datetime.fromtimestamp(presence.back_since).strftime("%H:%M"))

        if presence.settled(now):
            outing = (int(presence.departed_at), int(presence.back_since)) if presence.departed_at else None
            if job is None:
                job = ReadJob("arrival", ARRIVAL_RETRY, now + ARRIVAL_DEADLINE, outing)
            elif job.outing is None:
                job.outing = outing  # back during the nightly read: that read covers the outing
            presence.home()
        if job is None:
            tonight = nightly_window(args, tz, datetime.now(tz))
            if tonight and not read_since(con, tonight[0]):
                job = ReadJob("nightly", args.every * 60, tonight[1].timestamp())
                log.info("Nightly read; trying until %s", tonight[1].strftime("%H:%M %Z"))

        if job and job.due(now) and presence.recent(now):
            if scanning:
                await scanner.stop()
                scanning = False
            log.info("Reading (%s)", job.kind)
            try:
                fresh = await session(state, presence.device, state.password)
            except Exception as err:
                log.warning("Read failed: %s", err)
                job.next_try = now + job.retry
            else:
                if job.add(fresh, time.time()):
                    report(args, con, job.values, job.outing)
                    job = None
                else:
                    log.info("Battery figures incomplete; trying again in %d min", job.retry // 60)
        if job and now >= job.deadline:
            if job.values:
                report(args, con, job.values, job.outing)
            else:
                log.info("No %s read; any riding will be counted at the next one", job.kind)
            job = None

        if now - last_post >= RETRY_SECONDS:
            post_pending(con)
            last_post = now


def report(args, con, values, outing=None):
    trip = record(con, values, args.min_km, outing)
    battery = round(values["Battery"]) if "Battery" in values else None
    if trip:
        if trip["battery_start"] is not None:
            drop = f"{trip['battery_start']}% -> {trip['battery_end']}% (both batteries), {trip['energy_wh']} Wh"
        elif trip["battery_end"] is not None:
            drop = f"{trip['battery_end']}% left"
        else:
            drop = "unknown"
        span = ""
        if trip["elapsed_s"]:
            span = f", out {datetime.fromtimestamp(trip['started_at']):%H:%M}-{datetime.fromtimestamp(trip['ended_at']):%H:%M}"
        log.info("Trip: %s%s, battery %s", fmt_trip(trip["distance_km"], trip["riding_s"]), span, drop)
        if trip["posted"] == -1:
            log.info("Too short to post (under %.2f mi)", args.min_miles)
    else:
        log.info("No new distance (odometer %s, battery %s%%)", fmt_km(values["Total mileage"]), battery)
    post_pending(con)


def cmd_trips(args, con):
    rows = con.execute(
        "SELECT ended_at, distance_km, riding_s, battery_start, COALESCE(battery_end, battery_pct), posted FROM trips"
        " ORDER BY ended_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No trips logged yet.")
    for ended_at, km, riding_s, start, battery, posted in rows:
        when = datetime.fromtimestamp(ended_at).strftime("%Y-%m-%d %H:%M")
        charge = ""
        if battery is not None:
            charge = f"  (battery {start}% -> {battery}%)" if start is not None else f"  (battery {battery}%)"
        print(f"{when}  {fmt_trip(km, riding_s)}{charge}  [{STATUS.get(posted, posted)}]")


def cmd_add(args, con):
    """Log a ride the scooter couldn't be read for. It doesn't touch the odometer baseline."""
    try:
        start = datetime.fromisoformat(args.start)
    except ValueError:
        sys.exit('--start must look like "2026-11-13 14:44" or "2026-11-13T14:44-05:00"')
    started_at = int(start.timestamp())
    riding_s = round(args.minutes * 60)
    km = round(args.miles * KM_PER_MI, 2)
    trip = insert_trip(con, started_at, started_at + riding_s, km, riding_s, None, True)
    con.commit()
    print(f"Logged {trip['id']}: {fmt_trip(km, riding_s)}, started {datetime.fromtimestamp(started_at):%Y-%m-%d %H:%M} local")
    post_pending(con)


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

    watch = sub.add_parser("watch", help="read the scooter once a night and log the day's riding")
    watch.add_argument("--at", type=dtime.fromisoformat, default=dtime(21, 0), help="when to start looking (default 21:00)")
    watch.add_argument("--tz", default="America/New_York", help="time zone for --at (default America/New_York)")
    watch.add_argument("--hours", type=float, default=3.0, help="how long to keep looking (default 3)")
    watch.add_argument("--every", type=float, default=5.0, help="minutes between attempts (default 5)")

    trips = sub.add_parser("trips", help="list logged trips")
    trips.add_argument("--limit", type=int, default=20)

    sub.add_parser("post", help="retry posting trips the website hasn't received")

    add = sub.add_parser("add", help="log and post a ride by hand, e.g. one from before the logger ran")
    add.add_argument("--miles", type=float, required=True)
    add.add_argument("--minutes", type=float, required=True)
    add.add_argument("--start", required=True, help='start time, "2026-11-13 14:44" (local) or with an offset')

    args = p.parse_args()
    args.min_km = args.min_miles * KM_PER_MI
    # BlueZ reports a placeholder MTU on every connection; the library already handles it.
    warnings.filterwarnings("ignore", message="Using default MTU value")
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
        elif args.cmd == "add":
            cmd_add(args, con)
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
