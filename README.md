# Stoeckie Bridge: cloud appliances in SmartThings (bridge + Edge driver)

One small bridge brings cloud-only appliances into SmartThings:

| Source | What you get | Brand page |
|---|---|---|
| **AEG / Electrolux** dryer (official Electrolux Group Developer API) | state, job, remaining time, door, finished; start, pause, stop | [smartthings-aeg-bridge](https://github.com/Stoeckie/smartthings-aeg-bridge) |
| **Fujitsu FGLair** air conditioners | on/off, mode, setpoint, fan, swing, room and outdoor temperature (outdoor only on units with an outdoor sensor; others switch to a layout without it by themselves), Minimum Heat (as a mode, on units that have it) | [smartthings-fujitsu-fglair](https://github.com/Stoeckie/smartthings-fujitsu-fglair) |
| **Škoda** cars (official MyŠkoda public API) | battery, charging on/off and power, charging cable, climate, at home, doors | [smartthings-skoda-bridge](https://github.com/Stoeckie/smartthings-skoda-bridge) |
| **VELUX ACTIVE** departure mode (optional) | lock and unlock (departure mode: all windows and blinds blocked); rain sensor as its own device | windows: [VELUX ACTIVE (HomeKit local)](https://github.com/Stoeckie/smartthings-velux-active-local) |
| **Rain radar** (optional) | rain now and the next 2 hours at your address with a radius: rain or dry, mm/h, minutes until rain, how long it rains. Buienradar in NL/BE, Open-Meteo worldwide. Plus the outdoor dew point now and the highest in the next 48 hours, and the wind now in km/h (Open-Meteo) | |
| **Home Connect** (Bosch, Siemens, Neff) dishwasher and oven, local (optional) | on/off, running, remaining time, progress, door, oven temperature; choose and start programs, oven temperature and cooking time; stop, pause, resume. No cloud after setup | [below](#home-connect-bosch--siemens--neff-local) |
| **Samsung** washer, local (optional, experimental) | on/off, running, phase, remaining time, progress; start, pause, stop. No cloud | [below](#samsung-washer-local-experimental) |
| **Samsung TV** | not through the bridge: use the Edge driver [Samsung TV (local)](https://github.com/Stoeckie/smartthings-samsung-tv-local), which runs on the hub itself (power, volume, mute, input, channel). The older TV support in the bridge still works but is no longer developed | [smartthings-samsung-tv-local](https://github.com/Stoeckie/smartthings-samsung-tv-local) |
| **iRobot** favorites (optional) | favorite names and rooms for the [iRobot Roomba (local)](https://github.com/Stoeckie/smartthings-irobot-roomba-local) driver | |

**This repository is the only place with the code.** The brand pages above explain one brand each and point here. Use only what you need: a source you do not set up is skipped.

SmartThings Edge drivers can not reach the internet, so there are two parts:

```
Electrolux / FGLair / Škoda / iRobot / VELUX / Buienradar / Open-Meteo / Home Connect (LAN) <-- bridge (Windows pc or Docker, port 8787) <--LAN-- Edge driver on the hub
```

* `bridge/`: small Python service for a pc that is always on: Windows (installer) or any Docker host (Linux, NAS, Raspberry Pi)
* `driver/`: SmartThings Edge driver **Stoeckie Bridge (local)** that finds the bridge and creates the devices (it also finds the bridge again when it moves to another IP)

## 1. Bridge on Windows

1. Download this repository (Code → Download ZIP) and copy the `bridge` folder to the pc, e.g. `C:\StoeckieBridge`.
2. Double-click `install.bat`. It installs Python if needed (winget), the packages, a firewall rule for port 8787 (Windows asks once) and a scheduled task that starts the bridge at logon and restarts it on failure. Then the setup page opens.
3. Fill in the sections you need on the setup page (`setup.bat` or http://localhost:8787/setup, only reachable on the pc itself). See the brand pages for where to get each key.
4. `check.bat` tests the AEG and FGLair logins. `restart.bat` restarts the bridge, `stop.bat` stops it and switches its autostart off. Log: `bridge.log`.

Set the Windows network to **Private** and give the pc a fixed IP (DHCP reservation), otherwise the hub can not reach the bridge.

**Update:** copy the new files over the old ones and run `install.bat` again; `config.json` stays.

**Install or move to another Windows pc over the LAN:**

1. On the target pc, once: right click `prep-remote.bat` > Run as administrator (network to Private, PowerShell remoting on). The installer limits remoting on the target to the own network.
2. On the pc where the bridge runs now: `install-remote.bat`. It asks for the target IP and an administrator login of the target (not stored), installs Python, the bridge and the packages in `C:\StoeckieBridge`, copies `config.json`, opens port 8787, registers a task that starts the bridge at boot as SYSTEM (no logon needed), checks the target over the LAN and then switches the bridge off here. On any error the bridge here starts again.
3. SmartThings needs nothing: the bridge id moves with `config.json`, so the driver finds the same bridge on the new IP by itself within a few minutes (or Add device > Scan nearby). Give the target a DHCP reservation.
4. **Update later:** put the new files in the bridge folder here and run `update-remote.bat`. The first `install-remote.bat` run asks whether to save the login: it is stored in `remote-cred.xml`, encrypted with Windows DPAPI for your Windows account on this pc (no other account or pc can read it). Admin rights on this pc are only needed once (WinRM client service on, target IP trusted; this pc gets no remoting listener), so `update-remote.bat` runs without prompts. It never overwrites `config.json` on the target. Once the login and the answers are saved, `install-remote.bat` does not ask anything either. Delete `remote-cred.xml` to forget the login, `remote-target.json` to get the questions back. Or run `install-remote.bat` again and answer **n** when it asks to overwrite `config.json` (the target has the current tokens). Afterwards it copies the target's `config.json` back here, so the copy here stays a current backup. On the target the bridge runs as SYSTEM, so `restart.bat` there needs "Run as administrator".

Never run two bridges with the same `config.json`: Electrolux refresh tokens rotate and Škoda allows 20 requests per hour.

## 1b. Bridge in Docker (Linux, NAS, Raspberry Pi)

```
cd bridge
docker compose up -d --build
```

* **Update:** `git pull`, then in `bridge` again `docker compose up -d --build` (always with `--build`: since 2026-11-07 the image is `python:3.14-slim`). Settings in `bridge/data` stay.
* Host networking (Linux), so the hub finds the bridge on the host IP, port 8787. Data (config.json, log) lives in `bridge/data`.
* **Setup page:** with host networking open http://localhost:8787/setup in a browser **on the Docker host**. With port mapping (Docker Desktop, rootless Docker, many NAS) a browser on the host arrives from the Docker gateway, and on Docker Desktop so does every other computer on your LAN. So from the gateway the bridge asks for the **setup key** first (https only): it is in the log (`docker logs stoeckie-bridge`) and in `data/setup-key.txt`. No need for a text browser inside the container.
* **From another computer:** add its address to `STBRIDGE_SETUP_ALLOW` in `docker-compose.yml`, e.g. `STBRIDGE_SETUP_ALLOW: "192.168.1.20/32"`, run `docker compose up -d` and open https://<docker host ip>:8443/setup (https only, credentials never cross the LAN in plain text).
* **Without host networking** (Docker Desktop on Windows/Mac, many NAS): use `ports: ["8787:8787", "8443:8443"]` instead of `network_mode: host`. A browser on the Docker host then reaches the container from the Docker network gateway; the bridge (2026-11-06a or newer) recognizes that inside a container and allows it, so http://localhost:8787/setup on the host just works. Other computers still need `STBRIDGE_SETUP_ALLOW`. Switch this off with `STBRIDGE_SETUP_ALLOW_GATEWAY: "0"`. The hub must reach port 8787 on the host IP; set that IP in the bridge device settings if the automatic search does not find it.
* Credentials can also be passed once as environment variables (`ELECTROLUX_API_KEY`, `ELECTROLUX_ACCESS_TOKEN`, `ELECTROLUX_REFRESH_TOKEN`, `FGLAIR_USERNAME`, `FGLAIR_PASSWORD`, `FGLAIR_REGION`); after the first start `data/config.json` wins.

## 2. Edge driver on the hub

1. Enroll your hub in the channel: https://bestow-regional.api.smartthings.com/invite/qWjBdybRNG2y (Stoeckie Edge Drivers) and install **Stoeckie Bridge (local)**.
2. SmartThings app: **Add device → Scan nearby**. The driver scans the hub's subnet on port 8787 and creates a "Stoeckie Bridge" device; your appliances and cars appear at the next poll.

## Settings: setup page and SmartThings

Everything that is not a secret can be set in both places; the last change wins.

| Source | In SmartThings (device settings) | Setup page only |
|---|---|---|
| AEG / Electrolux | | API key, tokens |
| Fujitsu FGLair | update interval (any air conditioner, applies to all) | login, region |
| Škoda | update interval, climate temperature, battery history graph | API key, VIN |
| VELUX | update interval (on VELUX vertrekstand) | login, gateway pairing |
| Rain radar | on/off (Stoeckie Bridge device); postcode + house number, address or coordinates, radius, rain threshold, source, show (rain radar device) | |
| iRobot | | login |

## Rain radar (Buienradar, Open-Meteo)

Rain at your address, now and the next two hours, refreshed every 5 minutes. Sources:

* **Buienradar** in the Netherlands and Belgium: radar forecast in 5 minute steps.
* **Open-Meteo** everywhere else: forecast in 15 minute steps, no account or key ([open-meteo.com](https://open-meteo.com), CC BY 4.0).
* **Auto** (default) picks Buienradar inside NL/BE and Open-Meteo outside.

1. Switch it on: setting **Rain radar** of the **Stoeckie Bridge** device in SmartThings (or fill in the rain radar section of the setup page). A **Buienradar** / **Rain radar** device appears.
2. In the settings of that device:
   * **Postcode (NL)** and **House number**: looked up once via the PDOK Locatieserver.
   * **Or address (any country)**: e.g. `Rue de Rivoli 1, Paris, France`, looked up once via OpenStreetMap Nominatim.
   * **Or latitude, longitude**: wins over both.
   * **Radius (meters)**: 0 to 25000. With a radius the bridge also reads 8 points on a ring at that distance (and 8 at half of it, from 3 km) and takes the heaviest rain per time step, so "rain" means rain anywhere within the radius.
   * **Rain from (mm/h)**: default 0.1.
   * **Source**: Auto, Buienradar or Open-Meteo.
   * **Show**: **Detail** (default): `rainSensor` (rain detected or not), `precipitationSensor` (none, possible, light, moderate, heavy, violent) and the custom capability `silvervalley08660.rainForecast`: rain now (mm/h), rain starts in (minutes, 999 = no rain in the next 2 hours), rain lasts (minutes) and a short summary in English, all usable in routines, e.g. *rain starts in less than 15 minutes → close the windows*. **Rain or dry only**: just `rainSensor`.
   * **Dew point forecast (hours)**: look-ahead for *Dew point max*, 6 to 72, default 48.

The same device also has two values for routines that look ahead, both from the Open-Meteo hourly forecast (in the Netherlands the KNMI model), refreshed every hour for the same location:

* **Dew point now**: outdoor dew point (°C).
* **Dew point max (forecast)**: the highest outdoor dew point in the next 48 hours (setting above).
* **Wind**: wind speed, gusts (km/h, at 10 m) and direction now, every 15 minutes. Example: only open roof windows when the wind speed is 80 km/h or less.

Buienradar's free feed only has humidity as a station measurement, not as a forecast, so this part always comes from Open-Meteo. Example (a dryer in a crawl space, switched at most once a day because it runs on after switching off): at 14:00 switch it **on** when the crawl space humidity is 85% or more, or when *Dew point max* is 11 °C or more (humid air on its way); switch it **off** when the humidity is 80% or less **and** *Dew point max* is 8 °C or less. Air with a dew point of 8 °C is about 80% humid at 11 °C, so ventilation then dries the crawl space by itself.

The setup page has the same fields (except the dew point look-ahead); the last change wins. Data: [Buienradar](https://www.buienradar.nl) (free for personal use), [Open-Meteo](https://open-meteo.com) (CC BY 4.0); addresses: [PDOK](https://www.pdok.nl), [OpenStreetMap Nominatim](https://nominatim.org) (© OpenStreetMap contributors, ODbL).

## Battery history graph (car)

SmartThings draws no graph for battery %. The car's setting **Battery history graph** repeats the battery level as a reading that does get a graph:

* **Humidity (%)**: on the car itself, same number in %. The car then also counts as a humidity sensor (routines, voice assistants).
* **Power (W)**: in an extra part **Battery %**, 1 % = 1 W. Can show up in energy overviews.
* **Off** (default).

The driver switches the device to the matching profile by itself.

## Home Connect (Bosch / Siemens / Neff), local

Dishwashers and ovens (other Home Connect appliances follow) talk to the bridge over their own local connection, no cloud after setup:

1. Download the **Home Connect Profile Downloader** for your system (github.com/bruestel/homeconnect-profile-downloader, Releases), sign in with your Home Connect account, choose **Home Assistant - Home Connect Local** and save the zip(s).
2. Put the zips in a folder `homeconnect` next to `config.json` (Windows: `C:\StoeckieBridge\homeconnect`, Docker: the data volume). They contain the appliance keys: keep them private.
3. Give the appliances a fixed IP in your router. Optional `homeconnect\hosts.json`: `{"<haId>": "192.168.1.50"}` (otherwise the mDNS name is used).
4. Restart the bridge. Needs **Python 3.13** or newer; the installers and the Docker image use **3.14** (3.13 gets no Windows security releases any more).

SmartThings: dishwasher (on/off, running/finished, remaining time, progress, door, remote start allowed, stop) and oven (on/off, running/paused, cavity temperature, set temperature, remaining time, door, stop, pause/resume).

**Programs and remote start** (bridge and driver 2026-11-06h): a **Program** list with the programs your appliance offers and a **Start** button; the oven also gets **Oven temperature** and **Cooking time** (0 = no timer). In routines: *Start program* (choose a program, starts it right away), *Select program*, oven temperature and cooking time. Starting only works while remote start is allowed on the appliance (dishwasher: press Remote Start on the machine after closing the door; oven: switch on remote start in its settings); the device shows that as *Remote control*. Labels are English and Dutch (custom capabilities `silvervalley08660.applianceProgram` and `silvervalley08660.cookSettings`, definitions in `tools/presentation/gen_homeconnect.py`). Programs that only can be selected on the appliance (like pyrolysis) and the dish programs (recipes) are not offered.

Uses the homeconnect-websocket library (github.com/chris-mc1/homeconnect_websocket).

## Samsung washer, local (experimental)

Samsung appliances normally only work through the Samsung cloud. Many of them also accept a local session on the LAN (OCF over CoAP-DTLS); the bridge uses the [smartthings-local](https://pypi.org/project/smartthings-local/) library for that (MIT). Tested models and limits are in that project's compatibility notes; the newest laundry generation refuses this kind of session.

1. Give the washer a fixed IP in your router.
2. Create `samsung\appliances.json` next to `config.json` (Windows: `C:\StoeckieBridge\samsung`, Docker: the data volume): `[{"host": "192.168.1.60", "type": "washer", "name": "Washer"}]`
3. Restart the bridge. Check first with `python stoeckie_bridge.py --samsung-probe 192.168.1.60` (read only): it finds the port, makes one session and reads a few state values.

The bridge makes a client certificate once (in the `samsung` folder; it never leaves that folder). SmartThings gets: on/off, running/paused/stopped, cycle phase (wash, rinse, spin, finish), remaining time, progress, remote control; choose a course (the courses your washer reports), start (needs remote control switched on at the washer), pause, stop. The washer answers slowly under load, so the main state is read every 4 s and the rest less often. A washer that is switched off usually turns its wifi off too: it then shows as offline (and off, without remote control) until it is switched on again, also when the bridge starts while the washer is off. If the connection drops during a wash, the last state stays. After a bridge restart the washer shows offline until it answers, instead of briefly showing off. Note: these washers accept any client on the LAN that uses the published client UUID, so keep them on a separate IoT network if you can.

## VELUX ACTIVE departure mode

The windows and blinds run locally on the hub with the [VELUX ACTIVE (HomeKit local)](https://github.com/Stoeckie/smartthings-velux-active-local) driver. The departure mode (the departure switch, or "away" in the VELUX app) blocks all of them and only exists in the VELUX cloud, so the bridge adds a **lock** device for it:

1. Setup page, section VELUX: e-mail and password of the VELUX ACTIVE app. Locking works right away.
2. Unlocking must be signed with a key from the gateway. Once: fill in the gateway IP, click **Pair gateway**, wait until the gateway light flashes and press the gateway button within 3 minutes. The key stays in `config.json`.

The gateway's rain sensor becomes its own device, **VELUX regensensor** (rain sensor: detected = raining).

## Offline notification

SmartThings has no routine trigger for "device offline". The Stoeckie Bridge device therefore has a **Connection** status (Connected or Offline). Make a routine to get a message:

If Stoeckie Bridge device > Connection is Offline, then send a notification to members.

For a "back online" message as well, duplicate that routine and change the condition to Connection is Connected (e.g. text "... is back online"). Then you know the problem is over without opening the app.

Short drops (a reconnect or one missed poll) are ignored: Connection only turns Offline after **Offline after (minutes)** without a connection (setting, default 5), so a short network outage such as a router update at night gives no notification.

Every drop is remembered. When the connection is back, the device shows **Last drop**: time (UTC), how long it lasted and the reason (for example "session closed" or "timeout"). Older drops are in the device history.

## Security and privacy

* Credentials only live in `config.json` on the bridge pc (in `.gitignore`), never in the code or the driver.
* The setup page only answers on the pc itself (or `STBRIDGE_SETUP_ALLOW`, or the Docker gateway after the setup key), only when opened by IP or `localhost`, and refuses forms posted from other websites.
* From another computer the setup page only works over **https** (port 8443; plain http redirects there and refuses forms). If the certificate cannot be made, the setup page only opens on the pc itself (no plain http fallback for the LAN). The certificate is self-signed and made once on the bridge pc (`setup-cert.pem`, `setup-key.pem`); the browser warns the first time, the log shows its SHA-256 fingerprint to compare. `install-remote.bat` opens port 8443 only for the pc that may use the setup page.
* `install-remote.bat` limits the bridge folder on the target (keys, tokens, certificate) to SYSTEM, administrators and LOCAL SERVICE. The bridge itself runs as **LOCAL SERVICE**, without admin rights: a flaw in the bridge or a package does not hand over the pc. PowerShell remoting on the target only accepts the pc that manages it.
* Python packages are pinned to exact versions with hashes (`requirements.txt`, made from `requirements.in` with `uv pip compile --universal --generate-hashes`); a changed package is refused.
* Keys, tokens and passwords are only entered on the setup page, never in SmartThings: SmartThings settings are stored in the Samsung cloud.

### LAN API and pairing

The LAN API only answers private and local addresses, only when addressed by IP (no DNS rebinding), never a request from a web page (`Origin` header) and only JSON bodies (no cross-site form or text/plain posts).

> **Warning (bridge 2026-11-09d):** the LAN API is now **closed until a hub is paired**. Before, an unpaired bridge served your devices and commands to anyone on the LAN. After updating, your devices stop updating in SmartThings until you pair: update the driver (2026-11-02b or newer, 2026-11-09d logs clearer hints), open the setup page, press **Pair** and **Allow** your hub.

**Pairing** (bridge 2026-11-09d, driver from 2026-11-02b): press **Pair** on the setup page. Within a minute the hub asks and shows up on the setup page with its IP address; check that it is your hub (SmartThings app, hub details, or your router) and press **Allow**. Only that address gets the token (another device on the LAN cannot grab it first), the hub stores it and pairing closes. The setup page shows the paired hub's address. From then on:

* every request from the hub is signed with HMAC-SHA512 over method, path, time and body (`X-Stbridge-Time`, `X-Stbridge-Sig`); unsigned, changed, old (> 5 min) or replayed requests are refused;
* every answer is signed back, so a device that takes over the bridge's address or pretends to be the bridge cannot feed the hub false states; when the bridge moves, the driver only follows a bridge that proves it has the token;
* the token itself never crosses the LAN again. **New token** on the setup page makes a new one; a hub only accepts it with a proof made with the old token. After a bridge reinstall without the old config, switch on **Forget pairing** on the bridge device in SmartThings and press Pair.

Not paired yet: the API is closed (only `/stbridge/info`, `/stbridge/pair` and the iRobot favorites answer), the setup page and the log warn about it.

Samsung TV: the bridge keeps the SHA-256 of each TV's self-signed certificate from the first connection and only sends the TV token to a TV with that certificate. Another certificate (TV reset, or another device at the TV's address): the token is not sent, the bridge forgets it and the TV asks Allow again.

Endpoints: `GET /stbridge/info`, `POST /stbridge/pair`, `GET /stbridge/devices`, `POST /stbridge/devices/<id>/command` with `{"cmd": ..., "value": ...}`, `POST /stbridge/buienradar` (rain radar on/off), and the read-only `GET /stbridge/irobot/<blid>` (iRobot favorites for the Roomba driver, open without signature).

### Network advice

Put the bridge pc in the same network (VLAN) as the hub and the other IoT devices, and allow from your own pcs only the setup page (8443) and, for `update-remote.bat`, WinRM (5985) to the bridge pc. When the bridge gets an address outside the hub's /24, set its IP in the bridge device settings (the automatic search only scans the hub's own /24).

## Unofficial parts

* FGLair uses [ayla-iot-unofficial](https://pypi.org/project/ayla-iot-unofficial/) (the library of the Home Assistant FGLair integration). Each poll only reads the cloud values; the bridge asks the wifi adapter for a fresh room temperature (the library's "refresh" write) at most every 15 minutes, because doing that every minute kept the adapter busy and the FGLair app then could not change the unit. Minimum Heat is a separate `min_heat` property in the cloud; like the FGLair app the bridge shows it as an extra mode (`minHeat`), only on units that report that property as writable (some units only report it; the log line `[fglair] unit ...` says which). Choosing another mode or switching off ends Minimum Heat first. The air conditioner device uses two custom capabilities with English and Dutch labels, `silvervalley08660.airconMode` (mode list including Minimum Heat) and `silvervalley08660.targetTemperature` ("Set temperature"), and a custom device presentation that shows those instead of the standard mode list and "Cooling setpoint" (the standard ones stay on the device for voice assistants and the API). Units that report louver positions (`af_vertical_num_dir`, both test units have 4) also get **Vertical airflow** (`silvervalley08660.airflowDirection`, position 1 to n, written to `af_vertical_direction`); choosing a position switches vertical swing off; position 1 is the highest (most horizontal) louver position. Units with left/right louvers (`af_horizontal_num_dir` above 0) also get **Horizontal airflow** (`silvervalley08660.horizontalAirflow`, written to `af_horizontal_direction`, profiles `...-h`); choosing a position switches horizontal swing off. Position 1 is assumed to be the left end; please report it if yours is the other way round. **Fahrenheit:** the app converts a temperature to the unit of the location, but the range of a custom slider stays as it is, so a °C slider does not work in a °F location (and the other way round). For a °F location the device switches to a °F slider (`silvervalley08660.targetTemperatureF`, profiles `...-f`, 60 to 88 °F). Setting **Temperature unit**: Auto (°C, and °F as soon as SmartThings sends a °F value), °C or °F. Use °F only in a °F location: in a °C location the app shows "out of range". **Changes while off:** on a Fujitsu every command (mode, set temperature, fan, swing, louver position) switches the unit on. With the unit off the driver keeps such a change, shows it, and sends it when you switch the unit on; switching off or using the remote drops it. Setting **Changes while off**: Keep until switched on (default) or Send now (the unit switches on). A command with the value that is already shown (the app sends one when a list is opened and closed without a choice) is ignored. **Events:** the driver only emits a value when it changed (emitting everything every poll made the hub drop events, and the app then kept a stale value). After a driver start every value is sent once with state_change so the cloud catches up. The bridge device setting **Resend all values every (minutes)** (default 0 = off) does the same periodically; use it only if values keep getting stuck, because a forced event also fires routines and notifications again, like a button press, and SmartThings allows about 12 state events a minute per device (an air conditioner has about 15 values). **Diagnostics:** the bridge setup page shows the bridge version and per air conditioner the louver positions and airflow property names the bridge found, the driver build, profile and temperature unit the hub uses, and the last commands from SmartThings. Their definitions are in `tools/presentation`.
* iRobot favorites use [roombapy-prime](https://github.com/johnnyh1975/roombapy-prime); iRobot limits logins per account.
* VELUX departure mode uses the VELUX ACTIVE app login, as documented by [ha-velux-active](https://github.com/Niek/ha-velux-active) (MIT).
* Electrolux and Škoda use the manufacturers' official APIs.

## Development

```
pip install -r bridge/requirements.txt pytest lupa
python -m pytest bridge/tests driver/tests
```

Tests run without real accounts: a fake Ayla cloud (FGLair), a fake Škoda API, a fake iRobot account, a fake VELUX cloud and gateway pairing port, a fake Buienradar, Open-Meteo and Nominatim, and the Edge driver (`driver/src/init.lua`) with a mocked SmartThings runtime against the bridge in `--demo` mode.

## License

Apache 2.0, see LICENSE. Not affiliated with AB Electrolux, AEG, Fujitsu General, Ayla Networks, Škoda Auto, iRobot, VELUX, Netatmo, BSH Hausgeräte (Home Connect, Bosch, Siemens, Neff), Samsung, Buienradar, Open-Meteo, OpenStreetMap or Samsung SmartThings.
