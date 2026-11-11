"""Stoeckie Bridge: brings cloud appliances onto the LAN for a SmartThings Edge driver.

Sources
  * AEG / Electrolux appliances (dryer, washer-dryer) via the official Electrolux Group Developer API
    (developer.electrolux.one): state at start, live updates over SSE, poll as fallback.
  * Fujitsu air conditioners with an FGLair wifi adapter, via the Ayla cloud that the FGLair app uses
    (library ayla-iot-unofficial, the same one Home Assistant uses). Polled every 60 s.
  * Škoda cars via the official MyŠkoda public API (API key from the MyŠkoda app): battery, range, charging,
    plug, doors, climate; start/stop charging and climate. Polled every 10 minutes (the API allows 20 calls/hour).
  * iRobot account (optional): favorite names and rooms for the "iRobot Roomba (local)" Edge driver
    (library roombapy-prime). Only read, twice a day; the robot itself is controlled locally by the hub.
  * Rain radar (optional): rain now and the next 2 hours for an address with a radius, every 5 minutes. Buienradar in
    the Netherlands and Belgium (5 minute steps), Open-Meteo elsewhere (15 minute steps). Addresses: PDOK (NL
    postcode) or OpenStreetMap Nominatim (anywhere). The same device also shows the outdoor dew point now and the
    highest dew point in the next 48 hours (Open-Meteo hourly forecast, refreshed every hour), for routines that
    have to look ahead (e.g. keep a crawl space dryer on when humid air is coming).
  * Home Connect (optional, local): Bosch / Siemens / Neff dishwashers, ovens and more over their own local
    websocket (library homeconnect-websocket, Python 3.13+). Keys and descriptions come once from the Home Connect
    cloud with the Home Connect Profile Downloader; put its zips in the homeconnect folder next to config.json.
  * VELUX ACTIVE (optional): departure mode lock/unlock via the VELUX/Netatmo cloud. Unlock is signed with a key
    the gateway hands out locally once (button press). The windows stay local (HomeKit driver on the hub).
  * Tuya / Smart Life (optional, local): thermostats and heaters over the LAN (library tinytuya). The Tuya IoT
    Platform is only asked once for the device list, local keys and data point mapping; then everything is local.

Credentials live only in config.json next to this script (never in the code).

LAN API (default port 8787, LAN only; after pairing only requests signed with the hub token are accepted)
  GET  /stbridge/info                    -> {"service": "stoeckie-bridge", "id": ..., "version": ...}
  GET  /stbridge/devices                 -> {"devices": [ {id, type, name, online, state{...}} ]}
  POST /stbridge/devices/<id>/command    body {"cmd": "...", "value": ...}
      dryer:   run | pause | resume | stop | refresh
      aircon:  on | off | mode (auto cool dry fan heat) | setpoint (C) | fan (quiet low medium high auto)
               | swing (off vertical horizontal both) | vdir n | hdir n | refresh | settings {poll_seconds}
      car:     on | off (charging) | climate_on | climate_off | limit (percent) | refresh
      velux_lock: lock | unlock | refresh | settings {poll_seconds}
      rain:    refresh (VELUX gateway rain sensor)
      dishwasher / oven (Home Connect): on | off | stop | pause | resume (oven) | refresh
      tuya_thermostat / tuya_heater (Tuya, local): on | off | setpoint (C) | mode (off heat auto eco away ...)
               | child_lock true/false | eco true/false | refresh
      rainradar: refresh | settings {postcode, housenumber, address, location, radius_m, threshold, provider,
               humid_hours} (state: raining, rainRate mm/h, minutesUntilRain, minutesOfRain, intensity, summary,
               dewPointNow, dewPointMax and dewPointMaxAt over the next humid_hours hours, humidSummary; windSpeed, windGust (km/h) and windDirection now)
  POST /stbridge/buienradar               body {"enabled": true} (from the bridge device)
  GET  /stbridge/irobot/<blid>[?refresh=1] -> {"name", "favorites": [{name, id, command, params}], "updated"}
  POST /stbridge/srp                      body {"nonce", "box"} (signed and encrypted, paired hubs only)
      One SRP step of a HomeKit pairing for an Edge driver on the hub (e.g. Somfy (HomeKit local)): the hub
      needs minutes for it, some accessories give up sooner. The bridge only computes; it never talks to the
      accessory and keeps nothing.

Usage
  python stoeckie_bridge.py           run the bridge
  python stoeckie_bridge.py --check   test the AEG and FGLair logins and list appliances
  python stoeckie_bridge.py --demo    fake devices, to test the Edge driver
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import html
import ipaddress
import json
import os
import random
import re
import secrets
import logging
import logging.handlers
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

VERSION = "2026-11-11h"
HERE = Path(__file__).resolve().parent
# Data folder: next to the script on Windows, /data in the Docker image (STBRIDGE_DATA)
DATA = Path(os.environ.get("STBRIDGE_DATA") or HERE)
DATA.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA / "config.json"
LOG_FILE = DATA / "bridge.log"
# Extra networks allowed to open the setup page, e.g. "192.168.2.0/24" (Docker without host networking)
SETUP_ALLOW = [ipaddress.ip_network(n.strip(), strict=False)
               for n in os.environ.get("STBRIDGE_SETUP_ALLOW", "").split(",") if n.strip()]


def in_container() -> bool:
    return (os.environ.get("STBRIDGE_IN_DOCKER") == "1" or os.path.exists("/.dockerenv")
            or os.path.exists("/run/.containerenv"))


def container_gateway(route_file: str = "/proc/net/route", sys_net: str = "/sys/class/net"):
    """In Docker with a bridge network (port mapping, Docker Desktop), a browser on the Docker host reaches the
    container from the network's gateway, not from 127.0.0.1. Return that gateway so the host can open the setup
    page. Only when the default route goes over a virtual (veth) interface: with host networking the default
    gateway is the real LAN router, which must not be allowed. LAN clients keep their own source address and still
    need STBRIDGE_SETUP_ALLOW."""
    try:
        with open(route_file, encoding="ascii") as fh:
            next(fh)
            for line in fh:
                f = line.split()
                if len(f) < 3 or f[1] != "00000000":
                    continue
                iface, gw_hex = f[0], f[2]
                ifindex = Path(sys_net, iface, "ifindex").read_text().strip()
                iflink = Path(sys_net, iface, "iflink").read_text().strip()
                if ifindex == iflink:  # a real interface: host networking
                    return None
                gw = ipaddress.ip_address(int(gw_hex, 16).to_bytes(4, "little"))
                return gw if gw.is_private else None
    except (OSError, ValueError, StopIteration):
        return None
    return None


SETUP_GATEWAY = (container_gateway() if in_container() and os.environ.get("STBRIDGE_SETUP_ALLOW_GATEWAY", "1") != "0"
                 else None)
# The gateway is not always the Docker host alone: on Docker Desktop (Windows, macOS) and with rootless Docker or
# Podman every connection from the LAN to a published port arrives from that same address. So a browser that
# comes from the gateway must first enter the setup key (written to the log and to data/setup-key.txt).
SETUP_KEY_FILE = DATA / "setup-key.txt"


def setup_key() -> str:
    try:
        k = SETUP_KEY_FILE.read_text(encoding="ascii").strip()
        if len(k) >= 24:
            return k
    except OSError:
        pass
    k = secrets.token_urlsafe(18)
    SETUP_KEY_FILE.write_text(k + "\n", encoding="ascii")
    try:
        os.chmod(SETUP_KEY_FILE, 0o600)
    except OSError:
        pass
    return k


def setup_cookie_value() -> str:
    return hmac.new(setup_key().encode(), b"stbridge-setup", hashlib.sha256).hexdigest()


# STBRIDGE_SETUP_LAN_KEY=1: any device on the own network (private address, e.g. a phone whose address changes)
# may open the setup page after entering the setup key once (remembered in a cookie). The key is shown on the
# setup page of an allowed computer, in the log and in data/setup-key.txt.
SETUP_LAN_KEY = os.environ.get("STBRIDGE_SETUP_LAN_KEY", "") == "1"


def setup_source_allowed(ip) -> bool:
    return (ip.is_loopback or ip == SETUP_GATEWAY or any(ip in net for net in SETUP_ALLOW)
            or (SETUP_LAN_KEY and ip.is_private and not ip.is_link_local))


def setup_needs_key(ip) -> bool:
    """The Docker gateway and, with STBRIDGE_SETUP_LAN_KEY, other devices on the own network need the key:
    localhost is this computer, STBRIDGE_SETUP_ALLOW is your own choice."""
    if any(ip in net for net in SETUP_ALLOW):
        return False
    if SETUP_GATEWAY is not None and ip == SETUP_GATEWAY:
        return True
    return SETUP_LAN_KEY and ip.is_private and not ip.is_loopback

log = logging.getLogger("bridge")

# ---------------------------------------------------------------- registry

class Registry:
    """All devices the bridge exposes, keyed by a stable id."""

    def __init__(self) -> None:
        self.devices: dict[str, dict[str, Any]] = {}
        self.handlers: dict[str, Any] = {}  # id -> async fn(cmd, value)
        self.diag: dict[str, dict[str, Any]] = {}       # id -> what the Edge driver reports (build, profile, unit)
        self.commands: dict[str, list[str]] = {}        # id -> last commands from SmartThings (for the setup page)
        self.info: dict[str, str] = {}                  # id -> extra line for the setup page (e.g. louver properties)

    def note_command(self, dev_id: str, cmd: str, value: Any) -> None:
        lst = self.commands.setdefault(dev_id, [])
        lst.append(time.strftime("%H:%M:%S") + " " + cmd + ("" if value is None else " " + str(value)[:40]))
        del lst[:-5]

    def upsert(self, dev_id: str, dev_type: str, name: str, state: dict[str, Any],
               online: bool = True, handler=None) -> None:
        d = self.devices.setdefault(dev_id, {"id": dev_id, "type": dev_type, "name": name})
        d["type"], d["name"], d["online"] = dev_type, name, online
        d.setdefault("state", {}).update(state)
        d["updated"] = int(time.time())
        if handler:
            self.handlers[dev_id] = handler

    def set_online(self, prefix: str, online: bool) -> None:
        for d in self.devices.values():
            if d["id"].startswith(prefix):
                d["online"] = online


REG = Registry()


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(cfg: dict[str, Any]) -> None:
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


# ---------------------------------------------------------------- Electrolux / AEG

DRYER_TYPES = {"TD", "WD"}  # tumble dryer, washer-dryer


def aeg_configured(e: dict[str, Any]) -> bool:
    keys = ("api_key", "access_token", "refresh_token")
    return all(e.get(k) and not str(e.get(k)).startswith("PLAK_HIER") for k in keys)


def _iso_in(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    # whole minutes: the end time then stays the same between polls
    t = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(seconds) + 30)).replace(second=0, microsecond=0)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def dryer_state(reported: dict[str, Any], connection: str | None) -> dict[str, Any]:
    """Flatten an Electrolux TD/WD reported state into what the Edge driver needs."""
    app_state = str(reported.get("applianceState") or "OFF").upper()
    phase = str(reported.get("cyclePhase") or "").upper()
    sel = reported.get("userSelections") or {}
    tte = reported.get("timeToEnd")
    try:
        tte = float(tte) if tte is not None else None
    except (TypeError, ValueError):
        tte = None
    running = app_state in ("RUNNING", "DELAYED_START")
    return {
        "applianceState": app_state,       # OFF IDLE RUNNING PAUSED READY_TO_START DELAYED_START END_OF_CYCLE
        "cyclePhase": phase,
        "program": sel.get("programUID"),
        "timeToEnd": tte,                  # seconds
        "completionTime": _iso_in(tte) if running else None,
        "doorState": reported.get("doorState"),
        "remoteControl": reported.get("remoteControl"),
        "alerts": reported.get("alerts") or [],
        "connection": connection,
    }


class Electrolux:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.client = None
        self.appliances: dict[str, dict[str, Any]] = {}  # id -> {type, name, reported, connection}

    def _on_tokens(self, access: str, refresh: str, api_key: str) -> None:
        e = self.cfg.setdefault("electrolux", {})
        if e.get("refresh_token") != refresh or e.get("access_token") != access:
            e["access_token"], e["refresh_token"], e["api_key"] = access, refresh, api_key
            # merge into config.json as it is now: self.cfg is a copy from the start, saving it would undo
            # everything saved since (hub pairing, VELUX key, rain radar settings, TV tokens)
            cur = load_config()
            ce = cur.setdefault("electrolux", {})
            ce["access_token"], ce["refresh_token"], ce["api_key"] = access, refresh, api_key
            save_config(cur)
            log.info("[aeg] tokens refreshed and saved")

    def _publish(self, app_id: str) -> None:
        a = self.appliances[app_id]
        online = str(a.get("connection") or "").lower() != "disconnected"
        REG.upsert("aeg-" + app_id, "dryer", a["name"],
                   dryer_state(a["reported"], a.get("connection")), online, self._command(app_id))

    def _command(self, app_id: str):
        async def handler(cmd: str, value: Any) -> None:
            if cmd == "refresh":
                await self._load_state(app_id)
                return
            mapping = {"run": "START", "pause": "PAUSE", "resume": "RESUME", "stop": "STOPRESET"}
            if cmd not in mapping:
                raise ValueError(f"unsupported command {cmd}")
            await self.client.send_command(app_id, {"executeCommand": mapping[cmd]})
        return handler

    async def _load_state(self, app_id: str) -> None:
        st = await self.client.get_appliance_state(app_id)
        a = self.appliances[app_id]
        a["reported"] = (st.properties or {}).get("reported", {}) or {}
        a["connection"] = st.connectionState
        self._publish(app_id)

    def _on_event(self, event: dict[str, Any]) -> None:
        app_id = event.get("applianceId")
        if app_id not in self.appliances:
            return
        prop, value = event.get("property"), event.get("value")
        if prop is None:
            return
        a = self.appliances[app_id]
        if prop in ("connectionState", "connectivityState"):
            a["connection"] = value
        if prop != "connectionState":
            target = a["reported"]
            path = prop.split("/")
            for key in path[:-1]:
                target = target.setdefault(key, {})
            target[path[-1]] = value
        log.info("[aeg] %s %s = %s", a["name"], prop, value)
        self._publish(app_id)

    async def run(self) -> None:
        e = self.cfg.get("electrolux") or {}
        if not aeg_configured(e):
            log.info("[aeg] not configured: open http://localhost:%s/setup on this pc", self.cfg.get("port", 8787))
            return
        from electrolux_group_developer_sdk.auth.token_manager import TokenManager
        from electrolux_group_developer_sdk.client.appliance_client import ApplianceClient

        tm = TokenManager(e["access_token"], e["refresh_token"], e["api_key"], on_token_update=self._on_tokens)
        self.client = ApplianceClient(tm, external_user_agent=f"StoeckieBridge/{VERSION}")
        while True:
            try:
                apps = await self.client.get_appliances()
                break
            except Exception as ex:  # noqa: BLE001
                log.error("[aeg] cannot list appliances: %s (retry in 60 s)", ex)
                await asyncio.sleep(60)
        for app in apps:
            t = str(app.applianceType).upper()
            log.info("[aeg] appliance %s '%s' type %s", app.applianceId, app.applianceName, t)
            if t not in DRYER_TYPES:
                continue
            self.appliances[app.applianceId] = {"type": t, "name": app.applianceName or "Droger",
                                                "reported": {}, "connection": None}
            self.client.add_listener(app.applianceId, self._on_event)
            try:
                await self._load_state(app.applianceId)
            except Exception as ex:  # noqa: BLE001
                log.error("[aeg] state of %s failed: %s", app.applianceId, ex)
        if not self.appliances:
            log.warning("[aeg] no dryer found on this account")
            return

        async def resync() -> None:
            for app_id in self.appliances:
                try:
                    await self._load_state(app_id)
                except Exception as ex:  # noqa: BLE001
                    log.warning("[aeg] resync %s failed: %s", app_id, ex)

        async def poll() -> None:  # fallback when the stream is quiet or broken
            while True:
                await asyncio.sleep(int(e.get("poll_seconds", 300)))
                await resync()

        poller = asyncio.create_task(poll())
        try:
            await self.client.start_event_stream(do_on_livestream_opening_list=[resync])
        finally:
            poller.cancel()  # else every restart of this source adds another polling loop


# ---------------------------------------------------------------- Fujitsu FGLair (Ayla cloud)

def fglair_configured(f: dict[str, Any]) -> bool:
    return bool(f.get("username") and f.get("password") and not str(f.get("password")).startswith("PLAK_HIER"))


def _name(e) -> str:
    return getattr(e, "name", str(e)).lower()


SWING_NAMES = {"OFF": "off", "SWING_VERTICAL": "vertical", "SWING_HORIZONTAL": "horizontal", "SWING_BOTH": "both"}


def aircon_state(dev) -> dict[str, Any]:
    """Flatten a FujitsuHVAC object into what the Edge driver needs."""
    def safe(fn, default=None):
        try:
            return fn()
        except Exception:  # noqa: BLE001  (missing property on this model)
            return default
    mode = safe(lambda: _name(dev.op_mode), "off")
    lo, hi = safe(lambda: dev.temperature_range, (16.0, 30.0)) if mode not in ("off", "on") else (16.0, 30.0)
    has_min = min_heat_writable(dev)
    min_on = has_min and bool(safe(lambda: dev.get_property_value(MIN_HEAT_PROP), 0))
    modes = [m for m in (_name(x) for x in safe(lambda: dev.supported_op_modes, [])) if m not in ("off", "on")]
    if has_min:
        modes.append("minheat")  # the FGLair app shows Minimum Heat as an operation mode
    if min_on:
        mode = "minheat"
    vnum = vdir_count(dev)
    vdir = safe(lambda: int(dev.get_property_value(VDIR_PROP)), None) if vnum else None
    hnum = hdir_count(dev)
    hdir = safe(lambda: int(dev.get_property_value(HDIR_PROP)), None) if hnum else None
    return {
        "power": "off" if mode == "off" else "on",
        "mode": mode,                                   # off on auto cool dry fan heat minheat
        "setpoint": safe(lambda: dev.set_temp),
        "temperature": safe(lambda: dev.sensed_temp),   # None on models without a room sensor
        "outdoorTemperature": safe(lambda: dev.outdoor_temperature),
        "fan": safe(lambda: _name(dev.fan_speed)),
        "swing": safe(lambda: SWING_NAMES.get(dev.swing_mode.name, "off"), "off"),
        "supportedModes": modes,
        "supportedFans": [_name(x) for x in safe(lambda: dev.supported_fan_speeds, [])],
        "supportedSwings": [SWING_NAMES.get(x.name, "off") for x in safe(lambda: dev.supported_swing_modes, [])],
        "setpointRange": [lo, hi],
        "model": getattr(dev, "oem_model_number", None),
        # Minimum Heat (frost protection, about 10 C): a separate min_heat property in the cloud, shown as
        # mode "minheat" above. None when the unit has no such property.
        "minHeat": min_on if has_min else None,
        # louver position 1..vDirCount when swing is off (None: unit has no positions)
        "vDir": vdir if vdir and 1 <= vdir <= vnum else None,
        "vDirCount": vnum or None,
        # same for the horizontal louvers (left/right)
        "hDir": hdir if hdir and 1 <= hdir <= hnum else None,
        "hDirCount": hnum or None,
    }


# Property name used by the FGLair cloud for Minimum Heat. Not in ayla-iot-unofficial (it only has the
# capability bit OP_MIN_HEAT); the bridge logs every unit's property names once so this can be checked.
MIN_HEAT_PROP = "min_heat"
# Vertical airflow direction (louver position when swing is off): position 1..count, written as raw properties
# because the library has no setter for it.
VDIR_PROP, VNUM_PROP, VSWING_PROP = "af_vertical_direction", "af_vertical_num_dir", "af_vertical_swing"
# Horizontal airflow direction (left/right louvers), same scheme; units without horizontal louvers report 0
HDIR_PROP, HNUM_PROP, HSWING_PROP = "af_horizontal_direction", "af_horizontal_num_dir", "af_horizontal_swing"
VDIR_MAX = 8


def min_heat_writable(dev) -> bool:
    """True when the unit has a Minimum Heat property that can be switched (some units only report it)."""
    try:
        prop = dev.properties_full.get(MIN_HEAT_PROP)
    except Exception:  # noqa: BLE001
        return False
    return bool(prop) and not prop.get("read_only")


def vdir_count(dev, dir_prop: str = VDIR_PROP, num_prop: str = VNUM_PROP) -> int:
    """Number of louver positions the unit reports, 0 if it has none (vertical; horizontal with the H props)."""
    try:
        props = dev.properties_full
        if dir_prop not in props or num_prop not in props:
            return 0
        return max(0, min(VDIR_MAX, int(dev.get_property_value(num_prop) or 0)))
    except Exception:  # noqa: BLE001
        return 0


def hdir_count(dev) -> int:
    return vdir_count(dev, HDIR_PROP, HNUM_PROP)


def louver_info(dev) -> str:
    """Setup page: louver positions the bridge found, and the airflow property names the unit has."""
    try:
        names = sorted(str(k) for k in dev.properties_full.keys() if str(k).startswith("af_"))
    except Exception:  # noqa: BLE001
        names = []
    return (f"vertical positions {vdir_count(dev) or 'none'}, horizontal positions {hdir_count(dev) or 'none'}; "
            f"airflow properties: {', '.join(names) or 'none'}")


def describe_unit(dev) -> str:
    """One log line per unit: property names and the min heat capability bit (no values)."""
    try:
        names = sorted(str(k) for k in dev.properties_full.keys())
    except Exception:  # noqa: BLE001
        names = []
    try:
        from ayla_iot_unofficial.fujitsu_consts import Capability
        cap = "yes" if dev.has_capability(Capability.OP_MIN_HEAT) else "no"
    except Exception:  # noqa: BLE001
        cap = "?"
    try:
        mh = dev.properties_full.get(MIN_HEAT_PROP)
        mh_txt = "none" if not mh else ("read only" if mh.get("read_only") else "writable")
    except Exception:  # noqa: BLE001
        mh_txt = "?"
    return f"min heat capability {cap}, min_heat property {mh_txt}, properties: {', '.join(names)}"


class FGLair:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.f = cfg.get("fglair") or {}
        self.api = None
        self.devices: dict[str, Any] = {}  # dsn -> FujitsuHVAC
        self.skipped: set[str] = set()
        self.lock = asyncio.Lock()
        self.last_sense: dict[str, float] = {}  # dsn -> time of the last "refresh" write to the adapter
        self.described: set[str] = set()         # units whose property names are logged already

    # The library writes "refresh" = 1 to the adapter on every update so it reads the room temperature again
    # from the indoor unit. Every minute that keeps the adapter busy (the FGLair app then shows "information
    # acquisition not completed"), so the bridge only does that every REFRESH_EVERY seconds and otherwise just
    # reads the cloud values.
    REFRESH_EVERY = 900

    def _new_api(self, session):
        from ayla_iot_unofficial import new_ayla_api
        from ayla_iot_unofficial.fujitsu_consts import FGLAIR_APP_CREDENTIALS
        region = str(self.f.get("region") or "eu").lower()
        app_id, app_secret = FGLAIR_APP_CREDENTIALS.get(region, FGLAIR_APP_CREDENTIALS["default"])
        return new_ayla_api(self.f["username"], self.f["password"], app_id, app_secret,
                            websession=session, europe=(region == "eu"))

    async def _auth(self) -> None:
        from ayla_iot_unofficial.exc import AylaAuthExpiringError
        try:
            self.api.check_auth()
            return
        except AylaAuthExpiringError:
            try:
                await self.api.async_refresh_auth()
                return
            except Exception as ex:  # noqa: BLE001
                log.info("[fglair] token refresh failed (%s), signing in again", ex)
        except Exception:  # noqa: BLE001  (not signed in or expired)
            pass
        await self.api.async_sign_in()

    def _publish(self, dsn: str) -> None:
        dev = self.devices[dsn]
        try:
            name = dev.device_name or dev.name
        except Exception:  # noqa: BLE001
            name = getattr(dev, "name", None) or "Air conditioner"
        REG.upsert("fglair-" + dsn, "aircon", name, aircon_state(dev), dev.is_online(), self._command(dsn))

    async def _refresh(self, dsn: str) -> None:
        dev = self.devices[dsn]
        if dev.is_online():
            from ayla_iot_unofficial.fujitsu_hvac import FujitsuHVAC
            await super(FujitsuHVAC, dev).async_update()  # read only, no write to the adapter
            if dsn not in self.described:
                self.described.add(dsn)
                log.info("[fglair] unit %s: %s", dsn, describe_unit(dev))
            REG.info["fglair-" + dsn] = louver_info(dev)
            if time.time() - self.last_sense.get(dsn, 0) >= self.REFRESH_EVERY:
                self.last_sense[dsn] = time.time()
                await dev.refresh_sensed_temp()
        self._publish(dsn)

    async def _sync(self) -> None:
        """List devices (online state), then read every unit."""
        from ayla_iot_unofficial.fujitsu_hvac import FujitsuHVAC
        async with self.lock:
            await self._auth()
            listed = await self.api.async_get_devices()
            for d in listed:
                dsn = d.serial_number
                if not isinstance(d, FujitsuHVAC):
                    if dsn not in self.skipped:
                        self.skipped.add(dsn)
                        log.warning("[fglair] device %s '%s' model %s not supported by ayla-iot-unofficial",
                                    dsn, d.name, getattr(d, "oem_model_number", "?"))
                    continue
                if dsn in self.devices:
                    self.devices[dsn]._connection_status = d._connection_status
                else:
                    log.info("[fglair] unit %s '%s' model %s", dsn, d.name, d.oem_model_number)
                    self.devices[dsn] = d
                try:
                    await self._refresh(dsn)
                except Exception as ex:  # noqa: BLE001
                    log.warning("[fglair] update %s failed: %s", dsn, ex)
                    REG.set_online("fglair-" + dsn, False)

    @staticmethod
    async def _min_heat(dev, on: bool, strict: bool = False) -> None:
        """Switch Minimum Heat; when switching it off, only write if it is on (other units: no-op)."""
        if not min_heat_writable(dev):
            if on or strict:
                raise ValueError("this unit has no Minimum Heat that can be switched from the cloud")
            return
        if not on and not dev.get_property_value(MIN_HEAT_PROP):
            return
        await dev.async_set_property_value(MIN_HEAT_PROP, 1 if on else 0)

    def _command(self, dsn: str):
        async def handler(cmd: str, value: Any) -> None:
            from ayla_iot_unofficial.fujitsu_consts import FanSpeed, OpMode
            if cmd == "settings":  # from the SmartThings device settings (all air conditioners share it)
                sec = poll_seconds((value or {}).get("poll_seconds"), int(self.f.get("poll_seconds", 60)))
                self.f["poll_seconds"] = sec
                save_poll_seconds("fglair", sec)
                log.info("[fglair] settings from SmartThings: every %s s", sec)
                return
            dev = self.devices[dsn]
            async with self.lock:
                await self._auth()
                if cmd == "refresh":
                    pass
                elif cmd == "on":
                    await dev.async_set_op_mode(OpMode.ON)
                elif cmd == "off":
                    await self._min_heat(dev, False)
                    await dev.async_set_op_mode(OpMode.OFF)
                elif cmd == "mode" and str(value).lower() == "minheat":
                    await self._min_heat(dev, True)
                elif cmd == "mode":
                    await self._min_heat(dev, False)  # another mode ends Minimum Heat
                    await dev.async_set_op_mode(OpMode[str(value).upper()])
                elif cmd == "setpoint":
                    await dev.async_set_set_temp(float(value))
                elif cmd == "fan":
                    await dev.async_set_fan_speed(FanSpeed[str(value).upper()])
                elif cmd == "vdir":
                    n, count = int(value), vdir_count(dev)
                    if not 1 <= n <= count:
                        raise ValueError(f"airflow direction must be 1..{count}")
                    if dev.get_property_value(VSWING_PROP):  # a fixed position ends vertical swing
                        await dev.async_set_property_value(VSWING_PROP, 0)
                    await dev.async_set_property_value(VDIR_PROP, n)
                elif cmd == "hdir":
                    n, count = int(value), hdir_count(dev)
                    if not 1 <= n <= count:
                        raise ValueError(f"horizontal airflow direction must be 1..{count}")
                    if dev.get_property_value(HSWING_PROP):  # a fixed position ends horizontal swing
                        await dev.async_set_property_value(HSWING_PROP, 0)
                    await dev.async_set_property_value(HDIR_PROP, n)
                elif cmd == "minheat":  # older drivers (2026-11-06b) used a separate switch
                    await self._min_heat(dev, str(value).lower() in ("on", "1", "true"), strict=True)
                elif cmd == "swing":
                    # set each axis the unit has (the library also touches axes the unit lacks)
                    from ayla_iot_unofficial.fujitsu_consts import Capability
                    want = str(value).lower()
                    if want not in ("off", "vertical", "horizontal", "both"):
                        raise ValueError(f"unsupported swing {value}")
                    if dev.has_capability(Capability.SWING_VERTICAL):
                        await dev.async_set_vertical_swing(want in ("vertical", "both"))
                    if dev.has_capability(Capability.SWING_HORIZONTAL):
                        await dev.async_set_horizontal_swing(want in ("horizontal", "both"))
                else:
                    raise ValueError(f"unsupported command {cmd}")
                log.info("[fglair] %s %s %s", dsn, cmd, "" if value is None else value)
                if cmd != "refresh":
                    await asyncio.sleep(2)  # the cloud needs a moment before the unit reports back
                await self._refresh(dsn)
        return handler

    async def run(self) -> None:
        if not fglair_configured(self.f):
            log.info("[fglair] not configured")
            return
        import aiohttp
        async with aiohttp.ClientSession() as session:
            self.api = self._new_api(session)
            while True:
                try:
                    await self.api.async_sign_in()
                    break
                except Exception as ex:  # noqa: BLE001
                    log.error("[fglair] login failed: %s (retry in 5 min)", ex)
                    await asyncio.sleep(300)
            log.info("[fglair] logged in")
            while True:
                try:
                    await self._sync()
                except Exception as ex:  # noqa: BLE001
                    log.warning("[fglair] poll failed: %s", ex)
                    REG.set_online("fglair-", False)
                await asyncio.sleep(int(self.f.get("poll_seconds", 60)))


def poll_seconds(value: Any, default: int = 60) -> int:
    """Update interval from SmartThings or the setup page: 30..600 seconds."""
    try:
        return max(30, min(600, int(float(str(value).replace(",", ".")))))
    except (TypeError, ValueError):
        return default


def save_poll_seconds(section: str, seconds: int) -> None:
    cfg = load_config()
    if cfg.setdefault(section, {}).get("poll_seconds") != seconds:
        cfg[section]["poll_seconds"] = seconds
        save_config(cfg)


# ---------------------------------------------------------------- Škoda (official MyŠkoda public API)

SKODA_API = "https://public.api.connect.skoda-auto.cz/api/v1"
SKODA_PARTS = "info,status,fuelStatus,odometer,airConditioning,charging"


def skoda_configured(k: dict[str, Any]) -> bool:
    return bool(k.get("api_key") and k.get("vin") and not str(k.get("api_key")).startswith("PLAK_HIER"))


def _g(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def car_state(v: dict[str, Any]) -> dict[str, Any]:
    """Flatten a MyŠkoda vehicle into what the Edge driver needs. Missing parts stay None."""
    st = _g(v, "charging", "status") or {}
    bat = st.get("battery") or {}
    fuel = v.get("fuelStatus") or {}
    elec = None
    for eng in (fuel.get("primaryEngineRange"), fuel.get("secondaryEngineRange")):
        if isinstance(eng, dict) and str(eng.get("engineType", "")).lower() == "electric":
            elec = eng
    soc = bat.get("stateOfChargeInPercent")
    if soc is None and elec:
        soc = elec.get("currentSoCInPercent")
    rng = bat.get("remainingCruisingRangeInMeters")
    rng = round(rng / 1000) if isinstance(rng, (int, float)) else (elec or {}).get("remainingRangeInKm")
    overall = _g(v, "status", "overall") or {}
    ac = v.get("airConditioning") or {}
    return {
        "battery": soc,
        "electricRange": rng,
        "totalRange": fuel.get("totalRangeInKm"),
        "fuelLevel": next((e.get("currentFuelLevelInPercent") for e in (fuel.get("primaryEngineRange"),
                          fuel.get("secondaryEngineRange")) if isinstance(e, dict)
                          and e.get("currentFuelLevelInPercent") is not None), None),
        "charging": str(st.get("state") or "").upper() or None,          # e.g. CHARGING, READY_FOR_CHARGING
        "chargePowerKw": st.get("chargePowerInKw"),
        "minutesToFull": st.get("remainingTimeToFullyChargedInMinutes"),
        "plug": str(st.get("plugConnectionState") or "").upper() or None,  # CONNECTED / DISCONNECTED
        "targetSoc": _g(v, "charging", "settings", "targetStateOfChargeInPercent"),
        "atHome": _g(v, "charging", "isVehicleInSavedLocation"),
        "odometer": _g(v, "odometer", "mileageInKm"),
        "locked": str(overall.get("locked") or overall.get("doorsLocked") or "").upper() or None,
        "doors": str(overall.get("doors") or "").upper() or None,
        "windows": str(overall.get("windows") or "").upper() or None,
        "climate": str(ac.get("state") or "").upper() or None,             # ON / OFF / HEATING / COOLING ...
        "climateTarget": _g(ac, "targetTemperature", "value"),
        "updated": _g(v, "charging", "carCapturedTimestamp") or _g(v, "status", "carCapturedTimestamp"),
    }


def skoda_settings(poll: Any, temp: Any, current: dict[str, Any]) -> dict[str, Any]:
    """Validated poll interval (5..60 min) and climate temperature (16..29 C)."""
    out = {"poll_minutes": current.get("poll_minutes", 10), "climate_temp": current.get("climate_temp", 21.0)}
    try:
        if poll not in (None, ""):
            out["poll_minutes"] = max(5, min(60, int(float(str(poll).replace(",", ".")))))
    except ValueError:
        pass
    try:
        if temp not in (None, ""):
            out["climate_temp"] = max(16.0, min(29.0, round(float(str(temp).replace(",", ".")) * 2) / 2))
    except ValueError:
        pass
    return out


class Skoda:
    POLL = 600  # 6 calls per hour by default, leaves room for commands within the 20/hour limit

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.k = cfg.get("skoda") or {}
        self.vin = str(self.k.get("vin") or "").strip().upper()
        try:  # optional "poll_minutes" in config.json, at least 5
            self.POLL = max(300, int(float(self.k.get("poll_minutes") or 10) * 60))
        except (TypeError, ValueError):
            pass
        self.session = None
        self.wake = asyncio.Event()
        self.pause_until = 0.0
        self.last: dict[str, Any] = {}
        self.expect: tuple | None = None  # (command, value, retries left) after a command

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        if time.time() < self.pause_until:
            raise RuntimeError("rate limited, waiting")
        headers = {"X-API-Key": self.k["api_key"], "Accept": "application/json"}
        async with self.session.request(method, SKODA_API + path, headers=headers, json=body) as r:
            exp = r.headers.get("X-API-Key-Expires-At")
            if exp:
                self.last["keyExpires"] = exp
            if r.status == 429:
                wait = int(r.headers.get("Retry-After") or 600)
                self.pause_until = time.time() + wait
                raise RuntimeError(f"rate limited for {wait} s")
            if r.status in (401, 403):
                raise PermissionError("API key expired or not valid for this car (make a new one in the MyŠkoda app)")
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {text[:200]}")
            return json.loads(text) if text else {}

    async def _refresh(self) -> None:
        data = await self._call("GET", f"/vehicles/{self.vin}?include={SKODA_PARTS}")
        v = data.get("vehicle") or {}
        state = car_state(v)
        state["keyExpires"] = self.last.get("keyExpires")
        state["pollMinutes"] = self.POLL // 60
        state["climateTemp"] = float(self.k.get("climate_temp") or 21)
        state["errors"] = [e.get("type") for e in data.get("errors") or [] if isinstance(e, dict)]
        name = v.get("name") or v.get("licensePlate") or "Škoda"
        REG.upsert("skoda-" + self.vin, "car", name, state, True, self._command())
        log.info("[skoda] %s: battery %s%%, %s km, charging %s, plug %s", name, state["battery"],
                 state["electricRange"], state["charging"], state["plug"])
        if self.expect:
            cmd, value, left = self.expect
            done = {"on": state["charging"] == "CHARGING", "off": state["charging"] != "CHARGING",
                    "climate_on": state["climate"] not in (None, "OFF"), "climate_off": state["climate"] == "OFF",
                    "limit": state["targetSoc"] == value}.get(cmd, True)
            self.expect = None
            if not done and left > 0:
                self.expect = (cmd, value, left - 1)
                asyncio.get_running_loop().call_later(60, self.wake.set)

    def _command(self):
        async def handler(cmd: str, value: Any) -> None:
            paths = {"on": "/charging/start", "off": "/charging/stop",
                     "climate_off": "/air-conditioning/stop"}
            if cmd == "refresh":
                self.wake.set()
                return
            if cmd == "settings":  # from the SmartThings device settings
                v = value if isinstance(value, dict) else {}
                cfg = load_config()
                k = cfg.setdefault("skoda", {})
                new = skoda_settings(v.get("poll_minutes"), v.get("climate_temp"), k)
                if new != {"poll_minutes": k.get("poll_minutes", 10), "climate_temp": k.get("climate_temp", 21.0)}:
                    k.update(new)
                    save_config(cfg)
                self.k.update(new)
                self.POLL = int(new["poll_minutes"]) * 60
                log.info("[skoda] settings from SmartThings: every %s min, climate %s C", new["poll_minutes"],
                         new["climate_temp"])
                return
            if cmd == "climate_on":
                temp = float(self.k.get("climate_temp") or 21)
                await self._call("POST", f"/vehicles/{self.vin}/air-conditioning/start",
                                 {"targetTemperature": {"value": temp, "unit": "CELSIUS"}})
            elif cmd == "limit":
                await self._call("PUT", f"/vehicles/{self.vin}/charging/limit",
                                 {"targetStateOfChargeInPercent": int(value)})
            elif cmd in paths:
                await self._call("POST", f"/vehicles/{self.vin}{paths[cmd]}")
            else:
                raise ValueError(f"unsupported command {cmd}")
            log.info("[skoda] command %s %s", cmd, "" if value is None else value)
            # the car needs a moment to report back: look after 30 s, and once more 60 s later if the
            # change is not visible yet (each look costs one of the 20 requests per hour)
            self.expect = (cmd, value, 1)
            asyncio.get_running_loop().call_later(30, self.wake.set)
        return handler

    async def run(self) -> None:
        if not skoda_configured(self.k):
            log.info("[skoda] not configured")
            return
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            self.session = session
            while True:
                try:
                    await self._refresh()
                except PermissionError as ex:
                    log.error("[skoda] %s", ex)
                    REG.set_online("skoda-", False)
                    return
                except Exception as ex:  # noqa: BLE001
                    log.warning("[skoda] update failed: %s", ex)
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=self.POLL)
                except asyncio.TimeoutError:
                    pass


# ---------------------------------------------------------------- iRobot account (favorites for the Roomba driver)

IROBOT: dict[str, Any] = {"robots": {}, "updated": 0, "error": None, "refresh": None}


def irobot_configured(r: dict[str, Any]) -> bool:
    return bool(r.get("username") and r.get("password") and not str(r.get("password")).startswith("PLAK_HIER"))


def favorites_for(raw: list[dict[str, Any]], blid: str) -> list[dict[str, Any]]:
    """Turn the account's favorites into local start commands for one robot."""
    out = []
    for f in raw or []:
        if not isinstance(f, dict) or f.get("deleted") or f.get("hidden"):
            continue
        cmds = [c for c in (f.get("commanddefs") or []) if isinstance(c, dict)]
        mine = [c for c in cmds if str(c.get("robot_id") or c.get("robotId") or blid) == blid]
        if not mine:
            continue
        c = mine[0]
        # "id" names the command inside the favorite; the robot does not need it
        params = {k: v for k, v in c.items()
                  if k not in ("id", "robot_id", "robotId", "command", "time", "initiator")}
        out.append({"name": f.get("name") or "Favorite", "id": f.get("favorite_id"),
                    "command": c.get("command") or "start", "params": params, "order": f.get("display_order")})
    out.sort(key=lambda x: (x["order"] is None, x["order"] or 0))
    return out


