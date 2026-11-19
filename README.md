# Segway Max G3 trip logger

Watches for the scooter over Bluetooth. Each time it shows up, the script reads the odometer and logs the distance ridden since the last reading.

The encrypted Bluetooth protocol code in `ninebot_v2/` is vendored unmodified from [ha-ninebot](https://github.com/BobMcGlobus/ha-ninebot) (see `ninebot_v2/SOURCE.txt` and `ninebot_v2/NOTICE`). Because it includes that code, this project is licensed under the GNU AGPL-3.0 as well; see `LICENSE`.

## Setup

Requires Python 3.10 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Pairing

The scooter only talks to clients that know its pairing password. The best option is to reuse the Segway app's password, so the app keeps working:

1. Make an **unencrypted** iPhone backup in Finder.
2. Open it with [iBackup Viewer](https://www.imactools.com/iphonebackupviewer/) → AppDomain → `com.ninebot.segway` → `Library/Preferences/com.ninebot.segway.plist`.
3. Export that plist. The password is the key `<SERIAL>_decrypt`, stored as 16 raw bytes, so it won't display as text.
4. With the scooter on and the Segway app closed, pair with it, converting the bytes to hex on the way:

```bash
.venv/bin/python scooter_logger.py pair <SERIAL> --password "$(python3 -c '
import plistlib, sys
print(plistlib.load(open(sys.argv[1], "rb"))[sys.argv[2] + "_decrypt"].hex())
' com.ninebot.segway.plist <SERIAL>)"
```

Delete the exported plist afterwards. It contains the password.

The alternative is `pair <SERIAL> --button`, which asks you to press the power button. It may be refused while the app is paired, or it may force the app to pair again.

The key and the trip log are stored in `~/.segway-logger/`.

## Use

```bash
.venv/bin/python scooter_logger.py read    # one reading
.venv/bin/python scooter_logger.py watch   # run forever, reading once a night
.venv/bin/python scooter_logger.py trips   # list trips
.venv/bin/python scooter_logger.py add --miles 7.7 --minutes 38 --start "2026-11-13 14:44"   # a ride it missed
```

Add `-v` to see the Bluetooth handshake when debugging.

- With `MOVING_ADMIN_TOKEN` set, each trip is posted to `moving.cailinpitt.com` and appears on `/timeline` and `/moving`. Trips under `--min-miles` (default 0.25) are logged but not posted. A failed post is retried every 10 minutes, and `post` retries by hand. `trips` shows whether each trip was posted.
- Connecting to the scooter wakes it up, so `watch` connects only when it has to. It listens to the scooter's Bluetooth advertisements, which it sends even while off, without connecting. When they stop for a few minutes the scooter was taken out; when they come back and stay for two minutes, it's parked again, and `watch` reads it once. That read becomes a trip with real departure and return times.
- A nightly read at 9 PM Eastern (`--at`, `--tz`; retried every `--every` minutes for `--hours`) catches riding that never left range and keeps the last reading fresh. It's skipped if the scooter was already read in that window.
- A "trip" is the distance between two readings, so rides that never left range are merged into the next read.
- Each trip carries the battery level at the previous reading and now, combined across the internal battery and the external one (board `0x06`), weighted by capacity. The external battery recharges the internal one, so only the two together show what a ride used. A trip gets the starting level from the previous reading, or 100% if the charge went up in between, since charging always fills both batteries. If the previous reading is incomplete, it gets only the level left. Missing figures are filled from the state of charge, the last known full capacity, and further attempts in the nightly window.
- The same readings give each trip's energy in watt-hours: the drop in remaining capacity across both batteries at their nominal 46.8 V (13 cells). It's logged but not posted yet: a day's drop also includes standby use and the external battery refilling the internal one.
- Each reading also saves every register the protocol knows, from the scooter and its battery pack, as JSON in `readings.registers`.
- After each read the scooter's lifetime odometer is posted too, for the lifetime counter on `/moving`.
- The scooter accepts one connection at a time. If the Segway app is connected, reads fail until it disconnects.

## Running on a Linux home server

Copy this folder and `~/.segway-logger/state.json` to the server, or pair again on the server. Pair by serial rather than address, because macOS and Linux report different addresses. Then create `/etc/systemd/system/segway-logger.service`:

```ini
[Unit]
Description=Segway trip logger
After=bluetooth.target

[Service]
User=YOUR_USER
EnvironmentFile=/etc/segway-logger.env
WorkingDirectory=/path/to/segway-probe
ExecStart=/path/to/segway-probe/.venv/bin/python scooter_logger.py watch
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

`/etc/segway-logger.env` (`chmod 600`) holds the token from the site's `.env`. `TZ` decides which calendar day a ride lands on:

```
MOVING_ADMIN_TOKEN=...
TZ=America/Chicago
```

```bash
sudo systemctl enable --now segway-logger
journalctl -u segway-logger -f
```
