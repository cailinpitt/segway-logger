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
.venv/bin/python scooter_logger.py watch   # run forever
.venv/bin/python scooter_logger.py trips   # list trips
```

Add `-v` to see the Bluetooth handshake when debugging.

- With `MOVING_ADMIN_TOKEN` set, each trip is posted to `moving.cailinpitt.com` and appears on `/timeline` and `/moving`. Trips under `--min-miles` (default 0.25) are logged but not posted. A failed post is retried every 10 minutes, and `post` retries by hand. `trips` shows whether each trip was posted.
- A "trip" is the distance ridden between two readings. Rides that end while the scooter is off, or inside the cooldown window (`--cooldown`, default 5 min), are merged into the next trip rather than lost.
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