class IRobotCloud:
    REFRESH = 12 * 3600
    MIN_GAP = 600

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.r = cfg.get("irobot") or {}
        self.account = None
        self.wake = asyncio.Event()

    async def _fetch(self) -> None:
        raw = await self.account.classic_rest().get_favorites()
        robots = {}
        for blid, entry in (self.account.robots or {}).items():
            robots[blid] = {"name": getattr(entry, "name", None), "favorites": favorites_for(raw, blid)}
        IROBOT.update(robots=robots, updated=int(time.time()), error=None)
        log.info("[irobot] %d favorite(s) for %d robot(s)", sum(len(r["favorites"]) for r in robots.values()),
                 len(robots))

    async def run(self) -> None:
        if not irobot_configured(self.r):
            log.info("[irobot] not configured (optional: favorite names for the Roomba driver)")
            return
        import aiohttp
        from roombapy_prime import CloudAccount
        from roombapy_prime.auth import AuthCredentialsError, AuthRateLimitedError
        IROBOT["refresh"] = self.wake
        async with aiohttp.ClientSession() as session:
            last = 0.0
            while True:
                try:
                    if self.account is None:
                        self.account = await CloudAccount.login(session, self.r["username"], self.r["password"],
                                                                str(self.r.get("country") or "NL").upper())
                        log.info("[irobot] logged in, %d robot(s) on the account", len(self.account.robots or {}))
                    last = time.time()
                    await self._fetch()
                except AuthCredentialsError as ex:
                    IROBOT["error"] = "login refused (check e-mail and password)"
                    log.error("[irobot] login refused: %s. Not retrying; fix it on the setup page", ex)
                    return
                except AuthRateLimitedError as ex:
                    IROBOT["error"] = "iRobot asks to wait (too many logins)"
                    log.warning("[irobot] rate limited: %s (retry in 30 min)", ex)
                    self.account = None
                    await asyncio.sleep(1800)
                    continue
                except Exception as ex:  # noqa: BLE001
                    IROBOT["error"] = str(ex)[:200]
                    log.warning("[irobot] update failed: %s", ex)
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=self.REFRESH)
                except asyncio.TimeoutError:
                    pass
                gap = self.MIN_GAP - (time.time() - last)  # refresh from the hub: at most every 10 minutes
                if gap > 0:
                    await asyncio.sleep(gap)


# ---------------------------------------------------------------- Amazon Echo (speak, announce, volume, voice commands)
# Amazon has no official API for this. Like Alexa Media Player (Home Assistant) the bridge uses the Alexa web API
# through alexapy (Apache-2.0, gitlab.com/keatontaylor/alexapy). The login runs once through the setup page: the
# bridge relays the Amazon login page (proxy, so captchas and 2FA work in your own browser) and keeps only the
# OAuth refresh token in config.json. It can stop working when Amazon changes its website.

ECHO_DOMAINS = ("amazon.de", "amazon.com", "amazon.co.uk", "amazon.fr", "amazon.it", "amazon.es", "amazon.nl")
ECHO_LOGIN: dict[str, Any] = {"login": None, "proxy": None, "url": None, "error": None, "done": False}
ECHO: dict[str, Any] = {"devices": 0, "error": None, "updated": 0}
ECHO_DIR = DATA / "echo"


def echo_configured(a: dict[str, Any]) -> bool:
    return bool(a.get("email") and (a.get("oauth") or {}).get("refresh_token"))


def echo_wanted(d: dict[str, Any]) -> bool:
    """Speakers and screens that can play audio; not the Alexa app, Fire TV remotes or third party devices."""
    caps = set(d.get("capabilities") or [])
    fam = str(d.get("deviceFamily") or "")
    # speaker groups from the Alexa app (WHA) also get a device: speak or play music on the whole group
    return "AUDIO_PLAYER" in caps and fam not in ("VOX", "THIRD_PARTY_AVS_MEDIA_DISPLAY", "TABLET")


def echo_volume(value: Any) -> int:
    v = int(round(float(value)))
    return max(0, min(100, v))


def echo_text(value: Any, limit: int = 250) -> str:
    t = " ".join(str(value or "").split())
    if not t:
        raise ValueError("empty text")
    return t[:limit]


def echo_volumes_from(js: Any) -> dict[str, int]:
    """Speaker volume per serial from Amazon's allDeviceVolumes answer."""
    out: dict[str, int] = {}
    for v in (js or {}).get("volumes") or [] if isinstance(js, dict) else []:
        try:
            if v.get("dsn") is not None and v.get("speakerVolume") is not None:
                out[str(v["dsn"])] = echo_volume(v["speakerVolume"])
        except (TypeError, ValueError, AttributeError):
            continue
    return out


ECHO_TEXT_CMDS = ("speak", "announce", "command", "routine", "music", "push", "skill", "bluetooth")
# Alexa sound library ids for the SmartThings list (any other amzn_sfx_ id also works)
ECHO_SOUNDS = {
    "doorbell": "amzn_sfx_doorbell_chime_01", "doorbell2": "amzn_sfx_doorbell_01",
    "bell": "amzn_sfx_church_bell_1x_02", "alarm": "amzn_sfx_scifi_alarm_01", "buzzer": "amzn_sfx_buzz_electronic_01",
    "bugle": "amzn_sfx_trumpet_bugle_04", "applause": "amzn_sfx_crowd_applause_01", "cat": "amzn_sfx_cat_meow_1x_01",
    "dog": "amzn_sfx_dog_med_bark_1x_02", "ding": "amzn_sfx_scifi_sheilds_up_01",
}
ECHO_PROVIDERS = {"spotify": "SPOTIFY", "amazon": "AMAZON_MUSIC", "tunein": "TUNEIN", "apple": "APPLE_MUSIC",
                  "deezer": "DEEZER", "radio": "TUNEIN"}


def echo_music(text: Any) -> tuple[str | None, str]:
    """'spotify: Coldplay' -> ("SPOTIFY", "Coldplay"); without a known prefix Alexa picks the default service."""
    t = echo_text(text)
    head, sep, rest = t.partition(":")
    if sep and head.strip().lower() in ECHO_PROVIDERS and rest.strip():
        return ECHO_PROVIDERS[head.strip().lower()], rest.strip()
    return None, t


def echo_dnd_from(js: Any) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for x in (js or {}).get("doNotDisturbDeviceStatusList") or [] if isinstance(js, dict) else []:
        if isinstance(x, dict) and x.get("deviceSerialNumber"):
            out[str(x["deviceSerialNumber"])] = bool(x.get("enabled"))
    return out


def echo_bluetooth_from(js: Any) -> dict[str, dict[str, Any]]:
    """Per serial: the connected device name ('' = none) and the paired devices (name -> address)."""
    out: dict[str, dict[str, Any]] = {}
    for x in (js or {}).get("bluetoothStates") or [] if isinstance(js, dict) else []:
        if not isinstance(x, dict) or not x.get("deviceSerialNumber"):
            continue
        paired = [p for p in x.get("pairedDeviceList") or [] if isinstance(p, dict)]
        con = next((p.get("friendlyName") or p.get("address") or "" for p in paired if p.get("connected")), "")
        out[str(x["deviceSerialNumber"])] = {"connected": str(con),
                                             "paired": {str(p.get("friendlyName") or ""): p.get("address")
                                                        for p in paired if p.get("address")}}
    return out


def echo_alarms_from(items: Any, now: float, tz=None) -> dict[str, dict[str, str]]:
    """Next active alarm and running timer per serial, as local 'ddd HH:MM' / 'HH:MM'."""
    import datetime as dt
    best: dict[str, dict[str, float]] = {}
    for n in items or []:
        if not isinstance(n, dict) or n.get("status") != "ON" or not n.get("deviceSerialNumber"):
            continue
        serial, kind = str(n["deviceSerialNumber"]), str(n.get("type") or "")
        when = None
        if kind == "Timer" and n.get("remainingTime") is not None:
            try:
                when = now + float(n["remainingTime"]) / 1000
            except (TypeError, ValueError):
                when = None
            key = "timer"
        elif kind in ("Alarm", "MusicAlarm"):
            key = "nextAlarm"
            try:
                if n.get("alarmTime"):
                    when = float(n["alarmTime"]) / 1000
                if (when is None or when < now) and n.get("originalDate") and n.get("originalTime"):
                    d = dt.datetime.fromisoformat(f"{n['originalDate']}T{str(n['originalTime'])[:8]}")
                    when = d.replace(tzinfo=tz).timestamp() if tz else d.timestamp()
            except (TypeError, ValueError):
                when = None
        else:
            continue
        if when is None or when < now:
            continue
        cur = best.setdefault(serial, {})
        if key not in cur or when < cur[key]:
            cur[key] = when
    out: dict[str, dict[str, str]] = {}
    for serial, v in best.items():
        o = {}
        for key, when in v.items():
            t = dt.datetime.fromtimestamp(when, tz) if tz else dt.datetime.fromtimestamp(when)
            o[key] = t.strftime("%H:%M") if key == "timer" or when - now < 20 * 3600 else t.strftime("%a %H:%M")
        out[serial] = o
    return out


def echo_player_from(js: Any) -> dict[str, str]:
    p = (js or {}).get("playerInfo") or {} if isinstance(js, dict) else {}
    state = str(p.get("state") or "").upper()
    playback = {"PLAYING": "playing", "PAUSED": "paused"}.get(state, "stopped")
    info = p.get("infoText") or {}
    title = " - ".join(x for x in (info.get("title"), info.get("subText1")) if x) if isinstance(info, dict) else ""
    return {"playback": playback, "nowPlaying": (title or "")[:250] if playback != "stopped" else ""}


def echo_push_parse(msg: Any) -> list[tuple[str, dict[str, Any]]]:
    """Alexa push (HTTP2 directive) -> [(command, payload)]."""
    out = []
    try:
        ups = msg["directive"]["payload"]["renderingUpdates"]
    except (KeyError, TypeError):
        ups = [msg] if isinstance(msg, dict) and msg.get("command") else []
    for u in ups or []:
        meta = u.get("resourceMetadata") if isinstance(u, dict) and "resourceMetadata" in u else u
        try:
            meta = json.loads(meta) if isinstance(meta, str) else meta
            pl = meta.get("payload")
            pl = json.loads(pl) if isinstance(pl, str) else (pl or {})
            if meta.get("command"):
                out.append((str(meta["command"]), pl if isinstance(pl, dict) else {}))
        except (ValueError, AttributeError, TypeError):
            continue
    return out


def echo_push_serial(pl: dict[str, Any]) -> str | None:
    d = pl.get("dopplerId") if isinstance(pl, dict) else None
    if isinstance(d, dict) and d.get("deviceSerialNumber"):
        return str(d["deviceSerialNumber"])
    key = (pl.get("key") or {}).get("entryId") if isinstance(pl, dict) and isinstance(pl.get("key"), dict) else None
    parts = str(key or "").split("#")
    return parts[2] if len(parts) > 2 and parts[2] else None


def echo_iso(ms: Any) -> str:
    import datetime as dt
    try:
        v = float(ms)
    except (TypeError, ValueError):
        return ""
    v = v / 1000 if v > 1e11 else v
    return dt.datetime.fromtimestamp(v, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EchoDevice:
    """The few attributes AlexaAPI reads from a device."""

    def __init__(self, d: dict[str, Any]) -> None:
        self.device_serial_number = d.get("serialNumber")
        self._device_type = d.get("deviceType")
        self._device_family = d.get("deviceFamily")
        self._locale = d.get("locale") or "nl-NL"
        self._cluster_members = d.get("clusterMembers") or []


def echo_login_obj(a: dict[str, Any]):
    from alexapy import AlexaLogin
    ECHO_DIR.mkdir(parents=True, exist_ok=True)
    return AlexaLogin(url=str(a.get("url") or "amazon.de"), email=str(a.get("email") or ""),
                      password=str(a.get("password") or ""), outputpath=lambda p: str(ECHO_DIR / p),
                      otp_secret=str(a.get("otp_secret") or ""), oauth=a.get("oauth") or {}, oauth_login=True)


def echo_save_tokens(login) -> None:
    cfg = load_config()
    a = cfg.setdefault("echo", {})
    a["oauth"] = {"access_token": login.access_token, "refresh_token": login.refresh_token,
                  "expires_in": login.expires_in, "mac_dms": login.mac_dms,
                  "code_verifier": login.code_verifier, "authorization_code": login.authorization_code}
    a.pop("password", None)  # not needed after the first login; the refresh token is enough
    save_config(cfg)


class EchoCloud:
    REFRESH = 900  # device list every 15 minutes

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.a = cfg.get("echo") or {}
        self.login = None
        self.wake = asyncio.Event()
        self.devs: dict[str, dict[str, Any]] = {}
        self.volume: dict[str, int] = {}
        self.bt: dict[str, dict[str, Any]] = {}
        self.last_ts: dict[str, int] = {}  # newest voice history record per serial (ms)
        self.hist_wake = asyncio.Event()
        self.status_wake = asyncio.Event()
        self.player_due: set[str] = set()
        self.push_ok = False
        self.bg: list[asyncio.Task] = []

    async def _connect(self) -> None:
        self.login = echo_login_obj(self.a)
        await self.login.login(cookies=await self.login.load_cookie())
        if not await self.login.test_loggedin():
            raise PermissionError("Amazon login expired: log in again on the setup page")
        await self.login.finalize_login()
        echo_save_tokens(self.login)
        log.info("[echo] logged in")

    async def _fetch(self) -> None:
        from alexapy import AlexaAPI
        raw = await AlexaAPI.get_devices(self.login) or []
        await self._fetch_volumes()
        seen = set()
        for d in raw:
            if not isinstance(d, dict) or not echo_wanted(d) or not d.get("serialNumber"):
                continue
            serial = str(d["serialNumber"])
            seen.add(serial)
            self.devs[serial] = d
            state = {"volume": self.volume.get(serial), "family": d.get("deviceFamily"),
                     "model": d.get("deviceType")}
            REG.upsert("echo-" + serial, "echo", d.get("accountName") or "Echo", state,
                       bool(d.get("online", True)), self._command(serial))
        missing = [x for x in seen if x not in self.volume]
        if missing:
            await self._fetch_player_volumes()
            for x in missing:
                if x in self.volume:
                    d = self.devs[x]
                    REG.upsert("echo-" + x, "echo", d.get("accountName") or "Echo", {"volume": self.volume[x]},
                               bool(d.get("online", True)))
        log.info("[echo] volume known for %d of %d", sum(1 for x in seen if x in self.volume), len(seen))
        for serial in list(self.devs):
            if serial not in seen:
                REG.set_online("echo-" + serial, False)
        ECHO.update(devices=len(seen), error=None, updated=int(time.time()))
        log.info("[echo] %d Echo device(s)", len(seen))

    def _set(self, serial: str, state: dict[str, Any]) -> None:
        d = self.devs.get(serial)
        if d is not None:
            REG.upsert("echo-" + serial, "echo", d.get("accountName") or "Echo", state, bool(d.get("online", True)))

    async def _fetch_history(self) -> None:
        """Newest spoken command per Echo from the Alexa voice history (the text is never logged)."""
        from alexapy import AlexaAPI
        start = int((time.time() - (600 if self.last_ts else 86400)) * 1000)
        recs = await AlexaAPI.get_customer_history_records(self.login, start_time=start, max_record_size=20)
        if not recs:
            return
        newest: dict[str, tuple[int, str]] = {}
        for r in recs:
            if not isinstance(r, dict) or not AlexaAPI._is_usable_history_activity(r):
                continue
            try:
                ts = int(float(r.get("creationTimestamp") or 0))
            except (TypeError, ValueError):
                continue
            serial = str(r["deviceSerialNumber"])
            if ts > newest.get(serial, (self.last_ts.get(serial, 0), ""))[0]:
                newest[serial] = (ts, str((r.get("description") or {}).get("summary") or ""))
        for serial, (ts, text) in newest.items():
            self.last_ts[serial] = ts
            if serial in self.devs:
                self._set(serial, {"lastCommand": " ".join(text.split())[:250], "lastCommandTime": echo_iso(ts)})
                log.info("[echo] %s: voice command (%d chars)", self.devs[serial].get("accountName") or serial,
                         len(text))

    async def _fetch_status(self) -> None:
        """Do not disturb, bluetooth, alarms/timers and what is playing (a few requests for all Echos)."""
        from alexapy import AlexaAPI
        dnd = echo_dnd_from(await AlexaAPI.get_dnd_state(self.login))
        self.bt = echo_bluetooth_from(await AlexaAPI.get_bluetooth(self.login)) or self.bt
        notes = await AlexaAPI.get_notifications(self.login)
        alarms = echo_alarms_from(notes, time.time()) if notes is not None else None
        for serial in list(self.devs):
            st: dict[str, Any] = {}
            if serial in dnd:
                st["dnd"] = "on" if dnd[serial] else "off"
            if serial in self.bt:
                st["bluetooth"] = self.bt[serial]["connected"] or "none"
            if alarms is not None:
                a = alarms.get(serial, {})
                st["nextAlarm"], st["timer"] = a.get("nextAlarm", "none"), a.get("timer", "none")
            if st:
                self._set(serial, st)
        for serial in list(self.devs):
            await self._fetch_player(serial)

    async def _fetch_player(self, serial: str) -> None:
        from alexapy import AlexaAPI
        d = self.devs.get(serial)
        if d is None:
            return
        try:
            js = await AlexaAPI(EchoDevice(d), self.login).get_state()
        except Exception:  # noqa: BLE001
            return
        if js is None:
            return
        st: dict[str, Any] = dict(echo_player_from(js))
        v = (((js or {}).get("playerInfo") or {}).get("volume") or {}).get("volume")
        if v is not None:
            self.volume[serial] = echo_volume(v)
            st["volume"] = self.volume[serial]
        self._set(serial, st)

    async def _on_push(self, msg: Any) -> None:
        for cmd, pl in echo_push_parse(msg):
            serial = echo_push_serial(pl)
            if cmd == "PUSH_VOLUME_CHANGE" and serial and pl.get("volumeSetting") is not None:
                self.volume[serial] = echo_volume(pl["volumeSetting"])
                self._set(serial, {"volume": self.volume[serial]})
            elif cmd == "PUSH_DOPPLER_CONNECTION_CHANGE" and serial in self.devs:
                self.devs[serial]["online"] = pl.get("dopplerConnectionState") == "ONLINE"
                self._set(serial, {})
            elif cmd in ("PUSH_AUDIO_PLAYER_STATE", "PUSH_MEDIA_CHANGE", "PUSH_MEDIA_QUEUE_CHANGE") and serial:
                self.player_due.add(serial)
            elif cmd in ("PUSH_BLUETOOTH_STATE_CHANGE", "PUSH_NOTIFICATION_CHANGE"):
                self.status_wake.set()
            # a voice command arrives as activity; Amazon does not always send that one, so other pushes also
            # trigger a (cheap) history check
            if cmd != "PUSH_MEDIA_PROGRESS_CHANGE":
                self.hist_wake.set()

    async def _history_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self.hist_wake.wait(), timeout=15)
                await asyncio.sleep(2)  # the record shows up in the history shortly after the push
            except asyncio.TimeoutError:
                pass
            self.hist_wake.clear()
            if self.login is None:
                continue
            try:
                await self._fetch_history()
                for serial in list(self.player_due):
                    self.player_due.discard(serial)
                    await self._fetch_player(serial)
            except Exception as ex:  # noqa: BLE001
                log.debug("[echo] history: %s", safe_err(ex))

    async def _status_loop(self) -> None:
        while True:
            if self.login is not None:
                try:
                    await self._fetch_status()
                except Exception as ex:  # noqa: BLE001
                    log.info("[echo] status: %s", safe_err(ex))
            self.status_wake.clear()
            try:
                await asyncio.wait_for(self.status_wake.wait(), timeout=300)
                await asyncio.sleep(2)
            except asyncio.TimeoutError:
                pass

    async def _push_loop(self) -> None:
        """Live events from Amazon (volume, playing, voice activity); without it the history is checked every 15 s."""
        from alexapy import HTTP2EchoClient
        while True:
            if self.login is None:
                await asyncio.sleep(30)
                continue
            closed = asyncio.Event()

            async def on_open() -> None:
                self.push_ok = True
                log.info("[echo] live events connected")

            async def on_close() -> None:
                self.push_ok = False
                closed.set()

            async def on_error(err: str) -> None:
                self.push_ok = False
                log.info("[echo] live events: %s", str(err)[:100])
                closed.set()

            try:
                client = HTTP2EchoClient(self.login, self._on_push, on_open, on_close, on_error)
                await client.async_run()
                await closed.wait()
            except Exception as ex:  # noqa: BLE001
                log.info("[echo] live events unavailable: %s", safe_err(ex))
            self.push_ok = False
            await asyncio.sleep(60)

    def _start_background(self) -> None:
        if not self.bg:
            self.bg = [asyncio.create_task(self._history_loop()), asyncio.create_task(self._status_loop()),
                       asyncio.create_task(self._push_loop())]

    async def _fetch_volumes(self) -> None:
        """Current speaker volume of every Echo (one request for all devices)."""
        from alexapy import AlexaAPI
        from alexapy.helpers import get_json_value
        try:
            r = await AlexaAPI._static_request("get", self.login, "/api/devices/deviceType/dsn/audio/v1/allDeviceVolumes")
            js, *_ = await get_json_value(r, None, dict)
            self.volume.update(echo_volumes_from(js))
        except Exception as e:  # noqa: BLE001
            log.info("[echo] volumes not available: %s", type(e).__name__)

    async def _fetch_player_volumes(self) -> None:
        """Fallback per device: the volume in the player state (for Echos missing from allDeviceVolumes)."""
        from alexapy import AlexaAPI
        for serial, d in self.devs.items():
            if serial in self.volume:
                continue
            try:
                st = await AlexaAPI(EchoDevice(d), self.login).get_state()
                v = (((st or {}).get("playerInfo") or {}).get("volume") or {}).get("volume")
                if v is not None:
                    self.volume[serial] = echo_volume(v)
            except Exception:  # noqa: BLE001
                continue

    def _command(self, serial: str):
        async def handler(cmd: str, value: Any) -> None:
            from alexapy import AlexaAPI
            if cmd == "refresh":
                self.wake.set()
                return
            d = self.devs.get(serial)
            if d is None or self.login is None:
                raise RuntimeError("Echo not available yet")
            api = AlexaAPI(EchoDevice(d), self.login)
            if cmd == "speak":
                await api.send_tts(echo_text(value), queue_delay=0)
            elif cmd == "announce":
                await api.send_announcement(echo_text(value), method="all", title="SmartThings",
                                            targets=[serial], queue_delay=0)
            elif cmd == "command":  # anything you can say to Alexa, e.g. "play Radio 538"
                await api.run_custom(echo_text(value))
            elif cmd in ("volume", "volumeUp", "volumeDown"):
                cur = self.volume.get(serial, 30)
                v = echo_volume(value if cmd == "volume" else cur + (10 if cmd == "volumeUp" else -10))
                await api.set_volume(v / 100, queue_delay=0)
                self.volume[serial] = v
                REG.upsert("echo-" + serial, "echo", d.get("accountName") or "Echo", {"volume": v},
                           bool(d.get("online", True)))
            elif cmd == "stop":
                await api.stop(queue_delay=0)
                self.player_due.add(serial)
                self.hist_wake.set()
            elif cmd in ("play", "pause", "next", "previous"):
                await getattr(api, cmd)()
                self.player_due.add(serial)
                self.hist_wake.set()
            elif cmd == "routine":
                await api.run_routine(echo_text(value), queue_delay=0)
            elif cmd == "skill":
                await api.run_skill(echo_text(value))
            elif cmd == "dnd":
                on = str(value).lower() in ("on", "true", "1")
                await api.set_dnd_state(on)
                self._set(serial, {"dnd": "on" if on else "off"})
            elif cmd == "music":
                provider, phrase = echo_music(value)
                if provider:
                    await api.play_music(provider, phrase, queue_delay=0)
                else:
                    await api.run_custom("play " + phrase)
                self.player_due.add(serial)
                self.hist_wake.set()
            elif cmd == "sound":
                name = echo_text(value, 80)
                sid = ECHO_SOUNDS.get(name.lower(), name)
                if not sid.startswith("amzn_sfx_"):
                    raise ValueError("unknown sound")
                await api.play_sound(sid, queue_delay=0)
            elif cmd == "push":  # notification in the Alexa app on the phone
                await api.send_mobilepush(echo_text(value), title="SmartThings", queue_delay=0)
            elif cmd == "bluetooth":
                want = str(value or "").strip()
                if want.lower() in ("", "off", "none", "disconnect"):
                    await api.disconnect_bluetooth()
                else:
                    paired = (self.bt.get(serial) or {}).get("paired") or {}
                    mac = next((a for n, a in paired.items() if n.lower() == want.lower()), None)
                    mac = mac or next((a for a in paired.values() if str(a).lower() == want.lower()), None)
                    if not mac:
                        raise ValueError("bluetooth device not paired with this Echo")
                    await api.set_bluetooth(mac)
                self.status_wake.set()
            else:
                raise ValueError(f"unsupported command {cmd}")
            # the text itself can be personal: only its length goes into the log
            log.info("[echo] %s: %s%s", d.get("accountName") or serial, cmd,
                     f" ({len(str(value))} chars)" if cmd in ("speak", "announce", "command", "routine", "music",
                                                              "push", "skill", "bluetooth") else
                     ("" if value is None else f" {value}"))
        return handler

    async def run(self) -> None:
        if not echo_configured(self.a):
            log.info("[echo] not configured (optional: Amazon Echo speak, announce, volume)")
            return
        while True:
            try:
                if self.login is None:
                    await self._connect()
                await self._fetch()
                self._start_background()
            except PermissionError as ex:
                ECHO["error"] = str(ex)
                log.error("[echo] %s", ex)
                REG.set_online("echo-", False)
                return
            except Exception as ex:  # noqa: BLE001
                ECHO["error"] = safe_err(ex)[:200]
                log.warning("[echo] update failed: %s", safe_err(ex))
                self.login = None
                await asyncio.sleep(120)
                continue
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.REFRESH)
            except asyncio.TimeoutError:
                pass


# ---------------------------------------------------------------- VELUX ACTIVE departure mode (lock / unlock)
# The windows themselves are controlled locally over HomeKit by the VELUX driver on the hub. The departure mode
# (away = all windows and blinds blocked) only exists in the VELUX/Netatmo cloud. Locking is a plain command,
# unlocking must be signed with a key that the gateway hands out locally (port 25050) after its button is pressed.
# Protocol as documented by ha-velux-active (github.com/Niek/ha-velux-active, MIT).

VELUX_NETATMO = "https://api.netatmo.com/"
VELUX_SYNC = "https://app.velux-active.com/syncapi/v1/"
VELUX_CLIENT = {"client_id": "5931426da127d981e76bdd3f", "client_secret": "6ae2d89d15e767ae5c56b456b452d319"}
VELUX_APP_VERSION = "791302006"
VELUX_PAIR: dict[str, Any] = {"state": None, "error": None, "start": None}

# --- HomeKit pair-setup SRP helper ------------------------------------------------------------------------
# SRP-6a (RFC 5054 3072-bit group, SHA-512) with the HomeKit conventions, same as srp.lua in the Edge drivers.
SRP_N = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B"
    "302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7EDEE386BFB5A899FA5AE9F24117C4B1FE6"
    "49286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3BE39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7D"
    "B3970F85A6E1E4C7ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF", 16)
SRP_G = 5
_SRP_LAST = {"t": 0.0}


def srp_homekit_client(salt: bytes, b_pub: bytes, pin: str) -> tuple[bytes, bytes, bytes]:
    """Client side of HomeKit pair-setup M3: returns (A, M1 proof, session key K)."""
    def h(*parts: bytes) -> bytes:
        return hashlib.sha512(b"".join(parts)).digest()

    def pad(v: int) -> bytes:
        return v.to_bytes(384, "big")

    n_bytes = pad(SRP_N)
    big_b = int.from_bytes(b_pub, "big")
    if big_b % SRP_N == 0:
        raise ValueError("invalid accessory key")
    k = int.from_bytes(h(n_bytes, pad(SRP_G)), "big")
    x = int.from_bytes(h(salt, h(b"Pair-Setup:" + pin.encode())), "big")
    a = int.from_bytes(secrets.token_bytes(32), "big")
    big_a = pow(SRP_G, a, SRP_N)
    u = int.from_bytes(h(pad(big_a), pad(big_b)), "big")
    if u == 0:
        raise ValueError("invalid accessory key")
    base = (big_b - k * pow(SRP_G, x, SRP_N)) % SRP_N
    key = h(pad(pow(base, a + u * x, SRP_N)))
    group = bytes(p ^ q for p, q in zip(h(n_bytes), h(bytes([SRP_G]))))
    proof = h(group, h(b"Pair-Setup"), salt, pad(big_a), pad(big_b), key)
    return pad(big_a), proof, key


def srp_box_key(token: str, direction: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA512(), length=32, salt=b"stbridge-srp",
                info=direction.encode()).derive(token.encode())


# --- LAN API authentication -------------------------------------------------------------------------
# After pairing, every request from the hub carries an HMAC-SHA512 signature made with a shared token
# (X-Stbridge-Time, X-Stbridge-Sig) and every answer is signed back. The token itself never crosses the
# LAN again after pairing, a replayed request is refused, and a device pretending to be the bridge cannot
# produce a valid answer. Until a hub is paired the API is closed: only /stbridge/info and /stbridge/pair answer.
# Pairing: the hub asks while "Pair" is open, the setup page shows the asking address and the token only goes to
# the address you press Allow for (no race: another device on the LAN cannot grab the token first).
PAIRING: dict[str, Any] = {"until": 0.0, "asked": {}, "allowed": None, "last_ip": None}
PAIR_WINDOW = 300          # seconds the "Pair" button keeps pairing open
SIG_MAX_AGE = 300          # seconds a signed request stays valid (hub and pc clocks use NTP)
_SEEN_SIGS: dict[str, float] = {}


def api_token(cfg: dict[str, Any] | None = None) -> str:
    """The shared secret for hub requests; created on first use and kept in config.json."""
    cfg = cfg if cfg is not None else load_config()
    tok = cfg.get("api_token")
    if not tok:
        tok = cfg["api_token"] = secrets.token_hex(32)
        save_config(cfg)
    return tok


def api_paired(cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get("api_paired") and cfg.get("api_token"))


def sign(token: str, msg: bytes | str) -> str:
    return hmac.new(token.encode(), msg if isinstance(msg, bytes) else msg.encode(), hashlib.sha512).hexdigest()


def request_message(method: str, path: str, ts: str, body: bytes) -> bytes:
    return f"{method}\n{path}\n{ts}\n".encode() + body


def check_request_sig(token: str, method: str, path: str, ts: str, sig: str, body: bytes,
                      now: float | None = None) -> str | None:
    """None when the signature is valid and fresh, else the reason."""
    now = time.time() if now is None else now
    try:
        age = abs(now - float(ts))
    except (TypeError, ValueError):
        return "bad time"
    if age > SIG_MAX_AGE:
        return "request too old (check the clock)"
    sig = str(sig).lower()  # one spelling for the check and the replay list (else upper case replays a request)
    if not hmac.compare_digest(sign(token, request_message(method, path, ts, body)), sig):
        return "bad signature"
    for k in [k for k, t in _SEEN_SIGS.items() if now - t > SIG_MAX_AGE * 2]:
        del _SEEN_SIGS[k]
    if sig in _SEEN_SIGS:
        return "replayed request"
    _SEEN_SIGS[sig] = now
    return None


def safe_err(ex: BaseException) -> str:
    """An exception as text for logs and HTTP answers, without query strings (a websocket error carries the URL,
    and the Samsung TV token is in that URL) and without anything that looks like a token."""
    s = str(ex)
    s = re.sub(r"\?[^\s'\"]*", "", s)                          # query strings
    s = re.sub(r"(?i)(token|key|secret|password)=\S+", r"\1=...", s)
    return s[:300]


def velux_configured(v: dict[str, Any]) -> bool:
    return bool(v.get("username") and v.get("password"))


def velux_sign(hash_sign_key: str, message: str) -> str:
    """HMAC-SHA512 with the gateway key (base64), url-safe base64 like the VELUX app."""
    import hashlib
    import hmac
    raw = hash_sign_key.strip().replace("-", "+").replace("_", "/")
    key = base64.b64decode(raw + "=" * (-len(raw) % 4))
    digest = hmac.new(key, message.encode(), hashlib.sha512).digest()
    return base64.b64encode(digest).decode().replace("+", "-").replace("/", "_")


def velux_scenario_hash(hash_sign_key: str, scenario: str, ts: int, nonce: int, gateway: str) -> str:
    return velux_sign(hash_sign_key, f"scenario{scenario}{ts}{nonce}{gateway}")


def velux_retrieve_key(host: str, timeout: float = 180.0, port: int = 25050) -> tuple[str, str]:
    """Wait for the gateway's pairing listener (after its button is pressed) and fetch a signing key.
    Returns (sign_key_id, hash_sign_key), both url-safe base64. Blocking: run it in a thread."""
    import hashlib
    import struct
    from cryptography.hazmat.primitives.asymmetric import x25519
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives import serialization

    PING, PONG, CLOSE, SECURE = 0x0000, 0x0001, 0x0008, 0x0200
    ECDH_REQ, ECDH_RESP = 0x0201, 0x0202
    NONCE_REQ, NONCE_RESP, CHAL_REQ, CHAL_RESP, KEY_REQ, KEY_RESP = 0x020A, 0x020B, 0x020C, 0x020D, 0x020E, 0x020F

    def frame(t: int, payload: bytes = b"") -> bytes:
        return struct.pack("<HH", t, len(payload)) + payload

    def bump(n: bytearray) -> None:
        for i in range(11, 3, -1):
            if n[i] < 0xFF:
                n[i] += 1
                return
            n[i] = 0
        n[:] = bytes(12)

    def attempt() -> tuple[str, str]:
        s = socket.create_connection((host, port), timeout=10)
        s.settimeout(10)
        buf = bytearray()

        def recv() -> tuple[int, bytes]:
            while True:
                if len(buf) >= 4:
                    t, n = struct.unpack_from("<HH", buf)
                    if len(buf) >= 4 + n:
                        out = (t, bytes(buf[4:4 + n]))
                        del buf[:4 + n]
                        if t == PING:
                            s.sendall(frame(PONG))
                            continue
                        return out
                chunk = s.recv(4096)
                if not chunk:
                    raise ConnectionError("gateway closed the connection")
                buf.extend(chunk)

        try:
            s.sendall(frame(PING))
            if recv()[0] != PONG:
                raise ConnectionError("no pong")
            priv = x25519.X25519PrivateKey.generate()
            pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            s.sendall(frame(ECDH_REQ, b"\x01" + pub))
            t, gw_pub = recv()
            if t != ECDH_RESP:
                raise ConnectionError(f"unexpected frame {t:#06x}")
            if len(gw_pub) == 33 and gw_pub[0] in (0, 1):
                gw_pub = gw_pub[1:]
            shared = priv.exchange(x25519.X25519PublicKey.from_public_bytes(gw_pub))
            aead = ChaCha20Poly1305(hashlib.sha512(shared).digest()[:32])
            tx, rx = bytearray(12), bytearray(12)

            def send_secure(t: int, payload: bytes = b"") -> None:
                s.sendall(frame(SECURE, aead.encrypt(bytes(tx), frame(t, payload), None)))
                bump(tx)

            def recv_secure(want: int) -> bytes:
                t, p = recv()
                if t != SECURE:
                    raise ConnectionError(f"unexpected frame {t:#06x}")
                plain = aead.decrypt(bytes(rx), p, None)
                bump(rx)
                t, n = struct.unpack_from("<HH", plain)
                if t != want or len(plain) != 4 + n:
                    raise ConnectionError(f"unexpected secure frame {t:#06x}")
                return plain[4:]

            key_id = uuid.uuid4().bytes
            send_secure(KEY_REQ, b"\x00" + key_id)
            r = recv_secure(KEY_RESP)
            if not r or r[0] != 0 or len(r) != 33:
                raise PermissionError({1: "refused", 2: "bad key id", 3: "key table full"}.get(r[0] if r else -1,
                                                                                           "refused"))
            gw_key = r[1:]
            send_secure(NONCE_REQ, b"\x00")
            nonce = recv_secure(NONCE_RESP)
            send_secure(CHAL_REQ, key_id + hashlib.sha512(key_id + gw_key + nonce).digest())
            r = recv_secure(CHAL_RESP)
            if not r or r[0] != 0:
                raise PermissionError("gateway refused the key check")
            send_secure(CLOSE)
            return base64.urlsafe_b64encode(key_id).decode(), base64.urlsafe_b64encode(gw_key).decode()
        finally:
            s.close()

    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                pass
        except OSError:
            time.sleep(1)
            continue
        try:
            return attempt()
        except PermissionError:
            raise
        except Exception as ex:  # noqa: BLE001  (listener not ready yet: try again)
            last = ex
            time.sleep(1)
    raise TimeoutError(f"gateway did not open its pairing port ({last or 'button not pressed?'})")


