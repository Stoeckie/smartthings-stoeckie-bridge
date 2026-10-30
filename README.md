# Stoeckie Bridge: cloud appliances in SmartThings (bridge + Edge driver)

One small bridge brings cloud-only appliances into SmartThings:

| Source | What you get | Brand page |
|---|---|---|
| **AEG / Electrolux** dryer (official Electrolux Group Developer API) | state, job, remaining time, door, finished; start, pause, stop | [smartthings-aeg-bridge](https://github.com/Stoeckie/smartthings-aeg-bridge) |
| **Fujitsu FGLair** air conditioners | on/off, mode, setpoint, fan, swing, room and outdoor temperature | [smartthings-fujitsu-fglair](https://github.com/Stoeckie/smartthings-fujitsu-fglair) |
| **Škoda** cars (official MyŠkoda public API) | battery, charging on/off and power, charging cable, climate, at home, doors | [smartthings-skoda-bridge](https://github.com/Stoeckie/smartthings-skoda-bridge) |
| **iRobot** favorites (optional) | favorite names and rooms for the [iRobot Roomba (local)](https://github.com/Stoeckie/smartthings-irobot-roomba-local) driver | |

**This repository is the only place with the code.** The brand pages above explain one brand each and point here. Use only what you need: a source you do not set up is skipped.

SmartThings Edge drivers can not reach the internet, so there are two parts:

```
Electrolux / FGLair / Škoda / iRobot cloud <-- bridge (Windows pc or Docker, port 8787) <--LAN-- Edge driver on the hub
```

* `bridge/`: small Python service for a pc that is always on: Windows (installer) or any Docker host (Linux, NAS, Raspberry Pi)
* `driver/`: SmartThings Edge driver **Stoeckie Bridge (local)** that finds the bridge and creates the devices

## 1. Bridge on Windows

1. Download this repository (Code → Download ZIP) and copy the `bridge` folder to the pc, e.g. `C:\StoeckieBridge`.
2. Double-click `install.bat`. It installs Python if needed (winget), the packages, a firewall rule for port 8787 (Windows asks once) and a scheduled task that starts the bridge at logon and restarts it on failure. Then the setup page opens.
3. Fill in the sections you need on the setup page (`setup.bat` or http://localhost:8787/setup, only reachable on the pc itself). See the brand pages for where to get each key.
4. `check.bat` tests the AEG and FGLair logins. `restart.bat` restarts the bridge. Log: `bridge.log`.

Set the Windows network to **Private** and give the pc a fixed IP (DHCP reservation), otherwise the hub can not reach the bridge.

**Update:** copy the new files over the old ones and run `install.bat` again; `config.json` stays.

**Install or move to another Windows pc over the LAN:**

1. On the target pc, once: right click `prep-remote.bat` > Run as administrator (network to Private, PowerShell remoting on).
2. On the pc where the bridge runs now: `install-remote.bat`. It asks for the target IP and an administrator login of the target (not stored), installs Python, the bridge and the packages in `C:\StoeckieBridge`, copies `config.json`, opens port 8787, registers a task that starts the bridge at boot as SYSTEM (no logon needed), checks the target over the LAN and then switches the bridge off here. On any error the bridge here starts again.
3. SmartThings needs nothing: the bridge id moves with `config.json`, so the driver finds the same bridge on the new IP by itself within a few minutes (or Add device > Scan nearby). Give the target a DHCP reservation.

Never run two bridges with the same `config.json`: Electrolux refresh tokens rotate and Škoda allows 20 requests per hour.

## 1b. Bridge in Docker (Linux, NAS, Raspberry Pi)

```
cd bridge
docker compose up -d --build
```

* Host networking, so the hub finds the bridge on the host IP, port 8787. Data (config.json, log) lives in `bridge/data`.
* Setup page: http://localhost:8787/setup on the Docker host. From another computer: set `STBRIDGE_SETUP_ALLOW: "192.168.x.0/24"` in `docker-compose.yml`.
* Credentials can also be passed once as environment variables (`ELECTROLUX_API_KEY`, `ELECTROLUX_ACCESS_TOKEN`, `ELECTROLUX_REFRESH_TOKEN`, `FGLAIR_USERNAME`, `FGLAIR_PASSWORD`, `FGLAIR_REGION`); after the first start `data/config.json` wins.
* Without host networking (Docker Desktop on Windows/Mac): use `ports: ["8787:8787"]` instead of `network_mode: host`, and set `STBRIDGE_SETUP_ALLOW`.

## 2. Edge driver on the hub

1. Enroll your hub in the channel: https://bestow-regional.api.smartthings.com/invite/qWjBdybRNG2y (Stoeckie Edge Drivers) and install **Stoeckie Bridge (local)**.
2. SmartThings app: **Add device → Scan nearby**. The driver scans the hub's subnet on port 8787 and creates a "Stoeckie Bridge" device; your appliances and cars appear at the next poll.

## Security and privacy

* Credentials only live in `config.json` on the bridge pc (in `.gitignore`), never in the code or the driver.
* The setup page only answers on the pc itself (or `STBRIDGE_SETUP_ALLOW`), only when opened by IP or `localhost`, and refuses forms posted from other websites.
* The LAN API only answers private and local addresses: `GET /stbridge/info`, `GET /stbridge/devices`, `POST /stbridge/devices/<id>/command` with `{"cmd": ..., "value": ...}`, `GET /stbridge/irobot/<blid>`.

## Unofficial parts

* FGLair uses [ayla-iot-unofficial](https://pypi.org/project/ayla-iot-unofficial/) (the library of the Home Assistant FGLair integration).
* iRobot favorites use [roombapy-prime](https://github.com/johnnyh1975/roombapy-prime); iRobot limits logins per account.
* Electrolux and Škoda use the manufacturers' official APIs.

## Development

```
pip install -r bridge/requirements.txt pytest lupa
python -m pytest bridge/tests driver/tests
```

Tests run without real accounts: a fake Ayla cloud (FGLair), a fake Škoda API, a fake iRobot account, and the Edge driver (`driver/src/init.lua`) with a mocked SmartThings runtime against the bridge in `--demo` mode.

## License

Apache 2.0, see LICENSE. Not affiliated with AB Electrolux, AEG, Fujitsu General, Ayla Networks, Škoda Auto, iRobot or Samsung SmartThings.