class VeluxCloud:
    POLL = 60  # default, "poll_seconds" in config.json (setup page or SmartThings)

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.v = cfg.get("velux") or {}
        self.session = None
        self.wake = asyncio.Event()
        self.home: dict[str, Any] | None = None  # {"id", "timezone", "gateway", "name"}
        self.last_ts, self.last_nonce = 0, -1
        self.fast = 0  # fast polls left after a command

    # -- auth
    async def _tokens(self, data: dict[str, str]) -> None:
        body = {**VELUX_CLIENT, "app_version": VELUX_APP_VERSION, **data}
        async with self.session.post(VELUX_NETATMO + "oauth2/token", data=body) as r:
            raw = await r.json(content_type=None)
        if r.status >= 400 or "access_token" not in (raw or {}):
            err = (raw or {}).get("error") if isinstance(raw, dict) else None
            if r.status in (400, 401, 403) or err == "invalid_grant":
                raise PermissionError(f"login refused ({err or r.status})")
            raise RuntimeError(f"token request failed: HTTP {r.status}")
        t = {"access_token": raw["access_token"], "refresh_token": raw.get("refresh_token"),
             "expires_at": int(time.time()) + int(raw.get("expires_in") or raw.get("expire_in") or 3600)}
        self.v["tokens"] = t
        cfg = load_config()
        cfg.setdefault("velux", {})["tokens"] = t
        save_config(cfg)

    async def _access(self) -> str:
        t = self.v.get("tokens") or {}
        if t.get("access_token") and time.time() < t.get("expires_at", 0) - 60:
            return t["access_token"]
        if t.get("refresh_token"):
            try:
                await self._tokens({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
                return self.v["tokens"]["access_token"]
            except PermissionError:
                log.info("[velux] refresh token refused, logging in again")
        await self._tokens({"grant_type": "password", "username": self.v["username"], "password": self.v["password"],
                            "scope": "velux_scopes", "user_prefix": "velux"})
        return self.v["tokens"]["access_token"]

    async def _post(self, url: str, data: dict | None = None, body: dict | None = None) -> dict[str, Any]:
        for again in (True, False):
            headers = {"Authorization": "Bearer " + await self._access()}
            kw = {"json": body} if body is not None else {"data": data or {}}
            async with self.session.post(url, headers=headers, **kw) as r:
                text = await r.text()
            if r.status in (401, 403) and again:
                self.v.setdefault("tokens", {})["expires_at"] = 0  # token revoked: refresh once
                continue
            try:
                raw = json.loads(text) if text.strip() else {}
            except ValueError:
                raw = {"raw": text[:200]}
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {str(raw)[:200]}")
            errors = (raw.get("body") or {}).get("errors") if isinstance(raw.get("body"), dict) else None
            if errors:
                raise RuntimeError(f"VELUX refused: {errors}")
            return raw
        raise RuntimeError("unauthorized")

    # -- state
    async def _discover(self) -> None:
        raw = await self._post(VELUX_NETATMO + "api/homesdata")
        for h in (raw.get("body") or {}).get("homes") or []:
            gws = [m for m in h.get("modules") or [] if m.get("type") == "NXG"]
            if gws:
                gw = gws[0]
                self.home = {"id": h["id"], "timezone": h.get("timezone") or "Europe/Amsterdam",
                             "gateway": gw["id"], "name": gw.get("name") or h.get("name") or "VELUX"}
                log.info("[velux] home %s, gateway %s", h.get("name"), gw["id"])
                return
        raise RuntimeError("no VELUX gateway on this account")

    async def _refresh(self) -> None:
        if not self.home:
            await self._discover()
        raw = await self._post(VELUX_NETATMO + "api/homestatus", data={"home_id": self.home["id"]})
        mods = ((raw.get("body") or {}).get("home") or {}).get("modules") or []
        gw = next((m for m in mods if m.get("id") == self.home["gateway"]), None)
        if gw is None:
            REG.set_online("velux-", False)
            raise RuntimeError("gateway not in homestatus (offline?)")
        cfg = load_config().get("velux") or {}
        state = {"locked": bool(gw.get("locked")), "locking": bool(gw.get("locking")), "busy": bool(gw.get("busy")),
                 "canUnlock": bool(cfg.get("hash_sign_key") and cfg.get("sign_key_id")),
                 "pairing": VELUX_PAIR["state"]}
        REG.upsert("velux-" + self.home["gateway"], "velux_lock", "VELUX departure mode", state, True, self._command())
        if "is_raining" in gw:  # the gateway's rain sensor as its own device
            REG.upsert("velux-rain-" + self.home["gateway"], "rain", "VELUX rain sensor",
                       {"raining": bool(gw.get("is_raining"))}, True, self._refresh_only())
        log.debug("[velux] locked %s, raining %s", state["locked"], gw.get("is_raining"))

    async def _scenario(self, modules: list[dict]) -> None:
        payload = {"app_type": "app_velux", "app_version": VELUX_APP_VERSION,
                   "home": {"id": self.home["id"], "timezone": self.home["timezone"], "modules": modules}}
        await self._post(VELUX_SYNC + "setstate", body=payload)

    def _refresh_only(self):
        async def handler(cmd: str, value: Any) -> None:
            if cmd != "refresh":
                raise ValueError(f"unsupported command {cmd}")
            self.wake.set()
        return handler

    def _command(self):
        async def handler(cmd: str, value: Any) -> None:
            if cmd == "settings":  # from the SmartThings device settings
                sec = poll_seconds((value or {}).get("poll_seconds"), int(self.v.get("poll_seconds") or 60))
                self.v["poll_seconds"] = sec
                save_poll_seconds("velux", sec)
                log.info("[velux] settings from SmartThings: every %s s", sec)
                return
            if cmd == "refresh":
                self.wake.set()
                return
            if cmd == "lock":
                await self._scenario([{"id": self.home["gateway"], "scenario": "away"}])
            elif cmd == "unlock":
                cfg = load_config().get("velux") or {}
                key, key_id = cfg.get("hash_sign_key"), cfg.get("sign_key_id")
                if not (key and key_id):
                    raise PermissionError("unlock needs the gateway key: press 'Pair gateway' on the setup page")
                if cfg.get("sign_key_gateway") and cfg["sign_key_gateway"] != self.home["gateway"]:
                    raise PermissionError("key belongs to another gateway: pair again")
                now = int(time.time())
                ts, nonce = (self.last_ts, self.last_nonce + 1) if now <= self.last_ts else (now, 0)
                self.last_ts, self.last_nonce = ts, nonce
                await self._scenario([{"id": self.home["gateway"], "nonce": nonce, "sign_key_id": key_id,
                                       "scenario": "home", "timestamp": ts,
                                       "hash_scenario": velux_scenario_hash(key, "home", ts, nonce,
                                                                            self.home["gateway"])}])
            else:
                raise ValueError(f"unsupported command {cmd}")
            log.info("[velux] %s sent", cmd)
            self.fast = 4  # the gateway applies it with a delay
            self.wake.set()
        return handler

    async def pair(self, host: str) -> None:
        """Ask the cloud to put the gateway in key mode, then fetch the key locally once its button is pressed."""
        VELUX_PAIR.update(state="waiting", error=None, start=int(time.time()))
        try:
            if not self.home:
                await self._discover()
            await self._post(VELUX_NETATMO + "api/setstate", body={
                "home": {"id": self.home["id"], "modules": [{"id": self.home["gateway"], "retrieve_key": True}]}})
            log.info("[velux] gateway in key mode: press the button on the gateway now")
            VELUX_PAIR["state"] = "press"
            key_id, key = await asyncio.to_thread(velux_retrieve_key, host)
            cfg = load_config()
            v = cfg.setdefault("velux", {})
            v.update(sign_key_id=key_id, hash_sign_key=key, sign_key_gateway=self.home["gateway"], gateway_ip=host)
            save_config(cfg)
            VELUX_PAIR.update(state="done", error=None)
            log.info("[velux] gateway paired, unlocking is possible now")
            self.wake.set()
        except Exception as ex:  # noqa: BLE001
            VELUX_PAIR.update(state="failed", error=str(ex)[:200])
            log.warning("[velux] pairing failed: %s", ex)

    async def run(self) -> None:
        if not velux_configured(self.v):
            log.info("[velux] not configured (optional: VELUX departure mode lock/unlock)")
            return
        import aiohttp
        VELUX["source"] = self
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            self.session = session
            while True:
                try:
                    await self._refresh()
                except PermissionError as ex:
                    log.error("[velux] %s. Not retrying; fix it on the setup page", ex)
                    VELUX["error"] = str(ex)
                    REG.set_online("velux-", False)
                    return
                except Exception as ex:  # noqa: BLE001
                    log.warning("[velux] update failed: %s", ex)
                    self.home = None if "no VELUX gateway" in str(ex) else self.home
                self.wake.clear()
                wait = 5 if self.fast > 0 else int(self.v.get("poll_seconds") or self.POLL)
                self.fast = max(0, self.fast - 1)
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass


VELUX: dict[str, Any] = {"source": None, "error": None}


# ---------------------------------------------------------------- Buienradar (rain radar forecast, NL and BE)
# Two hours ahead in 5 minute steps for a point. For a radius the bridge also reads points on a ring around it
# (and a half ring) and takes the heaviest rain per time step. Data: Buienradar (gpsgadget raintext).

BUIENRADAR_URL = "https://gpsgadget.buienradar.nl/data/raintext"
RAIN_THRESHOLD = 0.1  # mm/h counts as rain


PDOK_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"  # Dutch addresses (Kadaster, free)
OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"  # worldwide, 15 minute steps (CC BY 4.0, no key)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"  # addresses worldwide (OpenStreetMap, ODbL)
RAIN_PROVIDERS = ("auto", "buienradar", "openmeteo")
HUMID_POLL = 3600  # dew point forecast: the weather models are updated hourly
WIND_POLL = 900    # wind now: Open-Meteo "current" values change every 15 minutes


def wind_summary(data: Any) -> dict[str, Any]:
    """Open-Meteo current wind (km/h, 10 m) -> windSpeed, windGust (km/h) and windDirection (degrees)."""
    cur = (data or {}).get("current") or {}
    out: dict[str, Any] = {}
    for src, key in (("wind_speed_10m", "windSpeed"), ("wind_gusts_10m", "windGust"), ("wind_direction_10m", "windDirection")):
        v = cur.get(src)
        if isinstance(v, (int, float)):
            out[key] = round(float(v), 1) if key != "windDirection" else int(round(v)) % 360
    return out
HUMID_HOURS = 48   # default look-ahead for the highest dew point


def humid_summary(data: Any, hours: int = HUMID_HOURS, now: dt.datetime | None = None) -> dict[str, Any]:
    """Open-Meteo hourly + current dew point -> dew point now and the highest one in the next `hours` hours."""
    now = now or dt.datetime.now(dt.timezone.utc)
    h = (data or {}).get("hourly") or {}
    rows = []
    for t, dew in zip(h.get("time") or [], h.get("dew_point_2m") or []):
        start = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc)
        if dew is None or start + dt.timedelta(hours=1) <= now:  # hour is over
            continue
        rows.append((start, float(dew)))
        if len(rows) >= hours:
            break
    if not rows:
        return {}
    cur = (data or {}).get("current") or {}
    dnow = float(cur["dew_point_2m"]) if cur.get("dew_point_2m") is not None else rows[0][1]
    at, dmax = max(rows, key=lambda r: r[1])
    when = at.astimezone().strftime("%a %H:%M")  # bridge pc local time
    out = {"dewPointNow": round(dnow, 1), "dewPointMax": round(max(dmax, dnow), 1), "dewPointMaxAt": at.isoformat(),
           "humidHours": hours,
           "humidSummary": f"Dew point now {dnow:.1f} C, highest {dmax:.1f} C ({when}) in the next {hours} h"}
    if cur.get("relative_humidity_2m") is not None:
        out["humidityNow"] = cur["relative_humidity_2m"]
    if cur.get("temperature_2m") is not None:
        out["temperatureNow"] = cur["temperature_2m"]
    return out


def in_benelux(lat: float, lon: float) -> bool:
    """Area of the Buienradar radar forecast (the Netherlands, Belgium and a margin)."""
    return 49.4 <= lat <= 53.7 and 2.5 <= lon <= 7.3


def rain_provider(b: dict[str, Any]) -> str:
    p = b.get("provider") or "auto"
    if p == "auto":
        return "buienradar" if buienradar_configured(b) and in_benelux(float(b["lat"]), float(b["lon"])) else "openmeteo"
    return p


def buienradar_enabled(b: dict[str, Any]) -> bool:
    return bool(b.get("enabled")) or b.get("lat") is not None


def buienradar_configured(b: dict[str, Any]) -> bool:
    return b.get("lat") is not None and b.get("lon") is not None


def normalize_postcode(pc: Any) -> str | None:
    import re
    t = re.sub(r"\s+", "", str(pc or "")).upper()
    return t if re.fullmatch(r"\d{4}[A-Z]{2}", t) else None


def house_number(n: Any) -> int | None:
    import re
    m = re.match(r"\s*(\d+)", str(n or ""))
    return int(m.group(1)) if m else None


def parse_location(text: Any) -> tuple[float, float] | None:
    """'52.35, 5.62' or '52.35 5.62' -> (52.35, 5.62); None when not a valid location."""
    import re
    nums = re.findall(r"-?\d+(?:[.,]\d+)?", str(text or ""))
    if len(nums) != 2:
        return None
    lat, lon = (float(n.replace(",", ".")) for n in nums)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def buienradar_settings(v: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Validated settings from the device or the setup page. Keys in v: postcode + housenumber (NL), address (any
    country, free text), location ('lat, lon', wins over both), radius_m (0..25000), threshold (mm/h), provider
    (auto, buienradar, openmeteo). Missing keys keep the current value; empty text clears it. lat/lon are filled in
    later from the postcode or the address."""
    out = {k: current.get(k) for k in ("postcode", "housenumber", "address", "location", "lat", "lon", "place")}
    out["provider"] = current.get("provider", "auto")
    if "address" in v:
        out["address"] = str(v.get("address") or "").strip()[:200] or None
    if v.get("provider") in RAIN_PROVIDERS:
        out["provider"] = v["provider"]
    out["radius_m"] = current.get("radius_m", int(float(current.get("radius_km") or 0) * 1000))
    out["threshold"] = current.get("threshold", RAIN_THRESHOLD)
    out["humid_hours"] = current.get("humid_hours", HUMID_HOURS)
    if "postcode" in v:
        out["postcode"] = normalize_postcode(v.get("postcode"))
    if "housenumber" in v:
        out["housenumber"] = house_number(v.get("housenumber"))
    if "location" in v:
        loc = parse_location(v.get("location"))
        out["location"] = f"{loc[0]}, {loc[1]}" if loc else None
    for key, lo, hi, cast in (("radius_m", 0, 25000, int), ("threshold", 0.05, 5.0, float), ("humid_hours", 6, 72, int)):
        try:
            if v.get(key) not in (None, ""):
                out[key] = max(lo, min(hi, cast(float(str(v[key]).replace(",", ".")))))
        except ValueError:
            pass
    loc = parse_location(out["location"]) if out["location"] else None
    if loc:
        out["lat"], out["lon"], out["place"] = round(loc[0], 4), round(loc[1], 4), out["location"]
    elif (not ((out["postcode"] and out["housenumber"]) or out["address"]) or current.get("location")
          or (out["postcode"], out["housenumber"], out["address"])
          != (current.get("postcode"), current.get("housenumber"), current.get("address"))):
        out["lat"] = out["lon"] = out["place"] = None  # look the address up again
    return out


async def geocode_address(session, address: str) -> tuple[float, float, str] | None:
    """Free text address anywhere -> (lat, lon, name) via OpenStreetMap Nominatim (once per address change)."""
    async with session.get(NOMINATIM_URL, params={"q": address, "format": "jsonv2", "limit": "1"},
                           headers={"User-Agent": f"stoeckie-bridge/{VERSION} (SmartThings Edge bridge)"}) as r:
        if r.status != 200:
            raise RuntimeError(f"Nominatim HTTP {r.status}")
        data = await r.json(content_type=None)
    if not data:
        return None
    d = data[0]
    return round(float(d["lat"]), 5), round(float(d["lon"]), 5), str(d.get("display_name") or address)[:120]


def parse_openmeteo(data: Any, now: dt.datetime | None = None) -> list[list[float]]:
    """Open-Meteo minutely_15 precipitation (mm per 15 min, one or more locations) -> mm/h per 5 minutes from now."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for loc in data if isinstance(data, list) else [data]:
        m = (loc or {}).get("minutely_15") or {}
        times, rain = m.get("time") or [], m.get("precipitation") or []
        rates: list[float] = []
        for t, mm in zip(times, rain):
            start = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc)
            for k in range(3):
                if start + dt.timedelta(minutes=5 * (k + 1)) > now:  # skip 5 minute steps that are over
                    rates.append(round(float(mm or 0) * 4, 2))
        out.append(rates[:24])
    return out


async def geocode_nl(session, postcode: str, number: int) -> tuple[float, float, str] | None:
    """Dutch postcode + house number -> (lat, lon, address) via the PDOK Locatieserver."""
    params = [("q", "*"), ("fq", f"postcode:{postcode}"), ("fq", f"huisnummer:{number}"), ("fq", "type:adres"),
              ("rows", "1"), ("fl", "weergavenaam,centroide_ll")]
    async with session.get(PDOK_URL, params=params) as r:
        if r.status != 200:
            raise RuntimeError(f"PDOK HTTP {r.status}")
        data = await r.json(content_type=None)
    docs = (data.get("response") or {}).get("docs") or []
    if not docs:
        return None
    import re
    m = re.match(r"POINT\(([-\d.]+) ([-\d.]+)\)", docs[0].get("centroide_ll") or "")
    if not m:
        return None
    return round(float(m.group(2)), 5), round(float(m.group(1)), 5), docs[0].get("weergavenaam") or postcode


def rain_points(lat: float, lon: float, radius_m: float) -> list[tuple[float, float]]:
    """Center plus rings at the radius (and half of it): 8 directions each, rounded like Buienradar (2 decimals)."""
    import math
    pts = [(round(lat, 2), round(lon, 2))]
    radius_km = radius_m / 1000
    rings = [radius_km] + ([radius_km / 2] if radius_km >= 3 else []) if radius_km > 0 else []
    for r in rings:
        for k in range(8):
            a = math.radians(45 * k)
            dlat = r * math.cos(a) / 111.32
            dlon = r * math.sin(a) / (111.32 * math.cos(math.radians(lat)))
            p = (round(lat + dlat, 2), round(lon + dlon, 2))
            if p not in pts:
                pts.append(p)
    return pts


def parse_raintext(text: str) -> list[float]:
    """Buienradar raintext ('077|10:35' per line) -> mm/h per 5 minutes."""
    out = []
    for line in text.splitlines():
        v = line.split("|")[0].strip()
        if not v.isdigit():
            continue
        n = int(v)
        out.append(0.0 if n == 0 else round(10 ** ((n - 109) / 32), 2))
    return out


def rain_summary(rates: list[float], threshold: float = RAIN_THRESHOLD, lang: str = "en") -> dict[str, Any]:
    """Now, minutes until rain, how long it rains, intensity class and a short summary (English)."""
    if not rates:
        return {}
    wet = [r >= threshold for r in rates]
    horizon = len(rates) * 5
    now = rates[0]
    if wet[0]:
        until = 0
        start = 0
    else:
        start = next((i for i, w in enumerate(wet) if w), None)
        until = 999 if start is None else start * 5
    lasts = 0
    if start is not None:
        end = next((i for i in range(start, len(wet)) if not wet[i]), len(wet))
        lasts = (end - start) * 5
    peak = max(rates[start:start + lasts // 5] or [0]) if start is not None else 0.0
    total = round(sum(rates) / 12, 1)  # mm in the next two hours

    def level(r: float) -> str:
        if r < threshold:
            return "none"
        if r < 2.5:
            return "light"
        if r < 10:
            return "moderate"
        if r < 50:
            return "heavy"
        return "violent"
    intensity = level(now)
    if intensity == "none" and until <= 15:
        intensity = "possiblePrecipitation"
    more = "+" if start is not None and start * 5 + lasts >= horizon else ""
    # Always English: device texts are not translated by the SmartThings app, so one language for everyone.
    if wet[0]:
        text = f"Raining now ({now:g} mm/h), {lasts}{more} more min"
    elif start is not None:
        text = f"Rain in {until} min, for {lasts}{more} min, up to {peak:g} mm/h"
    else:
        text = f"Dry, no rain expected ({horizon // 60} h)"
    return {"raining": bool(wet[0]), "rainRate": round(now, 2), "minutesUntilRain": until, "minutesOfRain": lasts,
            "peakRate": round(peak, 2), "totalMm": total, "intensity": intensity, "summary": text,
            "horizonMinutes": horizon}


class Buienradar:
    POLL = 300  # Buienradar refreshes every 5 minutes

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.b = cfg.get("buienradar") or {}
        self.session = None
        self.wake = asyncio.Event()
        self.error: str | None = None
        self.humid: dict[str, Any] = {}
        self.humid_key: tuple | None = None
        self.humid_next = 0.0  # next dew point forecast fetch (time.time())
        self.humid_ok = 0.0    # last good fetch
        self.wind: dict[str, Any] = {}
        self.wind_key: tuple | None = None
        self.wind_next = 0.0
        self.wind_ok = 0.0

    async def _fetch_humid(self, lat: float, lon: float, hours: int) -> dict[str, Any]:
        """Dew point forecast for one point (Open-Meteo, hourly; in NL the KNMI model)."""
        params = {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "timezone": "UTC",
                  "hourly": "dew_point_2m", "current": "dew_point_2m,temperature_2m,relative_humidity_2m",
                  "forecast_hours": str(hours + 1)}
        async with self.session.get(OPENMETEO_URL, params=params,
                                    headers={"User-Agent": f"stoeckie-bridge/{VERSION}"}) as r:
            if r.status != 200:
                raise RuntimeError(f"Open-Meteo HTTP {r.status}")
            return humid_summary(await r.json(content_type=None), hours)

    async def _humid_state(self, lat: float, lon: float) -> dict[str, Any]:
        """Hourly; a failure never stops the rain radar (the last values stay up to 3 hours)."""
        hours = int(self.b.get("humid_hours") or HUMID_HOURS)
        key = (round(lat, 4), round(lon, 4), hours)
        now = time.time()
        if key != self.humid_key or now >= self.humid_next:
            try:
                self.humid = await self._fetch_humid(lat, lon, hours)
                self.humid_key, self.humid_ok, self.humid_next = key, now, now + HUMID_POLL
                log.info("[buienradar] %s", self.humid.get("humidSummary", "no dew point forecast"))
            except Exception as ex:  # noqa: BLE001
                log.warning("[buienradar] dew point forecast failed: %s", ex)
                self.humid_next = now + 600
                if now - self.humid_ok > 3 * HUMID_POLL:
                    self.humid = {}
        return self.humid

    async def _wind_state(self, lat: float, lon: float) -> dict[str, Any]:
        """Wind now (Open-Meteo, km/h), every 15 minutes; a failure never stops the rain radar."""
        key = (round(lat, 4), round(lon, 4))
        now = time.time()
        if key != self.wind_key or now >= self.wind_next:
            params = {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "timezone": "UTC", "wind_speed_unit": "kmh",
                      "current": "wind_speed_10m,wind_gusts_10m,wind_direction_10m"}
            try:
                async with self.session.get(OPENMETEO_URL, params=params,
                                            headers={"User-Agent": f"stoeckie-bridge/{VERSION}"}) as r:
                    if r.status != 200:
                        raise RuntimeError(f"Open-Meteo HTTP {r.status}")
                    self.wind = wind_summary(await r.json(content_type=None))
                self.wind_key, self.wind_ok, self.wind_next = key, now, now + WIND_POLL
            except Exception as ex:  # noqa: BLE001
                log.warning("[buienradar] wind failed: %s", ex)
                self.wind_next = now + 300
                if now - self.wind_ok > 4 * WIND_POLL:
                    self.wind = {}
        return self.wind

    async def _fetch(self, lat: float, lon: float) -> list[float]:
        async with self.session.get(BUIENRADAR_URL, params={"lat": f"{lat:.2f}", "lon": f"{lon:.2f}"},
                                    headers={"User-Agent": "stoeckie-bridge (SmartThings)"}) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            return parse_raintext(await r.text())

    async def _fetch_openmeteo(self, pts: list[tuple[float, float]]) -> list[list[float]]:
        """All points in one request (Open-Meteo accepts lists of coordinates)."""
        params = {"latitude": ",".join(f"{p[0]:.4f}" for p in pts), "longitude": ",".join(f"{p[1]:.4f}" for p in pts),
                  "minutely_15": "precipitation", "forecast_minutely_15": "9", "timezone": "UTC"}
        async with self.session.get(OPENMETEO_URL, params=params,
                                    headers={"User-Agent": f"stoeckie-bridge/{VERSION}"}) as r:
            if r.status != 200:
                raise RuntimeError(f"Open-Meteo HTTP {r.status}")
            return parse_openmeteo(await r.json(content_type=None))

    def _settings_state(self) -> dict[str, Any]:
        b = self.b
        return {"postcode": b.get("postcode"), "housenumber": b.get("housenumber"), "address": b.get("address"),
                "place": b.get("place"),
                "lat": b.get("lat"), "lon": b.get("lon"), "radiusM": b.get("radius_m", 0),
                "threshold": b.get("threshold", RAIN_THRESHOLD), "humidHours": b.get("humid_hours", HUMID_HOURS)}

    async def _locate(self) -> None:
        """Postcode + house number (NL) or an address (anywhere) -> lat/lon (once, stored in config.json)."""
        b = self.b
        if buienradar_configured(b):
            return
        if b.get("postcode") and b.get("housenumber"):
            found = await geocode_nl(self.session, b["postcode"], int(b["housenumber"]))
            what = f"{b['postcode']} {b['housenumber']}"
        elif b.get("address"):
            found = await geocode_address(self.session, b["address"])
            what = b["address"]
        else:
            return
        if not found:
            self.error = f"Address not found: {what}"
            return
        b["lat"], b["lon"], b["place"] = found
        cfg = load_config()
        cfg.setdefault("buienradar", {}).update({k: b[k] for k in ("lat", "lon", "place")})
        save_config(cfg)
        log.info("[buienradar] location from the address: %s", b["place"])

    async def _refresh(self) -> None:
        self.error = None
        await self._locate()
        if not buienradar_configured(self.b):
            summary = self.error or "No location: enter your address or coordinates in the device settings"
            REG.upsert("buienradar", "rainradar", "Buienradar", {"summary": summary, **self._settings_state()}, True,
                       self._command())
            return
        lat, lon = float(self.b["lat"]), float(self.b["lon"])
        radius = float(self.b.get("radius_m") or 0)
        pts = rain_points(lat, lon, radius)
        provider = rain_provider(self.b)
        if provider == "openmeteo":
            series = [s for s in await self._fetch_openmeteo(pts) if s]
        else:
            sem = asyncio.Semaphore(4)

            async def one(p):
                async with sem:
                    return await self._fetch(*p)
            series = [s for s in await asyncio.gather(*(one(p) for p in pts), return_exceptions=True)
                      if isinstance(s, list) and s]
        if not series:
            raise RuntimeError(f"no data from {provider}")
        n = min(len(s) for s in series)
        rates = [max(s[i] for s in series) for i in range(n)]  # heaviest rain anywhere in the radius
        state = rain_summary(rates, float(self.b.get("threshold") or RAIN_THRESHOLD))
        state.update(points=len(series), provider=provider, **self._settings_state())
        state.update(await self._humid_state(lat, lon))
        state.update(await self._wind_state(lat, lon))
        name = "Buienradar" if provider == "buienradar" else "Rain radar"
        REG.upsert("buienradar", "rainradar", name, state, True, self._command())
        log.info("[buienradar] %s (%d point(s), radius %g m)", state["summary"], len(series), radius)

    def apply_settings(self, v: dict[str, Any]) -> bool:
        """Store new settings; True when something changed (then the location is looked up again)."""
        cfg = load_config()
        cur = cfg.setdefault("buienradar", {})
        new = buienradar_settings(v, cur)
        changed = any(cur.get(k) != new.get(k) for k in new)
        if changed:
            cur.update(new)
            cur["enabled"] = True
            cur.pop("radius_km", None)
            save_config(cfg)
            self.b = dict(cur)
            log.info("[buienradar] settings changed: radius %s m, from %s mm/h", new.get("radius_m"), new.get("threshold"))
        return changed

    def _command(self):
        async def handler(cmd: str, value: Any) -> None:
            if cmd == "refresh":
                self.wake.set()
                return
            if cmd == "settings":  # from the SmartThings device settings
                if self.apply_settings(value if isinstance(value, dict) else {}):
                    self.wake.set()
                return
            raise ValueError(f"unsupported command {cmd}")
        return handler

    async def run(self) -> None:
        if not buienradar_enabled(self.b):
            log.info("[buienradar] off (optional: switch it on in the Stoeckie Bridge device or on the setup page)")
            return
        if "radius_m" not in self.b and self.b.get("radius_km") is not None:
            self.b["radius_m"] = int(float(self.b["radius_km"]) * 1000)
        import aiohttp
        BUIENRADAR["source"] = self
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            self.session = session
            while True:
                try:
                    await self._refresh()
                except Exception as ex:  # noqa: BLE001
                    log.warning("[buienradar] update failed: %s", ex)
                    REG.set_online("buienradar", False)
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=self.POLL)
                except asyncio.TimeoutError:
                    pass


BUIENRADAR: dict[str, Any] = {"source": None}


# ---------------------------------------------------------------- Home Connect (Bosch / Siemens / Neff / Gaggenau), local

HC_DIR = DATA / "homeconnect"
HC_TYPES = {"Dishwasher": "dishwasher", "Oven": "oven", "Washer": "washer", "Dryer": "hcdryer",
            "WasherDryer": "washer", "CoffeeMaker": "coffee", "Refrigerator": "fridge", "FridgeFreezer": "fridge"}
HC_OPERATION = {"Inactive": "inactive", "Ready": "ready", "DelayedStart": "delayedStart", "Run": "run",
                "Pause": "pause", "ActionRequired": "actionRequired", "Finished": "finished", "Error": "error",
                "Aborting": "aborting"}
# Home Connect program name -> key of the SmartThings capability silvervalley08660.applianceProgram (labels EN/NL in
# tools/presentation/gen_homeconnect.py). Programs not listed here are not offered in SmartThings.
_HC_DW = "Dishcare.Dishwasher.Program."
_HC_OV = "Cooking.Oven.Program."
HC_PROGRAMS = {
    _HC_DW + "Eco50": "eco50", _HC_DW + "Auto1": "auto1", _HC_DW + "Auto2": "auto2", _HC_DW + "Auto3": "auto3",
    _HC_DW + "Intensiv70": "intensive70", _HC_DW + "Intensiv45": "intensive45", _HC_DW + "Quick45": "quick45",
    _HC_DW + "Quick65": "quick65", _HC_DW + "Super60": "super60", _HC_DW + "Glas40": "glass40",
    _HC_DW + "NightWash": "night", _HC_DW + "MachineCare": "machineCare", _HC_DW + "PreRinse": "preRinse",
    "BSH.Common.Program.Favorite.001": "favorite",
    _HC_OV + "HeatingMode.HotAir": "hotAir", _HC_OV + "HeatingMode.HotAirEco": "hotAirEco",
    _HC_OV + "HeatingMode.TopBottomHeating": "topBottom", _HC_OV + "HeatingMode.TopBottomHeatingEco": "topBottomEco",
    _HC_OV + "HeatingMode.HotAirGrilling": "hotAirGrill", _HC_OV + "HeatingMode.PizzaSetting": "pizza",
    _HC_OV + "HeatingMode.SlowCook": "slowCook", _HC_OV + "HeatingMode.BottomHeating": "bottomHeat",
    _HC_OV + "HeatingMode.KeepWarm": "keepWarm", _HC_OV + "HeatingMode.PreheatOvenware": "preheatOvenware",
    _HC_OV + "HeatingMode.FrozenHeatupSpecial": "frozenFood", _HC_OV + "HeatingMode.GrillLargeArea": "grillLarge",
    _HC_OV + "HeatingMode.GrillSmallArea": "grillSmall", _HC_OV + "HeatingMode.SabbathProgramme": "sabbath",
    _HC_OV + "MicroCombiModes.HotAir": "microHotAir", _HC_OV + "MicroCombiModes.TopBottomHeating": "microTopBottom",
    _HC_OV + "MicroCombiModes.HotAirGrilling": "microHotAirGrill",
    _HC_OV + "MicroCombiModes.GrillLargeArea": "microGrillLarge",
    _HC_OV + "MicroCombiModes.GrillSmallArea": "microGrillSmall",
    _HC_OV + "Microwave.90Watt": "micro90", _HC_OV + "Microwave.180Watt": "micro180",
    _HC_OV + "Microwave.360Watt": "micro360", _HC_OV + "Microwave.600Watt": "micro600",
    _HC_OV + "Microwave.900Watt": "micro900", _HC_OV + "Microwave.Max": "microMax",
    _HC_OV + "Cleaning.Drying": "drying",
}
HC_PROGRAM_NAME = {v: k for k, v in HC_PROGRAMS.items()}
HC_DEFAULT = {"dishwasher": {"program": "eco50"}, "oven": {"program": "hotAir", "setpoint": 180, "duration": 0}}
HC_SETPOINT = "Cooking.Oven.Option.SetpointTemperature"
HC_DURATION = "BSH.Common.Option.Duration"


def homeconnect_profiles(folder: Path = HC_DIR) -> list[dict[str, Any]]:
    """Profiles from the Home Connect Profile Downloader ("Home Assistant - Home Connect Local" zips) in folder.
    Each: {haId, type, brand, model, connectionType, key, iv, xml: (description, feature mapping)}.
    The zips hold the appliance keys: they stay in this folder (same protection as config.json)."""
    import zipfile
    out = []
    if not folder.exists():
        return out
    for z in sorted(folder.glob("*.zip")):
        try:
            with zipfile.ZipFile(z) as zf:
                for n in zf.namelist():
                    if not n.endswith(".json"):
                        continue
                    info = json.loads(zf.read(n))
                    if not info.get("key") or not info.get("haId"):
                        continue
                    info["xml"] = (zf.read(info["deviceDescriptionFileName"]).decode("utf-8"),
                                   zf.read(info["featureMappingFileName"]).decode("utf-8"))
                    out.append(info)
        except Exception as ex:  # noqa: BLE001
            log.warning("[homeconnect] %s: not a usable profile (%s)", z.name, ex)
    return out


def hc_default_host(prof: dict[str, Any]) -> str:
    """mDNS name of an appliance: haId as is when it already starts with the brand, else brand-type-haId."""
    ha_id, brand = str(prof.get("haId")), str(prof.get("brand") or "")
    if brand and ha_id.upper().startswith(brand.upper() + "-"):
        return f"{ha_id}.local"
    return f"{brand}-{prof.get('type', '')}-{ha_id}.local".lower()


def homeconnect_configured(h: dict[str, Any] | None = None) -> bool:
    return any(HC_DIR.glob("*.zip")) if HC_DIR.exists() else False


def _hc_val(group: dict, name: str) -> Any:
    e = group.get(name)
    if e is None:
        return None
    try:
        return e.value
    except Exception:  # noqa: BLE001
        return None


def hc_programs(app) -> list[str]:
    """Keys of the programs this appliance offers that can be started (not "select only", like pyrolysis)."""
    out = []
    for name, prog in (getattr(app, "programs", None) or {}).items():
        key = HC_PROGRAMS.get(name)
        if not key or key in out:
            continue
        if str(getattr(prog, "execution", "selectandstart")).lower().endswith("selectonly"):
            continue
        if getattr(prog, "available", None) is False:
            continue
        out.append(key)
    return out


def _hc_prog_key(prog) -> str | None:
    try:
        return HC_PROGRAMS.get(prog.name) if prog else None
    except Exception:  # noqa: BLE001
        return None


def hc_start_options(prog, kind: str, sel: dict[str, Any]) -> dict[int, Any]:
    """Options for /ro/activeProgram: the writable options with a known value, plus (oven) the chosen temperature
    and, when set, the cooking time. Keyed by option uid, as the library sends them."""
    opts: dict[int, Any] = {}
    for o in getattr(prog, "_options", None) or []:
        name = getattr(o, "name", "")
        if kind == "oven":
            if name == HC_SETPOINT and sel.get("setpoint"):
                opts[o.uid] = int(sel["setpoint"])
            elif name == HC_DURATION and int(sel.get("duration") or 0) > 0:
                opts[o.uid] = int(sel["duration"]) * 60
            continue
        try:
            writable = str(getattr(o, "access", "")).lower() == "readwrite"
            val = o.value_shadow
        except Exception:  # noqa: BLE001
            continue
        if writable and val is not None and name != "BSH.Common.Option.StartInRelative":
            opts[o.uid] = val
    return opts


def hc_state(app, kind: str, sel: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flatten a homeconnect_websocket HomeAppliance into what the Edge driver needs."""
    sel = sel or {}
    power = _hc_val(app.settings, "BSH.Common.Setting.PowerState")
    op = _hc_val(app.status, "BSH.Common.Status.OperationState")
    door = _hc_val(app.status, "BSH.Common.Status.DoorState")
    active = None
    try:
        ap = app.active_program
        active = ap.name if ap else None
    except Exception:  # noqa: BLE001
        active = None
    running = op in ("Run", "Pause", "DelayedStart", "ActionRequired")
    remaining = _hc_val(app.options, "BSH.Common.Option.RemainingProgramTime") if running else None
    progress = _hc_val(app.options, "BSH.Common.Option.ProgramProgress") if running else None
    st = {
        "power": "on" if power == "On" else "off",
        "operation": HC_OPERATION.get(str(op), str(op).lower() if op else "inactive"),
        "door": str(door).lower() if door else None,                 # open closed locked
        "program": None,
        "remainingTime": int(remaining) if isinstance(remaining, (int, float)) else None,   # seconds
        "completionTime": _iso_in(float(remaining)) if isinstance(remaining, (int, float)) and running else None,
        "progress": int(progress) if isinstance(progress, (int, float)) else None,           # percent
        "remoteStartAllowed": bool(_hc_val(app.status, "BSH.Common.Status.RemoteControlStartAllowed")),
        "remoteControlActive": bool(_hc_val(app.status, "BSH.Common.Status.RemoteControlActive")),
    }
    # program: the running one, else the one selected on the appliance, else the one chosen in SmartThings
    key = HC_PROGRAMS.get(active) if active and running else None
    if not key:
        try:
            key = _hc_prog_key(app.selected_program)
        except Exception:  # noqa: BLE001
            key = None
    st["program"] = key or sel.get("program") or "none"
    st["programs"] = hc_programs(app)
    if kind == "oven":
        t = _hc_val(app.status, "Cooking.Oven.Status.CurrentCavityTemperature")
        sp = _hc_val(app.options, HC_SETPOINT)
        st["temperature"] = float(t) if isinstance(t, (int, float)) else None
        st["setpoint"] = float(sp) if isinstance(sp, (int, float)) and running else float(sel.get("setpoint") or 180)
        st["duration"] = int(sel.get("duration") or 0)    # minutes, 0 = no timer
    return st


class HomeConnect:
    """Home Connect appliances over their local websocket (library homeconnect-websocket, Python 3.13+).
    Keys and descriptions come from the Home Connect Profile Downloader zips in the homeconnect folder;
    the appliance address from config ("homeconnect": {"hosts": {haId: ip}}), homeconnect/hosts.json or the mDNS
    name (give the appliances a fixed IP in the router)."""

    POLL = 5

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.h = dict(cfg.get("homeconnect") or {})
        # appliance addresses: config "homeconnect.hosts", or hosts.json next to the profile zips
        hosts = dict(self.h.get("hosts") or {})
        try:
            hosts.update(json.loads((HC_DIR / "hosts.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        self.h["hosts"] = hosts
        self.apps: dict[str, Any] = {}
        self.kinds: dict[str, str] = {}
        self.wake: dict[str, asyncio.Event] = {}
        # program, oven temperature and cooking time chosen in SmartThings, per appliance (kept in config.json)
        self.sel: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (self.h.get("selection") or {}).items()}

    def selection(self, ha_id: str) -> dict[str, Any]:
        cur = self.sel.setdefault(ha_id, {})
        for k, v in HC_DEFAULT.get(self.kinds.get(ha_id, ""), {}).items():
            cur.setdefault(k, v)
        return cur

    def _save_selection(self) -> None:
        cfg = load_config()
        cfg.setdefault("homeconnect", {})["selection"] = self.sel
        save_config(cfg)

    async def _start(self, ha_id: str, app, key: str | None) -> None:
        sel = self.selection(ha_id)
        op = _hc_val(app.status, "BSH.Common.Status.OperationState")
        if op == "Pause" and not key:
            c = app.commands.get("BSH.Common.Command.ResumeProgram")
            if c is not None:
                await c.execute(1)
                return
        if not _hc_val(app.status, "BSH.Common.Status.RemoteControlStartAllowed"):
            raise ValueError("remote start is not allowed: switch on Remote Start on the appliance first")
        if key:
            sel["program"] = key
        key = sel.get("program")
        prog = app.programs.get(HC_PROGRAM_NAME.get(key or "", "")) if key and key != "none" else None
        if prog is None:
            try:
                prog = app.selected_program
            except Exception:  # noqa: BLE001
                prog = None
        if prog is None:
            raise ValueError(f"program {key} not available on this appliance")
        opts = hc_start_options(prog, self.kinds.get(ha_id, ""), sel)
        await prog.start(opts, override_options=True)
        log.info("[homeconnect] %s start %s (%d options)", ha_id, _hc_prog_key(prog) or prog.name, len(opts))

    def _app_id(self) -> str:
        cfg = load_config()
        h = cfg.setdefault("homeconnect", {})
        if not h.get("app_id"):
            h["app_id"] = secrets.token_hex(8)
            save_config(cfg)
        return h["app_id"]

    def _command(self, ha_id: str):
        async def handler(cmd: str, value: Any) -> None:
            app = self.apps.get(ha_id)
            if app is None:
                raise ValueError("appliance not connected")
            if cmd == "refresh":
                return
            if cmd in ("on", "off"):
                e = app.settings.get("BSH.Common.Setting.PowerState")
                if e is None:
                    raise ValueError("no power setting")
                values = list((e.enum or {}).values())
                target = "On" if cmd == "on" else ("Off" if "Off" in values else "Standby")
                await e.set_value(target)
            elif cmd == "program":
                key = str(value or "")
                if key not in HC_PROGRAM_NAME:
                    raise ValueError(f"unknown program {key}")
                self.selection(ha_id)["program"] = key
                self._save_selection()
                prog = app.programs.get(HC_PROGRAM_NAME[key])
                if prog is not None and _hc_val(app.status, "BSH.Common.Status.RemoteControlActive"):
                    try:  # also show it on the appliance; not possible on every appliance or in every state
                        await prog.select()
                    except Exception as ex:  # noqa: BLE001
                        log.debug("[homeconnect] %s select %s: %s", ha_id, key, ex)
            elif cmd == "setpoint":
                self.selection(ha_id)["setpoint"] = max(30, min(300, int(round(float(value)))))
                self._save_selection()
            elif cmd == "duration":
                self.selection(ha_id)["duration"] = max(0, min(1440, int(round(float(value)))))
                self._save_selection()
            elif cmd == "start":
                if value and str(value) not in HC_PROGRAM_NAME:
                    raise ValueError(f"unknown program {value}")
                await self._start(ha_id, app, str(value) if value else None)
                if value:
                    self._save_selection()
            elif cmd in ("stop", "pause", "resume"):
                name = {"stop": "BSH.Common.Command.AbortProgram", "pause": "BSH.Common.Command.PauseProgram",
                        "resume": "BSH.Common.Command.ResumeProgram"}[cmd]
                c = app.commands.get(name)
                if c is None:
                    raise ValueError(f"{cmd} not supported by this appliance")
                await c.execute(1)
            else:
                raise ValueError(f"unsupported command {cmd}")
            log.info("[homeconnect] %s %s %s", ha_id, cmd, "" if value is None else value)
            ev = self.wake.get(ha_id)
            if ev is not None:
                ev.set()
        return handler

    async def _run_one(self, prof: dict[str, Any], app_id: str, session) -> None:
        from homeconnect_websocket import HomeAppliance, parse_device_description
        ha_id = prof["haId"]
        kind = HC_TYPES.get(prof.get("type"), "appliance")
        self.kinds[ha_id] = kind
        wake = self.wake[ha_id] = asyncio.Event()
        dev_id = f"homeconnect-{ha_id}"
        name = f"{prof.get('brand', '').title()} {prof.get('type', 'Appliance')}".strip()
        host = (self.h.get("hosts") or {}).get(ha_id) or hc_default_host(prof)
        desc = parse_device_description(*prof["xml"])
        state = {"connected": False}

        async def on_conn(new_state) -> None:
            up = str(new_state).lower() == "connected"
            state["connected"] = up
            REG.set_online(dev_id, up)
            log.info("[homeconnect] %s (%s): %s", name, host, str(new_state).split(".")[-1].lower())

        app = HomeAppliance(desc, host, "Stoeckie Bridge", app_id, prof["key"], prof.get("iv") or None,
                            session=session, logger=log, reconect=True, connection_callback=on_conn)
        REG.upsert(dev_id, kind, name, {"model": prof.get("vib"), "brand": prof.get("brand")}, online=False,
                   handler=self._command(ha_id))
        while True:
            try:
                await app.connect()
                break
            except Exception as ex:  # noqa: BLE001
                log.warning("[homeconnect] %s (%s): connect failed: %s, retry in 60 s", name, host, ex)
                await asyncio.sleep(60)
        self.apps[ha_id] = app
        last = None
        while True:
            try:
                st = hc_state(app, kind, self.selection(ha_id))
                if st != last:
                    REG.upsert(dev_id, kind, name, st, online=state["connected"])
                    last = st
                else:
                    REG.set_online(dev_id, state["connected"])
            except Exception as ex:  # noqa: BLE001
                log.debug("[homeconnect] %s: state: %s", name, ex)
            try:  # next check after POLL seconds, or right after a command
                await asyncio.wait_for(wake.wait(), self.POLL)
                await asyncio.sleep(0.5)
            except asyncio.TimeoutError:
                pass
            wake.clear()

    async def run(self) -> None:
        profiles = homeconnect_profiles()
        if not profiles:
            return
        if sys.version_info < (3, 13):
            log.warning("[homeconnect] %d profile(s) found, but local Home Connect needs Python 3.13 or newer "
                        "(this is %d.%d): run the installer again", len(profiles), *sys.version_info[:2])
            return
        import aiohttp
        app_id = self._app_id()
        log.info("[homeconnect] %d appliance(s): %s", len(profiles),
                 ", ".join(f"{p.get('type')} {p.get('vib')} ({p.get('connectionType')})" for p in profiles))
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(*(self._run_one(p, app_id, session) for p in profiles))


# ---------------------------------------------------------------- Samsung appliances (OCF over CoAP-DTLS), local

SS_DIR = DATA / "samsung"
# The client identity the factory ACL of compatible Samsung appliances grants access to (a public constant, see
# github.com/QuiteYellow/SmartThings-Local docs/certificates.md). The appliance checks only this UUID in the
# certificate subject; the key and the self-signed certificate are made here and never leave this folder.
SS_CLIENT_UUID = "ab0b0ac4-aae9-4958-a04d-8ec36fe1b2f9"
SS_PORTS = [5684] + list(range(49152, 49161))


def samsung_cert(folder: Path | None = None) -> tuple[Path, Path]:
    """Client certificate for local Samsung access (made once): RSA 2048, self-signed, UUID in OU, CN and SAN."""
    folder = folder or SS_DIR
    cert_p, key_p = folder / "client_fullchain.pem", folder / "client.key"
    if cert_p.exists() and key_p.exists():
        return cert_p, key_p
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    folder.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    u = SS_CLIENT_UUID
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, f"uuid:{u}"),
                      x509.NameAttribute(NameOID.COMMON_NAME, f"urn:uuid:{u}")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True, content_commitment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .add_extension(x509.SubjectAlternativeName([
                x509.UniformResourceIdentifier(f"urn:uuid:{u}"), x509.UniformResourceIdentifier(f"uri:uuid:{u}"),
                x509.UniformResourceIdentifier(f"uuid:{u}"), x509.DNSName(u)]), critical=False)
            .sign(key, hashes.SHA256()))
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                        serialization.NoEncryption()))
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_p, key_p


# Washer course codes per course table (from /st/washercourse/vs/0), from the localthings project
# (github.com/mbillow/localthings, MIT), confirmed there by reading back courses chosen on the machines.
SS_WASHER_COURSES = {
    "Table_00": {"01": "Normal", "55": "Whites", "57": "Self Clean+", "5b": "Cotton", "5c": "Extra Speed", "5d": "Super Eco Wash", "5e": "Delicates", "5f": "Baby Care", "60": "Outdoor", "61": "Dark Garment", "63": "Drum Clean", "64": "Rinse + Spin", "65": "Wool", "66": "Bedding", "67": "Synthetics", "68": "E Cotton", "6b": "Spin Only", "6c": "Denim", "70": "Heavy Duty", "71": "Bedding", "72": "Sanitize", "73": "Rinse + Spin", "74": "Active Wear", "75": "Delicates", "77": "Perm Press", "78": "Quick Wash", "91": "Self Clean", "94": "Towels", "9c": "Heavy Duty", "9d": "Rinse + Spin", "9e": "Perm Press", "9f": "Quick Wash", "a2": "Delicates", "a3": "Active Wear", "a5": "Bedding", "a9": "Sanitize", "aa": "Whites", "ab": "Eco Cold", "b1": "Self Clean+", "ba": "Drain/Spin", "d0": "Cotton", "d1": "E Cotton", "d2": "Synthetics", "d3": "Delicates", "d4": "Rinse + Spin", "d5": "Drum Clean", "d6": "Bedding", "d7": "Outdoor Care", "d8": "Wool", "da": "Super Eco Wash", "db": "Super Speed", "dc": "15' Quick Wash", "e4": "Anti Allergen"},
    "Table_02": {"01": "Normal", "02": "Extra Heavy Duty", "03": "Super Eco Wash", "04": "Quick Wash", "05": "Wool/Lingerie", "06": "Bedding", "07": "Outdoor", "08": "Rinse+Spin", "09": "Drum Clean", "0a": "Towels", "0b": "Boil Wash", "0c": "Baby Care", "0d": "Spin Only", "0e": "Cloudy Day", "0f": "Pure Wash", "10": "Spin Dry", "11": "Summer Bedding", "12": "Cottons", "13": "Black Cottons", "14": "Delicate Underwear", "15": "Activewear", "16": "Blouses", "17": "Downloaded", "18": "Soft Bubble", "19": "AI Wash", "1a": "Shirts", "1b": "Cotton", "1c": "Eco 40-60", "1d": "Super Speed", "1e": "15' Quick Wash", "1f": "Intense Cold", "20": "Hygiene Steam", "21": "Colors", "22": "Wool", "23": "Outdoor", "24": "Bedding", "25": "Synthetics", "26": "Delicates", "27": "Rinse+Spin", "28": "Drain/Spin", "29": "Drum Clean+", "2a": "Jeans", "2b": "AI Wash", "2d": "Silent Wash", "2e": "Baby Care", "2f": "Activewear", "30": "Cloudy Day", "32": "Shirts", "33": "Towels", "34": "Mixed", "35": "E Cotton", "36": "Wash+Dry", "37": "Air Wash", "38": "Cotton Dry", "39": "Synthetics Dry", "3a": "Drum Clean", "51": "Super Speed", "52": "Eco Cold", "53": "Heavy Duty", "54": "Towels", "55": "Activewear", "56": "Bedding", "57": "Delicates", "5a": "Steam Sanitize", "5b": "Small Load", "5e": "Rinse+Spin", "60": "Self Clean+", "64": "Steam Whites", "65": "Colors", "66": "Denim", "69": "AI Wash", "6a": "Wool", "6b": "Denim", "6c": "Blouses", "6d": "Delicates", "6e": "Active Wear", "6f": "Bedding", "70": "Towels", "71": "Quick Wash", "72": "Shirts", "73": "Sanitize", "74": "Drum Clean", "75": "Outdoor", "76": "Baby Care", "77": "Cottons", "78": "Rinse + Spin", "79": "Spin Only", "7c": "Whites", "7d": "Bedding/Waterproof", "7e": "Self-Clean", "7f": "Wool/Delicate", "85": "Steam Normal", "86": "Deep Wash", "87": "Download", "88": "Pet Care", "8b": "Quick Wash", "8c": "AI OptiWash", "8f": "Intense Cold", "96": "Less Microfiber", "a0": "15' Quick Wash", "b0": "Mixed Load", "58": "Wool", "5c": "Outdoor", "5d": "Power Rinse", "5f": "Spin Only", "63": "Power Steam", "67": "Steam Allergen", "68": "Steam Bulky"},
}
# Course label (above) -> key of the SmartThings capability silvervalley08660.applianceProgram (EN/NL labels in
# tools/presentation/gen_homeconnect.py). Courses without a key here are not offered in SmartThings.
SS_COURSE_KEY = {
    "Cotton": "cotton", "Cottons": "cotton", "E Cotton": "eCotton", "Eco 40-60": "eco4060",
    "Synthetics": "synthetics", "Delicates": "delicates", "Wool": "wool", "Bedding": "bedding",
    "Outdoor": "outdoor", "Outdoor Care": "outdoor", "Baby Care": "babyCare", "Anti Allergen": "antiAllergen",
    "15' Quick Wash": "quick15", "Quick Wash": "quickWash", "Super Speed": "superSpeed",
    "Super Eco Wash": "superEco", "Mixed Load": "mixed", "Mixed": "mixed", "Towels": "towels", "Denim": "denim",
    "Jeans": "denim", "Shirts": "shirts", "Active Wear": "activeWear", "Activewear": "activeWear",
    "Colors": "colors", "Whites": "whites", "AI Wash": "aiWash", "Hygiene Steam": "hygieneSteam",
    "Rinse + Spin": "rinseSpin", "Rinse+Spin": "rinseSpin", "Spin Only": "spinOnly", "Drain/Spin": "drainSpin",
    "Drum Clean": "drumClean", "Drum Clean+": "drumClean",
}
# Table_00 course BD is not in the table above; on EU washers it is Eco 40-60 (fixed 40 degrees, 3 rinses)
SS_EXTRA = {"Table_00": {"bd": "Eco 40-60"}}


def ss_course_codes(course: dict[str, Any], edit: dict[str, Any] | None = None) -> list[str]:
    """Course codes the washer offers: editCourseList when filled in, else the first byte of each record of
    supportedOptions (a 1 nibble header with the record width, then fixed width records), checked like the
    localthings project does: distinct codes that include the selected course."""
    raw = (edit or {}).get("x.com.samsung.da.editCourseList")
    if isinstance(raw, str) and "_" in raw:
        body = raw.split("_", 1)[1]
        codes = [body[i:i + 2] for i in range(0, len(body) - 1, 2)]
        if codes:
            return codes
    raw = course.get("x.com.samsung.da.supportedOptions")
    hexstr = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(hexstr, str) or len(hexstr) < 3 or (len(hexstr) - 1) % 2:
        return []
    body = hexstr[1:]
    total = len(body) // 2
    current = ss_option(course.get(SS_OPTIONS), "Course")

    def split(k: int) -> list[str] | None:
        if k < 1 or total % k or total // k < 2:
            return None
        firsts = [body[i * k * 2:i * k * 2 + 2] for i in range(total // k)]
        if len(set(firsts)) != len(firsts):
            return None
        if current is not None and current.lower() not in {f.lower() for f in firsts}:
            return None
        return firsts

    try:
        stated = split(1 + 2 * int(hexstr[0], 16))
    except ValueError:
        stated = None
    if stated:
        return stated
    for k in range(1, total + 1):
        got = split(k)
        if got:
            return got
    return []


SS_JOB = {"none": "none", "wash": "wash", "rinse": "rinse", "spin": "spin", "finish": "finish",
          "prewash": "preWash", "weightsensing": "weightSensing", "delaywash": "delayWash", "airwash": "airWash",
          "drying": "drying", "cooling": "cooling", "wrinkleprevent": "wrinklePrevent", "aiwash": "aIWash",
          "airinse": "aIRinse", "aispin": "aISpin", "freezeprotection": "freezeProtection"}
SS_OPTIONS = "x.com.samsung.da.options"


def ss_option(options: Any, prefix: str) -> str | None:
    """'<prefix>_<value>' from an options[] array."""
    for o in options or []:
        if isinstance(o, str) and o.startswith(prefix + "_"):
            return o.split("_", 1)[1]
    return None


def ss_seconds(raw: Any) -> int | None:
    """'HH:MM:SS' or 'MM:SS' -> seconds."""
    if not isinstance(raw, str):
        return None
    try:
        parts = [int(x) for x in raw.split(":")]
    except ValueError:
        return None
    if len(parts) == 2:
        parts = [0] + parts
    if len(parts) != 3:
        return None
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def ss_course_label(code: str | None, table: str | None) -> str | None:
    if not code:
        return None
    c = code.lower()
    return ((SS_WASHER_COURSES.get(table or "") or {}).get(c) or (SS_EXTRA.get(table or "") or {}).get(c)
            or f"Course {code.upper()}")


def ss_programs(res: dict[str, Any]) -> dict[str, str]:
    """{capability key: course code} for the courses of this washer that SmartThings can show."""
    table = (res.get("/st/washercourse/vs/0") or {}).get("x.com.samsung.da.st.courseTable")
    out: dict[str, str] = {}
    for code in ss_course_codes(res.get("/course/vs/0") or {}, res.get("/wm/editcourse/vs/0")):
        key = SS_COURSE_KEY.get(ss_course_label(code, table) or "")
        if key and key not in out:
            out[key] = code
    return out


def ss_washer_state(res: dict[str, Any]) -> dict[str, Any]:
    """Flatten the washer resources (CBOR decoded, keyed by href) into what the Edge driver needs."""
    op = res.get("/operational/state/vs/0") or {}
    pw = res.get("/power/vs/0") or {}
    p0 = res.get("/power/0") or {}
    rc = res.get("/remotectrl/vs/0") or {}
    course = res.get("/course/vs/0") or {}
    table = (res.get("/st/washercourse/vs/0") or {}).get("x.com.samsung.da.st.courseTable")
    if "x.com.samsung.da.power" in pw:
        power = str(pw.get("x.com.samsung.da.power")).lower() == "on"
    elif "value" in p0:
        power = bool(p0.get("value"))
    else:
        power = bool(op)
    st = str(op.get("x.com.samsung.da.state") or "").lower()
    progress = str(op.get("x.com.samsung.da.progress") or "None")
    running = st in ("run", "running") and progress != "Finish"
    paused = st in ("pause", "paused")
    finished = progress == "Finish"
    remaining = ss_seconds(op.get("x.com.samsung.da.remainingTime")) if (running or paused) else None
    pct = op.get("x.com.samsung.da.progressPercentage")
    try:
        pct = int(pct) if (running or paused) and pct is not None else None
    except (TypeError, ValueError):
        pct = None
    code = ss_option(course.get(SS_OPTIONS), "Course")
    progs = ss_programs(res)
    key = next((k for k, c in progs.items() if code and c.lower() == code.lower()), None)
    return {
        "program": key or ("none" if not progs else None),
        "programs": list(progs),
        "power": "on" if power else "off",
        "operation": "run" if running else ("pause" if paused else ("finished" if finished else "inactive")),
        "job": "finish" if finished else (SS_JOB.get(progress.lower(), "none") if (running or paused) else "none"),
        "remainingTime": remaining,
        "completionTime": _iso_in(float(remaining)) if remaining and running else None,
        "progress": pct,
        "remoteStartAllowed": str(rc.get("x.com.samsung.da.remoteControlEnabled")).lower() == "true",
        "course": ss_course_label(code, table),
        "courseCode": code,
    }


# what a washer that does not answer any more (switched off) shows, when it was not washing
SS_OFF = {"power": "off", "remoteStartAllowed": False, "operation": "inactive", "job": "none", "remainingTime": None,
          "completionTime": None, "progress": None}


class Samsung:
    """Samsung appliances on the LAN (OCF over CoAP-DTLS, library smartthings-local). Washers for now.
    Appliances come from config ("samsung": {"appliances": [{"host": ip, "type": "washer", "name": ...}]}) or
    samsung/appliances.json; give them a fixed IP. A washer switched off sleeps its wifi: it then shows as off."""

    POLL = 15          # power, remote control
    HOT_EVERY = 4      # operational state (this washer answers slowly when asked every second)
    PING = 25          # CoAP ping, keeps the session alive
    LIVENESS = 60      # no answer at all this long: new session
    RETRY = 30         # wait before a new session after a failure
    # small resources every POLL seconds; the course resource is large (several CoAP blocks) and the firmware
    # stops answering for a while when it is read too often, so it is read at connect, every COURSE_EVERY seconds
    # and right after a command
    # The firmware drops a DTLS session that is idle for about 10 s (it then also ignores a new handshake for a
    # while), so the small operational state is read every HOT seconds, like the smartthings-local bridge demo.
    HOT = (("operational", "state", "vs", "0"),)
    RESOURCES = (("power", "vs", "0"), ("remotectrl", "vs", "0"))
    COURSE = (("course", "vs", "0"),)
    COURSE_EVERY = 300
    ONCE = (("st", "washercourse", "vs", "0"),)

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        apps = list((cfg.get("samsung") or {}).get("appliances") or [])
        try:
            apps += json.loads((SS_DIR / "appliances.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.appliances = [a for a in apps if isinstance(a, dict) and a.get("host") and a.get("type", "washer") != "tv"]
        self.sessions: dict[str, Any] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.programs: dict[str, dict[str, str]] = {}   # host -> {capability key: course code}
        self.wake: dict[str, asyncio.Event] = {}
        self.executors: dict[str, Any] = {}

    async def _call(self, host: str, fn, *args):
        """Run a blocking library call on this appliance's own thread: the DTLS session is made, read and written
        from one thread only."""
        ex = self.executors.get(host)
        if ex is None:
            import concurrent.futures
            ex = self.executors[host] = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="samsung")
        return await asyncio.get_running_loop().run_in_executor(ex, lambda: fn(*args))

    @staticmethod
    def dev_id(host: str) -> str:
        return "samsung-" + host.replace(".", "-")

    def _open(self, host: str):
        """Find the DTLS port and open an authenticated session (blocking, run in a thread)."""
        from smartthings_local.protocol import dtls_probe
        from smartthings_local.protocol.auth import CertificateAuth
        from smartthings_local.protocol.dtls_session import DtlsCoapSession
        cert_p, key_p = samsung_cert()
        live = dtls_probe.probe_dtls_ports(host, SS_PORTS)
        port = live.selected_port or (live.responder_ports[0] if live.responder_ports else None)
        if not port:
            raise ConnectionError("no answer (switched off?)")
        # a fixed local UDP port per appliance (like the smartthings-local bridge demo): a reconnect then comes from
        # the same peer address, so the appliance replaces the old association instead of waiting for it to expire
        local = 26849 + sorted(a["host"] for a in self.appliances).index(host) if any(
            a["host"] == host for a in self.appliances) else None
        sess = DtlsCoapSession(host, port, auth=CertificateAuth.from_files(str(cert_p), str(key_p)), local_port=local)
        sess.connect(timeout=12.0)
        sess.start_reader()
        return sess, port

    @staticmethod
    def _read(sess, paths, timeout: float = 4.0) -> dict[str, Any]:
        """GET each path; a path that times out is skipped (the firmware sometimes wedges on one resource for a
        while). Raises only when nothing answered, so one slow resource does not tear the session down."""
        import cbor2
        out: dict[str, Any] = {}
        errors = 0
        last: Exception | None = None
        for path in paths:
            try:
                code, body = sess.get(list(path), timeout=timeout)
            except Exception as ex:  # noqa: BLE001
                errors += 1
                last = ex
                log.debug("[samsung] GET /%s: %r", "/".join(path), ex)
                if "closed" in str(ex).lower():
                    raise
                continue
            if code == 0x45 and body:
                try:
                    out["/" + "/".join(path)] = cbor2.loads(body)
                except Exception:  # noqa: BLE001
                    pass
        if paths and errors == len(paths):
            raise last or ConnectionError("no answer")
        return out

    @staticmethod
    def _write(sess, path, body: dict[str, Any]) -> None:
        import cbor2
        code, _ = sess.post(list(path), cbor2.dumps(body))
        if code >> 5 != 2:
            raise ValueError(f"appliance refused ({code >> 5}.{code & 0x1F:02d}); starting, pausing and changing "
                             "the course need Smart Control switched on at the washer")

    def _command(self, host: str):
        async def handler(cmd: str, value: Any) -> None:
            sess = self.sessions.get(host)
            if cmd == "refresh":
                return
            if sess is None:
                raise ValueError("washer not reachable (switched off?)")
            op = ("operational", "state", "vs", "0")
            body = {"run": {"x.com.samsung.da.state": "Run"}, "start": {"x.com.samsung.da.state": "Run"},
                    "program": None,
                    "resume": {"x.com.samsung.da.state": "Run"}, "pause": {"x.com.samsung.da.state": "Pause"},
                    "stop": {"x.com.samsung.da.state": "Ready"}}.get(cmd)
            async with self.locks[host]:
                if cmd in ("program", "start") and value:
                    code = self.programs.get(host, {}).get(str(value))
                    if not code:
                        raise ValueError(f"program {value} not available on this washer")
                    await self._call(host, self._write, sess, ("course", "vs", "0"), {SS_OPTIONS: [f"Course_{code}"]})
                    if cmd == "program":
                        body = None
                if body is not None:
                    await self._call(host, self._write, sess, op, body)
                elif cmd == "program":
                    pass
                elif cmd in ("on", "off"):
                    try:
                        await self._call(host, self._write, sess, ("power", "vs", "0"),
                                                {"x.com.samsung.da.power": "On" if cmd == "on" else "Off"})
                    except ValueError:
                        await self._call(host, self._write, sess, ("power", "0"), {"value": cmd == "on"})
                else:
                    raise ValueError(f"unsupported command {cmd}")
            log.info("[samsung] %s %s %s", host, cmd, "" if value is None else value)
            ev = self.wake.get(host)
            if ev is not None:
                ev.set()
        return handler

    @staticmethod
    def _read_soft(sess, paths, timeout: float) -> dict[str, Any]:
        """Like _read, but never raises for timeouts: a GET the firmware does not answer is just skipped."""
        try:
            return Samsung._read(sess, paths, timeout)
        except Exception as ex:  # noqa: BLE001
            if "closed" in str(ex).lower():
                raise
            return {}

    async def _run_one(self, a: dict[str, Any]) -> None:
        """Like the smartthings-local bridge demo: the small operational state every second (2 s timeout), the
        rest at a slower pace, and a GET that times out is skipped instead of tearing the session down (the
        firmware sometimes stops answering for a few seconds, and a torn down session makes it ignore new
        handshakes for a while). Only after LIVENESS seconds without any answer is the session closed and made
        again; a CoAP ping every PING seconds keeps it alive."""
        host = str(a["host"])
        kind = str(a.get("type") or "washer")
        name = str(a.get("name") or "Samsung Washer")
        dev_id = self.dev_id(host)
        self.locks[host] = asyncio.Lock()
        wake = self.wake[host] = asyncio.Event()
        handler = self._command(host)
        # offline and without a state until the first answer: a bridge restart then does not show the washer as
        # switched off with no course for a moment
        REG.upsert(dev_id, kind, name, {}, online=False, handler=handler)
        once: dict[str, Any] = {}
        cache: dict[str, Any] = {}
        last, fails, logged = None, 0, False
        while True:
            sess = self.sessions.get(host)
            if sess is None:
                try:
                    sess, port = await self._call(host, self._open, host)
                except Exception as ex:  # noqa: BLE001
                    fails += 1
                    if fails <= 3 or fails % 30 == 0:
                        log.info("[samsung] %s (%s): %r", name, host, ex)
                    if fails >= 2:
                        # no answer: switched off (its wifi sleeps) or another local client holds the only session.
                        # The device shows offline. When it was not washing (or did not answer at all since the
                        # bridge started), it also shows off and without remote control (a switched off washer can not
                        # be started); when the connection drops during a wash the last state stays.
                        if not last or last.get("operation") not in ("run", "pause"):
                            last = {**(last or {}), **SS_OFF}
                            REG.upsert(dev_id, kind, name, last, online=False)
                        else:
                            REG.set_online(dev_id, False)
                    await asyncio.sleep(self.RETRY if fails < 10 else 2 * self.RETRY)
                    continue
                self.sessions[host] = sess
                REG.set_online(dev_id, True)
                log.info("[samsung] %s (%s): connected on port %s", name, host, port)
                fails = 0
                now = time.monotonic()
                ok_at, warm_at, course_at, ping_at = now, 0.0, 0.0, now
                timeouts = 0
            try:
                now = time.monotonic()
                cmd = wake.is_set()
                wake.clear()
                async with self.locks[host]:
                    fresh = await self._call(host, self._read_soft, sess, self.HOT, 4.0)
                    if now - warm_at >= self.POLL or cmd:
                        fresh.update(await self._call(host, self._read_soft, sess, self.RESOURCES, 4.0))
                        warm_at = now
                    if not once:
                        once = await self._call(host, self._read_soft, sess, self.ONCE, 4.0)
                    if now - course_at >= self.COURSE_EVERY or cmd or "/course/vs/0" not in cache:
                        got = await self._call(host, self._read_soft, sess, self.COURSE, 20.0)
                        if got:
                            fresh.update(got)
                            course_at = now
                        else:
                            course_at = now - self.COURSE_EVERY + 30   # try again in 30 s
                    if now - ping_at >= self.PING:
                        ping_at = now
                        try:
                            await self._call(host, sess.ping)
                        except Exception:  # noqa: BLE001
                            pass
                if fresh:
                    ok_at = now
                    if timeouts >= 3:  # single skipped reads are normal for this firmware; only log real gaps
                        log.info("[samsung] %s: answering again after %d skipped read(s)", name, timeouts)
                    timeouts = 0
                else:
                    timeouts += 1
                    if timeouts in (3, 10, 30):
                        log.info("[samsung] %s: no answer to %d read(s) in a row", name, timeouts)
                if now - ok_at > self.LIVENESS:
                    raise ConnectionError(f"no answer for {self.LIVENESS} s")
                if fresh:
                    res = {**cache, **fresh, **once}
                    cache = res
                    if not logged:  # once, to learn this model's resources (no secrets in them)
                        logged = True
                        log.info("[samsung] %s resources: %s", name, _ss_short(res, 3000))
                    st = ss_washer_state(res)
                    self.programs[host] = ss_programs(res)
                    if st != last:
                        REG.upsert(dev_id, kind, name, st, online=True)
                        last = st
            except Exception as ex:  # noqa: BLE001
                log.info("[samsung] %s (%s): %r, new session", name, host, ex)
                try:
                    await self._call(host, sess.close)
                except Exception:  # noqa: BLE001
                    pass
                self.sessions.pop(host, None)
                await asyncio.sleep(self.RETRY)
                continue
            try:  # next read after HOT_EVERY seconds, or right after a command
                await asyncio.wait_for(wake.wait(), self.HOT_EVERY)
                await asyncio.sleep(0.5)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        if not self.appliances:
            return
        try:
            import smartthings_local  # noqa: F401
        except ImportError:
            log.warning("[samsung] %d appliance(s) configured, but smartthings-local is not installed: run the "
                        "installer again", len(self.appliances))
            return
        log.info("[samsung] %d appliance(s): %s", len(self.appliances),
                 ", ".join(f"{a.get('type', 'washer')} {a['host']}" for a in self.appliances))
        await asyncio.gather(*(self._run_one(a) for a in self.appliances))


# ---------------------------------------------------------------- Samsung TV (Tizen, 2016 and later), local

TV_INPUTS = {"HDMI1": "KEY_HDMI1", "HDMI2": "KEY_HDMI2", "HDMI3": "KEY_HDMI3", "HDMI4": "KEY_HDMI4",
             "digitalTv": "KEY_DTV"}
TV_RC = "/upnp/control/RenderingControl1"


def tv_soap(action: str, args: str = "") -> tuple[dict[str, str], str]:
    """Headers and body of a UPnP RenderingControl call (volume and mute; the TV's own DMR service, port 9197)."""
    body = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1"><InstanceID>0</InstanceID>'
            f'<Channel>Master</Channel>{args}</u:{action}></s:Body></s:Envelope>')
    headers = {"Content-Type": 'text/xml; charset="utf-8"',
               "SOAPACTION": f'"urn:schemas-upnp-org:service:RenderingControl:1#{action}"'}
    return headers, body


def tv_xml_value(xml: str, tag: str) -> str | None:
    i = xml.find(f"<{tag}>")
    j = xml.find(f"</{tag}>", i)
    return xml[i + len(tag) + 2:j].strip() if i >= 0 and j > i else None


def wol_packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    return b"\xff" * 6 + raw * 16


class SamsungTV:
    """Samsung TVs on the LAN: power state from http://tv:8001/api/v2/, volume and mute over UPnP (port 9197),
    keys (power, input, channel) over the TV's remote control websocket (wss, port 8002). The first key makes the TV
    ask "Allow" on screen; the token it then gives is kept in config.json (samsungtv.tokens). Power on from standby
    sends the power key and a wake-on-LAN packet (needs "Power on with mobile" / network standby on the TV).
    TVs come from samsung/appliances.json (type "tv", host, mac, name) or config "samsungtv": {"tvs": [...]}."""
    WS_PORT = 8002

    POLL = 5
    NAME = "Stoeckie Bridge"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        tvs = list((cfg.get("samsungtv") or {}).get("tvs") or [])
        try:
            tvs += [a for a in json.loads((SS_DIR / "appliances.json").read_text(encoding="utf-8"))
                    if isinstance(a, dict) and a.get("type") == "tv"]
        except (OSError, ValueError):
            pass
        self.tvs = [t for t in tvs if isinstance(t, dict) and t.get("host")]
        self.tokens: dict[str, str] = dict((cfg.get("samsungtv") or {}).get("tokens") or {})
        # SHA-256 of each TV's self-signed certificate, kept from the first connection: the token only goes to
        # a TV that shows the same certificate again (another device at the TV's address does not get it)
        self.certs: dict[str, str] = dict((cfg.get("samsungtv") or {}).get("certs") or {})
        self.session = None
        self.wake: dict[str, asyncio.Event] = {}
        self.last: dict[str, dict[str, Any]] = {}
        self.pair_now: dict[str, bool] = {}

    @staticmethod
    def dev_id(host: str) -> str:
        return "samsungtv-" + host.replace(".", "-")

    def _save_token(self, host: str, token: str) -> None:
        self.tokens[host] = token
        cfg = load_config()
        cfg.setdefault("samsungtv", {}).setdefault("tokens", {})[host] = token
        save_config(cfg)
        log.info("[samsungtv] %s: paired", host)

    def _save_cert(self, host: str, fp: str | None) -> None:
        cfg = load_config()
        certs = cfg.setdefault("samsungtv", {}).setdefault("certs", {})
        if fp:
            self.certs[host] = certs[host] = fp
        else:
            self.certs.pop(host, None)
            certs.pop(host, None)
            self.tokens.pop(host, None)
            cfg["samsungtv"].setdefault("tokens", {}).pop(host, None)
        save_config(cfg)

    async def _info(self, host: str) -> dict[str, Any] | None:
        import aiohttp
        try:
            async with self.session.get(f"http://{host}:8001/api/v2/", timeout=aiohttp.ClientTimeout(total=3)) as r:
                return (await r.json(content_type=None)).get("device") or {}
        except Exception:  # noqa: BLE001
            return None

    async def _soap(self, host: str, action: str, args: str = "") -> str | None:
        import aiohttp
        headers, body = tv_soap(action, args)
        try:
            async with self.session.post(f"http://{host}:9197{TV_RC}", data=body.encode(), headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=3)) as r:
                return await r.text() if r.status == 200 else None
        except Exception:  # noqa: BLE001
            return None

    async def _keys(self, host: str, keys: list[str], wait_pair: float = 30.0) -> None:
        """Send remote control keys over the websocket (pairs the first time: the TV asks "Allow")."""
        import ssl as _ssl
        import aiohttp
        name = base64.b64encode(self.NAME.encode()).decode()
        url = f"wss://{host}:{self.WS_PORT}/api/v2/channels/samsung.remote.control?name={name}"
        if self.tokens.get(host):
            url += f"&token={self.tokens[host]}"
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE   # the TV has a self-signed certificate: pinned below instead
        pin = self.certs.get(host)
        ssl_arg: Any = aiohttp.Fingerprint(bytes.fromhex(pin)) if pin else ctx
        try:
            ws_cm = await self.session.ws_connect(url, ssl=ssl_arg, timeout=aiohttp.ClientWSTimeout(ws_close=5),
                                                  heartbeat=None)
        except aiohttp.ServerFingerprintMismatch:
            # the token was not sent: the check runs right after the TLS handshake, before the request
            log.warning("[samsungtv] %s: the TV shows another certificate than before (TV reset, or another device "
                        "at its address): the token is not sent; choose Allow on the TV again", host)
            self._save_cert(host, None)
            raise ValueError("TV certificate changed: choose Allow on the TV again") from None
        async with ws_cm as ws:
            if not pin:
                sslobj = ws.get_extra_info("ssl_object")
                der = sslobj.getpeercert(binary_form=True) if sslobj is not None else None
                if der:
                    self._save_cert(host, hashlib.sha256(der).hexdigest())
            deadline = time.monotonic() + wait_pair
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ValueError("the TV did not answer: accept 'Allow' on the TV screen and try again")
                msg = await ws.receive(timeout=left)
                if msg.type != aiohttp.WSMsgType.TEXT:
                    raise ValueError(f"TV closed the connection ({getattr(msg.type, 'name', msg.type)}, "
                                     f"{ws.exception() or msg.extra or ''}); Allow not chosen in time?")
                ev = json.loads(msg.data)
                if ev.get("event") == "ms.channel.connect":
                    tok = (ev.get("data") or {}).get("token")
                    if tok and tok != self.tokens.get(host):
                        self._save_token(host, str(tok))
                    break
                if ev.get("event") == "ms.channel.unauthorized":
                    raise ValueError("not allowed on the TV (Settings, General, External Device Manager, "
                                     "Device Connection Manager: allow Stoeckie Bridge)")
            for k in keys:
                await ws.send_str(json.dumps({"method": "ms.remote.control", "params": {
                    "Cmd": "Click", "DataOfCmd": k, "Option": "false", "TypeOfRemote": "SendRemoteKey"}}))
                await asyncio.sleep(0.4)

    def _wol(self, mac: str) -> None:
        pkt = wol_packet(mac)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for _ in range(3):
                sk.sendto(pkt, ("255.255.255.255", 9))

    def _command(self, tv: dict[str, Any]):
        host = str(tv["host"])

        async def handler(cmd: str, value: Any) -> None:
            st = self.last.get(host) or {}
            on = st.get("power") == "on"
            if cmd == "refresh":
                if not self.tokens.get(host):
                    self.pair_now[host] = True
            elif cmd == "on":
                if not on:
                    if tv.get("mac"):
                        self._wol(str(tv["mac"]))
                    try:
                        await self._keys(host, ["KEY_POWER"], 10)
                    except Exception as ex:  # noqa: BLE001  (deep standby: only wake-on-LAN can wake it)
                        log.info("[samsungtv] %s on: %s", host, safe_err(ex))
            elif cmd == "off":
                if on:
                    await self._keys(host, ["KEY_POWER"])
            elif cmd == "volume":
                v = max(0, min(100, int(round(float(value)))))
                if await self._soap(host, "SetVolume", f"<DesiredVolume>{v}</DesiredVolume>") is None:
                    raise ValueError("TV did not accept the volume (switched off?)")
            elif cmd in ("volumeUp", "volumeDown"):
                await self._keys(host, ["KEY_VOLUP" if cmd == "volumeUp" else "KEY_VOLDOWN"])
            elif cmd == "mute":
                m = 1 if str(value).lower() in ("1", "true", "muted", "on") else 0
                if await self._soap(host, "SetMute", f"<DesiredMute>{m}</DesiredMute>") is None:
                    raise ValueError("TV did not accept mute (switched off?)")
            elif cmd == "input":
                key = TV_INPUTS.get(str(value))
                if not key:
                    raise ValueError(f"unknown input {value}")
                await self._keys(host, [key])
                st["input"] = str(value)
            elif cmd in ("channelUp", "channelDown"):
                await self._keys(host, ["KEY_CHUP" if cmd == "channelUp" else "KEY_CHDOWN"])
            elif cmd == "key":
                k = str(value or "").upper()
                if not k.startswith("KEY_") or not k[4:].replace("_", "").isalnum():
                    raise ValueError("key must look like KEY_HOME")
                await self._keys(host, [k])
            else:
                raise ValueError(f"unsupported command {cmd}")
            log.info("[samsungtv] %s %s %s", host, cmd, "" if value is None else value)
            ev = self.wake.get(host)
            if ev is not None:
                ev.set()
        return handler

    async def _read(self, tv: dict[str, Any]) -> dict[str, Any]:
        host = str(tv["host"])
        info = await self._info(host)
        power = "on" if info and str(info.get("PowerState", "on")).lower() == "on" else "off"
        st: dict[str, Any] = {"power": power, "model": (info or {}).get("modelName"),
                              "input": (self.last.get(host) or {}).get("input"),
                              "inputs": list(TV_INPUTS), "paired": bool(self.tokens.get(host))}
        if power == "on":
            vol = tv_xml_value(await self._soap(host, "GetVolume") or "", "CurrentVolume")
            mute = tv_xml_value(await self._soap(host, "GetMute") or "", "CurrentMute")
            st["volume"] = int(vol) if vol and vol.isdigit() else None
            st["mute"] = None if mute is None else mute == "1"
        return st

    async def _run_one(self, tv: dict[str, Any]) -> None:
        host = str(tv["host"])
        name = str(tv.get("name") or "TV")
        dev_id = self.dev_id(host)
        wake = self.wake[host] = asyncio.Event()
        REG.upsert(dev_id, "tv", name, {"power": "off", "inputs": list(TV_INPUTS)}, online=True,
                   handler=self._command(tv))
        asked_at, tries = -1e9, 0
        while True:
            try:
                st = await self._read(tv)
                if st.get("model") is None and (self.last.get(host) or {}).get("model"):
                    st["model"] = self.last[host]["model"]
                if st != self.last.get(host):
                    self.last[host] = st
                    REG.upsert(dev_id, "tv", name, st, online=True)
                now = time.monotonic()
                if (st["power"] == "on" and not self.tokens.get(host) and tries < 5
                        and (now - asked_at > 120 or self.pair_now.pop(host, False))):
                    # the TV shows "Allow" for the bridge for about 30 s; ask again every 2 minutes (at most 5
                    # times per start) or right away after Refresh on the device
                    asked_at, tries = now, tries + 1
                    log.info("[samsungtv] %s: asking the TV for access, choose Allow on the TV screen", name)
                    try:
                        await self._keys(host, [], 45)
                    except Exception as ex:  # noqa: BLE001
                        log.info("[samsungtv] %s: pairing: %s", name, safe_err(ex))
            except Exception as ex:  # noqa: BLE001
                log.info("[samsungtv] %s: %s", name, safe_err(ex))
            try:
                await asyncio.wait_for(wake.wait(), self.POLL)
                await asyncio.sleep(1)
            except asyncio.TimeoutError:
                pass
            wake.clear()

    async def run(self) -> None:
        if not self.tvs:
            return
        import aiohttp
        log.info("[samsungtv] %d TV(s): %s", len(self.tvs), ", ".join(t["host"] for t in self.tvs))
        async with aiohttp.ClientSession() as self.session:
            await asyncio.gather(*(self._run_one(t) for t in self.tvs))


# ---------------------------------------------------------------- Tuya (Smart Life), local

# Tuya WiFi devices (Smart Life / Tuya Smart app) are controlled on the LAN with tinytuya: every device has a local
# key, and its data points (DPs) are numbered per product. The Tuya IoT Platform (a cloud project with the Smart
# Life app linked) only hands out the device list, the local keys and the DP mapping, once (Fetch devices on the
# setup page). Everything after that is local: status every 30 s, commands right away.
TUYA_REGIONS = ("eu", "us", "cn", "in")
TUYA_POLL = 30          # seconds between two status reads of one device
TUYA_SCAN_SECS = 18     # how long a UDP discovery listens (devices announce themselves about every 5 s)
TUYA_RESCAN = 300       # look for a device that does not answer at most every 5 minutes
TUYA_OFFLINE_AFTER = 3  # failed reads in a row before a device shows offline
TUYA_TIMEOUT = 12.0     # seconds for one local call (connect, send, answer), also below the driver's 15 s
TUYA: dict[str, Any] = {"error": None, "fetching": False, "fetched": None, "found": 0}
# thermostat categories (wk: thermostat, wkf: radiator valve, wkcz: floor heating); other devices with a set
# temperature (qn: heater, e.g. infrared panels) become a heater with a set temperature
TUYA_THERMOSTAT_CATS = {"wk", "wkf", "wkcz"}
# function codes per role: the first code the device has wins. Tuya products use different codes for the same
# thing, so the DP number always comes from the device's own mapping, never from a fixed list.
TUYA_ROLES: dict[str, tuple[str, ...]] = {
    "switch": ("switch", "switch_1", "power", "Power"),
    "temp_set": ("temp_set", "set_temp", "TempSet", "temp_set_c"),
    "temp_current": ("temp_current", "temp_indoor", "current_temp", "TempCurrent", "temp_current_c"),
    "temp_floor": ("temp_floor", "floor_temp", "temp_current_external", "temp_sensor", "floortemp",
                   "temp_floor_current", "temp_current_floor"),
    "mode": ("mode", "Mode", "work_mode"),
    "child_lock": ("child_lock", "lock", "ChildLock", "childlock"),
    "eco": ("eco", "eco_mode", "Eco"),
    "work_state": ("work_state", "valve_state", "heating_state", "valve", "work_status"),
    "upper_temp": ("upper_temp", "temp_upper", "upper_limit", "max_temp", "temp_set_max"),
    "lower_temp": ("lower_temp", "temp_lower", "lower_limit", "min_temp", "temp_set_min"),
    "unit": ("temp_unit_convert", "c_f", "temp_unit"),
}
# Tuya mode value (lower case) -> SmartThings thermostatMode. A value that is not here is not offered.
TUYA_MODE_TO_ST = {"auto": "auto", "program": "auto", "programming": "auto", "schedule": "auto", "smart": "auto",
                   "manual": "heat", "hand": "heat", "hold": "heat", "temp_hold": "heat", "constant": "heat",
                   "comfort": "heat", "holiday": "away", "away": "away", "leave": "away", "eco": "eco",
                   "energy": "eco", "frost": "frostguard", "anti_frost": "frostguard", "antifreeze": "frostguard",
                   "freeze": "frostguard"}
TUYA_HEATING = {"heating", "heat", "warming", "open", "opened", "on", "working", "start", "true", "1"}


def tuya_cloud_configured(t: dict[str, Any]) -> bool:
    return bool(t.get("api_key") and t.get("api_secret"))


def mask(s: Any) -> str:
    """A key or id as it may be shown: the first and last two characters only."""
    s = str(s or "")
    return (s[:2] + "..." + s[-2:]) if len(s) > 8 else "..."


def tuya_functions(mapping: Any) -> dict[str, dict[str, Any]]:
    """Function code -> {"dp", "code", "type", "values"} from a tinytuya mapping ({dp: {code, type, values}}) or
    from the cloud specification ({"status": [...], "functions": [...]} with dp_id; values as JSON text)."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(mapping, dict):
        return out
    items: list[tuple[str, dict[str, Any]]] = []
    if isinstance(mapping.get("status"), list) or isinstance(mapping.get("functions"), list):
        for m in list(mapping.get("functions") or []) + list(mapping.get("status") or []):
            if isinstance(m, dict) and m.get("code") is not None and m.get("dp_id") is not None:
                items.append((str(m["dp_id"]), m))
    else:
        items = [(str(dp), m) for dp, m in mapping.items() if isinstance(m, dict) and m.get("code")]
    for dp, m in items:
        values = m.get("values")
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except ValueError:
                values = {}
        code = str(m["code"])
        out.setdefault(code, {"dp": dp, "code": code, "type": str(m.get("type") or "").lower(),
                              "values": values if isinstance(values, dict) else {}})
    return out


def tuya_roles(funcs: dict[str, dict[str, Any]], override: Any = None) -> dict[str, dict[str, Any]]:
    """Role (switch, temp_set, ...) -> function, by code. Config "dps" overrides per role, as a DP number or
    {"dp", "scale", "min", "max", "step"} (for devices without a mapping from the cloud)."""
    roles: dict[str, dict[str, Any]] = {}
    for role, codes in TUYA_ROLES.items():
        for c in codes:
            if c in funcs:
                roles[role] = funcs[c]
                break
    for role, v in (override or {}).items() if isinstance(override, dict) else ():
        if role not in TUYA_ROLES:
            continue
        info = v if isinstance(v, dict) else {"dp": v}
        if info.get("dp") is None:
            continue
        base = dict(roles.get(role) or {"code": role, "type": "", "values": {}})
        vals = dict(base.get("values") or {})
        vals.update({k: info[k] for k in ("scale", "min", "max", "step", "range", "heating") if k in info})
        roles[role] = dict(base, dp=str(info["dp"]), values=vals)
    if not funcs and "switch" not in roles:
        roles["switch"] = {"dp": "1", "code": "switch", "type": "boolean", "values": {}}  # DP 1 is on/off
    return roles


def _tuya_scale(info: dict[str, Any]) -> int:
    try:
        return max(0, min(4, int((info.get("values") or {}).get("scale", 0))))
    except (TypeError, ValueError):
        return 0


def _tuya_unit_f(roles: dict[str, dict[str, Any]], dps: dict[str, Any]) -> bool:
    u = roles.get("unit")
    return bool(u) and str(dps.get(u["dp"], "")).lower() in ("f", "fahrenheit")


def tuya_decode_temp(raw: Any, info: dict[str, Any], unit_f: bool = False) -> float | None:
    """A temperature DP in °C: scale 1 means the value is in tenths (215 = 21.5 °C)."""
    if isinstance(raw, bool):
        return None
    try:
        v = float(raw) / (10 ** _tuya_scale(info))
    except (TypeError, ValueError):
        return None
    if unit_f:
        v = (v - 32) * 5 / 9
    return round(v, 1)


def tuya_setpoint_range(roles: dict[str, dict[str, Any]], dps: dict[str, Any]) -> tuple[float, float, float] | None:
    """(min, max, step) in °C of the set temperature: from the spec, narrowed by the upper and lower limits the
    device reports (those can be changed on the thermostat itself)."""
    info = roles.get("temp_set")
    if not info:
        return None
    v, s = info.get("values") or {}, _tuya_scale(info)
    unit_f = _tuya_unit_f(roles, dps)
    try:
        lo = float(v.get("min", 5 * 10 ** s)) / 10 ** s
        hi = float(v.get("max", 35 * 10 ** s)) / 10 ** s
        step = max(1, int(v.get("step", 1))) / 10 ** s
    except (TypeError, ValueError):
        lo, hi, step = 5.0, 35.0, 1.0 / 10 ** s
    if unit_f:
        lo, hi = (lo - 32) * 5 / 9, (hi - 32) * 5 / 9
    for role, pick in (("lower_temp", max), ("upper_temp", min)):
        r = roles.get(role)
        t = tuya_decode_temp(dps.get(r["dp"]), r, unit_f) if r else None
        if t is not None and lo <= t <= hi:
            if role == "lower_temp":
                lo = pick(lo, t)
            else:
                hi = pick(hi, t)
    if step < 0.5 and not unit_f:
        step = 0.5 if s else 1.0  # SmartThings shows 0.1 steps as a long slider; the device still gets its own step
    return round(lo, 1), round(hi, 1), step


def tuya_encode_temp(c: float, roles: dict[str, dict[str, Any]], dps: dict[str, Any]) -> int:
    """°C from SmartThings -> the raw set temperature: rounded to the device step, kept within min and max."""
    info = roles["temp_set"]
    v, s = info.get("values") or {}, _tuya_scale(info)
    unit_f = _tuya_unit_f(roles, dps)
    rng = tuya_setpoint_range(roles, dps)
    if rng:
        c = max(rng[0], min(rng[1], float(c)))
    val = float(c) * 9 / 5 + 32 if unit_f else float(c)
    try:
        step_raw = max(1, int(v.get("step", 1)))
    except (TypeError, ValueError):
        step_raw = 1
    raw = int(round(round(val * 10 ** s / step_raw) * step_raw))
    try:
        raw = max(int(v["min"]), raw) if "min" in v else raw
        raw = min(int(v["max"]), raw) if "max" in v else raw
    except (TypeError, ValueError):
        pass
    return raw


def tuya_mode_values(roles: dict[str, dict[str, Any]]) -> list[str]:
    info = roles.get("mode")
    if not info:
        return []
    rng = (info.get("values") or {}).get("range")
    return [str(x) for x in rng] if isinstance(rng, list) else []


def tuya_modes(roles: dict[str, dict[str, Any]]) -> dict[str, str]:
    """SmartThings thermostatMode -> Tuya mode value (the first Tuya value wins when two map to the same mode)."""
    out: dict[str, str] = {}
    for val in tuya_mode_values(roles):
        st = TUYA_MODE_TO_ST.get(val.lower())
        if st and st not in out:
            out[st] = val
    return out


def tuya_kind(d: dict[str, Any], roles: dict[str, dict[str, Any]]) -> str:
    """tuya_thermostat (a thermostat category, or a set temperature with modes) or tuya_heater (on/off, maybe a
    room temperature and a set temperature, like an infrared panel). Config "type" decides when set."""
    want = str(d.get("type") or "").lower()
    if want in ("thermostat", "heater"):
        return "tuya_" + want
    cat = str(d.get("category") or "").lower()
    if "temp_set" in roles and (cat in TUYA_THERMOSTAT_CATS or (not cat and "mode" in roles)):
        return "tuya_thermostat"
    return "tuya_heater"


def tuya_state(kind: str, roles: dict[str, dict[str, Any]], dps: dict[str, Any]) -> dict[str, Any]:
    """What the Edge driver gets for one Tuya device, from its DP values."""
    unit_f = _tuya_unit_f(roles, dps)

    def val(role):
        r = roles.get(role)
        return dps.get(r["dp"]) if r else None

    def temp(role):
        r = roles.get(role)
        return tuya_decode_temp(dps.get(r["dp"]), r, unit_f) if r and r["dp"] in dps else None

    st: dict[str, Any] = {}
    on = val("switch")
    st["power"] = "off" if on is False else "on"
    for key, role in (("temperature", "temp_current"), ("setpoint", "temp_set")):
        t = temp(role)
        if t is not None:
            st[key] = t
    rng = tuya_setpoint_range(roles, dps)
    if rng:
        st["setpointRange"], st["setpointStep"] = [rng[0], rng[1]], rng[2]
    if kind == "tuya_heater":
        st["hasSetpoint"] = "temp_set" in roles
        return st
    floor = temp("temp_floor")
    st["hasFloor"] = "temp_floor" in roles
    if floor is not None:
        st["floorTemperature"] = floor
    lock = val("child_lock")
    st["hasChildLock"] = "child_lock" in roles
    if isinstance(lock, bool):
        st["childLock"] = lock
    eco = val("eco")
    if isinstance(eco, bool):
        st["eco"] = eco
    modes = tuya_modes(roles)
    supported = (["off"] if "switch" in roles else []) + [m for m in modes]
    if "eco" in roles and "eco" not in supported:
        supported.append("eco")
    if "heat" not in supported:
        supported.insert(1 if supported[:1] == ["off"] else 0, "heat")  # no manual mode: heat means on
    st["supportedModes"] = supported
    raw = val("mode")
    st["tuyaMode"] = None if raw is None else str(raw)
    if st["power"] == "off":
        st["mode"] = "off"
    elif eco is True:
        st["mode"] = "eco"
    else:
        st["mode"] = TUYA_MODE_TO_ST.get(str(raw or "").lower(), "heat")
    ws = val("work_state")
    if ws is not None:
        # a device can name its own heating values ("heating" in the config dps override), e.g. a floor
        # thermostat that reports "heating" while the relay is on and "warming" while it waits (seen 2026-11-09)
        own = ((roles.get("work_state") or {}).get("values") or {}).get("heating")
        heating = str(ws).lower() in ({str(x).lower() for x in own} if isinstance(own, list) else TUYA_HEATING)
        st["operatingStateFrom"] = "device"
    else:
        # no valve or work state DP: an estimate from room and set temperature
        t = st.get("floorTemperature") if st.get("temperature") is None else st.get("temperature")
        heating = t is not None and st.get("setpoint") is not None and t < st["setpoint"]
        st["operatingStateFrom"] = "estimate"
    st["operatingState"] = "heating" if st["power"] == "on" and heating else "idle"
    return st


def tuya_command(cmd: str, value: Any, roles: dict[str, dict[str, Any]], dps: dict[str, Any]) -> dict[str, Any]:
    """The DP values to send for a command from SmartThings (raises ValueError for what the device can not do)."""
    def need(role):
        if role not in roles:
            raise ValueError(f"this device has no {role.replace('_', ' ')}")
        return roles[role]["dp"]

    def on_too(out):
        if "switch" in roles and dps.get(roles["switch"]["dp"]) is False:
            out[roles["switch"]["dp"]] = True
        return out

    if cmd in ("on", "off"):
        return {need("switch"): cmd == "on"}
    if cmd == "setpoint":
        try:
            c = float(value)
        except (TypeError, ValueError):
            raise ValueError("setpoint must be a number (°C)") from None
        return {need("temp_set"): tuya_encode_temp(c, roles, dps)}
    if cmd in ("child_lock", "eco"):
        on = value is True or str(value).lower() in ("1", "true", "on", "locked")
        return {need(cmd): on}
    if cmd == "mode":
        m = str(value or "")
        if m == "off":
            return {need("switch"): False}
        modes = tuya_modes(roles)
        out: dict[str, Any] = {}
        if m == "eco" and "eco" not in modes:
            out[need("eco")] = True
            return on_too(out)
        if m in modes:
            out[need("mode")] = modes[m]
        elif m != "heat":
            raise ValueError(f"mode {m} not supported by this device")
        eco = roles.get("eco")
        if eco and dps.get(eco["dp"]) is True and m != "eco":
            out[eco["dp"]] = False
        return on_too(out)
    raise ValueError(f"unsupported command {cmd}")


def tuya_devices(t: dict[str, Any]) -> list[dict[str, Any]]:
    """Devices from the cloud fetch and the manual list in config (same id: the manual fields win when set)."""
    by_id: dict[str, dict[str, Any]] = {}
    for d in list(t.get("cloud_devices") or []) + list(t.get("devices") or []):
        if not isinstance(d, dict) or not d.get("id"):
            continue
        cur = by_id.setdefault(str(d["id"]), {})
        cur.update({k: v for k, v in d.items() if v not in (None, "", "auto")})
    ips = t.get("ips") or {}
    out = []
    for dev_id, d in by_id.items():
        d["id"] = dev_id
        if not d.get("key") or d.get("sub"):  # a device behind a gateway has no local key of its own
            continue
        found = ips.get(dev_id) or {}
        d.setdefault("found_ip", found.get("ip"))
        d.setdefault("found_version", found.get("version"))
        out.append(d)
    return out


def tuya_cloud_fetch(region: str, api_key: str, api_secret: str) -> list[dict[str, Any]]:
    """One call to the Tuya IoT Platform: devices with their local keys and DP mapping (blocking)."""
    import tinytuya
    try:
        cloud = tinytuya.Cloud(apiRegion=region, apiKey=api_key, apiSecret=api_secret)
    except TypeError:
        raise ValueError("Access ID and Access Secret are required") from None
    if getattr(cloud, "error", None):
        raise ValueError("Tuya cloud refused the login: " + str((cloud.error or {}).get("Payload") or cloud.error)[:200])
    devs = cloud.getdevices(verbose=False, include_map=True)
    if isinstance(devs, dict):
        raise ValueError("Tuya cloud: " + str(devs.get("Payload") or devs.get("Error") or devs)[:200])
    keep = ("id", "name", "key", "category", "product_id", "product_name", "mapping", "sub", "mac")
    return [{k: d[k] for k in keep if k in d} for d in devs or [] if isinstance(d, dict) and d.get("id")]


class TuyaLocal:
    """Tuya WiFi devices on the LAN (tinytuya). Devices: config "tuya": {"cloud_devices": [...]} (filled by Fetch
    devices on the setup page) and/or {"devices": [{id, key, name, ip, version, dps}]} by hand; ip and version
    "auto" are found with a UDP broadcast scan and kept in "ips". One local call at a time per device, in a thread
    (tinytuya blocks). Local keys never go to the log, the setup page or SmartThings."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.t = dict(cfg.get("tuya") or {})
        self.devices = tuya_devices(self.t)
        self.locks: dict[str, asyncio.Lock] = {}
        self.conns: dict[str, Any] = {}
        self.dps: dict[str, dict[str, Any]] = {}
        self.roles: dict[str, dict[str, dict[str, Any]]] = {}
        self.kinds: dict[str, str] = {}
        self.wake: dict[str, asyncio.Event] = {}
        self.fails: dict[str, int] = {}
        self.scan_lock = asyncio.Lock()
        self.last_scan = -1e9
        self.alias: dict[str, str] = {}

    def dev_id(self, tuya_id: str) -> str:
        """SmartThings key of a device. "st_key" (manual "devices" list in config) keeps the SmartThings device,
        its name, room and routines when the device got a new Tuya id (removed and paired again in the app)."""
        return "tuya-" + str(self.alias.get(tuya_id) or tuya_id)

    async def _call(self, fn, *args, timeout: float = TUYA_TIMEOUT):
        return await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, fn, *args), timeout)

    async def fetch_cloud(self) -> int:
        """Fetch the device list, local keys and DP mapping once and keep them in config.json."""
        t = self.t
        TUYA.update(fetching=True, error=None)
        try:
            devs = await self._call(tuya_cloud_fetch, str(t.get("region") or "eu"), str(t["api_key"]),
                                    str(t["api_secret"]), timeout=120)
        except Exception as ex:  # noqa: BLE001
            TUYA.update(fetching=False, error=safe_err(ex))
            log.warning("[tuya] fetching the devices from the Tuya cloud failed: %s", safe_err(ex))
            cfg = load_config()
            cfg.setdefault("tuya", {}).pop("fetch", None)
            save_config(cfg)
            return 0
        cfg = load_config()
        tc = cfg.setdefault("tuya", {})
        tc["cloud_devices"] = devs
        tc["fetched"] = int(time.time())
        tc.pop("fetch", None)
        save_config(cfg)
        self.t = dict(tc)
        TUYA.update(fetching=False, fetched=tc["fetched"], error=None)
        log.info("[tuya] %d device(s) fetched from the Tuya cloud: %s", len(devs),
                 ", ".join(f"{d.get('name')} ({d.get('category') or '?'})" for d in devs))
        return len(devs)

    def _scan(self, wanted: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        from tinytuya import scanner
        found = scanner.devices(verbose=False, scantime=TUYA_SCAN_SECS, color=False, poll=False, forcescan=False,
                                byID=True, discover=True, wantids=[d["id"] for d in wanted],
                                tuyadevices=[{"id": d["id"], "key": d["key"], "name": d.get("name", "")}
                                             for d in wanted])
        return {k: v for k, v in (found or {}).items() if isinstance(v, dict) and v.get("ip")}

    async def discover(self, force: bool = False) -> None:
        """UDP broadcast scan for the devices with ip or version "auto"; found addresses are kept in config."""
        async with self.scan_lock:
            if not force and time.monotonic() - self.last_scan < TUYA_RESCAN:
                return
            self.last_scan = time.monotonic()
            wanted = [d for d in self.devices if not d.get("ip") or not d.get("version")]
            if not wanted:
                return
            try:
                found = await self._call(self._scan, wanted, timeout=TUYA_SCAN_SECS + 20)
            except Exception as ex:  # noqa: BLE001
                log.warning("[tuya] scan: %s", safe_err(ex))
                return
            TUYA["found"] = len(found)
            if not found:
                log.info("[tuya] scan found none of %d device(s) (needs UDP 6666/6667/7000 from the devices: same "
                         "network, Docker with host networking)", len(wanted))
                return
            cfg = load_config()
            ips = cfg.setdefault("tuya", {}).setdefault("ips", {})
            changed = False
            for d in wanted:
                f = found.get(d["id"])
                if not f:
                    continue
                new = {"ip": str(f["ip"]), "version": str(f.get("version") or "")}
                if d.get("found_ip") != new["ip"] or str(d.get("found_version")) != new["version"]:
                    log.info("[tuya] %s at %s (protocol %s)", d.get("name") or mask(d["id"]), new["ip"],
                             new["version"] or "?")
                    self._drop(d["id"])  # connect again at the new address
                d["found_ip"], d["found_version"] = new["ip"], new["version"]
                if ips.get(d["id"]) != new:
                    ips[d["id"]] = new
                    changed = True
            if changed:
                save_config(cfg)

    @staticmethod
    def address(d: dict[str, Any]) -> tuple[str | None, str | None]:
        return (d.get("ip") or d.get("found_ip") or None), str(d.get("version") or d.get("found_version") or "") or None

    def _device(self, d: dict[str, Any]):
        """The tinytuya device, kept open between calls (persistent socket); made again after an error."""
        dev = self.conns.get(d["id"])
        if dev is None:
            import tinytuya
            ip, ver = self.address(d)
            if not ip:
                raise ConnectionError("address not found yet (scan)")
            dev = tinytuya.Device(d["id"], address=ip, local_key=d["key"], version=float(ver or 3.3), persist=True,
                                  connection_timeout=5, connection_retry_limit=2, connection_retry_delay=1)
            self.conns[d["id"]] = dev
        return dev

    def _drop(self, tuya_id: str) -> None:
        dev = self.conns.pop(tuya_id, None)
        if dev is not None:
            try:
                dev.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _check(resp: Any) -> dict[str, Any]:
        """tinytuya returns errors as {"Error", "Err"} instead of raising."""
        if isinstance(resp, dict) and resp.get("Error"):
            code = str(resp.get("Err") or "")
            hint = " (wrong local key or protocol version?)" if code in ("914", "904") else ""
            raise ConnectionError(f"{resp['Error']} {code}".strip() + hint)
        return resp if isinstance(resp, dict) else {}

    def _status(self, d: dict[str, Any]) -> dict[str, Any]:
        return self._check(self._device(d).status())

    def _set(self, d: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        dev = self._device(d)
        if len(values) == 1:
            dp, v = next(iter(values.items()))
            return self._check(dev.set_value(dp, v))
        return self._check(dev.set_multiple_values(values))

    def _update(self, d: dict[str, Any], resp: dict[str, Any]) -> None:
        tid = d["id"]
        new = resp.get("dps")
        if isinstance(new, dict):
            self.dps.setdefault(tid, {}).update({str(k): v for k, v in new.items()})
        # raw data points and their codes for the setup page, to check or fix the mapping (no keys in here)
        m = d.get("mapping") if isinstance(d.get("mapping"), dict) else {}
        TUYA.setdefault("raw", {})[self.dev_id(tid)] = ", ".join(
            f"{k}{'=' + str((m.get(k) or {}).get('code')) if isinstance(m.get(k), dict) and m[k].get('code') else ''}: {v}"
            for k, v in sorted((self.dps.get(tid) or {}).items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 999))
        st = tuya_state(self.kinds[tid], self.roles[tid], self.dps.get(tid) or {})
        REG.upsert(self.dev_id(tid), self.kinds[tid], str(d.get("name") or "Tuya"), st, online=True)

    async def read(self, d: dict[str, Any]) -> None:
        tid = d["id"]
        async with self.locks[tid]:
            try:
                resp = await self._call(self._status, d)
            except BaseException:
                self._drop(tid)
                raise
            self._update(d, resp)

    def _command(self, d: dict[str, Any]):
        tid = d["id"]

        async def handler(cmd: str, value: Any) -> None:
            if cmd == "refresh":
                self.wake[tid].set()
                return
            values = tuya_command(cmd, value, self.roles[tid], self.dps.get(tid) or {})
            if not values:  # already so (e.g. heat on a device that is on and has no modes)
                return
            async with self.locks[tid]:
                try:
                    resp = await self._call(self._set, d, values)
                except BaseException:
                    self._drop(tid)
                    raise
                # the answer has the changed DPs; show them now, the next status read confirms the rest
                if not isinstance(resp.get("dps"), dict):
                    resp = {"dps": values}
                self._update(d, resp)
            log.info("[tuya] %s %s %s", d.get("name") or mask(tid), cmd, "" if value is None else value)
            self.wake[tid].set()
        return handler

    def _info(self, d: dict[str, Any]) -> str:
        ip, ver = self.address(d)
        roles = self.roles.get(d["id"]) or {}
        codes = ", ".join(f"{r} {roles[r]['code']} (DP {roles[r]['dp']})" for r in TUYA_ROLES if r in roles)
        return (f"{ip or 'address not found yet'}, protocol {ver or '?'}, category {d.get('category') or '?'}; "
                f"{codes or 'no data points known'}")

    def add(self, d: dict[str, Any]) -> str:
        """Register a device: its data points by function code, its kind and its command handler."""
        tid = d["id"]
        if d.get("st_key"):
            self.alias[tid] = str(d["st_key"])
        self.roles[tid] = tuya_roles(tuya_functions(d.get("mapping")), d.get("dps"))
        kind = self.kinds[tid] = tuya_kind(d, self.roles[tid])
        self.locks[tid] = asyncio.Lock()
        self.wake[tid] = asyncio.Event()
        REG.upsert(self.dev_id(tid), kind, str(d.get("name") or "Tuya " + mask(tid)), {}, online=False,
                   handler=self._command(d))
        return tid

    async def poll_once(self, d: dict[str, Any]) -> float:
        """One status read. Offline after TUYA_OFFLINE_AFTER failures in a row; returns the seconds until the
        next read (30 s with a few seconds jitter, longer after failures)."""
        tid = d["id"]
        dev_id = self.dev_id(tid)
        name = str(d.get("name") or "Tuya " + mask(tid))
        if not self.address(d)[0]:
            await self.discover()
        try:
            await self.read(d)
            if self.fails.get(tid):
                log.info("[tuya] %s: answering again", name)
            self.fails[tid] = 0
        except Exception as ex:  # noqa: BLE001
            n = self.fails[tid] = self.fails.get(tid, 0) + 1
            log.info("[tuya] %s: %s (%d in a row)", name, safe_err(ex) or type(ex).__name__, n)
            if n >= TUYA_OFFLINE_AFTER and dev_id in REG.devices:
                REG.devices[dev_id]["online"] = False
            if n >= 2 and not d.get("ip"):
                await self.discover()  # the router may have given it another address
        REG.info[dev_id] = self._info(d)
        n = self.fails.get(tid, 0)
        return TUYA_POLL + random.uniform(-3, 3) if not n else min(300, TUYA_POLL * 2 ** min(n - 1, 4))

    async def _run_one(self, d: dict[str, Any]) -> None:
        tid = self.add(d)
        wake = self.wake[tid]
        await asyncio.sleep(random.uniform(0, 5))  # not all devices at the same moment
        while True:
            wait = await self.poll_once(d)
            try:
                await asyncio.wait_for(wake.wait(), wait)
                await asyncio.sleep(1)
            except asyncio.TimeoutError:
                pass
            wake.clear()

    async def run(self) -> None:
        logging.getLogger("tinytuya").setLevel(logging.WARNING)  # its debug lines can carry the local key
        if tuya_cloud_configured(self.t) and (self.t.get("fetch") or not self.t.get("cloud_devices")):
            await self.fetch_cloud()
            self.devices = tuya_devices(self.t)
        if not self.devices:
            if tuya_cloud_configured(self.t) or self.t.get("devices"):
                log.info("[tuya] no devices with a local key (fetch devices on the setup page)")
            return
        log.info("[tuya] %d device(s): %s", len(self.devices),
                 ", ".join(f"{d.get('name') or mask(d['id'])} ({d.get('category') or '?'})" for d in self.devices))
        if any(not d.get("ip") or not d.get("version") for d in self.devices):
            await self.discover(force=True)
        await asyncio.gather(*(self._run_one(d) for d in self.devices))


def _ss_short(v: Any, n: int = 600) -> str:
    t = json.dumps(v, default=lambda o: o.hex() if isinstance(o, (bytes, bytearray)) else str(o))
    return t if len(t) <= n else t[:n] + "..."


def samsung_probe(host: str) -> int:
    """Read-only check whether a Samsung appliance accepts a local session: finds the DTLS port, does one
    handshake with the client certificate and reads a few state resources (never /oic/sec/*)."""
    import cbor2
    from smartthings_local.protocol import dtls_probe, ocf_discovery
    from smartthings_local.protocol.auth import CertificateAuth
    from smartthings_local.protocol.dtls_session import DtlsCoapSession
    cert_p, key_p = samsung_cert()
    print(f"[samsung] {host}: client certificate {cert_p}")
    ports: list[int] = []
    try:
        disc = ocf_discovery.discover_ocf_secure_ports(host)
        print(f"[samsung] OCF directory (udp 5683): {disc!r}")
        ports = [int(x) for x in (getattr(disc, "ports", None) or []) if isinstance(x, int)]
    except Exception as ex:  # noqa: BLE001
        print(f"[samsung] OCF directory: {ex!r}")
    live = dtls_probe.probe_dtls_ports(host, ports + [p for p in SS_PORTS if p not in ports])
    print(f"[samsung] DTLS probe: outcome={live.outcome} selected={live.selected_port} "
          f"responders={live.responder_ports}")
    port = live.selected_port or (live.responder_ports[0] if live.responder_ports else None)
    if not port:
        print("[samsung] no DTLS server found (appliance off, asleep or not on this network)")
        return 1
    try:
        diag = dtls_probe.diagnose_dtls_handshake(host, port, cert_path=str(cert_p), key_path=str(key_p))
        print(f"[samsung] handshake: {diag!r}")
    except Exception as ex:  # noqa: BLE001
        print(f"[samsung] handshake diagnostic: {ex!r}")
    sess = DtlsCoapSession(host, port, auth=CertificateAuth.from_files(str(cert_p), str(key_p)))
    ok = 0
    try:
        sess.connect(timeout=12.0)
        sess.start_reader()
        for path in (["oic", "d"], ["operational", "state", "vs", "0"], ["power", "vs", "0"], ["power", "0"],
                     ["remotectrl", "vs", "0"], ["course", "vs", "0"], ["st", "washercourse", "vs", "0"],
                     ["washer", "vs", "0"], ["kidslock", "vs", "0"]):
            try:
                code, body = sess.get(path)
                try:
                    val = cbor2.loads(body) if body else None
                except Exception:  # noqa: BLE001
                    val = body
                print(f"[samsung] GET /{'/'.join(path)}: {code} {_ss_short(val)}")
                if str(code) in ("2.05", "69") or getattr(code, "name", "") == "CONTENT":
                    ok += 1
            except Exception as ex:  # noqa: BLE001
                print(f"[samsung] GET /{'/'.join(path)}: {ex!r}")
    except Exception as ex:  # noqa: BLE001
        print(f"[samsung] session: {ex!r}")
    finally:
        try:
            sess.close()
        except Exception:  # noqa: BLE001
            pass
    print(f"[samsung] result: {ok} resource(s) read")
    return 0 if ok else 2


# ---------------------------------------------------------------- HTTPS for the setup page (self-signed)

def _local_ips() -> list[str]:
    ips = {"127.0.0.1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # no packet is sent; picks the LAN interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips.update(a for a in socket.gethostbyname_ex(socket.gethostname())[2] if not a.startswith("169.254."))
    except OSError:
        pass
    return sorted(ips)


def setup_tls(data_dir: Path):
    """SSL context for the setup page with a self-signed certificate (made once, kept in the data folder).
    Returns (context, SHA-256 fingerprint) or (None, error)."""
    import ssl
    cert, key = data_dir / "setup-cert.pem", data_dir / "setup-key.pem"
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        if not (cert.exists() and key.exists()):
            k = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Stoeckie Bridge " + socket.gethostname())])
            alt = [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(i)) for i in _local_ips()]
            now = dt.datetime.now(dt.timezone.utc)
            c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
                 .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
                 .not_valid_after(now + dt.timedelta(days=3650))
                 .add_extension(x509.SubjectAlternativeName(alt), critical=False)
                 .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                 .sign(k, hashes.SHA256()))
            key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
            cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
            log.info("[https] new self-signed certificate for %s", ", ".join(_local_ips()))
        c = x509.load_pem_x509_certificate(cert.read_bytes())
        fp = c.fingerprint(hashes.SHA256()).hex(":").upper()
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(cert), str(key))
        return ctx, fp
    except Exception as ex:  # noqa: BLE001
        return None, str(ex)


# ---------------------------------------------------------------- LAN HTTP API

SETUP_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Stoeckie Bridge setup</title>{refresh}
<style>
:root{--bg:#f2f2f6;--card:#fff;--text:#1c1c1e;--sub:#6e6e73;--line:#e3e3e8;--accent:#3e6ae1;--warn:#e5484d;--field:#f2f2f6}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--text:#f2f2f2;--sub:#9a9aa0;--line:#2c2c2e;
--accent:#4f7cff;--warn:#ff6b6b;--field:#2c2c2e}}
*{box-sizing:border-box}body{font:16px system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);
color:var(--text);max-width:680px;margin:0 auto;padding:20px 16px 48px}
a{color:var(--accent);text-decoration:none}h1{font-size:26px;margin:8px 4px 4px}h1 small{font-size:15px;color:var(--sub);
font-weight:400}.intro{color:var(--sub);font-size:14px;margin:0 4px 18px}
h2.g{font-size:14px;font-weight:600;color:var(--sub);text-transform:uppercase;letter-spacing:.04em;margin:30px 8px 8px}
.card{background:var(--card);border-radius:22px;padding:18px 20px;margin:0 0 12px}
.card h3{font-size:19px;margin:0 0 4px}.list{background:var(--card);border-radius:22px;padding:4px 20px;margin:0 0 8px}
.item{display:flex;align-items:center;gap:12px;padding:13px 0;border-bottom:1px solid var(--line);color:var(--text)}
.item:last-child{border-bottom:0}.item .t{flex:1}.item b{display:block;font-weight:600}
.item .t span{display:block;color:var(--sub);font-size:14px;margin-top:2px}.item.bad .t span{color:var(--warn);font-weight:600}
.item .go{color:var(--sub);font-size:22px;line-height:1}.item:hover b,.item:active b{color:var(--accent)}
.st{color:var(--sub);font-size:14px;line-height:1.5;padding:0 0 12px;margin:0 0 12px;border-bottom:1px solid var(--line)}
.st.bad{color:var(--warn)}.card p{font-size:14px;line-height:1.5}
label{display:block;margin:14px 0 6px;font-size:14px;font-weight:600}
input,textarea,select{width:100%;padding:12px 14px;border:0;border-radius:14px;background:var(--field);color:var(--text);
font:15px system-ui,sans-serif}textarea{height:90px}
button{margin-top:16px;padding:12px 22px;font-size:16px;font-weight:600;border:0;border-radius:999px;background:var(--accent);
color:#fff}.step{border:1px solid var(--line);border-radius:16px;padding:14px 16px;margin:12px 0}.step.active{border:2px solid var(--accent)}
.step.off{opacity:.55}.step.done summary b::after{content:"  \\2713 done (tap to change)";color:var(--accent);font-weight:400}
summary{cursor:pointer}.hint{color:var(--sub);font-size:13px}
a.btn{display:inline-block;margin-top:8px;padding:12px 22px;font-weight:600;border-radius:999px;background:var(--accent);
color:#fff}form+form{margin-top:6px;padding-top:6px;border-top:1px solid var(--line)}.top{font-size:13px;margin:14px 0 0}
</style></head><body>
<h1 id="top">Stoeckie Bridge <small>{version}</small></h1>
<p class="intro">Everything you enter here is stored only locally, in config.json on this computer. Tap a part below to open its status and settings.</p>
<p class="intro"><a href="/setup/claude">Claude on web and phone (SmartThings control)</a></p>
{status}
<h2 class="g">SmartThings</h2>
<div class="card"><h3 id="hub">SmartThings hub</h3>
{st_hub}
<p>Until a hub is paired the bridge does not serve your devices on the LAN. After pairing it only accepts signed
requests from the paired hub. Press Pair; within a minute your hub shows up here with its IP address. Check that it
is your hub (SmartThings app, hub details, or your router) and press Allow (driver Stoeckie Bridge 2026-11-02b or
newer).</p>
{pair_requests}
<form method="post"><input type="hidden" name="section" value="api_pair">
<button type="submit">Pair (open for 5 minutes)</button></form>
<form method="post"><input type="hidden" name="section" value="api_new_token">
<button type="submit">New token (pair all hubs again)</button></form>
<p class="top"><a href="#top">Back to the overview</a></p></div>
<h2 class="g">Climate</h2>
<div class="card"><h3 id="fglair">Fujitsu air conditioners (FGLair)</h3>
{st_fglair}
<p>Email and password of the FGLair app.</p>
<form method="post"><input type="hidden" name="section" value="fglair">
<label>Email</label><input name="username" type="email" autocomplete="off" required>
<label>Password</label><input name="password" type="password" autocomplete="off" required>
<label>Region</label><select name="region"><option value="eu">Europe</option><option value="default">Other (Americas, Asia)</option>
<option value="cn">China</option></select>
<button type="submit">Save FGLair and connect</button></form>
<form method="post"><input type="hidden" name="section" value="fglair_settings">
<label>Update the air conditioners every (seconds, 30 to 600)</label>
<input name="poll_seconds" type="number" min="30" max="600" value="{fglair_poll}" required>
<button type="submit">Save setting</button></form>
<p>Also available per air conditioner in the SmartThings app (Settings).</p>
<p class="top"><a href="#top">Back to the overview</a></p></div>
<div class="card"><h3 id="tuya">Tuya / Smart Life thermostats and heaters (local, optional)</h3>
{st_tuya}
<p>Thermostats and heaters from the Smart Life (or Tuya Smart) app, controlled on the LAN. On <b>iot.tuya.com</b>
create a cloud project (data center of your app account, e.g. Central Europe), link your Smart Life app account
with its QR code (Devices, Link App Account) and copy the Access ID and Access Secret from the project overview.
The bridge asks the Tuya cloud once for your devices, their local keys and data points; after that everything is
local. Keys stay in config.json and are never shown here.</p>
<p>{tuya_saved}</p>
<form method="post"><input type="hidden" name="section" value="tuya">
<label>Access ID</label><input name="api_key" autocomplete="off" required>
<label>Access Secret</label><input name="api_secret" type="password" autocomplete="off" required>
<label>Region (data center)</label><select name="region"><option value="eu"{tuya_r_eu}>Europe</option>
<option value="us"{tuya_r_us}>Americas</option><option value="cn"{tuya_r_cn}>China</option>
<option value="in"{tuya_r_in}>India</option></select>
<button type="submit">Save Tuya and fetch devices</button></form>
<form method="post"><input type="hidden" name="section" value="tuya_fetch">
<button type="submit">Fetch devices again (new device, new key after a reset)</button></form>
<p class="top"><a href="#top">Back to the overview</a></p></div>
<h2 class="g">Appliances</h2>
<div class="card"><h3 id="aeg">AEG / Electrolux dryer</h3>
{st_aeg}
<p>On <b>developer.electrolux.one</b> (sign in with your AEG / Electrolux account) create an API key and generate
an access token and a refresh token.</p>
<form method="post"><input type="hidden" name="section" value="aeg">
<label>API key</label><input name="api_key" autocomplete="off" required>
<label>Access token</label><textarea name="access_token" required></textarea>
<label>Refresh token</label><textarea name="refresh_token" required></textarea>
<button type="submit">Save AEG and connect</button></form>
<p class="top"><a href="#top">Back to the overview</a></p></div>
<div class="card"><h3 id="homeconnect">Home Connect dishwasher and oven (local, optional)</h3>
{st_homeconnect}
<p>Set up with the profile files of the Home Connect Profile Downloader in the folder homeconnect next to the bridge (see the README); nothing to enter here.</p>
<p class="top"><a href="#top">Back to the overview</a></p></div>
<div class="card"><h3 id="samsung">Samsung washer (local, experimental)</h3>
{st_samsung}
<p>Set up in config.json (see the README); nothing to enter here.</p>
<p class="top"><a href="#top">Back to the overview</a></p></div>
<h2 class="g">Car</h2>
<div class="card"><h3 id="skoda">Škoda</h3>
{st_skoda}
<p>In the MyŠkoda app (8.16 or newer) create an API key: <b>Settings, Smart Home, API keys</b>, and choose your car.
The VIN (17 characters) is shown in the app with the car and on the registration document.</p>
<form method="post"><input type="hidden" name="section" value="skoda">
<label>API key</label><input name="api_key" autocomplete="off" required>
<label>VIN</label><input name="vin" maxlength="17" minlength="17" required>
<button type="submit">Save Škoda and connect</button></form>
<form method="post"><input type="hidden" name="section" value="skoda_settings">
<label>Update the car every (minutes, at least 5; Škoda allows 20 requests per hour)</label>
<input name="poll_minutes" type="number" min="5" max="60" value="{skoda_poll}" required>
<label>Temperature for climate on (°C)</label><input name="climate_temp" value="{skoda_temp}" required>
<button type="submit">Save settings</button></form>
<p>These settings are also available with the car in the SmartThings app (Settings).</p>
<p class="top"><a href="#top">Back to the overview</a></p></div>
<h2 class="g">Home</h2>
<div class="card"><h3 id="velux">VELUX ACTIVE departure mode (optional)</h3>
{st_velux}
<p>To lock and unlock (departure mode: all windows and blinds blocked) from SmartThings.
The windows themselves keep running locally over HomeKit. Email and password of the VELUX ACTIVE app.</p>
<form method="post"><input type="hidden" name="section" value="velux">
<label>Email</label><input name="username" type="email" autocomplete="off" required>
<label>Password</label><input name="password" type="password" autocomplete="off" required>
<button type="submit">Save VELUX and connect</button></form>
<p>Unlocking must be signed with a key from the gateway. Once: enter the IP of the VELUX gateway,
click Pair gateway, wait until the light on the gateway blinks and then press the button on the gateway within 3 minutes.</p>
<form method="post"><input type="hidden" name="section" value="velux_pair">
<label>IP of the VELUX gateway</label><input name="gateway_ip" value="{velux_ip}" required>
<button type="submit">Pair gateway</button></form>
<form method="post"><input type="hidden" name="section" value="velux_settings">
<label>Update departure mode and rain sensor every (seconds, 30 to 600)</label>
<input name="poll_seconds" type="number" min="30" max="600" value="{velux_poll}" required>
<button type="submit">Save setting</button></form>
<p>Also available on VELUX departure mode in the SmartThings app (Settings).</p><p class="top"><a href="#top">Back to the overview</a></p></div>
<div class="card"><h3 id="rain">Rain radar (optional)</h3>
{st_rain}
<p>Rain now and in the next 2 hours around your home, with a radius. Buienradar in the Netherlands and Belgium,
Open-Meteo everywhere else. Also available on the Buienradar device in SmartThings
(switch it on with the setting Buienradar on the Stoeckie Bridge device). The last change wins.</p>
<form method="post"><input type="hidden" name="section" value="buienradar">
<label>Postcode (NL)</label><input name="postcode" value="{rain_pc}" placeholder="1234AB">
<label>House number (NL)</label><input name="housenumber" value="{rain_nr}" placeholder="12">
<label>Or address (any country: street number, city, country)</label><input name="address" value="{rain_addr}" placeholder="Rue de la Loi 16, Brussels, Belgium">
<label>Or latitude, longitude</label><input name="location" value="{rain_loc}" placeholder="52.37, 4.89">
<label>Source</label><select name="provider"><option value="auto"{rain_p_auto}>Automatic (Buienradar in NL/BE, otherwise Open-Meteo)</option>
<option value="buienradar"{rain_p_buienradar}>Buienradar (NL/BE, radar, 5 minutes)</option>
<option value="openmeteo"{rain_p_openmeteo}>Open-Meteo (worldwide, 15 minutes)</option></select>
<label>Radius in meters (0 = only this address, max 25000)</label><input name="radius_m" type="number" min="0" max="25000" value="{rain_radius}">
<label>Rain from (mm/h)</label><input name="threshold" value="{rain_thr}">
<button type="submit">Save rain radar</button></form>
<p class="top"><a href="#top">Back to the overview</a></p></div>
<h2 class="g">Voice and helpers</h2>
<div class="card"><h3 id="echo">Amazon Echo (optional, unofficial)</h3>
{st_echo}
<p>Let SmartThings routines make an Echo speak, announce, set its volume or run anything you can say to Alexa
("play Radio 538"). Amazon has no official API for this: the bridge uses the Alexa website, like Alexa Media Player
in Home Assistant, and it can stop working when Amazon changes it. Afterwards only a login token is kept.</p>
<p><b>The login has two steps.</b></p>
<div class="step{echo_s1}"><details{echo_s1_open}><summary><b>Step 1 of 2: your Amazon account</b></summary>
<form method="post"><input type="hidden" name="section" value="echo">
<label>Amazon email</label><input name="email" type="email" autocomplete="off" required>
<label>Password (optional: leave empty to type it at Amazon in step 2; not kept)</label><input name="password" type="password" autocomplete="off">
<label>Authenticator app key (recommended)</label><input name="otp_secret" autocomplete="off">
<p class="hint">You need Amazon 2-step verification with a code: a passkey does not work through the bridge (it only
works on amazon.com itself). Best is an authenticator app: add one at Amazon (Login &amp; security, 2-Step
Verification, Add new app, Can't scan the barcode) and paste that key here, the bridge then fills in the code itself.
A code by SMS may work too, but did not always arrive in tests.</p>
<label>Amazon site of your Alexa account</label><select name="url">{echo_domains}</select>
<button type="submit">Continue to step 2</button></form></details></div>
<div class="step{echo_s2}" id="echo2"><b>Step 2 of 2: log in at Amazon</b>
{echo_link}</div>
<p class="top"><a href="#top">Back to the overview</a></p></div>
<div class="card"><h3 id="irobot">iRobot favorites (optional)</h3>
{st_irobot}
<p>Email and password of the iRobot app. The bridge only reads the names and rooms of your favorites,
twice a day, for the Roomba driver on the hub. The robot itself is controlled locally.</p>
<form method="post"><input type="hidden" name="section" value="irobot">
<label>Email</label><input name="username" type="email" autocomplete="off" required>
<label>Password</label><input name="password" type="password" autocomplete="off" required>
<label>Country (2 letters)</label><input name="country" value="NL" maxlength="2" required>
<button type="submit">Save iRobot and connect</button></form>
<p class="top"><a href="#top">Back to the overview</a></p></div>
</body></html>"""


def make_app(bridge_id: str, on_setup=None, https_port: int | None = None) -> web.Application:
    app = web.Application(middlewares=[])

    def local_only(req) -> bool:
        """Setup page: only from this pc (or STBRIDGE_SETUP_ALLOW), only addressed by IP or localhost (blocks
        DNS rebinding) and never posted from another site (blocks cross-site requests from a browser)."""
        try:
            ip = ipaddress.ip_address(req.remote or "")
        except ValueError:
            return False
        if not setup_source_allowed(ip):
            return False
        host = (req.host or "").rsplit(":", 1)[0].strip("[]").lower()
        if host != "localhost":
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return False
        origin = req.headers.get("Origin")
        if origin and origin != "null":
            from urllib.parse import urlsplit
            if urlsplit(origin).netloc.lower() != (req.host or "").lower():
                return False
        elif origin == "null":
            return False
        return True

    def lan_api_refusal(req) -> web.Response | None:
        """The LAN API is only for the hub: a private address, addressed by IP (no DNS rebinding), never from a
        web page (browsers send Origin) and only JSON (a cross-site form or text/plain post is refused)."""
        try:
            ip = ipaddress.ip_address(req.remote or "")
        except ValueError:
            ip = None
        if ip is None or not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return web.json_response({"error": "forbidden"}, status=403)
        host = (req.host or "").rsplit(":", 1)[0].strip("[]").lower()
        if host != "localhost":
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return web.json_response({"error": "address the bridge by IP"}, status=403)
        if "Origin" in req.headers or req.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
            return web.json_response({"error": "no browser requests"}, status=403)
        if req.method == "POST" and req.content_type != "application/json":
            return web.json_response({"error": "JSON only"}, status=415)
        return None

    @web.middleware
    async def guard(req, handler):
        if req.path.startswith("/stbridge/"):
            refusal = lan_api_refusal(req)
            if refusal is not None:
                return refusal
            cfg = load_config()
            token = cfg.get("api_token") if api_paired(cfg) else None
            sig = req.headers.get("X-Stbridge-Sig")
            if token and sig:
                body = await req.read()
                why = check_request_sig(token, req.method, req.raw_path, req.headers.get("X-Stbridge-Time", ""),
                                        sig, body)
                if why:
                    log.warning("[api] refused %s %s from %s: %s", req.method, req.path, req.remote, why)
                    return web.json_response({"error": why}, status=401)
                PAIRING["last_ip"] = req.remote
                resp = await handler(req)
                if isinstance(resp, web.Response) and isinstance(resp.body, (bytes, bytearray)):
                    resp.headers["X-Stbridge-Sig"] = sign(token, sig.lower().encode() + b"\n" + bytes(resp.body))
                return resp
            # open even after pairing: discovery info, the pairing call itself, and the read-only iRobot favorites
            # that the Roomba driver (another driver, no token) fetches
            open_paths = req.path in ("/stbridge/info", "/stbridge/pair") or (
                req.method == "GET" and req.path.startswith("/stbridge/irobot/"))
            if not open_paths:
                # also before pairing: an unpaired bridge no longer serves devices or commands to the whole LAN
                return web.json_response({"error": "pairing required: press Pair on the bridge setup page and "
                                                   "Allow your hub"}, status=401)
        resp = await handler(req)
        if req.path.startswith("/setup/echo-auth"):  # Amazon's own login page (relayed): its scripts must run
            resp.headers["Cache-Control"] = "no-store"
        elif req.path.startswith("/setup"):
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["X-Frame-Options"] = "DENY"
            resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
        return resp

    def status_sections() -> list[tuple[str, str, str, list[str]]]:
        """Per part of the page: (anchor, title, one-line summary, detail lines)."""
        cfg = load_config()
        mem = memory_mb()
        out: list[tuple[str, str, str, list[str]]] = []

        # SmartThings hub
        lines = []
        if PAIRING["until"] > time.time():
            summary = f"pairing open ({int(PAIRING['until'] - time.time())} s left)"
            lines.append("Pairing is open: wait until your hub shows up below and press Allow.")
        elif api_paired(cfg):
            seen = PAIRING.get("last_ip") or cfg.get("api_paired_ip")
            summary = "paired" + (f" ({seen})" if seen else "")
            lines.append("The LAN API only accepts signed requests from this hub.")
        else:
            summary = "NOT PAIRED: your devices do not reach SmartThings"
            lines.append("The LAN API is closed until you pair. Press Pair below and Allow your hub.")
        if mem:
            lines.append(f"Memory: {mem[0]:.0f} MB (highest {mem[1]:.0f} MB since the start)")
        if SETUP_LAN_KEY:
            lines.append(f"Setup key for a phone or another device on your network: {setup_key()}")
        out.append(("hub", "SmartThings hub", summary, lines))

        # Fujitsu
        f = cfg.get("fglair") or {}
        acs = [d for d in REG.devices.values() if d["type"] == "aircon"]
        lines = []
        if not fglair_configured(f):
            summary = "not set up"
        elif acs:
            summary = f"connected, {len(acs)} air conditioner(s)"
            lines += [f"{d['name']}: {d['state'].get('mode')}, {d['state'].get('setpoint')} °C" for d in acs]
        else:
            summary = "set up, no air conditioner found yet (see bridge.log)"
        for d in acs:  # details per unit (also for the demo unit)
            if REG.info.get(d["id"]):
                lines.append(f"{d['name']}: {REG.info[d['id']]}")
            g = REG.diag.get(d["id"])
            if g:
                lines.append(f"{d['name']} in SmartThings: driver {g.get('build', '?')}, profile {g.get('profile', '?')}, "
                             f"set temperature in {g.get('unit', '?')} (setting {g.get('unitSetting', '?')}), "
                             f"horizontal airflow {g.get('horizontal', '?')}")
            if REG.commands.get(d["id"]):
                lines.append(f"{d['name']} last commands from SmartThings: " + "; ".join(REG.commands[d["id"]]))
        out.append(("fglair", "Fujitsu air conditioners (FGLair)", summary, lines))

        # Tuya
        t = cfg.get("tuya") or {}
        tds = [d for d in REG.devices.values() if d["type"] in ("tuya_thermostat", "tuya_heater")]
        lines = []
        if TUYA.get("fetching"):
            lines.append("Fetching the devices from the Tuya cloud...")
        elif TUYA.get("error"):
            lines.append("Tuya cloud: " + str(TUYA["error"]))
        if tuya_cloud_configured(t) or t.get("devices") or tds:
            fetched = t.get("fetched")
            lines.append(f"{len(t.get('cloud_devices') or [])} device(s) from the cloud"
                         + (time.strftime(" (fetched %Y-%m-%d %H:%M)", time.localtime(fetched)) if fetched else "")
                         + f", {len(t.get('devices') or [])} by hand in config.json")
            on = sum(1 for d in tds if d.get("online"))
            summary = f"{on} of {len(tds)} device(s) online" if tds else "set up, no device yet"
        else:
            summary = "not set up"
        for d in tds:
            s_ = d["state"]
            temp = s_.get("temperature")
            lines.append(f"{d['name']}: {'online' if d.get('online') else 'offline'}, {s_.get('power', '?')}"
                         + (f", {temp} °C" if temp is not None else "")
                         + (f", set {s_['setpoint']} °C" if s_.get("setpoint") is not None else "")
                         + (f", {s_['mode']}" if s_.get("mode") else "")
                         + (f"; {REG.info[d['id']]}" if REG.info.get(d["id"]) else ""))
            if REG.commands.get(d["id"]):
                lines.append(f"{d['name']} last commands from SmartThings: " + "; ".join(REG.commands[d["id"]]))
            if (TUYA.get("raw") or {}).get(d["id"]):
                lines.append(f"{d['name']} data points: {TUYA['raw'][d['id']]}")
        out.append(("tuya", "Tuya / Smart Life thermostats and heaters (local)", summary, lines))

        # AEG
        e = cfg.get("electrolux") or {}
        devs = [d for d in REG.devices.values() if d["type"] == "dryer"]
        if not aeg_configured(e):
            summary, lines = "not set up", []
        elif devs:
            summary = "connected"
            lines = [f"{d['name']}: {d['state'].get('applianceState')}" for d in devs]
        else:
            summary, lines = "set up, no dryer found yet (see bridge.log)", []
        out.append(("aeg", "AEG / Electrolux dryer", summary, lines))

        # Home Connect and Samsung (set up with files, see README)
        hcs = [d for d in REG.devices.values() if d["id"].startswith("homeconnect-")]
        if hcs:
            summary = f"{sum(1 for d in hcs if d.get('online'))} of {len(hcs)} connected"
            lines = [f"{d['name']}: {'connected' if d.get('online') else 'not connected'}, "
                     f"{d['state'].get('operation', '?')}" for d in hcs]
        elif homeconnect_configured():
            summary, lines = "profiles found, starting", ["Needs Python 3.13 or newer, see bridge.log."]
        else:
            summary, lines = "not set up", []
        out.append(("homeconnect", "Home Connect dishwasher and oven (local)", summary, lines))
        sss = [d for d in REG.devices.values() if d["id"].startswith("samsung-")]
        summary = (f"{sum(1 for d in sss if d.get('online'))} of {len(sss)} online" if sss else "not set up")
        lines = [f"{d['name']}: {'online' if d.get('online') else 'offline'}" for d in sss]
        out.append(("samsung", "Samsung washer (local, experimental)", summary, lines))

        # Škoda
        k = cfg.get("skoda") or {}
        cars = [d for d in REG.devices.values() if d["type"] == "car"]
        if not skoda_configured(k):
            summary, lines = "not set up", []
        elif cars:
            summary = "connected"
            lines = [f"{d['name']}: {d['state'].get('battery')}%, key valid until {d['state'].get('keyExpires') or '?'}"
                     for d in cars]
        else:
            summary, lines = "set up, no data yet (see bridge.log)", []
        out.append(("skoda", "Škoda car", summary, lines))

        # VELUX
        v = cfg.get("velux") or {}
        lines = []
        if not velux_configured(v):
            summary = "not set up"
        else:
            locks = [d for d in REG.devices.values() if d["type"] == "velux_lock"]
            if VELUX.get("error"):
                summary = "error"
                lines.append(str(VELUX["error"]))
            elif locks:
                st = locks[0]["state"]
                summary = "locked (departure mode)" if st.get("locked") else "unlocked"
                if not st.get("canUnlock"):
                    lines.append("Unlocking needs Pair gateway first.")
            else:
                summary = "set up, connecting"
            pstate = VELUX_PAIR.get("state")
            lines += {"press": ["Press the button on the gateway NOW (light blinks). Then reload this page."],
                      "waiting": ["Starting to pair the gateway..."],
                      "done": ["Gateway paired: unlocking works."],
                      "failed": ["Pairing failed: " + str(VELUX_PAIR.get("error"))]}.get(pstate, [])
        out.append(("velux", "VELUX ACTIVE departure mode", summary, lines))

        # Rain radar
        b = cfg.get("buienradar") or {}
        if buienradar_enabled(b):
            rd = REG.devices.get("buienradar")
            summary = rd["state"].get("summary", "") if rd else "fetching"
            lines = [f"{b.get('place') or 'no location'}, radius {b.get('radius_m', 0)} m"]
        else:
            summary, lines = "off", []
        out.append(("rain", "Rain radar", summary, lines))

        # Amazon Echo
        a = cfg.get("echo") or {}
        lines = []
        if ECHO_LOGIN.get("error"):
            lines.append("Last login attempt: " + str(ECHO_LOGIN["error"]))
        if not echo_configured(a):
            summary = "not set up"
        elif ECHO.get("error"):
            summary = "error"
            lines.append(str(ECHO["error"]))
        elif ECHO.get("updated"):
            summary = f"connected, {ECHO['devices']} device(s)"
            lines += [d["name"] for d in REG.devices.values() if d["type"] == "echo"]
        else:
            summary = "logged in, fetching devices"
        if echo_configured(a):
            lines.append(f"Logged in on {a.get('url', 'amazon.de')}. Log in again only when the bridge reports that "
                         "the login expired.")
        out.append(("echo", "Amazon Echo", summary, lines))

        # iRobot
        r = cfg.get("irobot") or {}
        lines = []
        if not irobot_configured(r):
            summary = "not set up"
        elif IROBOT.get("error"):
            summary = "error"
            lines.append(str(IROBOT["error"]))
        elif IROBOT.get("updated"):
            summary = "connected"
            lines = [f"{v_.get('name') or k_}: {len(v_['favorites'])} favorite(s)" for k_, v_ in IROBOT["robots"].items()]
        else:
            summary = "set up, fetching"
        out.append(("irobot", "iRobot favorites", summary, lines))
        return out

    def status_html(sections) -> tuple[str, dict[str, str]]:
        """Overview (a list like the SmartThings app, each part with its status below it), and the status block per part."""
        items = []
        blocks = {}
        for anchor, title, summary, lines in sections:
            bad = summary.startswith(("NOT", "error")) or summary == "error"
            items.append(f'<a class="item{" bad" if bad else ""}" href="#{anchor}"><span class="t"><b>{html.escape(title)}</b>'
                         f'<span>{html.escape(summary[:1].upper() + summary[1:])}</span></span>'
                         '<span class="go" aria-hidden="true">&#x203A;</span></a>')
            body = "<br>".join(html.escape(x) for x in [summary[:1].upper() + summary[1:]] + lines)
            blocks[anchor] = f'<div class="st{" bad" if bad else ""}">{body}</div>'
        return '<div class="list">' + "".join(items) + "</div>", blocks

    def plain_from_lan(req) -> bool:
        """Setup page over plain http from another address than this computer: credentials would cross the LAN
        unencrypted. This includes the Docker gateway (on Docker Desktop that is also the whole LAN). Without https
        (certificate could not be made) only this computer can open the setup page."""
        try:
            loop = ipaddress.ip_address(req.remote or "").is_loopback
        except ValueError:
            loop = False
        return not req.secure and not loop

    def key_ok(req) -> bool:
        try:
            ip = ipaddress.ip_address(req.remote or "")
        except ValueError:
            return False
        if not setup_needs_key(ip):
            return True
        return hmac.compare_digest(req.cookies.get("stbridge_setup", ""), setup_cookie_value())

    def no_https(_req) -> web.Response:
        return web.Response(status=403, text="https is not available on this bridge (see bridge.log), so the setup "
                            "page only opens in a browser on this computer (http://localhost:8787/setup)")

    def key_page(bad: bool = False) -> web.Response:
        return web.Response(content_type="text/html", headers={"Cache-Control": "no-store"}, text=(
            '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Stoeckie Bridge setup key</title>'
            '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
            '<body style="font:15px system-ui,sans-serif;max-width:640px;margin:32px auto;padding:0 16px">'
            '<h2>Setup key</h2>' + ('<p><b>Wrong key.</b></p>' if bad else '') +
            '<p>The bridge runs in a container and cannot tell this browser apart from other computers on your LAN. '
            'Enter the setup key: it is in the bridge log (<code>docker logs stoeckie-bridge</code>) and in '
            '<code>data/setup-key.txt</code>.</p><form method="post"><input type="hidden" name="section" '
            'value="setup_key"><input name="key" autocomplete="off" required style="width:100%;padding:8px">'
            '<button type="submit" style="margin-top:14px;padding:10px 18px">Open setup</button></form></body></html>'))

    def echo_link() -> str:
        if ECHO_LOGIN.get("proxy") is None or ECHO_LOGIN.get("done"):
            return ('<p class="hint">Available after step 1. You then log in on Amazon\'s own page (relayed by the '
                    'bridge) and come back here by yourself.</p>')
        return ('<p class="hint">Amazon\'s own login page opens, relayed by the bridge. Log in there; afterwards you '
                'come back here by yourself and the status above says connected.</p>'
                '<a class="btn" href="' + html.escape(str(ECHO_LOGIN["url"])) + '">Log in at Amazon</a>')

    async def setup_get(req):
        if not local_only(req):
            return web.Response(status=403, text="setup only from this computer (localhost) or an address in STBRIDGE_SETUP_ALLOW")
        if plain_from_lan(req):
            if https_port is None:
                return no_https(req)
            host = (req.host or "").rsplit(":", 1)[0]
            raise web.HTTPFound(f"https://{host}:{https_port}/setup")
        if not key_ok(req):
            return key_page()
        k = load_config().get("skoda") or {}
        rb = load_config().get("buienradar") or {}
        tu = load_config().get("tuya") or {}
        tuya_region = str(tu.get("region") or "eu")
        # only a masked Access ID: the secret and the local keys never go into the page
        tuya_saved = (f"Saved: Access ID {mask(tu.get('api_key'))}, region {tuya_region}. Enter both again to change "
                      "them." if tuya_cloud_configured(tu) else "Not set up yet.")
        echo_url = str((load_config().get("echo") or {}).get("url") or "amazon.de")
        pairing_open = PAIRING["until"] > time.time()
        reqs = "".join(
            '<form method="post"><input type="hidden" name="section" value="api_allow">'
            f'<input type="hidden" name="ip" value="{html.escape(ip)}"><button type="submit">Allow hub '
            f'{html.escape(ip)}</button></form>' for ip in (PAIRING["asked"] if pairing_open else {}))
        if pairing_open and PAIRING["allowed"]:
            reqs += f"<p>Allowed {html.escape(PAIRING['allowed'])}: the hub picks up the token at its next poll.</p>"
        elif pairing_open and not reqs:
            reqs = "<p>Waiting for the hub to ask (it asks at every poll).</p>"
        overview, blocks = status_html(status_sections())
        page = SETUP_HTML
        for anchor, block in blocks.items():
            page = page.replace("{st_" + anchor + "}", block)
        page = (page.replace("{version}", "version " + VERSION).replace("{status}", overview)
                .replace("{refresh}", '<meta http-equiv="refresh" content="10">' if pairing_open else "")
                .replace("{pair_requests}", reqs)
                .replace("{skoda_poll}", html.escape(str(k.get("poll_minutes", 10))))
                .replace("{skoda_temp}", html.escape(str(k.get("climate_temp", 21))))
                .replace("{fglair_poll}", html.escape(str((load_config().get("fglair") or {}).get("poll_seconds", 60))))
                .replace("{velux_poll}", html.escape(str((load_config().get("velux") or {}).get("poll_seconds", 60))))
                .replace("{rain_pc}", html.escape(str(rb.get("postcode") or "")))
                .replace("{rain_nr}", html.escape(str(rb.get("housenumber") or "")))
                .replace("{rain_loc}", html.escape(str(rb.get("location") or "")))
                .replace("{rain_addr}", html.escape(str(rb.get("address") or "")))
                .replace("{rain_p_auto}", " selected" if rb.get("provider", "auto") == "auto" else "")
                .replace("{rain_p_buienradar}", " selected" if rb.get("provider") == "buienradar" else "")
                .replace("{rain_p_openmeteo}", " selected" if rb.get("provider") == "openmeteo" else "")
                .replace("{rain_radius}", html.escape(str(rb.get("radius_m", int(float(rb.get("radius_km") or 0) * 1000)))))
                .replace("{rain_thr}", html.escape(str(rb.get("threshold", RAIN_THRESHOLD))))
                .replace("{velux_ip}", html.escape(str((load_config().get("velux") or {}).get("gateway_ip", ""))))
                .replace("{tuya_saved}", html.escape(tuya_saved))
                .replace("{echo_domains}", "".join(
                    f'<option value="{d}"{" selected" if d == echo_url else ""}>{d}</option>' for d in ECHO_DOMAINS))
                .replace("{echo_link}", echo_link())
                .replace("{echo_s1}", " done" if ECHO_LOGIN.get("proxy") is not None and not ECHO_LOGIN.get("done") else "")
                .replace("{echo_s1_open}", "" if ECHO_LOGIN.get("proxy") is not None and not ECHO_LOGIN.get("done")
                         else " open")
                .replace("{echo_s2}", " active" if ECHO_LOGIN.get("proxy") is not None and not ECHO_LOGIN.get("done")
                         else " off"))
        for r in TUYA_REGIONS:
            page = page.replace("{tuya_r_" + r + "}", " selected" if tuya_region == r else "")
        return web.Response(text=page, content_type="text/html")

    async def setup_post(req):
        if not local_only(req):
            return web.Response(status=403, text="setup only from this computer (localhost) or an address in STBRIDGE_SETUP_ALLOW")
        if plain_from_lan(req):
            return no_https(req) if https_port is None else web.Response(status=403, text="use https for the setup page")
        form = await req.post()
        section = str(form.get("section", "aeg"))
        if section == "setup_key":
            if hmac.compare_digest(str(form.get("key", "")).strip(), setup_key()):
                resp = web.Response(status=302, headers={"Location": "/setup"})
                resp.set_cookie("stbridge_setup", setup_cookie_value(), httponly=True, secure=True,
                                samesite="Strict", max_age=365 * 86400, path="/setup")
                log.info("[setup] setup key accepted from %s", req.remote)
                return resp
            await asyncio.sleep(2)
            return key_page(bad=True)
        if not key_ok(req):
            return key_page()
        cfg = load_config()
        if section == "skoda":
            k = cfg.setdefault("skoda", {})
            k["api_key"] = "".join(str(form.get("api_key", "")).split())
            k["vin"] = "".join(str(form.get("vin", "")).split()).upper()
            save_config(cfg)
            log.info("[setup] Škoda key saved")
        elif section == "skoda_settings":
            k = cfg.setdefault("skoda", {})
            k.update(skoda_settings(form.get("poll_minutes"), form.get("climate_temp"), k))
            save_config(cfg)
            log.info("[setup] Škoda settings: every %s min, climate %s C", k["poll_minutes"], k["climate_temp"])
            section = "skoda"
        elif section == "irobot":
            r = cfg.setdefault("irobot", {})
            r["username"] = str(form.get("username", "")).strip()
            r["password"] = str(form.get("password", ""))
            r["country"] = str(form.get("country", "NL")).strip().upper()[:2] or "NL"
            save_config(cfg)
            log.info("[setup] iRobot login saved")
        elif section == "echo":
            a = cfg.setdefault("echo", {})
            a["email"] = str(form.get("email", "")).strip()
            url = str(form.get("url", "amazon.de"))
            a["url"] = url if url in ECHO_DOMAINS else "amazon.de"
            otp = "".join(str(form.get("otp_secret", "")).split())
            if otp:
                a["otp_secret"] = otp
            save_config(cfg)
            login_cfg = dict(a, password=str(form.get("password", "")), oauth={})
            try:
                from alexapy import AlexaProxy
                login = echo_login_obj(login_cfg)
                base = f"https://{req.host}/setup/echo-auth"
                proxy = AlexaProxy(login, base)
                done = f"https://{req.host}/setup/echo-done"
                ECHO_LOGIN.update(login=login, proxy=proxy, done=False, error=None,
                                  url=str(proxy.access_url().with_query({"config_flow_id": "stbridge",
                                                                         "callback_url": done})))
                login._session.cookie_jar.clear()  # noqa: SLF001
                login.proxy_url = ECHO_LOGIN["url"]
                log.info("[setup] Amazon Echo login started")
            except Exception as ex:  # noqa: BLE001
                ECHO_LOGIN.update(proxy=None, error=safe_err(ex)[:200])
                log.warning("[setup] Amazon Echo login could not start: %s", safe_err(ex))
            raise web.HTTPFound("/setup#echo2")
        elif section in ("fglair_settings", "velux_settings"):
            name = section.split("_")[0]
            sec = poll_seconds(form.get("poll_seconds"), 60)
            save_poll_seconds(name, sec)
            log.info("[setup] %s: every %s s", name, sec)
            # the running source picks it up at its next update; restart it so it applies now
            section = name
        elif section == "buienradar":
            b = cfg.setdefault("buienradar", {})
            v = {k: form.get(k, "") for k in ("postcode", "housenumber", "address", "location", "radius_m", "threshold",
                                              "provider")}
            new = buienradar_settings(v, b)
            if not (new.get("location") or (new.get("postcode") and new.get("housenumber")) or new.get("address")):
                return web.Response(status=400, text="enter a postcode and house number, an address, or latitude, longitude")
            b.update(new)
            b["enabled"] = True
            b.pop("radius_km", None)
            save_config(cfg)
            log.info("[setup] Buienradar settings saved, radius %s m", b.get("radius_m"))
        elif section == "velux":
            v = cfg.setdefault("velux", {})
            v["username"] = str(form.get("username", "")).strip()
            v["password"] = str(form.get("password", ""))
            v.pop("tokens", None)
            save_config(cfg)
            VELUX["error"] = None
            log.info("[setup] VELUX login saved")
        elif section == "api_pair":
            api_token(cfg)
            PAIRING.update(until=time.time() + PAIR_WINDOW, asked={}, allowed=None)
            log.info("[setup] pairing with SmartThings open for %d s", PAIR_WINDOW)
            raise web.HTTPFound("/setup")
        elif section == "api_allow":
            ip = str(form.get("ip", ""))
            if PAIRING["until"] > time.time() and ip in PAIRING["asked"]:
                PAIRING["allowed"] = ip
                log.info("[setup] pairing allowed for hub %s", ip)
            raise web.HTTPFound("/setup")
        elif section == "api_new_token":
            if cfg.get("api_token"):
                cfg["api_token_prev"] = cfg["api_token"]
            cfg["api_token"] = secrets.token_hex(32)
            cfg["api_paired"] = True
            save_config(cfg)
            PAIRING.update(until=time.time() + PAIR_WINDOW, asked={}, allowed=None)
            log.info("[setup] new API token, pairing open for %d s", PAIR_WINDOW)
            raise web.HTTPFound("/setup")
        elif section == "velux_pair":
            ip = str(form.get("gateway_ip", "")).strip()
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                return web.Response(status=400, text="not a valid IP address")
            src = VELUX.get("source")
            if src is None or src.session is None:
                return web.Response(status=409, text="Set up the VELUX login first (and wait until it is connected).")
            if VELUX_PAIR.get("state") not in ("waiting", "press"):
                asyncio.get_running_loop().create_task(src.pair(ip))
            await asyncio.sleep(3)
            raise web.HTTPFound("/setup")
        elif section in ("tuya", "tuya_fetch"):
            t = cfg.setdefault("tuya", {})
            if section == "tuya":
                key = "".join(str(form.get("api_key", "")).split())
                secret = "".join(str(form.get("api_secret", "")).split())
                region = str(form.get("region", "eu"))
                if not (re.fullmatch(r"[A-Za-z0-9]{8,64}", key) and re.fullmatch(r"[A-Za-z0-9]{8,64}", secret)
                        and region in TUYA_REGIONS):
                    return web.Response(status=400, text="Access ID and Access Secret are letters and digits "
                                                         "(copy them from the cloud project on iot.tuya.com)")
                t.update(api_key=key, api_secret=secret, region=region)
            elif not tuya_cloud_configured(t):
                return web.Response(status=409, text="Save the Tuya Access ID and Access Secret first.")
            t["fetch"] = True
            save_config(cfg)
            TUYA["error"] = None
            log.info("[setup] Tuya %s", "login saved" if section == "tuya" else "fetch devices")
            section = "tuya"
        elif section == "fglair":
            f = cfg.setdefault("fglair", {})
            f["username"] = str(form.get("username", "")).strip()
            f["password"] = str(form.get("password", ""))
            f["region"] = str(form.get("region", "eu"))
            save_config(cfg)
            log.info("[setup] FGLair login saved")
        else:
            e = cfg.setdefault("electrolux", {})
            for k in ("api_key", "access_token", "refresh_token"):
                e[k] = "".join(str(form.get(k, "")).split())
            save_config(cfg)
            log.info("[setup] AEG credentials saved")
        if on_setup:
            on_setup(section)
        await asyncio.sleep(4)
        raise web.HTTPFound("/setup")

    async def info(_req):
        return web.json_response({"service": "stoeckie-bridge", "id": bridge_id, "version": VERSION,
                                  "name": socket.gethostname(), "paired": api_paired()})

    async def pair(req):
        """Hand the token to a hub, only while "Pair" on the setup page is open (5 minutes, one hub) and only to the
        address that was allowed on the setup page. Until then the hub gets {"waiting": ...} and asks again."""
        if time.time() > PAIRING["until"]:
            return web.json_response({"error": "pairing closed: press Pair on the bridge setup page"},
                                     status=403)
        ip = str(req.remote or "")
        if PAIRING["allowed"] != ip:
            if ip not in PAIRING["asked"]:
                if len(PAIRING["asked"]) >= 8:
                    return web.json_response({"error": "too many pairing requests"}, status=429)
                log.info("[api] %s asks to pair: press Allow on the setup page if this is your hub", ip)
            PAIRING["asked"][ip] = time.time()
            return web.json_response({"waiting": "press Allow for " + ip + " on the bridge setup page", "ip": ip})
        cfg = load_config()
        tok = api_token(cfg)
        cfg["api_paired"] = True
        cfg["api_paired_ip"] = ip
        save_config(cfg)
        PAIRING.update(until=0.0, asked={}, allowed=None, last_ip=ip)
        log.info("[api] hub %s paired", ip)
        # a hub that already has a (previous) token only accepts the new one with this proof, so a device on the
        # LAN that pretends to be the bridge cannot slip it a token of its own
        proof = sign(cfg.get("api_token_prev") or tok, tok.encode())
        return web.json_response({"token": tok, "id": bridge_id, "proof": proof})

    async def srp(req):
        """HomeKit pair-setup helper for a paired hub. The request must be signed (checked by guard) and is
        encrypted with a key derived from the shared token, so the HomeKit code never crosses the LAN in the clear."""
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        cfg = load_config()
        if not api_paired(cfg) or not req.headers.get("X-Stbridge-Sig"):
            return web.json_response({"error": "pairing required"}, status=401)
        if time.time() - _SRP_LAST["t"] < 5:
            return web.json_response({"error": "busy, try again in a few seconds"}, status=429)
        _SRP_LAST["t"] = time.time()
        tok = cfg["api_token"]
        try:
            body = await req.json()
            plain = ChaCha20Poly1305(srp_box_key(tok, "request")).decrypt(
                bytes.fromhex(body["nonce"]), bytes.fromhex(body["box"]), None)
            d = json.loads(plain)
            salt, b_pub, pin = bytes.fromhex(d["salt"]), bytes.fromhex(d["B"]), str(d["pin"])
            if not (0 < len(salt) <= 64 and 0 < len(b_pub) <= 384) or len(pin) != 10 or \
                    not (pin[:3] + pin[4:6] + pin[7:]).isdigit() or pin[3] != "-" or pin[6] != "-":
                raise ValueError("bad input")
            t0 = time.time()
            big_a, proof, key = await asyncio.get_running_loop().run_in_executor(
                None, srp_homekit_client, salt, b_pub, pin)
        except Exception as err:  # noqa: BLE001
            log.warning("[api] SRP helper refused a request from %s: %s", req.remote, type(err).__name__)
            return web.json_response({"error": "bad request"}, status=400)
        log.info("[api] SRP step for a HomeKit pairing computed for %s in %.2f s", req.remote, time.time() - t0)
        nonce = secrets.token_bytes(12)
        out = json.dumps({"A": big_a.hex(), "M1": proof.hex(), "K": key.hex()}).encode()
        box = ChaCha20Poly1305(srp_box_key(tok, "response")).encrypt(nonce, out, None)
        return web.json_response({"nonce": nonce.hex(), "box": box.hex()})

    async def devices(_req):
        # "diag": false tells the driver the bridge has no diagnostics for an air conditioner yet (e.g. after a
        # bridge restart), so it sends them again
        out = [dict(d, diag=d["id"] in REG.diag) if d.get("type") == "aircon" else d for d in REG.devices.values()]
        return web.json_response({"devices": out, "time": int(time.time())})

    async def irobot(req):
        blid = req.match_info["blid"]
        if req.query.get("refresh") and IROBOT.get("refresh"):
            IROBOT["refresh"].set()
        robot = IROBOT["robots"].get(blid)
        if robot is None:
            return web.json_response({"error": "unknown robot or iRobot not set up", "updated": IROBOT["updated"]},
                                     status=404)
        return web.json_response({"name": robot["name"], "favorites": robot["favorites"],
                                  "updated": IROBOT["updated"]})

    async def buienradar_set(req):
        """Switch the rain radar on/off from the SmartThings bridge device ({"enabled": true}); optional settings."""
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            body = {}
        cfg = load_config()
        b = cfg.setdefault("buienradar", {})
        changed = False
        if "enabled" in body and bool(body["enabled"]) != bool(b.get("enabled")):
            b["enabled"] = bool(body["enabled"])
            changed = True
        settings = {k: body[k] for k in ("postcode", "housenumber", "address", "location", "radius_m", "threshold",
                                         "provider", "humid_hours") if k in body}
        if settings:
            new = buienradar_settings(settings, b)
            if any(b.get(k) != new.get(k) for k in new):
                b.update(new)
                b.pop("radius_km", None)
                changed = True
        if changed:
            if not b.get("enabled"):
                b.pop("lat", None)  # off means off, also when a location was set before
                b.pop("lon", None)
            save_config(cfg)
            log.info("[buienradar] %s from SmartThings", "on" if b.get("enabled") else "off")
            if on_setup:
                on_setup("buienradar")
            if not b.get("enabled"):
                REG.devices.pop("buienradar", None)
        return web.json_response({"ok": True, "enabled": bool(b.get("enabled"))})

    async def command(req):
        dev_id = req.match_info["dev_id"]
        handler = REG.handlers.get(dev_id)
        if not handler:
            return web.json_response({"error": "unknown device"}, status=404)
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            body = {}
        cmd = str(body.get("cmd", ""))
        if cmd == "diag":  # the driver tells what it shows (driver build, profile, unit); only for the setup page
            if isinstance(body.get("value"), dict):
                new = {k: str(v)[:60] for k, v in list(body["value"].items())[:8]}
                if REG.diag.get(dev_id) != new:  # one line per change, so the log shows which driver build runs
                    log.info("[api] %s: driver build %s, profile %s, unit %s", dev_id, new.get("build", "?"),
                             new.get("profile", "?"), new.get("unit", "?"))
                REG.diag[dev_id] = new
            return web.json_response({"ok": True})
        if cmd != "refresh":
            # settings carry an address or postcode: not in the log or on the setup page
            shown = None if cmd == "settings" or body.get("value") is None else body.get("value")
            # texts for an Echo (spoken, notification, music, routine) can be personal: only their length
            if dev_id.startswith("echo-") and cmd in ECHO_TEXT_CMDS and shown is not None:
                shown = f"({len(str(shown))} chars)"
            REG.note_command(dev_id, cmd, shown)
            log.info("[api] %s: %s %s", dev_id, cmd, "" if shown is None else shown)
        try:
            await handler(cmd, body.get("value"))
        except Exception as ex:  # noqa: BLE001
            log.error("command %s on %s failed: %s", cmd, dev_id, safe_err(ex))
            return web.json_response({"error": safe_err(ex)}, status=500)
        return web.json_response({"ok": True, "device": REG.devices.get(dev_id)})

    app.middlewares.append(guard)
    app.router.add_get("/setup", setup_get)

    async def claude_setup(req):
        if not local_only(req):
            return web.Response(status=403, text="setup only from this computer or an allowed address")
        if plain_from_lan(req):
            return no_https(req) if https_port is None else web.Response(status=403, text="use https for the setup page")
        if not key_ok(req):
            return key_page()
        if req.method == "POST":
            go = await claude_setup_post(await req.post(), load_config())
            raise web.HTTPFound(go or "/setup/claude")
        return web.Response(content_type="text/html", text=claude_setup_html(load_config()),
                            headers={"Cache-Control": "no-store"})

    app.router.add_route("*", "/setup/claude", claude_setup)
    app.router.add_post("/setup", setup_post)
    async def root(_req):
        raise web.HTTPFound("/setup")

    app.router.add_get("/", root)
    async def echo_auth(req):
        if not local_only(req) or plain_from_lan(req) or not key_ok(req):
            return web.Response(status=403, text="setup only")
        proxy = ECHO_LOGIN.get("proxy")
        if proxy is None or ECHO_LOGIN.get("done"):
            raise web.HTTPFound("/setup")
        return await proxy.all_handler(req)

    async def echo_done(req):
        if not local_only(req) or plain_from_lan(req) or not key_ok(req):
            return web.Response(status=403, text="setup only")
        login = ECHO_LOGIN.get("login")
        try:
            if login is None or not await login.test_loggedin():
                raise RuntimeError("Amazon did not confirm the login, try again")
            await login.finalize_login()
            echo_save_tokens(login)
            ECHO_LOGIN.update(done=True, error=None, proxy=None)
            log.info("[setup] Amazon Echo login done")
            if on_setup:
                on_setup("echo")
            await asyncio.sleep(4)
        except Exception as ex:  # noqa: BLE001
            ECHO_LOGIN.update(error=safe_err(ex)[:200])
            log.warning("[setup] Amazon Echo login failed: %s", safe_err(ex))
        raise web.HTTPFound("/setup#echo")

    app.router.add_route("*", "/setup/echo-auth{tail:.*}", echo_auth)
    app.router.add_get("/setup/echo-done", echo_done)
    app.router.add_get("/stbridge/info", info)
    app.router.add_post("/stbridge/pair", pair)
    app.router.add_post("/stbridge/srp", srp)
    app.router.add_get("/stbridge/devices", devices)
    app.router.add_post("/stbridge/devices/{dev_id}/command", command)
    app.router.add_get("/stbridge/irobot/{blid}", irobot)
    app.router.add_post("/stbridge/buienradar", buienradar_set)
    return app


async def demo_devices() -> None:
    """Fake devices for testing the Edge driver without real hardware (--demo)."""
    pos = {"v": 0}

    async def win(cmd, value):
        pos["v"] = {"open": 100, "close": 0}.get(cmd, int(value or 0) if cmd == "level" else pos["v"])
        REG.upsert("demo-window", "window", "Demo roof window", {"position": pos["v"], "target": pos["v"],
                                                             "positionState": "stopped"})

    async def dry(cmd, value):
        pass

    ac = {"power": "on", "mode": "heat", "setpoint": 21.0, "temperature": 19.5, "outdoorTemperature": 8.0,
          "fan": "auto", "swing": "off", "supportedModes": ["auto", "cool", "dry", "fan", "heat"],
          "supportedFans": ["quiet", "low", "medium", "high", "auto"],
          "supportedSwings": ["off", "vertical"], "setpointRange": [16.0, 30.0], "model": "DEMO",
          "minHeat": False, "vDir": 2, "vDirCount": 4, "hDir": 3, "hDirCount": 5}
    ac["supportedModes"].append("minheat")

    async def aircon(cmd, value):
        if cmd == "on":
            ac["power"] = "on"
            ac["mode"] = ac.get("last_mode", "heat")
        elif cmd == "off":
            ac["last_mode"], ac["power"], ac["mode"] = ac["mode"], "off", "off"
        elif cmd == "mode":
            ac["power"], ac["mode"] = "on", str(value)
            ac["minHeat"] = ac["mode"] == "minheat"
        elif cmd == "setpoint":
            ac["setpoint"] = float(value)
        elif cmd in ("fan", "swing"):
            ac[cmd] = str(value)
        elif cmd == "vdir" and ac["vDirCount"] and 1 <= int(value) <= ac["vDirCount"]:
            ac["vDir"], ac["swing"] = int(value), "off"
        elif cmd == "hdir" and ac["hDirCount"] and 1 <= int(value) <= ac["hDirCount"]:
            ac["hDir"], ac["swing"] = int(value), "off"
        elif cmd == "minheat":
            ac["minHeat"] = str(value).lower() in ("on", "1", "true")
            ac["power"], ac["mode"] = "on", "minheat" if ac["minHeat"] else "heat"
        if cmd == "off":
            ac["minHeat"] = False
        REG.upsert("demo-aircon", "aircon", "Demo air conditioner", ac, True, aircon)

    await win("refresh", None)
    REG.handlers["demo-window"] = win
    await aircon("refresh", None)
    car = {"battery": 64, "electricRange": 38, "totalRange": 610, "charging": "READY_FOR_CHARGING",
           "chargePowerKw": 0, "plug": "CONNECTED", "atHome": True, "doors": "CLOSED", "windows": "CLOSED",
           "locked": "YES", "climate": "OFF", "odometer": 41234, "targetSoc": 100}

    async def demo_car(cmd, value):
        if cmd == "on":
            car.update(charging="CHARGING", chargePowerKw=3.6)
        elif cmd == "off":
            car.update(charging="READY_FOR_CHARGING", chargePowerKw=0)
        elif cmd == "climate_on":
            car["climate"] = "ON"
        elif cmd == "climate_off":
            car["climate"] = "OFF"
        REG.upsert("demo-car", "car", "Demo car", car, True, demo_car)

    await demo_car("refresh", None)
    echo = {"volume": 30, "said": [], "dnd": "off", "playback": "stopped", "nowPlaying": "", "bluetooth": "none",
            "nextAlarm": "07:00", "timer": "none", "lastCommand": "what time is it",
            "lastCommandTime": "2026-11-11T07:00:00Z"}

    async def demo_echo(cmd, value):
        if cmd == "volume":
            echo["volume"] = int(value)
        elif cmd in ("speak", "announce", "command"):
            echo["said"] = echo["said"][-4:] + [cmd + ":" + str(value)]
            if cmd == "command":
                echo["lastCommand"] = str(value)[:250]
                echo["lastCommandTime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        elif cmd == "dnd":
            echo["dnd"] = "on" if str(value).lower() in ("on", "true", "1") else "off"
        elif cmd == "music":
            echo["playback"], echo["nowPlaying"] = "playing", echo_music(value)[1]
        elif cmd in ("play", "pause", "stop"):
            echo["playback"] = {"play": "playing", "pause": "paused"}.get(cmd, "stopped")
        elif cmd == "bluetooth":
            echo["bluetooth"] = "none" if str(value).lower() in ("", "off", "none") else str(value)
        REG.upsert("demo-echo", "echo", "Demo Echo", dict(echo), True, demo_echo)

    await demo_echo("refresh", None)
    velux = {"locked": True, "canUnlock": True}
    REG.upsert("demo-rain", "rain", "Demo rain sensor", {"raining": False}, True, dry)

    async def demo_velux(cmd, value):
        if cmd in ("lock", "unlock"):
            velux["locked"] = cmd == "lock"
        REG.upsert("demo-velux", "velux_lock", "Demo VELUX departure mode", velux, True, demo_velux)

    await demo_velux("refresh", None)
    rain = rain_summary([0.0] * 4 + [0.5, 1.2, 0.8] + [0.0] * 17)
    rain.update(dewPointNow=7.4, dewPointMax=11.2, humidHours=48)
    REG.upsert("demo-rainradar", "rainradar", "Demo rain radar", rain, True, dry)
    dish = {"power": "on", "operation": "run", "door": "closed", "program": "eco50", "remainingTime": 3600,
            "completionTime": _iso_in(3600), "progress": 40, "remoteStartAllowed": False,
            "remoteControlActive": True, "programs": ["eco50", "auto2", "quick45", "machineCare"]}
    oven = {"power": "on", "operation": "run", "door": "closed", "program": "hotAir", "remainingTime": 1200,
            "completionTime": _iso_in(1200), "progress": 20, "remoteStartAllowed": False,
            "remoteControlActive": True, "temperature": 165.0, "setpoint": 180.0, "duration": 0,
            "programs": ["hotAir", "topBottom", "pizza", "micro600"]}

    async def demo_hc(dev, st, cmd, value):
        if cmd in ("on", "off"):
            st["power"] = cmd
        elif cmd == "stop":
            st.update(operation="inactive", remainingTime=None, completionTime=None, progress=None)
        elif cmd in ("pause", "resume"):
            st["operation"] = "pause" if cmd == "pause" else "run"
        elif cmd == "program" and value in st.get("programs", []):
            st["program"] = value
        elif cmd == "setpoint":
            st["setpoint"] = float(value)
        elif cmd == "duration":
            st["duration"] = int(value)
        elif cmd == "start":
            if value in st.get("programs", []):
                st["program"] = value
            st["operation"] = "run"
        REG.upsert(dev, dev.split("-")[1], "Demo " + dev.split("-")[1], st, True,
                   lambda c, v, d=dev, s_=st: demo_hc(d, s_, c, v))

    washer = {"power": "on", "operation": "run", "job": "wash", "remainingTime": 2400,
              "completionTime": _iso_in(2400), "progress": 35, "remoteStartAllowed": True, "course": "Cotton",
              "courseCode": "d0", "program": "cotton", "programs": ["cotton", "eco4060", "quick15", "wool"]}

    async def demo_washer(cmd, value):
        if cmd in ("on", "off"):
            washer["power"] = cmd
        elif cmd in ("run", "start", "resume"):
            washer["operation"] = "run"
        elif cmd == "pause":
            washer["operation"] = "pause"
        if cmd in ("program", "start") and value in washer["programs"]:
            washer["program"] = value
        elif cmd == "stop":
            washer.update(operation="inactive", job="none", remainingTime=None, completionTime=None, progress=None)
        REG.upsert("demo-washer", "washer", "Demo washer", washer, True, demo_washer)

    await demo_washer("refresh", None)
    # Tuya: the real mapping, state and command code on a floor heating thermostat and an infrared panel
    tuya_demo = {
        "demo-tuya-thermostat": ("tuya_thermostat", "Demo floor heating", {
            "1": {"code": "switch", "type": "Boolean", "values": {}},
            "2": {"code": "temp_set", "type": "Integer", "values": {"min": 50, "max": 350, "scale": 1, "step": 5}},
            "3": {"code": "temp_current", "type": "Integer", "values": {"min": -100, "max": 500, "scale": 1}},
            "4": {"code": "mode", "type": "Enum", "values": {"range": ["auto", "manual", "holiday"]}},
            "5": {"code": "eco", "type": "Boolean", "values": {}},
            "6": {"code": "child_lock", "type": "Boolean", "values": {}},
            "36": {"code": "valve_state", "type": "Enum", "values": {"range": ["open", "close"]}},
            "102": {"code": "temp_floor", "type": "Integer", "values": {"min": 0, "max": 990, "scale": 1}}},
            {"1": True, "2": 215, "3": 198, "4": "manual", "5": False, "6": False, "36": "open", "102": 243}),
        "demo-tuya-heater": ("tuya_heater", "Demo infrared panel", {
            "1": {"code": "switch", "type": "Boolean", "values": {}},
            "2": {"code": "temp_set", "type": "Integer", "values": {"min": 5, "max": 40, "scale": 0, "step": 1}},
            "3": {"code": "temp_current", "type": "Integer", "values": {"min": -20, "max": 60, "scale": 0}}},
            {"1": False, "2": 20, "3": 18}),
    }

    async def demo_tuya(dev, cmd, value):
        kind, name, mapping, dps = tuya_demo[dev]
        roles = tuya_roles(tuya_functions(mapping))
        if cmd != "refresh":
            dps.update(tuya_command(cmd, value, roles, dps))
        REG.upsert(dev, kind, name, tuya_state(kind, roles, dps), True, lambda c, v, d=dev: demo_tuya(d, c, v))

    for dev in tuya_demo:
        await demo_tuya(dev, "refresh", None)
    await demo_hc("demo-dishwasher", dish, "refresh", None)
    await demo_hc("demo-oven", oven, "refresh", None)
    t0 = time.time()
    while True:
        el = int(time.time() - t0) % 180
        reported = {"applianceState": "RUNNING" if el < 120 else "END_OF_CYCLE",
                    "cyclePhase": "DRY" if el < 90 else ("COOL" if el < 120 else "UNAVAILABLE"),
                    "timeToEnd": max(0, 120 - el), "userSelections": {"programUID": "COTTON_PR_COTTONSECO"},
                    "doorState": "CLOSED", "remoteControl": "ENABLED"}
        REG.upsert("demo-dryer", "dryer", "Demo dryer", dryer_state(reported, "connected"), True, dry)
        await asyncio.sleep(5)


def seed_from_env(cfg: dict[str, Any]) -> None:
    """First start in Docker: take credentials from env vars. Afterwards config.json (with refreshed tokens) wins."""
    e = cfg.get("electrolux") or {}
    env = {k: os.environ.get("ELECTROLUX_" + k.upper()) for k in ("api_key", "access_token", "refresh_token")}
    if all(env.values()) and not aeg_configured(e):
        cfg.setdefault("electrolux", {}).update(env)
        save_config(cfg)
        log.info("[setup] AEG credentials taken from environment")
    f = cfg.get("fglair") or {}
    user, pw = os.environ.get("FGLAIR_USERNAME"), os.environ.get("FGLAIR_PASSWORD")
    if user and pw and not fglair_configured(f):
        cfg.setdefault("fglair", {}).update({"username": user, "password": pw,
                                             "region": os.environ.get("FGLAIR_REGION", "eu")})
        save_config(cfg)
        log.info("[setup] FGLair login taken from environment")


# ---------------------------------------------------------------- Claude (web and phone): MCP server
#
# Claude on claude.ai (web, phone apps) reaches the bridge through a public https address (Tailscale Funnel on
# this pc forwards it to 127.0.0.1:<claude.port>; nothing listens on the LAN for this). Three locks:
# * only Anthropic's published outbound addresses may use the MCP and token endpoints (claude.allowed_ips);
# * OAuth 2.1 with PKCE: connecting the connector in Claude opens a sign-in page here that asks the password set
#   on the setup page (stored as a PBKDF2 hash); Claude gets a 1 h access token and a rotating refresh token;
# * the SmartThings side uses an own OAuth app with a rotating refresh token in config.json (claude.st).
# Tokens are stored only as SHA-256 hashes (claude-tokens.json next to config.json). Nothing Claude asks or the
# answers are logged, only the tool name.

CLAUDE_DEFAULT_PORT = 8789
ANTHROPIC_NETS = ("160.79.104.0/21", "153.61.208.0/20")  # platform.claude.com/docs/en/api/ip-addresses
CLAUDE_REDIRECT_HOSTS = ("claude.ai", "claude.com")
CLAUDE_TOKENS = DATA / "claude-tokens.json"
ST_API = "https://api.smartthings.com/v1"
ST_OAUTH = "https://api.smartthings.com/oauth"
# A SmartThings CLI login (own profile, made once on a pc with the CLI) also works: public client with PKCE, the
# bridge keeps refreshing it. Used when the account may not create OAuth apps (403 on /apps).
ST_CLI_CLIENT = "d18cf96e-c626-4433-bf51-ddbb10c5d1ed"
ST_CLI_REFRESH = "https://auth-global.api.smartthings.com/oauth/token"
ST_SCOPES = ("r:devices:* x:devices:* r:locations:* x:locations:* r:scenes:* x:scenes:* r:rules:* x:rules:*")
CLAUDE_STATE: dict[str, Any] = {"codes": {}, "fails": [], "st_states": {}, "ip_warned": False}


def claude_cfg(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    return (cfg if cfg is not None else load_config()).get("claude") or {}


def claude_hash_password(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 300_000)
    return "pbkdf2$300000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def claude_check_password(pw: str, stored: str) -> bool:
    try:
        _, n, salt, dk = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), base64.b64decode(salt), int(n))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except Exception:  # noqa: BLE001
        return False


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def claude_tokens() -> dict[str, Any]:
    try:
        return json.loads(CLAUDE_TOKENS.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"clients": {}, "access": {}, "refresh": {}}


def claude_save_tokens(t: dict[str, Any]) -> None:
    now = time.time()
    for k in ("access", "refresh"):
        t[k] = {h: v for h, v in t.get(k, {}).items() if v.get("exp", 0) > now}
    tmp = CLAUDE_TOKENS.with_suffix(".tmp")
    tmp.write_text(json.dumps(t, indent=1), encoding="utf-8")
    tmp.replace(CLAUDE_TOKENS)


def claude_redirect_ok(uri: str) -> bool:
    from urllib.parse import urlsplit
    u = urlsplit(uri or "")
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in CLAUDE_REDIRECT_HOSTS)


def claude_client_ip(req) -> str:
    """The address of the caller. Behind Tailscale Funnel the request comes from this pc (loopback) with the real
    address in X-Forwarded-For."""
    remote = req.remote or ""
    try:
        loop = ipaddress.ip_address(remote).is_loopback
    except ValueError:
        loop = False
    if loop:
        xff = req.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return ""
    return remote


def claude_ip_allowed(req, cfg: dict[str, Any]) -> bool:
    c = claude_cfg(cfg)
    if c.get("ip_filter", True) is False:
        return True
    ip_s = claude_client_ip(req)
    if not ip_s:
        # no forwarded address (the tunnel did not add one): OAuth still protects every call
        if not CLAUDE_STATE["ip_warned"]:
            CLAUDE_STATE["ip_warned"] = True
            log.warning("[claude] the tunnel gives no caller address, address filter not applied")
        return True
    try:
        ip = ipaddress.ip_address(ip_s)
    except ValueError:
        return False
    nets = list(ANTHROPIC_NETS) + list(c.get("allowed_ips") or [])
    return any(ip in ipaddress.ip_network(n, strict=False) for n in nets)


def claude_public_url(req, cfg: dict[str, Any]) -> str:
    u = str(claude_cfg(cfg).get("public_url") or "").rstrip("/")
    if u:
        return u
    return f"https://{req.host}"


# ---------- SmartThings with the bridge's own OAuth app

class STCloud:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.cache: dict[str, Any] = {"t": 0}

    @staticmethod
    def configured(cfg: dict[str, Any] | None = None) -> bool:
        s = claude_cfg(cfg).get("st") or {}
        return bool(s.get("client_id") and s.get("refresh_token"))

    async def _token(self, session, force: bool = False) -> str:
        async with self.lock:
            cfg = load_config()
            s = (cfg.get("claude") or {}).get("st") or {}
            if not s.get("client_id"):
                raise RuntimeError("SmartThings is not connected yet (bridge setup page, Claude part)")
            if not force and s.get("access_token") and float(s.get("expires", 0)) > time.time() + 300:
                return s["access_token"]
            if not s.get("refresh_token"):
                raise RuntimeError("SmartThings is not connected yet (bridge setup page, Claude part)")
            cli = not s.get("client_secret")
            auth = None if cli else aiohttp.BasicAuth(s["client_id"], s["client_secret"])
            async with session.post(ST_CLI_REFRESH if cli else ST_OAUTH + "/token", auth=auth, data={
                    "grant_type": "refresh_token", "client_id": s["client_id"], "refresh_token": s["refresh_token"]}) as r:
                j = await r.json(content_type=None)
                if r.status != 200 or "access_token" not in j:
                    log.warning("[claude] SmartThings token refresh failed: HTTP %s %s", r.status,
                                str(j.get("error") if isinstance(j, dict) else "")[:80])
                    raise RuntimeError("SmartThings login expired: connect SmartThings again on the bridge setup page")
            cfg = load_config()
            s = cfg.setdefault("claude", {}).setdefault("st", {})
            s["access_token"] = j["access_token"]
            s["expires"] = time.time() + float(j.get("expires_in", 86400))
            if j.get("refresh_token"):
                s["refresh_token"] = j["refresh_token"]
            save_config(cfg)
            log.info("[claude] SmartThings token refreshed")
            return s["access_token"]

    async def call(self, method: str, path: str, body: Any = None, retry: bool = True) -> Any:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            tok = await self._token(session)
            url = path if path.startswith("http") else ST_API + path
            async with session.request(method, url, json=body, headers={"Authorization": "Bearer " + tok,
                                                                          "Accept": "application/json"}) as r:
                if r.status == 401 and retry:
                    await self._token(session, force=True)
                    return await self.call(method, path, body, False)
                txt = await r.text()
                try:
                    j = json.loads(txt) if txt else None
                except ValueError:
                    j = txt
                if r.status >= 400:
                    err = j.get("error") if isinstance(j, dict) else j
                    raise RuntimeError(f"SmartThings {method} {path.split('?')[0]}: HTTP {r.status} {str(err)[:300]}")
                return j

    async def all(self, path: str) -> list[Any]:
        items, nxt = [], path
        for _ in range(20):
            if not nxt:
                break
            j = await self.call("GET", nxt)
            items += (j or {}).get("items") or []
            nxt = (((j or {}).get("_links") or {}).get("next") or {}).get("href")
        return items

    async def load(self, force: bool = False) -> dict[str, Any]:
        c = self.cache
        if not force and c.get("loc") and time.time() - c["t"] < 120:
            return c
        locs = await self.all("/locations")
        if not locs:
            raise RuntimeError("no SmartThings location")
        want = str(claude_cfg().get("location") or "").lower()
        c["loc"] = next((x for x in locs if want and (x["locationId"] == want or x["name"].lower() == want)), locs[0])
        lid = c["loc"]["locationId"]
        c["rooms"] = await self.all(f"/locations/{lid}/rooms")
        c["devices"] = await self.all(f"/devices?locationId={lid}")
        c["t"] = time.time()
        return c


ST_CLOUD = STCloud()


def _norm(s: Any) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def _label(d: dict) -> str:
    return d.get("label") or d.get("name") or ""


def _caps(d: dict) -> list[str]:
    out: list[str] = []
    for c in d.get("components") or []:
        for x in c.get("capabilities") or []:
            if x.get("id") not in out:
                out.append(x.get("id"))
    return out


def _room(c: dict, rid: Any) -> str:
    return next((r.get("name") for r in c.get("rooms") or [] if r.get("roomId") == rid), "")


def _brief(c: dict, d: dict) -> dict:
    return {"name": _label(d), "id": d.get("deviceId"), "room": _room(c, d.get("roomId")),
            "type": (d.get("ocf") or {}).get("ocfDeviceType") or d.get("deviceTypeName") or d.get("type"),
            "capabilities": _caps(d)}


def st_flat_status(s: dict) -> dict:
    out: dict[str, Any] = {}
    for comp, cs in (s.get("components") or {}).items():
        for cap, attrs in (cs or {}).items():
            for a, v in (attrs or {}).items():
                if not isinstance(v, dict) or v.get("value") is None:
                    continue
                if re.match(r"^(supported|available)", a) or len(json.dumps(v["value"])) > 300:
                    continue
                key = ("" if comp == "main" else comp + ".") + cap + "." + a
                out[key] = f"{v['value']} {v['unit']}" if v.get("unit") else v["value"]
    return out


def st_find_device(c: dict, q: str) -> dict:
    if not q:
        raise ValueError("device is required")
    devs = c.get("devices") or []
    for d in devs:
        if d.get("deviceId") == q:
            return d
    n = _norm(q)
    hits = [d for d in devs if _norm(_label(d)) == n]
    if not hits:
        hits = [d for d in devs if n in _norm(_label(d))]
    if not hits:
        words = n.split()
        hits = [d for d in devs if all(w in _norm(_label(d) + " " + _room(c, d.get("roomId"))) for w in words)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f'No device matches "{q}". Use list_devices.')
    raise ValueError(f'"{q}" matches {len(hits)} devices: ' + "; ".join(
        f"{_label(d)} ({_room(c, d.get('roomId'))}, {d.get('deviceId')})" for d in hits[:15]) +
        ". Use the exact name or the id.")


def st_lock_check(cmd: str, capability: str) -> None:
    mode = str(claude_cfg().get("locks") or "full").lower()
    if capability != "lock" or mode == "full" or (mode == "lock" and cmd == "lock"):
        return
    raise ValueError(f"Locks are {'lock only' if mode == 'lock' else 'read only'} for Claude (bridge setting).")


GET_MANY_KEYS = ("switch.switch", "switchLevel.level", "temperatureMeasurement.temperature", "lock.lock",
                 "contactSensor.contact", "motionSensor.motion", "powerMeter.power", "battery.battery",
                 "windowShade.windowShade", "thermostatMode.thermostatMode", "thermostatHeatingSetpoint.heatingSetpoint",
                 "airConditionerMode.airConditionerMode", "presenceSensor.presence",
                 "relativeHumidityMeasurement.humidity", "mediaPlayback.playbackStatus")


async def st_tool(name: str, a: dict) -> Any:
    st = ST_CLOUD
    if name == "list_rooms":
        c = await st.load(True)
        return {"location": c["loc"]["name"], "rooms": [
            {"name": r.get("name"), "id": r.get("roomId"),
             "devices": sum(1 for d in c["devices"] if d.get("roomId") == r.get("roomId"))} for r in c["rooms"]]}
    if name == "list_devices":
        c = await st.load(True)
        ds = c["devices"]
        if a.get("room"):
            ds = [d for d in ds if _norm(a["room"]) in _norm(_room(c, d.get("roomId")))]
        if a.get("name"):
            ds = [d for d in ds if _norm(a["name"]) in _norm(_label(d))]
        if a.get("capability"):
            ds = [d for d in ds if a["capability"] in _caps(d)]
        return {"count": len(ds), "devices": [_brief(c, d) for d in ds]}
    if name == "get_status":
        c = await st.load()
        d = st_find_device(c, a.get("device", ""))
        s, h = await asyncio.gather(st.call("GET", f"/devices/{d['deviceId']}/status"),
                                    st.call("GET", f"/devices/{d['deviceId']}/health"), return_exceptions=True)
        if isinstance(s, Exception):
            raise s
        return {**_brief(c, d), "health": None if isinstance(h, Exception) else (h or {}).get("state"),
                "status": st_flat_status(s)}
    if name == "get_many":
        r = await st_tool("list_devices", a)
        if r["count"] > 80:
            raise ValueError(f"{r['count']} devices; narrow it down with room, name or capability")

        async def one(d):
            try:
                s = st_flat_status({"components": {"main": await st.call("GET", f"/devices/{d['id']}/components/main/status")}})
                return {"name": d["name"], "room": d["room"],
                        **{k.split(".")[1]: s[k] for k in GET_MANY_KEYS if k in s}}
            except Exception as ex:  # noqa: BLE001
                return {"name": d["name"], "room": d["room"], "error": str(ex)[:120]}
        out = []
        for i in range(0, len(r["devices"]), 10):
            out += await asyncio.gather(*(one(d) for d in r["devices"][i:i + 10]))
        return out
    if name == "send_command":
        c = await st.load()
        d = st_find_device(c, a.get("device", ""))
        cap, cmd = str(a.get("capability", "")), str(a.get("command", ""))
        if not cap or not cmd:
            raise ValueError("capability and command are required")
        st_lock_check(cmd, cap)
        body = {"commands": [{"component": a.get("component") or "main", "capability": cap, "command": cmd,
                              "arguments": a.get("args") or []}]}
        r = await st.call("POST", f"/devices/{d['deviceId']}/commands", body)
        log.info("[claude] command %s %s to %s", cap, cmd, _label(d))
        return {"device": _label(d), "room": _room(c, d.get("roomId")), "sent": body["commands"][0],
                "result": (((r or {}).get("results") or [{}])[0]).get("status", "ACCEPTED")}
    if name in ("list_scenes", "run_scene"):
        c = await st.load()
        scenes = await st.all(f"/scenes?locationId={c['loc']['locationId']}")
        if name == "list_scenes":
            return [{"name": x.get("sceneName"), "id": x.get("sceneId")} for x in scenes]
        q = str(a.get("scene", ""))
        hits = [x for x in scenes if x.get("sceneId") == q or _norm(x.get("sceneName")) == _norm(q)] or \
               [x for x in scenes if _norm(q) and _norm(q) in _norm(x.get("sceneName"))]
        if len(hits) != 1:
            raise ValueError(("Several scenes match: " + "; ".join(x.get("sceneName") for x in hits)) if hits
                             else f'No scene matches "{q}"')
        await st.call("POST", f"/scenes/{hits[0]['sceneId']}/execute")
        log.info("[claude] scene %s", hits[0].get("sceneName"))
        return {"ran": hits[0].get("sceneName")}
    if name in ("get_mode", "set_mode"):
        c = await st.load()
        lid = c["loc"]["locationId"]
        modes = (await st.call("GET", f"/locations/{lid}/modes") or {}).get("items") or []
        if name == "get_mode":
            cur = await st.call("GET", f"/locations/{lid}/modes/current") or {}
            return {"current": cur.get("label") or cur.get("name"), "modes": [m.get("label") or m.get("name") for m in modes]}
        m = next((m for m in modes if _norm(m.get("label") or m.get("name")) == _norm(a.get("mode"))), None)
        if not m:
            raise ValueError("Unknown mode. Modes: " + ", ".join(m.get("label") or m.get("name") for m in modes))
        await st.call("PUT", f"/locations/{lid}/modes/current", {"modeId": m["id"]})
        log.info("[claude] mode %s", m.get("label") or m.get("name"))
        return {"mode": m.get("label") or m.get("name")}
    if name == "device_history":
        c = await st.load()
        d = st_find_device(c, a.get("device", ""))
        n = max(1, min(100, int(a.get("limit") or 20)))
        j = await st.call("GET", f"/history/devices?locationId={c['loc']['locationId']}&deviceId={d['deviceId']}&limit={n}")
        return {"device": _label(d), "events": [{"time": e.get("time"), "attribute": f"{e.get('capability')}.{e.get('attribute')}",
                                                 "value": e.get("value"), "unit": e.get("unit")}
                                                for e in ((j or {}).get("items") or [])[:n]]}
    if name in ("list_app_routines", "get_routine", "update_routine", "create_routine"):
        # the routines made in the SmartThings app live on client.smartthings.com (the public /rules only shows
        # rules made through the API by the same client)
        c = await st.load()
        lid = c["loc"]["locationId"]
        base = "https://client.smartthings.com/rules"
        if name == "list_app_routines":
            j = await st.call("GET", f"{base}?locationId={lid}&max=500")
            items = (j or {}).get("items") or []
            q = _norm(a.get("name"))
            return [{"name": x.get("name"), "id": x.get("id"), "status": x.get("status")}
                    for x in items if not q or q in _norm(x.get("name"))]
        if name == "get_routine":
            return await st.call("GET", f"{base}/{a['id']}?locationId={lid}")
        rule = a.get("rule")
        if isinstance(rule, str):
            rule = json.loads(rule)
        if not isinstance(rule, dict) or not rule.get("name") or not isinstance(rule.get("actions"), list):
            raise ValueError("rule must be an object with name and actions")
        body = {"name": rule["name"], "actions": rule["actions"]}
        for k in ("timeZoneId", "sequence"):
            if k in rule:
                body[k] = rule[k]
        if name == "update_routine":
            r = await st.call("PUT", f"{base}/{a['id']}?locationId={lid}", body)
        else:
            r = await st.call("POST", f"{base}?locationId={lid}", body)
        log.info("[claude] routine %s %s", "updated" if name == "update_routine" else "created", body["name"])
        return {"id": (r or {}).get("id"), "name": (r or {}).get("name"), "status": (r or {}).get("status")}
    if name in ("list_routines", "run_routine"):
        c = await st.load()
        lid = c["loc"]["locationId"]
        rules = await st.all(f"/rules?locationId={lid}")
        if name == "list_routines":
            return [{"name": x.get("name"), "id": x.get("id"), "status": x.get("status")} for x in rules]
        x = next((y for y in rules if y.get("id") == a.get("routine") or _norm(y.get("name")) == _norm(a.get("routine"))), None)
        if not x:
            raise ValueError(f'No routine named "{a.get("routine")}"')
        await st.call("POST", f"/rules/execute/{x['id']}?locationId={lid}")
        log.info("[claude] routine %s", x.get("name"))
        return {"ran": x.get("name")}
    raise ValueError("unknown tool " + name)


_OBJ = {"type": "object", "properties": {}}
CLAUDE_TOOLS = [
    {"name": "list_rooms", "description": "List the rooms of the SmartThings home with the number of devices in each.",
     "inputSchema": _OBJ, "annotations": {"readOnlyHint": True}},
    {"name": "list_devices", "description": "List SmartThings devices (name, id, room, type, capabilities). Optional "
     "filters: room, text in the name, capability id (switch, lock, temperatureMeasurement, thermostatHeatingSetpoint, ...).",
     "inputSchema": {"type": "object", "properties": {"room": {"type": "string"}, "name": {"type": "string"},
                                                      "capability": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_status", "description": "Current state of one device (by name or id): every attribute with its "
     "value (switch.switch, temperatureMeasurement.temperature, lock.lock, battery.battery, ...) and online state.",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string"}}, "required": ["device"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_many", "description": "Main values of many devices at once (switch, level, temperature, lock, "
     "contact, motion, power, battery, shade, thermostat). Filter by room, capability or name. For questions like "
     "'which lights are on' or 'is everything locked'.",
     "inputSchema": {"type": "object", "properties": {"room": {"type": "string"}, "capability": {"type": "string"},
                                                      "name": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "send_command", "description": "Send a command to a device (by name or id). Examples: capability "
     "'switch' command 'on'/'off'; 'switchLevel' 'setLevel' args [50]; 'windowShade' 'open'/'close'/'pause'; "
     "'windowShadeLevel' 'setShadeLevel' args [30]; 'thermostatHeatingSetpoint' 'setHeatingSetpoint' args [20]; "
     "'lock' 'lock'/'unlock'; 'colorTemperature' 'setColorTemperature' args [2700]. Use get_status first to see the "
     "capabilities of the device.",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string"}, "capability": {"type": "string"},
                                                      "command": {"type": "string"}, "args": {"type": "array", "items": {}},
                                                      "component": {"type": "string", "description": "default main"}},
                     "required": ["device", "capability", "command"]}},
    {"name": "list_scenes", "description": "List the scenes (manual routines) of the home.", "inputSchema": _OBJ,
     "annotations": {"readOnlyHint": True}},
    {"name": "run_scene", "description": "Run a scene by name or id.",
     "inputSchema": {"type": "object", "properties": {"scene": {"type": "string"}}, "required": ["scene"]}},
    {"name": "get_mode", "description": "Current location mode (Home, Away, Night, ...) and the modes available.",
     "inputSchema": _OBJ, "annotations": {"readOnlyHint": True}},
    {"name": "set_mode", "description": "Set the location mode by name.",
     "inputSchema": {"type": "object", "properties": {"mode": {"type": "string"}}, "required": ["mode"]}},
    {"name": "device_history", "description": "Recent events of one device, newest first (default 20, at most 100).",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string"}, "limit": {"type": "number"}},
                     "required": ["device"]}, "annotations": {"readOnlyHint": True}},
    {"name": "list_routines", "description": "List the routines of the home (name, id, status).", "inputSchema": _OBJ,
     "annotations": {"readOnlyHint": True}},
    {"name": "list_app_routines", "description": "List all routines made in the SmartThings app (name, id, status). "
     "Optional filter: text in the name.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_routine", "description": "Full definition (Rules API JSON: name, actions with if, conditions, "
     "commands) of one routine by id.", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                                                         "required": ["id"]}, "annotations": {"readOnlyHint": True}},
    {"name": "update_routine", "description": "Replace a routine by id with a new definition (object with name and "
     "actions in the Rules API format, as returned by get_routine). Always read it with get_routine first and keep "
     "everything that should not change.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "rule": {"type": "object"}},
                     "required": ["id", "rule"]}},
    {"name": "create_routine", "description": "Create a new routine (object with name and actions in the Rules API "
     "format). Conditions can use remains (for example mode stays Away for 12 hours).",
     "inputSchema": {"type": "object", "properties": {"rule": {"type": "object"}}, "required": ["rule"]}},
    {"name": "run_routine", "description": "Run a routine by exact name or id.",
     "inputSchema": {"type": "object", "properties": {"routine": {"type": "string"}}, "required": ["routine"]}},
]


async def claude_rpc(m: dict) -> dict | None:
    mid, method, params = m.get("id"), m.get("method"), m.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "stoeckie-smartthings", "version": VERSION},
            "instructions": "SmartThings home control. Find devices with list_devices or get_many before sending "
                            "commands. Device names are matched loosely; when several match, ask which one."}}
    if method is None or str(method).startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": CLAUDE_TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        if name not in {t["name"] for t in CLAUDE_TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            r = await st_tool(name, params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False)}]}}
        except Exception as ex:  # noqa: BLE001
            log.info("[claude] tool %s failed: %s", name, str(ex)[:160])
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [{"type": "text", "text": str(ex)}]}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


CLAUDE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<style>body{{font:16px system-ui,sans-serif;max-width:420px;margin:40px auto;padding:0 16px;background:#fff;color:#111}}
input{{font:inherit;width:100%;box-sizing:border-box;padding:10px;margin:8px 0}}button{{font:inherit;padding:10px 20px}}
@media (prefers-color-scheme:dark){{body{{background:#111;color:#eee}}}}</style></head><body>{body}</body></html>"""


def claude_html(title: str, body: str, status: int = 200) -> web.Response:
    return web.Response(status=status, content_type="text/html", text=CLAUDE_PAGE.format(title=html.escape(title), body=body),
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https:"})


def make_claude_app() -> web.Application:
    app = web.Application(client_max_size=256 * 1024)

    def deny() -> web.Response:
        return web.json_response({"error": "forbidden"}, status=403)

    def guarded(req) -> bool:
        cfg = load_config()
        if not claude_cfg(cfg).get("enabled"):
            return False
        ok = claude_ip_allowed(req, cfg)
        if not ok:
            log.warning("[claude] refused %s %s from %s", req.method, req.path, claude_client_ip(req))
        return ok

    async def prm(req):
        if not guarded(req):
            return deny()
        base = claude_public_url(req, load_config())
        return web.json_response({"resource": base + "/mcp", "authorization_servers": [base],
                                  "bearer_methods_supported": ["header"]})

    async def asm(req):
        if not guarded(req):
            return deny()
        base = claude_public_url(req, load_config())
        return web.json_response({
            "issuer": base, "authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token",
            "registration_endpoint": base + "/register", "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["smartthings"]})

    async def register(req):
        if not guarded(req):
            return deny()
        try:
            j = await req.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid_client_metadata"}, status=400)
        uris = [str(u) for u in (j.get("redirect_uris") or [])]
        if not uris or not all(claude_redirect_ok(u) for u in uris):
            log.warning("[claude] registration refused: redirect %s", uris[:2])
            return web.json_response({"error": "invalid_redirect_uri"}, status=400)
        t = claude_tokens()
        if len(t.get("clients", {})) >= 50:
            return web.json_response({"error": "too many clients"}, status=429)
        cid = "c_" + secrets.token_urlsafe(18)
        t.setdefault("clients", {})[cid] = {"redirect_uris": uris, "name": str(j.get("client_name") or "")[:80],
                                            "t": int(time.time())}
        claude_save_tokens(t)
        log.info("[claude] client registered: %s", str(j.get("client_name") or "?")[:40])
        return web.json_response({"client_id": cid, "client_id_issued_at": int(time.time()), "redirect_uris": uris,
                                  "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                                  "response_types": ["code"], "client_name": j.get("client_name")}, status=201)

    def client_redirect_ok(cid: str, uri: str) -> bool:
        if not claude_redirect_ok(uri):
            return False
        c = claude_tokens().get("clients", {}).get(cid)
        if c:
            return uri in c["redirect_uris"]
        # Claude's published client identity (a metadata URL on claude.ai / claude.com)
        return claude_redirect_ok(cid)

    def too_many_fails() -> bool:
        now = time.time()
        CLAUDE_STATE["fails"] = [t for t in CLAUDE_STATE["fails"] if now - t < 900]
        return len(CLAUDE_STATE["fails"]) >= 5

    def auth_form(p: dict, msg: str = "") -> web.Response:
        hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(str(p.get(k, "")))}">'
                         for k in ("client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method",
                                   "scope", "resource"))
        return claude_html("Stoeckie Bridge sign in", (
            "<h2>Connect Claude to your home</h2><p>Claude asks to control your SmartThings home through the "
            "Stoeckie Bridge. Enter the bridge password for Claude to allow it.</p>"
            + (f"<p><b>{html.escape(msg)}</b></p>" if msg else "") +
            f'<form method="post">{hidden}<input type="password" name="password" autocomplete="current-password" '
            'autofocus required><button type="submit">Allow</button></form>'))

    async def authorize_get(req):
        cfg = load_config()
        c = claude_cfg(cfg)
        if not c.get("enabled") or not c.get("password_hash"):
            return claude_html("Not available", "<p>Claude access is not switched on in the bridge.</p>", 403)
        p = dict(req.query)
        if p.get("response_type") != "code" or p.get("code_challenge_method") != "S256" or not p.get("code_challenge"):
            return claude_html("Error", "<p>Invalid request (code flow with PKCE S256 required).</p>", 400)
        if not client_redirect_ok(p.get("client_id", ""), p.get("redirect_uri", "")):
            return claude_html("Error", "<p>Unknown client or redirect address.</p>", 400)
        return auth_form(p)

    async def authorize_post(req):
        cfg = load_config()
        c = claude_cfg(cfg)
        if not c.get("enabled") or not c.get("password_hash"):
            return claude_html("Not available", "<p>Claude access is not switched on in the bridge.</p>", 403)
        f = await req.post()
        p = {k: str(f.get(k, "")) for k in ("client_id", "redirect_uri", "state", "code_challenge",
                                             "code_challenge_method", "scope", "resource")}
        if p["code_challenge_method"] != "S256" or not p["code_challenge"] or \
                not client_redirect_ok(p["client_id"], p["redirect_uri"]):
            return claude_html("Error", "<p>Invalid request.</p>", 400)
        if too_many_fails():
            log.warning("[claude] sign in blocked: too many wrong passwords (from %s)", claude_client_ip(req))
            return auth_form(p, "Too many wrong passwords. Try again in 15 minutes.")
        if not claude_check_password(str(f.get("password", "")), c["password_hash"]):
            CLAUDE_STATE["fails"].append(time.time())
            log.warning("[claude] wrong password (from %s)", claude_client_ip(req))
            await asyncio.sleep(2)
            return auth_form(p, "Wrong password.")
        code = secrets.token_urlsafe(32)
        CLAUDE_STATE["codes"] = {k: v for k, v in CLAUDE_STATE["codes"].items() if v["exp"] > time.time()}
        CLAUDE_STATE["codes"][_h(code)] = {"client_id": p["client_id"], "redirect_uri": p["redirect_uri"],
                                           "challenge": p["code_challenge"], "exp": time.time() + 300}
        log.info("[claude] sign in allowed (from %s)", claude_client_ip(req))
        from urllib.parse import urlencode
        q = {"code": code}
        if p["state"]:
            q["state"] = p["state"]
        sep = "&" if "?" in p["redirect_uri"] else "?"
        raise web.HTTPFound(p["redirect_uri"] + sep + urlencode(q))

    def issue(t: dict, cid: str) -> dict:
        at, rt = secrets.token_urlsafe(32), secrets.token_urlsafe(40)
        now = time.time()
        t.setdefault("access", {})[_h(at)] = {"client_id": cid, "exp": now + 3600}
        t.setdefault("refresh", {})[_h(rt)] = {"client_id": cid, "exp": now + 90 * 86400}
        claude_save_tokens(t)
        return {"access_token": at, "token_type": "Bearer", "expires_in": 3600, "refresh_token": rt, "scope": "smartthings"}

    async def token(req):
        if not guarded(req):
            return deny()
        f = await req.post()
        gt = str(f.get("grant_type", ""))
        err = lambda e, s=400: web.json_response({"error": e}, status=s, headers={"Cache-Control": "no-store"})  # noqa: E731
        if gt == "authorization_code":
            x = CLAUDE_STATE["codes"].pop(_h(str(f.get("code", ""))), None)
            if not x or x["exp"] < time.time():
                return err("invalid_grant")
            if str(f.get("client_id", "")) not in ("", x["client_id"]) or str(f.get("redirect_uri", "")) != x["redirect_uri"]:
                return err("invalid_grant")
            ver = str(f.get("code_verifier", ""))
            chal = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).decode().rstrip("=")
            if not ver or not hmac.compare_digest(chal, x["challenge"]):
                return err("invalid_grant")
            log.info("[claude] Claude connected")
            return web.json_response(issue(claude_tokens(), x["client_id"]), headers={"Cache-Control": "no-store"})
        if gt == "refresh_token":
            t = claude_tokens()
            x = t.get("refresh", {}).pop(_h(str(f.get("refresh_token", ""))), None)
            if not x or x["exp"] < time.time():
                claude_save_tokens(t)
                return err("invalid_grant")
            return web.json_response(issue(t, x["client_id"]), headers={"Cache-Control": "no-store"})
        return err("unsupported_grant_type")

    def bearer_ok(req) -> bool:
        a = req.headers.get("Authorization", "")
        if not a.startswith("Bearer "):
            return False
        x = claude_tokens().get("access", {}).get(_h(a[7:].strip()))
        return bool(x and x["exp"] > time.time())

    async def mcp(req):
        if not guarded(req):
            return deny()
        base = claude_public_url(req, load_config())
        if not bearer_ok(req):
            return web.json_response({"error": "invalid_token"}, status=401, headers={
                "WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'})
        if req.method == "GET":
            return web.Response(status=405, headers={"Allow": "POST"})
        if req.method == "DELETE":
            return web.Response(status=200)
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, status=400)
        msgs = body if isinstance(body, list) else [body]
        out = [r for r in [await claude_rpc(m) for m in msgs if isinstance(m, dict)] if r is not None]
        if not out:
            return web.Response(status=202)
        for m in msgs:
            if isinstance(m, dict) and m.get("method") == "tools/call":
                log.info("[claude] tool %s", (m.get("params") or {}).get("name"))
        return web.json_response(out if isinstance(body, list) else out[0])

    async def st_callback(req):
        """SmartThings OAuth app: the browser comes back here after signing in at Samsung."""
        state = req.query.get("state", "")
        exp = CLAUDE_STATE["st_states"].pop(state, 0)
        if not state or exp < time.time():
            return claude_html("Error", "<p>Expired or unknown request. Start again on the bridge setup page.</p>", 400)
        code = req.query.get("code", "")
        cfg = load_config()
        s = cfg.setdefault("claude", {}).setdefault("st", {})
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.post(ST_OAUTH + "/token", auth=aiohttp.BasicAuth(s["client_id"], s["client_secret"]),
                                        data={"grant_type": "authorization_code", "code": code, "client_id": s["client_id"],
                                              "redirect_uri": s["redirect_uri"]}) as r:
                    j = await r.json(content_type=None)
            if "access_token" not in j:
                raise RuntimeError(f"HTTP {r.status} {str(j.get('error') if isinstance(j, dict) else '')[:80]}")
        except Exception as ex:  # noqa: BLE001
            log.warning("[claude] SmartThings sign in failed: %s", ex)
            return claude_html("Error", f"<p>SmartThings sign in failed: {html.escape(str(ex))}</p>", 502)
        cfg = load_config()
        s = cfg.setdefault("claude", {}).setdefault("st", {})
        s.update(access_token=j["access_token"], refresh_token=j.get("refresh_token"),
                 expires=time.time() + float(j.get("expires_in", 86400)), installed_app_id=j.get("installed_app_id"))
        save_config(cfg)
        ST_CLOUD.cache["t"] = 0
        log.info("[claude] SmartThings connected")
        return claude_html("SmartThings connected", "<h2>SmartThings connected</h2><p>You can close this page.</p>")

    app.router.add_get("/.well-known/oauth-protected-resource", prm)
    app.router.add_get("/.well-known/oauth-protected-resource/mcp", prm)
    app.router.add_get("/.well-known/oauth-authorization-server", asm)
    app.router.add_get("/.well-known/oauth-authorization-server/mcp", asm)
    app.router.add_post("/register", register)
    app.router.add_get("/authorize", authorize_get)
    app.router.add_post("/authorize", authorize_post)
    app.router.add_post("/token", token)
    app.router.add_route("*", "/mcp", mcp)
    app.router.add_get("/st/callback", st_callback)
    return app


def claude_setup_html(cfg: dict[str, Any]) -> str:
    c = claude_cfg(cfg)
    s = c.get("st") or {}
    t = claude_tokens()
    st_state = (("connected (CLI login)" if not s.get("client_secret") else "connected") if STCloud.configured(cfg) else
                "app ready, not signed in yet" if s.get("client_id") else "no SmartThings app yet")
    rows = [
        ("Claude access", "on" if c.get("enabled") else "off"),
        ("Password", "set" if c.get("password_hash") else "not set"),
        ("Public address", html.escape(str(c.get("public_url") or "not set")) + ("/mcp" if c.get("public_url") else "")),
        ("SmartThings", st_state),
        ("Claude sessions", f"{len(t.get('refresh', {}))} active"),
        ("Locks", html.escape(str(c.get("locks") or "full"))),
    ]
    tbl = "".join(f"<tr><td>{k}</td><td><b>{v}</b></td></tr>" for k, v in rows)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Claude access</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>body{{font:15px system-ui,sans-serif;max-width:640px;
margin:32px auto;padding:0 16px}}td{{padding:4px 12px 4px 0}}input,select{{font:inherit;padding:8px;margin:4px 0}}
form{{margin:14px 0;padding-top:10px;border-top:1px solid #ccc}}</style></head><body>
<p><a href="/setup">Back to setup</a></p><h2>Claude on web and phone</h2><table>{tbl}</table>
<form method="post"><input type="hidden" name="act" value="password"><b>Password for Claude</b><br>
<input type="password" name="pw" minlength="12" autocomplete="new-password" required placeholder="at least 12 characters">
<input type="password" name="pw2" minlength="12" autocomplete="new-password" required placeholder="again">
<br><button type="submit">Save password and switch Claude access on</button></form>
<form method="post"><input type="hidden" name="act" value="st"><b>SmartThings</b><br>
<button type="submit" {'disabled' if not s.get('client_id') else ''}>Sign in at SmartThings</button></form>
<form method="post"><input type="hidden" name="act" value="locks"><b>Locks</b><br><select name="locks">
{''.join(f'<option value="{v}"{" selected" if (c.get("locks") or "full") == v else ""}>{l}</option>' for v, l in (("full", "lock and unlock"), ("lock", "lock only"), ("read", "read only")))}
</select> <button type="submit">Save</button></form>
<form method="post"><input type="hidden" name="act" value="revoke"><b>Sign Claude out</b><br>
<button type="submit">End all Claude sessions</button></form>
<form method="post"><input type="hidden" name="act" value="off"><button type="submit">Switch Claude access off</button></form>
</body></html>"""


async def claude_setup_post(form, cfg: dict[str, Any]) -> str | None:
    """Handles the Claude part of the setup page; returns a URL to send the browser to, or None."""
    act = str(form.get("act", ""))
    c = cfg.setdefault("claude", {})
    if act == "password":
        pw, pw2 = str(form.get("pw", "")), str(form.get("pw2", ""))
        if len(pw) >= 12 and pw == pw2:
            c["password_hash"] = claude_hash_password(pw)
            c["enabled"] = True
            save_config(cfg)
            log.info("[setup] Claude password saved, Claude access on")
    elif act == "locks":
        v = str(form.get("locks", "full"))
        c["locks"] = v if v in ("full", "lock", "read") else "full"
        save_config(cfg)
        log.info("[setup] Claude locks: %s", c["locks"])
    elif act == "revoke":
        t = claude_tokens()
        t["access"], t["refresh"] = {}, {}
        claude_save_tokens(t)
        log.info("[setup] all Claude sessions ended")
    elif act == "off":
        c["enabled"] = False
        save_config(cfg)
        log.info("[setup] Claude access off")
    elif act == "st":
        s = c.get("st") or {}
        if s.get("client_id") and s.get("redirect_uri"):
            from urllib.parse import urlencode
            state = secrets.token_urlsafe(24)
            CLAUDE_STATE["st_states"][state] = time.time() + 600
            return ST_OAUTH + "/authorize?" + urlencode({"client_id": s["client_id"], "response_type": "code",
                                                         "redirect_uri": s["redirect_uri"], "scope": ST_SCOPES,
                                                         "state": state})
    return None


async def claude_start() -> None:
    cfg = load_config()
    c = claude_cfg(cfg)
    port = int(c.get("port") or CLAUDE_DEFAULT_PORT)
    runner = web.AppRunner(make_claude_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()   # only this pc: the tunnel forwards to it
    log.info("[claude] MCP server on 127.0.0.1:%d (%s)", port, "on" if c.get("enabled") else "off until a password is set")

    async def keep_st_login():
        # a SmartThings refresh token expires after 29 days without use: read the locations twice a day, which
        # renews the access token (and with it the refresh token) whenever it is due, also when Claude is not used
        while True:
            await asyncio.sleep(60)
            try:
                if STCloud.configured():
                    await ST_CLOUD.call("GET", "/locations")
            except Exception as ex:  # noqa: BLE001
                log.warning("[claude] SmartThings keep-alive: %s", str(ex)[:160])
            await asyncio.sleep(12 * 3600)

    asyncio.create_task(keep_st_login())



async def serve(args) -> None:
    cfg = load_config()
    seed_from_env(cfg)
    bridge_id = cfg.get("bridge_id")
    if not bridge_id:
        bridge_id = cfg["bridge_id"] = uuid.uuid4().hex[:12]
        save_config(cfg)
    port = int(cfg.get("port", 8787))
    sources = {"aeg": Electrolux, "fglair": FGLair, "skoda": Skoda, "irobot": IRobotCloud, "velux": VeluxCloud,
               "buienradar": Buienradar, "homeconnect": HomeConnect,
               "samsung": Samsung, "samsungtv": SamsungTV, "tuya": TuyaLocal, "echo": EchoCloud}
    tasks: dict[str, asyncio.Task] = {}

    def start(section: str) -> None:
        if section not in sources:
            return
        if tasks.get(section):
            tasks[section].cancel()
        for dev_id in [d for d in REG.devices if d.startswith(section + "-")]:
            REG.devices.pop(dev_id, None)
            REG.handlers.pop(dev_id, None)
        tasks[section] = asyncio.create_task(guarded(section, sources[section](load_config()).run))

    https_port = int(cfg.get("https_port", 8443))
    tls, fp = (None, "demo") if args.demo else setup_tls(DATA)
    runner = web.AppRunner(make_app(bridge_id, None if args.demo else start, https_port if tls else None),
                           access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Stoeckie Bridge %s listening on port %d (id %s)", VERSION, port, bridge_id)
    if SETUP_GATEWAY is not None:
        log.info("[setup] running in a container: a browser that comes from the Docker gateway (%s) needs the setup "
                 "key %s (also in data/setup-key.txt)", SETUP_GATEWAY, setup_key())
    if not api_paired():
        log.warning("[api] no SmartThings hub paired: the LAN API is closed until you open the setup page, press "
                    "Pair and Allow your hub (driver 2026-11-02b or newer)")
    if tls:
        await web.TCPSite(runner, "0.0.0.0", https_port, ssl_context=tls).start()
        log.info("[https] setup page on https://<this pc>:%d/setup, certificate SHA-256 %s", https_port, fp)
    elif not args.demo:
        log.warning("[https] not available (%s); the setup page only opens on this computer (localhost)", fp)

    async def guarded(name, coro):
        while True:
            try:
                await coro()
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("[%s] crashed, restart in 60 s", name)
                await asyncio.sleep(60)

    async def memory_log():
        # one line an hour, so a slow leak shows in bridge.log (reported on a small Docker host, 2026-11-09)
        while True:
            await asyncio.sleep(3600)
            m = memory_mb()
            if m:
                log.info("[bridge] memory %.0f MB (highest %.0f MB), %d tasks", m[0], m[1], len(asyncio.all_tasks()))

    asyncio.create_task(memory_log())
    if not args.demo:
        try:
            await claude_start()
        except OSError as ex:
            log.warning("[claude] MCP server not started: %s", ex)
    if args.demo:
        asyncio.create_task(demo_devices())
    else:
        for section in sources:
            start(section)
    await asyncio.Event().wait()


def memory_mb() -> tuple[float, float] | None:
    """Current and highest memory use of the bridge in MB (Linux /proc; Windows via psutil when installed)."""
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            st = dict(line.split(":", 1) for line in f if ":" in line)
        return int(st["VmRSS"].split()[0]) / 1024, int(st["VmHWM"].split()[0]) / 1024
    except (OSError, KeyError, ValueError):
        pass
    try:
        import psutil  # type: ignore
        info = psutil.Process().memory_info()
        return info.rss / 1048576, getattr(info, "peak_wset", info.rss) / 1048576
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- CLI

async def check_fglair() -> None:
    """Quick test of the FGLair login in config.json."""
    cfg = load_config()
    if not fglair_configured(cfg.get("fglair") or {}):
        print("FGLair: not set up yet.")
        return
    import aiohttp
    fg = FGLair(cfg)
    async with aiohttp.ClientSession() as session:
        fg.api = fg._new_api(session)
        await fg.api.async_sign_in()
        await fg._sync()
    print(f"FGLair OK, {len(fg.devices)} air conditioner(s):")
    for d in REG.devices.values():
        if d["type"] == "aircon":
            s = d["state"]
            print(f"  {d['name']} model={s.get('model')} online={d['online']} mode={s.get('mode')} "
                  f"setpoint={s.get('setpoint')} indoor={s.get('temperature')} outdoor={s.get('outdoorTemperature')} "
                  f"fan={s.get('fan')} modes={s.get('supportedModes')}")


async def check_all() -> None:
    for name, fn in (("AEG", check_aeg), ("FGLair", check_fglair)):
        try:
            await fn()
        except Exception as ex:  # noqa: BLE001
            print(f"{name} ERROR: {ex}")


async def check_aeg() -> None:
    """Quick test of the Electrolux credentials in config.json."""
    e = (load_config().get("electrolux") or {})
    if not aeg_configured(e):
        print("AEG: not set up yet.")
        return
    from electrolux_group_developer_sdk.auth.token_manager import TokenManager
    from electrolux_group_developer_sdk.client.appliance_client import ApplianceClient
    cfg = load_config()
    holder = Electrolux(cfg)
    tm = TokenManager(e["access_token"], e["refresh_token"], e["api_key"], on_token_update=holder._on_tokens)
    client = ApplianceClient(tm, external_user_agent=f"StoeckieBridge/{VERSION}")
    apps = await client.get_appliances()
    print(f"AEG OK, {len(apps)} appliance(s):")
    for a in apps:
        st = await client.get_appliance_state(a.applianceId)
        rep = (st.properties or {}).get("reported", {}) or {}
        print(f"  {a.applianceName} type={a.applianceType} connection={st.connectionState} "
              f"status={rep.get('applianceState')} phase={rep.get('cyclePhase')} timeToEnd={rep.get('timeToEnd')}")


def setup_logging(verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout is not None:  # pythonw has no console
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    for noisy in ("aiohttp.access", "tinytuya"):  # tinytuya debug lines can carry a local key
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    p = argparse.ArgumentParser(description="Stoeckie Bridge")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--demo", action="store_true", help="fake devices, for testing the driver")
    p.add_argument("--check", action="store_true", help="test the AEG and FGLair logins, list appliances and exit")
    p.add_argument("--samsung-probe", metavar="IP", help="check if a Samsung appliance accepts a local session")
    args = p.parse_args()
    if args.samsung_probe:
        sys.exit(samsung_probe(args.samsung_probe))
    setup_logging(args.verbose)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if args.check:
        asyncio.run(check_all())
    else:
        asyncio.run(serve(args))


if __name__ == "__main__":
    main()
